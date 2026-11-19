"""Tests for bounded FFprobe execution."""

import json
import subprocess
import unittest
from unittest.mock import patch

from video_streaming.ffmpeg.probe import FFprobeExecutionError, probe_media


class ProbeMediaTests(unittest.TestCase):

    @patch("video_streaming.ffmpeg.probe.run_cancellable_process")
    def test_runs_ffprobe_with_process_supervision_and_parses_json(self, runner):
        runner.return_value = (
            json.dumps({"streams": [{"codec_type": "video"}]}).encode("utf-8"),
            b"",
        )
        result = probe_media(
            "/tmp/output.mp4",
            ffprobe_bin_path="/usr/bin/ffprobe",
            timeout_seconds=7,
        )
        self.assertEqual(result["streams"][0]["codec_type"], "video")
        args, kwargs = runner.call_args
        self.assertEqual(
            args[0],
            [
                "/usr/bin/ffprobe", "-v", "error",
                "-show_format", "-show_streams", "-of", "json",
                "/tmp/output.mp4",
            ],
        )
        self.assertEqual(kwargs["timeout_seconds"], 7)

    @patch("video_streaming.ffmpeg.probe.run_cancellable_process")
    def test_timeout_is_reported_as_validation_error(self, runner):
        runner.side_effect = subprocess.TimeoutExpired(["ffprobe"], 2)
        with self.assertRaises(FFprobeExecutionError):
            probe_media("/tmp/output.mp4", timeout_seconds=2)

    @patch("video_streaming.ffmpeg.probe.run_cancellable_process")
    def test_cancellation_is_not_silently_accepted(self, runner):
        runner.side_effect = RuntimeError("cancelled")
        with self.assertRaises(FFprobeExecutionError):
            probe_media("/tmp/output.mp4")

    @patch("video_streaming.ffmpeg.probe.run_cancellable_process")
    def test_invalid_json_is_rejected(self, runner):
        runner.return_value = (b"not-json", b"")
        with self.assertRaises(FFprobeExecutionError):
            probe_media("/tmp/output.mp4")

    @patch("video_streaming.ffmpeg.probe.run_cancellable_process")
    def test_missing_stream_collection_is_rejected(self, runner):
        runner.return_value = (b'{"format": {}}', b"")
        with self.assertRaises(FFprobeExecutionError):
            probe_media("/tmp/output.mp4")


if __name__ == "__main__":
    unittest.main()
