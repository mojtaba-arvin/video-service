"""Contract tests for durable thumbnail and watermark handlers."""
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from video_streaming.workflows.media_handlers import build_media_handlers


class _Context:
    def __init__(self, artifacts):
        self.job_id = "job-media"
        self.attempt_id = "attempt-media"
        self.node_id = "media-node"
        self.input_artifacts = artifacts
        self.heartbeat_calls = 0
        self.cancelled = False

    def heartbeat(self):
        self.heartbeat_calls += 1
        return True

    def is_cancelled(self):
        return self.cancelled

    def cancel(self, reason=None):
        self.cancelled = True


class _ArtifactStore:
    def __init__(self):
        self.objects = {}

    def add(self, uri, payload, logical_name=None):
        self.objects[uri] = payload
        return {
            "uri": uri,
            "size_bytes": len(payload),
            "checksum_sha256": hashlib.sha256(payload).hexdigest(),
            "logical_name": logical_name,
        }

    def download_file(
            self, uri, destination_path, expected_sha256=None, on_chunk=None):
        payload = self.objects[uri]
        digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 and expected_sha256 != digest:
            raise ValueError("checksum mismatch")
        with open(destination_path, "wb") as target:
            target.write(payload)
        if on_chunk and payload:
            on_chunk(len(payload))
        return {
            "path": destination_path,
            "size_bytes": len(payload),
            "checksum_sha256": digest,
        }

    def put_file(self, path, logical_key, attempt_id, media_type=None):
        with open(path, "rb") as source:
            payload = source.read()
        digest = hashlib.sha256(payload).hexdigest()
        uri = "s3://artifacts/{}/{}".format(attempt_id, logical_key)
        self.objects[uri] = payload
        return {
            "uri": uri,
            "size_bytes": len(payload),
            "checksum_sha256": digest,
            "media_type": media_type,
        }


def _context_with_manifest(store, include_watermark=False):
    video = store.add("s3://inputs/video", b"video-input")
    inputs = {
        "video": {
            "file_artifact": {
                **video,
                "logical_name": "input.video.file_artifact",
            },
            "probe_metadata": {},
        }
    }
    if include_watermark:
        watermark = store.add("s3://inputs/watermark", b"watermark-input")
        inputs["watermark"] = {
            "file_artifact": {
                **watermark,
                "logical_name": "input.watermark.file_artifact",
            },
            "probe_metadata": {},
        }
    manifest = store.add(
        "s3://inputs/manifest",
        json.dumps({"schema_version": 1, "inputs": inputs}).encode("utf-8"),
    )
    return _Context({"inputs-funnel": [{
        **manifest,
        "logical_name": "inputs.manifest",
        "artifact_type": "input_manifest",
    }]})


class DurableMediaHandlerTests(unittest.TestCase):
    def test_thumbnail_consumes_manifest_and_publishes_artifact(self):
        store = _ArtifactStore()
        context = _context_with_manifest(store)
        renderer_calls = []

        def renderer(**kwargs):
            renderer_calls.append(kwargs)
            with open(kwargs["output_path"], "wb") as target:
                target.write(b"jpeg-image")
            if kwargs["usage_callback"]:
                kwargs["usage_callback"]({
                    "process_id": "ffmpeg-thumbnail",
                    "cpu_seconds": 0.25,
                    "peak_memory_bytes": 1024,
                    "memory_duration_ms": 100,
                })

        def probe(path, **kwargs):
            self.assertTrue(os.path.isfile(path))
            return {"streams": [{
                "codec_type": "video", "width": 320, "height": 180,
            }]}

        handlers = build_media_handlers(
            store, thumbnail_renderer=renderer, media_probe=probe
        )
        result = handlers["video.thumbnail.generate"](
            {"parameters": {
                "thumbnail_time": "00:00:02",
                "scale_width": 320,
                "scale_height": 180,
                "use_watermark": False,
            }},
            context,
        )
        self.assertEqual(len(renderer_calls), 1)
        self.assertEqual(
            result["artifacts"][0]["logical_name"], "thumbnail.image"
        )
        self.assertEqual(result["validation_report"]["width"], 320)
        self.assertEqual(result["metrics"]["cpu_seconds"], 0.25)
        self.assertGreaterEqual(context.heartbeat_calls, 4)

    def test_thumbnail_consumes_watermark_video_dependency(self):
        store = _ArtifactStore()
        context = _context_with_manifest(store)
        watermarked = store.add("s3://outputs/watermarked", b"watermarked-video")
        context.input_artifacts["watermark-apply"] = [{
            **watermarked,
            "logical_name": "watermark.video",
            "artifact_type": "media_file",
        }]
        consumed = []

        def renderer(**kwargs):
            with open(kwargs["video_path"], "rb") as source:
                consumed.append(source.read())
            with open(kwargs["output_path"], "wb") as target:
                target.write(b"jpeg-image")

        handlers = build_media_handlers(
            store,
            thumbnail_renderer=renderer,
            media_probe=lambda *_args, **_kwargs: {
                "streams": [{
                    "codec_type": "video", "width": 10, "height": 10,
                }]
            },
        )
        handlers["video.thumbnail.generate"](
            {"parameters": {
                "thumbnail_time": 0,
                "scale_width": 0,
                "scale_height": 0,
                "use_watermark": True,
            }},
            context,
        )
        self.assertEqual(consumed, [b"watermarked-video"])

    def test_watermark_process_publishes_video_artifact_without_celery(self):
        store = _ArtifactStore()
        context = _context_with_manifest(store, include_watermark=True)
        handlers = build_media_handlers(
            store,
            media_probe=lambda *_args, **_kwargs: {
                "streams": [{
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "codec_name": "h264",
                }]
            },
        )

        class _Stream:
            def output(self, path):
                self.path = path
                return self

        class _Input:
            pass

        def fake_run(command, **kwargs):
            output_path = command[-1]
            with open(output_path, "wb") as target:
                target.write(b"watermarked-mp4")
            if kwargs["on_usage"]:
                kwargs["on_usage"]({
                    "process_id": "ffmpeg-watermark",
                    "cpu_seconds": 1.0,
                    "peak_memory_bytes": 4096,
                    "memory_duration_ms": 250,
                })

        with patch(
            "video_streaming.workflows.media_handlers.ffmpeg.input",
            side_effect=lambda *_args, **_kwargs: _Input(),
        ), patch(
            "video_streaming.workflows.media_handlers.ffmpeg.filter",
            return_value=_Stream(),
        ), patch(
            "video_streaming.workflows.media_handlers.ffmpeg.compile",
            side_effect=lambda stream, **_kwargs: ["ffmpeg", stream.path],
        ), patch(
            "video_streaming.workflows.media_handlers.run_cancellable_process",
            side_effect=fake_run,
        ):
            result = handlers["video.watermark.apply"](
                {"parameters": {}}, context
            )

        self.assertEqual(result["artifacts"][0]["logical_name"], "watermark.video")
        self.assertEqual(result["validation_report"]["width"], 1920)
        self.assertEqual(result["metrics"]["output_bytes"], len(b"watermarked-mp4"))

    def test_thumbnail_rejects_missing_manifest_dependency(self):
        store = _ArtifactStore()
        handlers = build_media_handlers(store)
        with self.assertRaises(Exception):
            handlers["video.thumbnail.generate"](
                {"parameters": {"thumbnail_time": 0}},
                _Context({}),
            )


if __name__ == "__main__":
    unittest.main()
