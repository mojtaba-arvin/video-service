"""Integration tests for HLS and DASH package validation."""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from video_streaming.ffmpeg.package_validation import (
    StreamingPackageValidationError,
    ordered_package_files,
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

    def test_validates_every_dash_representation(self):
        directory = self.root / "dash-valid"
        manifest = self.make_dash_package(directory)
        result = validate_streaming_package(
            str(directory), str(manifest), "dash"
        )
        self.assertEqual(result["renditions"], 2)
        self.assertGreaterEqual(result["segments"], 2)

    def test_rejects_dash_package_with_missing_media_segment(self):
        directory = self.root / "dash-missing-segment"
        manifest = self.make_dash_package(directory)
        segment = next(directory.glob("*chunk*"))
        segment.unlink()
        with self.assertRaises(StreamingPackageValidationError):
            validate_streaming_package(
                str(directory), str(manifest), "dash"
            )

    def test_rejects_dash_base_url_until_resolution_is_implemented(self):
        directory = self.root / "dash-base-url"
        manifest = self.make_dash_package(directory)
        text = manifest.read_text(encoding="utf-8")
        text = text.replace("<MPD ", '<MPD><BaseURL>media/</BaseURL><MPD ', 1)
        # Keep the document well-formed while adding a root BaseURL.
        text = manifest.read_text(encoding="utf-8")
        text = text.replace(
            "</MPD>", "<BaseURL>media/</BaseURL></MPD>", 1
        )
        manifest.write_text(text, encoding="utf-8")
        with self.assertRaises(StreamingPackageValidationError):
            validate_streaming_package(
                str(directory), str(manifest), "dash"
            )

    def test_rejects_dynamic_dash_manifest(self):
        directory = self.root / "dash-dynamic"
        manifest = self.make_dash_package(directory)
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                'type="static"', 'type="dynamic"'
            ),
            encoding="utf-8",
        )
        with self.assertRaises(StreamingPackageValidationError):
            validate_streaming_package(
                str(directory), str(manifest), "dash"
            )

    def test_upload_order_places_all_manifests_after_media(self):
        directory = self.root / "upload-order"
        directory.mkdir()
        (directory / "master.m3u8").write_text("#EXTM3U\n")
        (directory / "variant.m3u8").write_text("#EXTM3U\n")
        (directory / "manifest.mpd").write_text("<MPD/>")
        (directory / "segment-0001.m4s").write_bytes(b"segment")
        (directory / "nested").mkdir()
        (directory / "nested" / "init.mp4").write_bytes(b"init")

        files = ordered_package_files(str(directory), "master.m3u8")
        names = [relative for _, relative, _ in files]
        self.assertLess(names.index("segment-0001.m4s"), names.index("variant.m3u8"))
        self.assertLess(names.index("variant.m3u8"), names.index("master.m3u8"))
        self.assertEqual(names[-1], "master.m3u8")
        self.assertIn("nested/init.mp4", names)

    def test_dash_manifest_is_uploaded_after_every_representation(self):
        directory = self.root / "dash-upload-order"
        directory.mkdir()
        (directory / "manifest.mpd").write_text("<MPD/>")
        (directory / "init.mp4").write_bytes(b"init")
        (directory / "segment.m4s").write_bytes(b"segment")
        files = ordered_package_files(str(directory), "manifest.mpd")
        self.assertEqual(files[-1][1], "manifest.mpd")

    def test_upload_order_rejects_empty_artifacts(self):
        directory = self.root / "upload-empty"
        directory.mkdir()
        (directory / "master.m3u8").write_text("#EXTM3U\n")
        (directory / "empty.ts").write_bytes(b"")
        with self.assertRaises(StreamingPackageValidationError):
            ordered_package_files(str(directory), "master.m3u8")

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
