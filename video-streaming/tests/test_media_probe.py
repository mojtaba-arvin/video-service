"""Tests for bounded FFprobe metadata inspection."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_streaming.execution.media_probe import probe_media
from video_streaming.ffmpeg.thumbnail import validate_thumbnail_output


class MediaProbeTests(unittest.TestCase):

    @patch("video_streaming.execution.media_probe.run_cancellable_process")
    def test_invokes_shared_runner_and_decodes_json(self, runner):
        runner.return_value = (
            b'{"streams":[{"codec_type":"video","width":320,"height":180}]}',
            b"",
        )
        cancelled = lambda: False

        result = probe_media(
            "/tmp/output.mp4",
            "/usr/bin/ffprobe",
            timeout_seconds=7,
            should_cancel=cancelled,
        )

        self.assertEqual(result["streams"][0]["width"], 320)
        runner.assert_called_once_with(
            [
                "/usr/bin/ffprobe",
                "-v", "error",
                "-show_format",
                "-show_streams",
                "-of", "json",
                "/tmp/output.mp4",
            ],
            timeout_seconds=7,
            should_cancel=cancelled,
        )

    @patch("video_streaming.execution.media_probe.run_cancellable_process")
    def test_timeout_is_not_swallowed(self, runner):
        runner.side_effect = subprocess.TimeoutExpired(["ffprobe"], 2)
        with self.assertRaises(subprocess.TimeoutExpired):
            probe_media("output.mp4", "ffprobe", timeout_seconds=2)

    def test_rejects_non_positive_timeout_before_launch(self):
        with patch(
            "video_streaming.execution.media_probe.run_cancellable_process"
        ) as runner:
            with self.assertRaises(ValueError):
                probe_media("output.mp4", "ffprobe", timeout_seconds=0)
            runner.assert_not_called()

    @patch("video_streaming.ffmpeg.thumbnail.probe_media")
    def test_thumbnail_validation_uses_supervised_probe(self, probe):
        probe.return_value = {
            "streams": [{
                "codec_type": "video",
                "width": 320,
                "height": 180,
            }]
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "thumbnail.png"
            output.write_bytes(b"image")
            cancelled = lambda: False

            result = validate_thumbnail_output(
                str(output),
                ffprobe_bin_path="/usr/bin/ffprobe",
                expected_width=320,
                expected_height=180,
                timeout_seconds=4,
                should_cancel=cancelled,
            )

        self.assertEqual(result, {"width": 320, "height": 180})
        probe.assert_called_once_with(
            str(output),
            ffprobe_bin_path="/usr/bin/ffprobe",
            timeout_seconds=4,
            should_cancel=cancelled,
        )


if __name__ == "__main__":
    unittest.main()
