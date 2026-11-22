"""Contract tests for durable publication and webhook handlers."""
import hashlib
import json
import os
import tempfile
import unittest

from video_streaming.workflows.publication_handlers import (
    build_publication_handlers,
)


class _Context:
    def __init__(self, artifacts=None, attempt_id="attempt-123"):
        self.job_id = "job-123"
        self.attempt_id = attempt_id
        self.node_id = "node-publication"
        self.idempotency_key = "job-123:fingerprint:webhook-node"
        self.input_artifacts = artifacts or {}
        self.heartbeats = 0

    def heartbeat(self):
        self.heartbeats += 1
        return True

    def is_cancelled(self):
        return False

    def cancel(self, reason=None):
        raise AssertionError("unexpected cancellation: {}".format(reason))


class _ArtifactStore:
    def __init__(self):
        self.objects = {}

    def add(self, uri, payload, logical_name=None):
        checksum = hashlib.sha256(payload).hexdigest()
        self.objects[uri] = payload
        return {
            "uri": uri, "size_bytes": len(payload),
            "checksum_sha256": checksum, "logical_name": logical_name,
            "media_type": "application/octet-stream",
        }

    def download_file(self, uri, destination_path, expected_sha256=None, on_chunk=None):
        payload = self.objects[uri]
        digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 and digest != expected_sha256:
            raise ValueError("checksum mismatch")
        with open(destination_path, "wb") as target:
            target.write(payload)
        if payload and on_chunk:
            on_chunk(len(payload))
        return {"path": destination_path, "size_bytes": len(payload),
                "checksum_sha256": digest}

    def put_bytes(self, payload, logical_key, attempt_id, media_type=None, on_chunk=None):
        if on_chunk and payload:
            on_chunk(len(payload))
        uri = "s3://artifacts/{}/{}".format(attempt_id, logical_key)
        checksum = hashlib.sha256(payload).hexdigest()
        self.objects[uri] = payload
        return {"uri": uri, "size_bytes": len(payload),
                "checksum_sha256": checksum, "media_type": media_type}


class _S3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, Metadata, ContentType,
                   IfNoneMatch=None):
        if IfNoneMatch == "*" and (Bucket, Key) in self.objects:
            error = RuntimeError("precondition failed")
            error.response = {
                "ResponseMetadata": {"HTTPStatusCode": 412},
                "Error": {"Code": "PreconditionFailed"},
            }
            raise error
        payload = Body.read()
        self.objects[(Bucket, Key)] = {
            "body": payload, "metadata": dict(Metadata),
            "content_type": ContentType,
        }
        return {"ETag": '"test-etag"'}

    def head_object(self, Bucket, Key):
        value = self.objects[(Bucket, Key)]
        return {"ContentLength": len(value["body"]),
                "Metadata": value["metadata"]}


class _Repository:
    def get_job_snapshot(self, job_id):
        return {"id": job_id, "reference_id": "reference-456"}


class _Response:
    status = 202

    def close(self):
        self.closed = True


