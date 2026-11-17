"""Unit and FFmpeg integration tests for thumbnail rendering and validation."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import ffmpeg

from video_streaming.ffmpeg.thumbnail import (
    ThumbnailValidationError,
    normalize_thumbnail_scale,
    render_thumbnail,
    validate_thumbnail_output,
)


class ThumbnailScaleTests(unittest.TestCase):

    def test_zero_width_is_normalized_independently(self):
        self.assertEqual(normalize_thumbnail_scale(0, 180), (-1, 180))

    def test_zero_height_is_normalized_independently(self):
        self.assertEqual(normalize_thumbnail_scale(320, 0), (320, -1))

    def test_both_zero_dimensions_are_normalized(self):
        self.assertEqual(normalize_thumbnail_scale(0, 0), (-1, -1))

    def test_explicit_dimensions_are_preserved(self):
        self.assertEqual(normalize_thumbnail_scale(320, 240), (320, 240))


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and FFprobe are required for thumbnail integration tests",
)
class ThumbnailRenderingIntegrationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.work_dir = Path(cls.temp_dir.name)
        cls.input_path = cls.work_dir / "input.mp4"
        subprocess.run(
            [
                shutil.which("ffmpeg"),
                "-hide_banner",
                "-loglevel", "error",
                "-f", "lavfi",
                "-i", "color=c=black:s=640x360:d=1:r=1",
                "-c:v", "mpeg4",
                "-pix_fmt", "yuv420p",
                "-y",
                str(cls.input_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def assert_thumbnail_dimensions(self, name, width, height, expected):
        output_path = self.work_dir / name
        render_thumbnail(
            video_path=str(self.input_path),
            output_path=str(output_path),
            thumbnail_time="0",
            scale_width=width,
            scale_height=height,
            ffmpeg_bin_path=shutil.which("ffmpeg"),
            ffprobe_bin_path=shutil.which("ffprobe"),
        )

        probe = ffmpeg.probe(
            str(output_path),
            cmd=shutil.which("ffprobe"),
        )
        video_stream = next(
            stream for stream in probe["streams"]
            if stream.get("codec_type") == "video"
        )
        self.assertEqual(
            (video_stream["width"], video_stream["height"]),
            expected,
        )

    def test_width_only_preserves_aspect_ratio(self):
        self.assert_thumbnail_dimensions(
            "width-only.png", 320, 0, (320, 180)
        )

    def test_height_only_preserves_aspect_ratio(self):
        self.assert_thumbnail_dimensions(
            "height-only.png", 0, 180, (320, 180)
        )

    def test_explicit_width_and_height_are_used(self):
        self.assert_thumbnail_dimensions(
            "explicit.png", 320, 240, (320, 240)
        )

    def test_unspecified_dimensions_preserve_source_size(self):
        self.assert_thumbnail_dimensions(
            "original-size.png", 0, 0, (640, 360)
        )

    def test_missing_output_is_rejected(self):
        with self.assertRaises(ThumbnailValidationError):
            validate_thumbnail_output(
                str(self.work_dir / "missing.png"),
                ffprobe_bin_path=shutil.which("ffprobe"),
            )

    def test_empty_output_is_rejected(self):
        empty_path = self.work_dir / "empty.png"
        empty_path.write_bytes(b"")
        with self.assertRaises(ThumbnailValidationError):
            validate_thumbnail_output(
                str(empty_path),
                ffprobe_bin_path=shutil.which("ffprobe"),
            )

    def test_corrupt_output_is_rejected(self):
        corrupt_path = self.work_dir / "corrupt.png"
        corrupt_path.write_bytes(b"not-an-image")
        with self.assertRaises(ThumbnailValidationError):
            validate_thumbnail_output(
                str(corrupt_path),
                ffprobe_bin_path=shutil.which("ffprobe"),
            )

    def test_dimension_mismatch_is_rejected(self):
        output_path = self.work_dir / "dimension-mismatch.png"
        render_thumbnail(
            video_path=str(self.input_path),
            output_path=str(output_path),
            scale_width=320,
            scale_height=0,
            ffmpeg_bin_path=shutil.which("ffmpeg"),
            ffprobe_bin_path=shutil.which("ffprobe"),
        )
        with self.assertRaises(ThumbnailValidationError):
            validate_thumbnail_output(
                str(output_path),
                ffprobe_bin_path=shutil.which("ffprobe"),
                expected_width=640,
            )


if __name__ == "__main__":
    unittest.main()
