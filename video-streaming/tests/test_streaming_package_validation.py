"""Integration tests for HLS and DASH package validation."""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from video_streaming.ffmpeg.package_validation import (
    PackageValidationReport,
    StreamingPackageValidationError,
    ordered_package_files,
    reset_package_directory,
    validate_package,
    validate_streaming_package,
)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and FFprobe are required for packaging integration tests",
)
class StreamingPackageValidationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp_dir.name)
        cls.input_path = cls.root / "input.mp4"
        subprocess.run(
            [
                shutil.which("ffmpeg"),
                "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=3",
                "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-y", str(cls.input_path),
            ],
            check=True, capture_output=True, text=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def make_hls_package(self, directory):
        directory.mkdir()
        for height, width in ((180, 320), (90, 160)):
            playlist = directory / ("video_{}p.m3u8".format(height))
            segment_pattern = directory / (
                "video_{}p_%03d.ts".format(height)
            )
            subprocess.run(
                [
                    shutil.which("ffmpeg"),
                    "-hide_banner", "-loglevel", "error",
                    "-i", str(self.input_path),
                    "-vf", "scale={}:{}".format(width, height),
                    "-an", "-c:v", "libx264",
                    "-hls_time", "1", "-hls_playlist_type", "vod",
                    "-hls_segment_filename", str(segment_pattern),
                    "-y", str(playlist),
                ],
                check=True, capture_output=True, text=True,
            )
        (directory / "video.m3u8").write_text(
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=320x180\n"
            "video_180p.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=150000,RESOLUTION=160x90\n"
            "video_90p.m3u8\n",
            encoding="utf-8",
        )
        return directory / "video.m3u8"

    def make_dash_package(self, directory):
        directory.mkdir()
        manifest = directory / "video.mpd"
        subprocess.run(
            [
                shutil.which("ffmpeg"),
                "-hide_banner", "-loglevel", "error",
                "-i", str(self.input_path),
                "-map", "0:v:0", "-map", "0:v:0",
                "-filter:v:0", "scale=320:180",
                "-filter:v:1", "scale=160:90",
                "-an", "-c:v", "libx264",
                "-b:v:0", "300k", "-b:v:1", "150k",
                "-f", "dash", "-seg_duration", "1",
                "-use_timeline", "1", "-use_template", "1",
                "-init_seg_name", "video_init_$RepresentationID$.$ext$",
                "-media_seg_name",
                "video_chunk_$RepresentationID$_$Number%05d$.$ext$",
                "-adaptation_sets", "id=0,streams=v",
                "-y", str(manifest),
            ],
            check=True, capture_output=True, text=True,
        )
        return manifest

    def test_validates_every_hls_rendition_and_segment(self):
        directory = self.root / "hls-valid"
        manifest = self.make_hls_package(directory)
        result = validate_streaming_package(
            str(directory), str(manifest), "hls"
        )
        self.assertEqual(result["renditions"], 2)
        self.assertGreaterEqual(result["segments"], 2)

    def test_structured_report_captures_expected_rendition_mismatch(self):
        directory = self.root / "hls-expected-mismatch"
        manifest = self.make_hls_package(directory)
        report = validate_package(
            str(directory),
            "hls",
            ["video_180p.m3u8", "video_720p.m3u8"],
            str(manifest),
        )
        self.assertIsInstance(report, PackageValidationReport)
        self.assertFalse(report.valid)
        self.assertEqual(
            report.errors[0].code, "expected_renditions_mismatch"
        )
        self.assertEqual(report.errors[0].location, str(manifest))
        with self.assertRaises(StreamingPackageValidationError):
            report.require_valid()

    def test_structured_report_returns_actual_renditions(self):
        directory = self.root / "hls-expected-valid"
        manifest = self.make_hls_package(directory)
        report = validate_package(
            str(directory),
            "hls",
            ["video_180p.m3u8", "video_90p.m3u8"],
            str(manifest),
        )
        self.assertTrue(report.valid)
        self.assertEqual(
            report.actual_renditions,
            ["video_180p.m3u8", "video_90p.m3u8"],
        )

    def test_rejects_empty_root_playlist_with_structured_error(self):
        directory = self.root / "hls-empty-root"
        directory.mkdir()
        manifest = directory / "master.m3u8"
        manifest.write_text("", encoding="utf-8")
        report = validate_package(str(directory), "hls", [], str(manifest))
        self.assertFalse(report.valid)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.errors[0].location, str(manifest))

    def test_rejects_hls_package_with_missing_segment(self):
        directory = self.root / "hls-missing-segment"
        manifest = self.make_hls_package(directory)
        variant = directory / "video_180p.m3u8"
        segment = next(
            line.strip()
            for line in variant.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        )
        (directory / segment).unlink()
        with self.assertRaises(StreamingPackageValidationError):
            validate_streaming_package(
                str(directory), str(manifest), "hls"
            )

    def test_rejects_hls_master_with_missing_rendition(self):
        directory = self.root / "hls-missing-rendition"
        manifest = self.make_hls_package(directory)
        (directory / "video_90p.m3u8").unlink()
        with self.assertRaises(StreamingPackageValidationError):
            validate_streaming_package(
                str(directory), str(manifest), "hls"
            )

    def test_rejects_hls_playlist_with_unpaired_extinf(self):
        directory = self.root / "hls-unpaired-extinf"
        manifest = self.make_hls_package(directory)
        variant = directory / "video_180p.m3u8"
        text = variant.read_text(encoding="utf-8")
        text = text.replace("#EXTINF:", "#EXTINF:1.0,\n#EXTINF:", 1)
        variant.write_text(text, encoding="utf-8")
        with self.assertRaises(StreamingPackageValidationError):
            validate_streaming_package(
                str(directory), str(manifest), "hls"
            )

    def test_rejects_hls_live_playlist_without_endlist(self):
        directory = self.root / "hls-no-endlist"
        manifest = self.make_hls_package(directory)
        variant = directory / "video_180p.m3u8"
        variant.write_text(
            variant.read_text(encoding="utf-8").replace(
                "#EXT-X-ENDLIST\n", ""
            ),
            encoding="utf-8",
        )
        with self.assertRaises(StreamingPackageValidationError):
            validate_streaming_package(
                str(directory), str(manifest), "hls"
            )

    def test_rejects_manifest_resolution_mismatch_against_media_probe(self):
        directory = self.root / "hls-resolution-mismatch"
        manifest = self.make_hls_package(directory)
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "RESOLUTION=320x180", "RESOLUTION=1920x1080"
            ),
            encoding="utf-8",
        )
        report = validate_package(
            str(directory), "hls",
            ["video_180p.m3u8", "video_90p.m3u8"],
            str(manifest),
            ffprobe_bin_path=shutil.which("ffprobe"),
        )
        self.assertIn(
            "resolution_mismatch", [issue.code for issue in report.errors]
        )

    def test_rejects_manifest_codec_mismatch_against_media_probe(self):
        directory = self.root / "hls-codec-mismatch"
        manifest = self.make_hls_package(directory)
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "BANDWIDTH=300000",
                'BANDWIDTH=300000,CODECS="vp09.00.10.08"',
            ),
            encoding="utf-8",
        )
        report = validate_package(
            str(directory), "hls",
            ["video_180p.m3u8", "video_90p.m3u8"],
            str(manifest),
            ffprobe_bin_path=shutil.which("ffprobe"),
        )
        self.assertIn(
            "codec_mismatch", [issue.code for issue in report.errors]
        )

    def test_rejects_manifest_bandwidth_below_probed_bitrate(self):
        directory = self.root / "hls-bitrate-mismatch"
        manifest = self.make_hls_package(directory)
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "BANDWIDTH=300000", "BANDWIDTH=1"
            ),
            encoding="utf-8",
        )
        report = validate_package(
            str(directory), "hls",
            ["video_180p.m3u8", "video_90p.m3u8"],
            str(manifest),
            ffprobe_bin_path=shutil.which("ffprobe"),
        )
        self.assertIn(
            "bitrate_mismatch", [issue.code for issue in report.errors]
        )

    def test_rejects_playlist_duration_mismatch_against_media_probe(self):
        directory = self.root / "hls-duration-mismatch"
        manifest = self.make_hls_package(directory)
        variant = directory / "video_180p.m3u8"
        segment = directory / "duration-check.ts"
        subprocess.run(
            [
                shutil.which("ffmpeg"),
                "-hide_banner", "-loglevel", "error",
                "-i", str(self.input_path),
                "-an", "-c:v", "libx264",
                "-f", "mpegts", "-y", str(segment),
            ],
            check=True, capture_output=True, text=True,
        )
        variant.write_text(
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-TARGETDURATION:30\n"
            "#EXTINF:30.000000,\n"
            "duration-check.ts\n"
            "#EXT-X-ENDLIST\n",
            encoding="utf-8",
        )
        report = validate_package(
            str(directory), "hls",
            ["video_180p.m3u8", "video_90p.m3u8"],
            str(manifest),
            ffprobe_bin_path=shutil.which("ffprobe"),
        )
        self.assertIn(
            "duration_mismatch", [issue.code for issue in report.errors]
        )

    def test_rejects_symlinked_manifest(self):
        directory = self.root / "hls-symlink-manifest"
        directory.mkdir()
        target = directory / "real.m3u8"
        target.write_text("#EXTM3U\n")
        manifest = directory / "master.m3u8"
        manifest.symlink_to(target)
        with self.assertRaises(StreamingPackageValidationError):
            validate_streaming_package(
                str(directory), str(manifest), "hls"
            )

    def test_retry_reset_removes_only_current_output_directory(self):
        job_root = self.root / "outputs_job"
        output_dir = job_root / "p0"
        sibling = job_root / "p1"
        output_dir.mkdir(parents=True)
        sibling.mkdir()
        (output_dir / "stale.ts").write_bytes(b"old")
        (sibling / "keep.ts").write_bytes(b"keep")

        reset = reset_package_directory(
            str(output_dir), str(job_root)
        )
        self.assertTrue(Path(reset).is_dir())
        self.assertFalse((output_dir / "stale.ts").exists())
        self.assertEqual((sibling / "keep.ts").read_bytes(), b"keep")

    def test_retry_reset_rejects_path_outside_job_output_root(self):
        job_root = self.root / "outputs_job"
        job_root.mkdir()
        outside = self.root / "other" / "p0"
        outside.mkdir(parents=True)
        with self.assertRaises(StreamingPackageValidationError):
            reset_package_directory(str(outside), str(job_root))

    def test_rejects_hls_path_traversal_reference(self):
        directory = self.root / "hls-path-traversal"
        directory.mkdir()
        (directory / "master.m3u8").write_text(
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=100000\n"
            "../outside.m3u8\n",
            encoding="utf-8",
        )
        with self.assertRaises(StreamingPackageValidationError):
            validate_streaming_package(
                str(directory), str(directory / "master.m3u8"), "hls"
            )

    def test_rejects_unsupported_dash_template_repeat(self):
        directory = self.root / "dash-negative-repeat"
        manifest = self.make_dash_package(directory)
        manifest_text = manifest.read_text(encoding="utf-8")
        manifest_text = re.sub(r' r="-?\d+"', "", manifest_text, count=1)
        manifest_text = manifest_text.replace("<S ", '<S r="-1" ', 1)
        manifest.write_text(manifest_text, encoding="utf-8")
        with self.assertRaises(StreamingPackageValidationError):
            validate_streaming_package(
                str(directory), str(manifest), "dash"
            )


if __name__ == "__main__":
    unittest.main()