class DurablePublicationHandlerTests(unittest.TestCase):
    def setUp(self):
        self.s3 = _S3()
        self.store = _ArtifactStore()
        self.repository = _Repository()

    def build(self, **kwargs):
        return build_publication_handlers(
            self.s3, self.store, self.repository, **kwargs
        )

    def test_installed_s3_sdk_exposes_conditional_write_parameters(self):
        import botocore.session

        model = botocore.session.get_session().get_service_model("s3")
        self.assertIn(
            "IfNoneMatch",
            model.operation_model("PutObject").input_shape.members,
        )
        self.assertIn(
            "IfNoneMatch",
            model.operation_model("CompleteMultipartUpload").input_shape.members,
        )

    def test_single_file_upload_uses_attempt_scoped_key_when_replace_allowed(self):
        source = self.store.add("s3://input/thumbnail", b"jpeg-data")
        context = _Context({"thumbnail-node": [{
            **source, "logical_name": "thumbnail.image",
        }]})
        handler = self.build()["video.output.upload_file"]
        result = handler({"parameters": {
            "bucket": "media-output", "key": "thumbs/image.jpg",
            "dont_replace": False,
        }}, context)
        record = result["artifacts"][0]
        self.assertIn(".workflow-attempts/", record["uri"])
        self.assertEqual(record["logical_name"], "output.published_object")
        self.assertEqual(result["metrics"]["output_bytes"], len(b"jpeg-data"))

    def test_single_file_retry_reuses_same_idempotent_publication(self):
        source = self.store.add("s3://input/thumbnail", b"jpeg-data")
        artifacts = {"thumbnail-node": [{
            **source, "logical_name": "thumbnail.image",
        }]}
        handler = self.build()["video.output.upload_file"]
        first = handler({"parameters": {
            "bucket": "media-output", "key": "thumbs/retry.jpg",
            "dont_replace": True,
        }}, _Context(artifacts, attempt_id="attempt-first"))
        second = handler({"parameters": {
            "bucket": "media-output", "key": "thumbs/retry.jpg",
            "dont_replace": True,
        }}, _Context(artifacts, attempt_id="attempt-retry"))
        self.assertEqual(first["artifacts"][0]["uri"], second["artifacts"][0]["uri"])
        self.assertTrue(second["artifacts"][0]["metadata"]["replayed"])

    def test_single_file_dont_replace_is_atomic_at_requested_key(self):
        source = self.store.add("s3://input/thumbnail", b"jpeg-data")
        context = _Context({"thumbnail-node": [{
            **source, "logical_name": "thumbnail.image",
        }]})
        handler = self.build()["video.output.upload_file"]
        handler({"parameters": {
            "bucket": "media-output", "key": "thumbs/image.jpg",
            "dont_replace": True,
        }}, context)
        stored = self.s3.objects[("media-output", "thumbs/image.jpg")]
        self.assertEqual(stored["body"], b"jpeg-data")

    def test_directory_upload_publishes_versioned_files_before_manifest(self):
        files = []
        for path, payload in (
            ("master.m3u8", b"#EXTM3U\n720p.m3u8\n"),
            ("720p.m3u8", b"#EXTM3U\n#EXTINF:2,\nsegment.ts\n"),
            ("segment.ts", b"transport-stream"),
        ):
            artifact = self.store.add("s3://package/" + path, payload)
            files.append({"path": path, "artifact": artifact})
        entrypoint = files[0]["artifact"]
        manifest_payload = json.dumps({
            "schema_version": 1, "protocol": "hls",
            "entrypoint": entrypoint, "files": files,
        }).encode("utf-8")
        manifest = self.store.add("s3://package/directory.json", manifest_payload)
        context = _Context({"package-node": [{
            **manifest, "logical_name": "package.directory_manifest",
        }]})
        result = self.build()["video.output.upload_directory"](
            {"parameters": {
                "bucket": "media-output", "key": "vod/master.m3u8",
                "protocol": "hls", "dont_replace": False,
            }},
            context,
        )
        published = result["artifacts"][0]
        self.assertIn(".workflow-attempts/", published["uri"])
        bucket, key = published["uri"][5:].split("/", 1)
        root = self.s3.objects[(bucket, key)]["body"].decode("utf-8")
        self.assertIn(".workflow-versions/", root)
        self.assertEqual(result["metrics"]["artifact_count"], 4)

    def test_directory_retry_reuses_versioned_package_and_entrypoint(self):
        files = []
        for path, payload in (
            ("master.m3u8", b"#EXTM3U\n720p.m3u8\n"),
            ("720p.m3u8", b"#EXTM3U\n#EXTINF:2,\nsegment.ts\n"),
            ("segment.ts", b"transport-stream"),
        ):
            artifact = self.store.add("s3://retry-package/" + path, payload)
            files.append({"path": path, "artifact": artifact})
        manifest_payload = json.dumps({
            "schema_version": 1, "protocol": "hls",
            "entrypoint": files[0]["artifact"], "files": files,
        }).encode("utf-8")
        manifest = self.store.add("s3://retry-package/manifest.json", manifest_payload)
        artifacts = {"package-node": [{
            **manifest, "logical_name": "package.directory_manifest",
        }]}
        handler = self.build()["video.output.upload_directory"]
        params = {"bucket": "media-output", "key": "vod/master.m3u8",
                  "protocol": "hls", "dont_replace": False}
        first = handler({"parameters": params}, _Context(artifacts, "attempt-one"))
        second = handler({"parameters": params}, _Context(artifacts, "attempt-two"))
        self.assertEqual(first["artifacts"][0]["uri"], second["artifacts"][0]["uri"])
        self.assertEqual(second["validation_report"]["valid"], True)

    def test_webhook_uses_stable_delivery_id_header_and_writes_receipt(self):
        calls = []

        def requester(url, **kwargs):
            calls.append((url, kwargs))
            return _Response()

        handlers = self.build(
            webhook_secret_resolver=lambda job_id, secret_id: "https://callback.example/hook",
            webhook_requester=requester,
        )
        context = _Context()
        result = handlers["video.webhook.deliver"](
            {"parameters": {
                "enabled": True, "webhook_secret_ref": "opaque-secret-reference",
            }},
            context,
        )
        self.assertEqual(calls[0][0], "https://callback.example/hook")
        self.assertEqual(
            calls[0][1]["headers"]["Idempotency-Key"],
            context.idempotency_key,
        )
        self.assertEqual(
            json.loads(calls[0][1]["body"]),
            {"request_id": "job-123", "reference_id": "reference-456"},
        )
        self.assertEqual(
            result["artifacts"][0]["logical_name"],
            "webhook.delivery_receipt",
        )
        receipt_uri = result["artifacts"][0]["uri"]
        receipt = json.loads(self.store.objects[receipt_uri].decode("utf-8"))
        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual(receipt["delivery_id"], context.idempotency_key)

    def test_disabled_webhook_emits_skipped_receipt_without_resolving_secret(self):
        handlers = self.build(
            webhook_secret_resolver=lambda *_args: self.fail("must not resolve")
        )
        result = handlers["video.webhook.deliver"](
            {"parameters": {"enabled": False}}, _Context()
        )
        self.assertEqual(result["validation_report"]["status"], "skipped")
        self.assertEqual(result["metrics"]["delivery_count"], 0)


if __name__ == "__main__":
    unittest.main()
