"""Contract tests for durable input handlers and cross-worker artifact handoff."""
import hashlib
import json
import unittest

from video_streaming.workflows.domain import AttemptLeaseLost
from video_streaming.workflows.errors import PermanentOperationError
from video_streaming.workflows.input_handlers import build_input_handlers


class _Body:
    def __init__(self, payload):
        self.payload = payload
        self.offset = 0
        self.closed = False

    def read(self, size):
        chunk = self.payload[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class _S3:
    def __init__(self, payload=b"video-bytes"):
        self.payload = payload
        self.head_calls = []
        self.get_calls = []

    def head_object(self, **kwargs):
        self.head_calls.append(kwargs)
        return {
            "ContentLength": len(self.payload),
            "ETag": '"source-etag"',
            "VersionId": "version-1",
            "ContentType": "video/mp4",
        }

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs)
        return {"Body": _Body(self.payload)}


class _ArtifactStore:
    def __init__(self):
        self.objects = {}

    def _store(self, payload, logical_key, attempt_id, media_type):
        digest = hashlib.sha256(payload).hexdigest()
        key = "{}/.attempts/{}/{}".format(
            logical_key.rpartition("/")[0], attempt_id,
            logical_key.rpartition("/")[2],
        )
        uri = "s3://workflow-artifacts/{}".format(key)
        self.objects[uri] = payload
        return {
            "uri": uri,
            "size_bytes": len(payload),
            "checksum_sha256": digest,
            "media_type": media_type,
        }

    def put_bytes(self, payload, logical_key, attempt_id, media_type=None):
        return self._store(payload, logical_key, attempt_id, media_type)

    def put_file(
            self, local_path, logical_key, attempt_id, media_type=None,
            on_chunk=None):
        with open(local_path, "rb") as source:
            payload = source.read()
        if on_chunk is not None and payload:
            on_chunk(len(payload))
        return self._store(payload, logical_key, attempt_id, media_type)

    def download_file(
            self, uri, destination_path, expected_sha256=None, on_chunk=None):
        payload = self.objects[uri]
        digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("checksum mismatch")
        with open(destination_path, "wb") as target:
            target.write(payload)
        if on_chunk is not None and payload:
            on_chunk(len(payload))
        return {
            "path": destination_path,
            "size_bytes": len(payload),
            "checksum_sha256": digest,
        }


class _Context:
    def __init__(self, node_id, input_artifacts=None, heartbeat_result=True):
        self.job_id = "job-1"
        self.node_id = node_id
        self.attempt_id = "attempt-{}".format(node_id)
        self.input_artifacts = input_artifacts or {}
        self.heartbeat_result = heartbeat_result
        self.cancelled = False
        self.heartbeat_calls = 0

    def is_cancelled(self):
        return self.cancelled

    def heartbeat(self):
        self.heartbeat_calls += 1
        return self.heartbeat_result

    def cancel(self, reason=None):
        self.cancelled = True


