"""Regression tests for the isolated JIT CMAF packaging prototype."""

from concurrent.futures import ThreadPoolExecutor
import json
import shutil
import subprocess
import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path

from video_streaming.ffmpeg.jit_packaging import (
    AtomicPackageCache,
    PackageIdentity,
    ffmpeg_copy_command,
    package_or_fallback,
    validate_track_timing,
    run_ffmpeg_copy_package,
)


class JitPackageCacheTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.identity = PackageIdentity(
            asset_id="asset-1",
            asset_version="v3",
            profile_version="h264-720p-v1",
            source_sha256="a" * 64,
            protocol="hls",
        )
        self.cache = AtomicPackageCache(self.root / "cache")

    def build(self, directory):
        (directory / "segment_00001.m4s").write_bytes(b"segment")
        (directory / "master.m3u8").write_text(
            "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\nsegment_00001.m4s\n",
            encoding="utf-8",
        )
        (directory / "init.mp4").write_bytes(b"init")
        return "master.m3u8"

    def validate(self, directory, manifest):
        self.assertTrue(manifest.is_file())
        self.assertTrue((directory / "segment_00001.m4s").is_file())
        self.assertTrue((directory / "init.mp4").is_file())

    def test_identity_is_stable_and_versions_protocol_and_drm_are_keyed(self):
        self.assertEqual(self.identity.cache_key(), self.identity.cache_key())
        self.assertNotEqual(
            self.identity.cache_key(),
            PackageIdentity(**dict(
                asset_id="asset-1", asset_version="v4",
                profile_version="h264-720p-v1", source_sha256="a" * 64,
                protocol="hls",
            )).cache_key(),
        )
        self.assertNotEqual(
            self.identity.cache_key(),
            PackageIdentity(
                asset_id="asset-1", asset_version="v3",
                profile_version="h264-720p-v1", source_sha256="a" * 64,
                protocol="dash",
            ).cache_key(),
        )

    def test_concurrent_cache_fills_publish_one_complete_package(self):
        calls = []
        calls_lock = threading.Lock()

        def slow_build(directory):
            with calls_lock:
                calls.append(1)
            (directory / "init.mp4").write_bytes(b"init")
            (directory / "segment_00001.m4s").write_bytes(b"segment")
            time.sleep(0.08)
            (directory / "master.m3u8").write_text(
                "#EXTM3U\nsegment_00001.m4s\n", encoding="utf-8"
            )
            return "master.m3u8"

        def get():
            return self.cache.get_or_create(
                self.identity, slow_build, self.validate
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: get(), range(8)))

        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(not item.cache_hit for item in results), 1)
        for result in results:
            self.assertTrue(result.manifest.is_file())
            self.assertTrue((result.directory / "segment_00001.m4s").is_file())

    def test_manifest_bytes_are_deterministic_for_fixed_inputs(self):
        first_cache = AtomicPackageCache(self.root / "one")
        second_cache = AtomicPackageCache(self.root / "two")
        first = first_cache.get_or_create(self.identity, self.build)
        second = second_cache.get_or_create(self.identity, self.build)
        self.assertEqual(
            hashlib.sha256(first.manifest.read_bytes()).hexdigest(),
            hashlib.sha256(second.manifest.read_bytes()).hexdigest(),
        )

    def test_version_change_creates_an_independent_cache_entry(self):
        first = self.cache.get_or_create(self.identity, self.build)
        newer = PackageIdentity(
            asset_id="asset-1", asset_version="v4",
            profile_version="h264-720p-v1", source_sha256="a" * 64,
            protocol="hls",
        )
        second = self.cache.get_or_create(newer, self.build)
        self.assertNotEqual(first.directory, second.directory)
        self.assertTrue(first.manifest.is_file())
        self.assertTrue(second.manifest.is_file())

    def test_invalidation_removes_only_the_selected_version(self):
        first = self.cache.get_or_create(self.identity, self.build)
        self.assertTrue(self.cache.invalidate(self.identity))
        self.assertFalse(first.directory.exists())
        self.assertFalse(self.cache.invalidate(self.identity))

    def test_failed_build_never_publishes_partial_directory(self):
        def fail(directory):
            (directory / "partial.m4s").write_bytes(b"partial")
            raise RuntimeError("simulated packaging failure")

        with self.assertRaises(RuntimeError):
            self.cache.get_or_create(self.identity, fail)
        self.assertEqual(
            [path for path in (self.root / "cache").iterdir()
             if path.is_dir()],
            [],
        )

    def test_manifest_path_cannot_escape_staging_directory(self):
        with self.assertRaises(ValueError):
            self.cache.get_or_create(
                self.identity,
                lambda directory: "../outside.m3u8",
            )

    def test_copy_command_never_reencodes(self):
        command = ffmpeg_copy_command(
            "ffmpeg", self.root / "encoded.mp4", self.root / "out", "hls"
        )
        self.assertIn("-c", command)
        self.assertIn("copy", command)
        self.assertNotIn("libx264", command)
        self.assertNotIn("-preset", command)

    def test_drm_is_rejected_for_conventional_fallback(self):
        with self.assertRaises(ValueError):
            ffmpeg_copy_command(
                "ffmpeg", "encoded.mp4", "out", "dash", drm_profile="cbcs"
            )
        self.assertEqual(
            package_or_fallback(
                lambda: (_ for _ in ()).throw(RuntimeError("no DRM adapter")),
                lambda: "pre-packaged",
            ),
            "pre-packaged",
        )

    def test_audio_video_and_subtitle_start_times_must_align(self):
        streams = [
            {"codec_type": "video", "start_time": "0.000"},
            {"codec_type": "audio", "start_time": "0.040"},
            {"codec_type": "subtitle", "start_time": "0.080"},
        ]
        self.assertTrue(validate_track_timing(streams, 0.1))
        streams[-1]["start_time"] = "0.500"
        with self.assertRaises(ValueError):
            validate_track_timing(streams, 0.1)

    def test_missing_track_timing_is_not_treated_as_zero(self):
        with self.assertRaises(ValueError):
            validate_track_timing([
                {"codec_type": "video", "start_time": "0"},
                {"codec_type": "audio"},
            ])


class JitFfmpegIntegrationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = shutil.which("ffmpeg")
        cls.ffprobe = shutil.which("ffprobe")
        cls.source = Path("/tmp/synthetic-vod-v1/portrait-audio-30fps.mp4")
        if not cls.ffmpeg or not cls.ffprobe or not cls.source.is_file():
            raise unittest.SkipTest(
                "requires generated synthetic-vod-v1 fixture and FFmpeg/FFprobe"
            )

    def test_hls_and_dash_package_existing_encoded_audio_video_without_encoding(self):
        for protocol, manifest_name in (
            ("hls", "master.m3u8"),
            ("dash", "manifest.mpd"),
        ):
            with self.subTest(protocol=protocol):
                with tempfile.TemporaryDirectory() as temporary:
                    output = Path(temporary)
                    metrics = run_ffmpeg_copy_package(
                        self.ffmpeg, self.source, output, protocol,
                        segment_seconds=2,
                    )
                    manifest = output / manifest_name
                    self.assertTrue(manifest.is_file())
                    self.assertGreater(manifest.stat().st_size, 0)
                    self.assertGreater(len(list(output.glob("*.m4s"))), 0)
                    self.assertIn("-c", metrics["command"])
                    self.assertIn("copy", metrics["command"])
                    probed = subprocess.run(
                        [self.ffprobe, "-v", "error", "-show_entries",
                         "stream=codec_type,start_time,duration", "-of", "json",
                         str(manifest)],
                        check=True, capture_output=True, text=True, timeout=30,
                    )
                    streams = json.loads(probed.stdout).get("streams", [])
                    kinds = {item.get("codec_type") for item in streams}
                    self.assertIn("video", kinds)
                    self.assertIn("audio", kinds)
                    self.assertTrue(validate_track_timing(streams, 0.25))


if __name__ == "__main__":
    unittest.main()
