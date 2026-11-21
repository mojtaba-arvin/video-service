import hashlib
import io
import os
import tempfile
import unittest

from video_streaming.workflows.artifact_store import (
    ArtifactStoreError,
    S3ArtifactStore,
    artifact_uri,
    parse_artifact_uri,
)


class _Body(io.BytesIO):
    pass


class _S3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kwargs):
        if kwargs.get("IfNoneMatch") == "*" and (Bucket, Key) in self.objects:
            error = RuntimeError("precondition failed")
            error.response = {
                "ResponseMetadata": {"HTTPStatusCode": 412},
                "Error": {"Code": "PreconditionFailed"},
            }
            raise error
        data = Body.read()
        self.objects[(Bucket, Key)] = {
            "body": data,
            "Metadata": kwargs.get("Metadata", {}),
            "ContentType": kwargs.get("ContentType"),
        }

    def get_object(self, Bucket, Key):
        item = self.objects[(Bucket, Key)]
        return {"Body": _Body(item["body"]), "Metadata": item["Metadata"]}

    def head_object(self, Bucket, Key):
        item = self.objects[(Bucket, Key)]
        return {
            "ContentLength": len(item["body"]),
            "Metadata": item["Metadata"],
            "ContentType": item["ContentType"],
        }


class ArtifactStoreTests(unittest.TestCase):
    def test_uri_is_canonical_and_rejects_traversal(self):
        uri = artifact_uri("media-output", "job/file.mp4")
        self.assertEqual(parse_artifact_uri(uri), ("media-output", "job/file.mp4"))
        encoded = artifact_uri("media-output", "renders/frame#1.jpg")
        self.assertIn("%23", encoded)
        self.assertEqual(
            parse_artifact_uri(encoded), ("media-output", "renders/frame#1.jpg")
        )
        for value in (
            "https://media-output/job/file.mp4",
            "s3://media-output/../file",
            "s3://media-output//file",
            "s3://media-output/file?version=1",
        ):
            with self.subTest(uri=value), self.assertRaises(ValueError):
                parse_artifact_uri(value)

    def test_attempt_scoped_upload_and_verified_cross_worker_download(self):
        s3 = _S3()
        store = S3ArtifactStore(s3, "media-output")
        payload = b"durable artifact from worker A"
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.bin")
            destination = os.path.join(directory, "worker-b", "copy.bin")
            with open(source, "wb") as stream:
                stream.write(payload)
            published = store.put_file(source, "jobs/j1/output.mp4", "attempt_01")
            self.assertEqual(
                published["checksum_sha256"],
                hashlib.sha256(payload).hexdigest(),
            )
            self.assertIn("/.attempts/attempt_01/", published["uri"])
            head = store.head(published["uri"])
            self.assertEqual(head["size_bytes"], len(payload))
            restored = store.download_file(published["uri"], destination)
            self.assertEqual(restored["checksum_sha256"], published["checksum_sha256"])
            with open(destination, "rb") as stream:
                self.assertEqual(stream.read(), payload)

    def test_put_bytes_persists_json_without_exposing_local_path(self):
        store = S3ArtifactStore(_S3(), "media-output")
        payload = b'{"duration": 3.5}'
        published = store.put_bytes(
            payload,
            "jobs/j1/probe.json",
            "attempt_03",
            media_type="application/json",
        )
        self.assertTrue(published["uri"].startswith("s3://media-output/"))
        self.assertNotIn("/tmp/", published["uri"])
        self.assertEqual(
            published["checksum_sha256"],
            hashlib.sha256(payload).hexdigest(),
        )

    def test_attempt_key_is_create_only_but_identical_replay_is_idempotent(self):
        s3 = _S3()
        store = S3ArtifactStore(s3, "media-output")
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source")
            with open(source, "wb") as stream:
                stream.write(b"first")
            first = store.put_file(source, "out.bin", "attempt-immutable")
            replay = store.put_file(source, "out.bin", "attempt-immutable")
            self.assertEqual(replay, first)
            with open(source, "wb") as stream:
                stream.write(b"second")
            with self.assertRaises(ArtifactStoreError):
                store.put_file(source, "out.bin", "attempt-immutable")
            stored = s3.objects[("media-output", ".attempts/attempt-immutable/out.bin")]
            self.assertEqual(stored["body"], b"first")

    def test_upload_hashes_the_exact_private_snapshot_sent_to_storage(self):
        source_payload = b"hashed-and-uploaded-snapshot"
        replacement_payload = b"mutated-after-snapshot"
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source")
            with open(source, "wb") as stream:
                stream.write(source_payload)

            class MutatingS3(_S3):
                def put_object(self, Bucket, Key, Body, **kwargs):
                    with open(source, "wb") as stream:
                        stream.write(replacement_payload)
                    super().put_object(Bucket, Key, Body, **kwargs)

            store = S3ArtifactStore(MutatingS3(), "media-output")
            published = store.put_file(source, "out.bin", "attempt-snapshot")
            self.assertEqual(
                published["checksum_sha256"],
                hashlib.sha256(source_payload).hexdigest(),
            )
            stored = store._client.objects[
                ("media-output", ".attempts/attempt-snapshot/out.bin")
            ]
            self.assertEqual(stored["body"], source_payload)

    def test_download_requires_a_valid_checksum_before_replacing_destination(self):
        s3 = _S3()
        store = S3ArtifactStore(s3, "media-output")
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source")
            destination = os.path.join(directory, "destination")
            with open(source, "wb") as stream:
                stream.write(b"payload")
            published = store.put_file(source, "out.bin", "attempt-no-digest")
            s3.objects[("media-output", ".attempts/attempt-no-digest/out.bin")][
                "Metadata"
            ].pop("sha256")
            with open(destination, "wb") as stream:
                stream.write(b"preserve")
            with self.assertRaises(ArtifactStoreError):
                store.download_file(published["uri"], destination)
            with open(destination, "rb") as stream:
                self.assertEqual(stream.read(), b"preserve")

    def test_checksum_mismatch_never_replaces_destination(self):
        s3 = _S3()
        store = S3ArtifactStore(s3, "media-output")
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source")
            destination = os.path.join(directory, "destination")
            with open(source, "wb") as stream:
                stream.write(b"original")
            published = store.put_file(source, "out.bin", "attempt_02")
            with open(destination, "wb") as stream:
                stream.write(b"keep")
            with self.assertRaises(ArtifactStoreError):
                store.download_file(
                    published["uri"], destination, expected_sha256="0" * 64
                )
            with open(destination, "rb") as stream:
                self.assertEqual(stream.read(), b"keep")


if __name__ == "__main__":
    unittest.main()
