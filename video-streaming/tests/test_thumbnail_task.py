"""Regression tests for durable thumbnail task path and accounting behavior."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import importlib

thumbnail_task = importlib.import_module(
    "video_streaming.ffmpeg.tasks.generate_thumbnail"
)
generate_thumbnail = thumbnail_task.generate_thumbnail


class _Repository:
    def __init__(self):
        self.completed = None

    def claim_attempt(self, **kwargs):
        return "attempt-1", 1, "lease-token", None

    def complete_processing_attempt(self, **kwargs):
        self.completed = kwargs


class GenerateThumbnailTaskTests(unittest.TestCase):

    def test_explicit_output_path_is_resolved_before_attempt_scoping(self):
        repository = _Repository()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "input.mp4"
            input_path.write_bytes(b"synthetic-input")
            requested_path = root / "requested" / "thumbnail.jpg"
            requested_path.parent.mkdir()

            def render(**kwargs):
                Path(kwargs["output_path"]).write_bytes(b"synthetic-thumbnail")

            generate_thumbnail.push_request(
                id="celery-task-1", hostname="worker-1", retries=0
            )
            try:
                with patch.object(
                    thumbnail_task,
                    "get_workflow_repository",
                    return_value=repository,
                ), patch.object(
                    thumbnail_task,
                    "AttemptLeaseHeartbeat",
                    return_value=type(
                        "HeartbeatStub", (),
                        {"assert_owned": lambda self: None,
                         "on_usage": lambda self, _metrics: None}
                    )(),
                ), patch.object(
                    thumbnail_task, "render_thumbnail", side_effect=render,
                ), patch.object(
                    thumbnail_task, "validate_thumbnail_output",
                    return_value={"width": 320, "height": 180},
                ), patch.object(
                    thumbnail_task, "capture_child_cpu_seconds",
                    return_value=0.0,
                ), patch.object(
                    thumbnail_task, "child_cpu_seconds_since",
                    return_value=0.0,
                ), patch.object(
                    thumbnail_task, "configured_pricing",
                    return_value=None,
                ), patch.object(
                    generate_thumbnail, "is_forced_to_stop",
                    return_value=False,
                ), patch.object(
                    generate_thumbnail, "is_output_forced_to_stop",
                    return_value=False,
                ), patch.object(
                    generate_thumbnail, "save_primary_status"
                ), patch.object(
                    generate_thumbnail, "save_output_status"
                ):
                    result = generate_thumbnail.run(
                        video_path=str(input_path),
                        output_path=str(requested_path),
                        request_id="job-1",
                        output_id="thumbnail-1",
                    )
            finally:
                generate_thumbnail.pop_request()

            expected = (
                requested_path.parent
                / ".attempts"
                / "attempt-1"
                / requested_path.name
            )
            self.assertEqual(Path(result["file_path"]), expected)
            self.assertTrue(expected.is_file())
            self.assertEqual(repository.completed["attempt_id"], "attempt-1")
            self.assertEqual(repository.completed["validation_report"]["valid"], True)


if __name__ == "__main__":
    unittest.main()