class DurableInputHandlerTests(unittest.TestCase):
    def setUp(self):
        self.s3 = _S3()
        self.store = _ArtifactStore()
        self.probe_calls = []

        def probe(path, binary, timeout, should_cancel=None, on_usage=None):
            self.probe_calls.append((path, binary, timeout))
            if on_usage:
                on_usage({
                    "process_id": "ffprobe-1",
                    "cpu_seconds": 0.01,
                    "peak_memory_bytes": 1024,
                    "memory_duration_ms": 20,
                })
            return {
                "format": {"format_name": "mov,mp4"},
                "streams": [{"codec_type": "video", "codec_name": "h264"}],
            }

        self.handlers = build_input_handlers(
            self.s3,
            self.store,
            ffprobe_bin_path="/usr/bin/ffprobe",
            probe_timeout_seconds=15,
            probe=probe,
        )

    @staticmethod
    def _node(name, parameters):
        return {"node_id": name, "parameters": parameters}

    def test_video_input_pipeline_hands_off_only_durable_artifact_references(self):
        check_context = _Context("input-video-check")
        checked = self.handlers["video.input.check"](
            self._node("input-video-check", {
                "input_name": "video", "bucket": "source-bucket",
                "key": "uploads/source.mp4",
            }),
            check_context,
        )
        self.assertTrue(checked["validation_report"]["valid"])
        self.assertTrue(checked["artifacts"][0]["uri"].startswith("s3://"))

        download_context = _Context(
            "input-video-download",
            {"input-video-check": checked["artifacts"]},
        )
        downloaded = self.handlers["video.input.download"](
            self._node("input-video-download", {
                "input_name": "video", "bucket": "source-bucket",
                "key": "uploads/source.mp4",
            }),
            download_context,
        )
        file_artifact = downloaded["artifacts"][0]
        self.assertEqual(file_artifact["artifact_type"], "media_file")
        self.assertTrue(file_artifact["uri"].startswith("s3://"))
        self.assertNotIn("path", file_artifact)

        analyze_context = _Context(
            "input-video-analyze",
            {"input-video-download": downloaded["artifacts"]},
        )
        analyzed = self.handlers["video.input.analyze"](
            self._node("input-video-analyze", {"input_name": "video"}),
            analyze_context,
        )
        self.assertEqual(
            {item["logical_name"] for item in analyzed["artifacts"]},
            {"input.video.file_artifact", "input.video.probe_metadata"},
        )
        self.assertEqual(analyzed["metrics"]["cpu_seconds"], 0.01)

        funnel_context = _Context(
            "inputs-funnel",
            {"input-video-analyze": analyzed["artifacts"]},
        )
        funnel = self.handlers["video.inputs.funnel"](
            self._node("inputs-funnel", {}),
            funnel_context,
        )
        self.assertTrue(funnel["validation_report"]["valid"])
        manifest_artifact = funnel["artifacts"][0]
        manifest_bytes = self.store.objects[manifest_artifact["uri"]]
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertIn("video", manifest["inputs"])
        self.assertEqual(
            manifest["inputs"]["video"]["file_artifact"]["uri"],
            file_artifact["uri"],
        )
        self.assertEqual(len(self.s3.head_calls), 1)
        self.assertEqual(len(self.s3.get_calls), 1)
        self.assertEqual(self.s3.get_calls[0]["VersionId"], "version-1")
        self.assertEqual(check_context.heartbeat_calls, 1)
        # Heartbeats cover source S3 reads and the subsequent Artifact Store
        # snapshot/upload, not only the initial source download.
        self.assertGreaterEqual(download_context.heartbeat_calls, 3)
        self.assertGreaterEqual(analyze_context.heartbeat_calls, 3)

    def test_missing_parent_artifact_fails_closed(self):
        context = _Context("input-video-download")
        with self.assertRaises(PermanentOperationError):
            self.handlers["video.input.download"](
                self._node("download", {
                    "input_name": "video", "bucket": "source-bucket",
                    "key": "uploads/source.mp4",
                }),
                context,
            )
        self.assertEqual(self.s3.get_calls, [])

    def test_unplanned_input_role_is_rejected(self):
        with self.assertRaises(PermanentOperationError):
            self.handlers["video.input.check"](
                self._node("check", {
                    "input_name": "../unexpected",
                    "bucket": "source-bucket",
                    "key": "uploads/source.mp4",
                }),
                _Context("invalid-input-role"),
            )
        self.assertEqual(self.s3.head_calls, [])

    def test_empty_source_object_is_a_permanent_input_error(self):
        handlers = build_input_handlers(
            _S3(payload=b""), self.store,
            ffprobe_bin_path="/usr/bin/ffprobe",
            probe_timeout_seconds=15,
        )
        with self.assertRaises(PermanentOperationError):
            handlers["video.input.check"](
                self._node("check", {
                    "input_name": "video", "bucket": "source-bucket",
                    "key": "uploads/empty.mp4",
                }),
                _Context("check"),
            )

    def test_lease_loss_stops_before_storage_side_effect(self):
        context = _Context("check", heartbeat_result=False)
        with self.assertRaises(AttemptLeaseLost):
            self.handlers["video.input.check"](
                self._node("check", {
                    "input_name": "video", "bucket": "source-bucket",
                    "key": "uploads/source.mp4",
                }),
                context,
            )
        self.assertEqual(self.s3.head_calls, [])

    def test_watermark_analysis_defers_format_validation_to_ffmpeg(self):
        stored = self.store.put_bytes(
            b"<svg></svg>", "inputs/watermark/source.svg", "download-attempt",
            media_type="image/svg+xml",
        )
        context = _Context("watermark-analyze", {
            "input-watermark-download": [{
                "logical_name": "input.watermark.file_artifact",
                "artifact_type": "media_file",
                **stored,
            }]
        })
        result = self.handlers["video.input.analyze"](
            self._node("watermark-analyze", {"input_name": "watermark"}),
            context,
        )
        self.assertEqual(self.probe_calls, [])
        metadata_artifact = next(
            item for item in result["artifacts"]
            if item["logical_name"] == "input.watermark.probe_metadata"
        )
        metadata = json.loads(
            self.store.objects[metadata_artifact["uri"]].decode("utf-8")
        )
        self.assertEqual(metadata["inspection"], "deferred_to_ffmpeg")

    def test_primary_input_must_contain_a_video_stream(self):
        def audio_only(*_args, **_kwargs):
            return {"streams": [{"codec_type": "audio"}], "format": {}}

        handlers = build_input_handlers(
            self.s3, self.store, probe=audio_only,
            ffprobe_bin_path="/usr/bin/ffprobe",
            probe_timeout_seconds=15,
        )
        file_artifact = self.store.put_bytes(
            b"video-bytes", "inputs/video/source.bin", "prior-attempt",
            media_type="video/mp4",
        )
        context = _Context("analyze", {
            "input-video-download": [{
                "logical_name": "input.video.file_artifact",
                "artifact_type": "media_file",
                **file_artifact,
            }]
        })
        with self.assertRaises(PermanentOperationError):
            handlers["video.input.analyze"](
                self._node("analyze", {"input_name": "video"}), context
            )


if __name__ == "__main__":
    unittest.main()
