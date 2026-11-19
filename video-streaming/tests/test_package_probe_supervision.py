"""Tests for process-group supervision of package metadata FFprobe calls."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_streaming.ffmpeg.package_validation import (
    _probe_media,
    validate_package,
)


class PackageProbeSupervisionTests(unittest.TestCase):

    @patch(
        "video_streaming.ffmpeg.package_validation.run_cancellable_process"
    )
    def test_metadata_probe_uses_shared_process_supervisor(self, runner):
        runner.return_value = (
            json.dumps({
                "streams": [{"codec_type": "video", "codec_name": "h264"}],
                "format": {"duration": "2.0"},
            }).encode("utf-8"),
            b"",
        )
        cancelled = lambda: False

        result = _probe_media(
            "/tmp/rendition.ts",
            "/usr/bin/ffprobe",
            8,
            should_cancel=cancelled,
        )

        self.assertEqual(result["format"]["duration"], "2.0")
        args, kwargs = runner.call_args
        self.assertEqual(args[0][0], "/usr/bin/ffprobe")
        self.assertIn("-show_entries", args[0])
        self.assertEqual(kwargs["timeout_seconds"], 8)
        self.assertIs(kwargs["should_cancel"], cancelled)

    @patch(
        "video_streaming.ffmpeg.package_validation.run_cancellable_process"
    )
    def test_timeout_is_propagated_from_supervisor(self, runner):
        runner.side_effect = subprocess.TimeoutExpired(["ffprobe"], 3)
        with self.assertRaises(subprocess.TimeoutExpired):
            _probe_media("rendition.ts", "ffprobe", 3)

    @patch(
        "video_streaming.ffmpeg.package_validation._probe_package_metadata"
    )
    @patch(
        "video_streaming.ffmpeg.package_validation.validate_streaming_package"
    )
    def test_package_validator_forwards_cancellation_callback(
            self, structural, metadata):
        structural.return_value = {
            "protocol": "hls",
            "manifest": "master.m3u8",
            "renditions": 1,
            "rendition_names": ["720p.m3u8"],
            "segments": 1,
        }
        metadata.return_value = []
        cancelled = lambda: False

        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "master.m3u8"
            manifest.write_text("#EXTM3U\n", encoding="utf-8")
            report = validate_package(
                directory=directory,
                package_type="hls",
                manifest_path=str(manifest),
                expected_renditions=["720p.m3u8"],
                ffprobe_bin_path="/usr/bin/ffprobe",
                metadata_probe_timeout_seconds=9,
                should_cancel=cancelled,
            )

        self.assertTrue(report.valid)
        self.assertEqual(metadata.call_args.kwargs["timeout_seconds"], 9)
        self.assertIs(metadata.call_args.kwargs["should_cancel"], cancelled)


if __name__ == "__main__":
    unittest.main()
