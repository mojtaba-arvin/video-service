"""Failure-oriented tests for the research-only JIT CMAF package cache."""
import concurrent.futures
import importlib.util
import json
import shutil as file_shutil
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE_DIR = HERE / "fixtures" / "synthetic-vod-v1"
BENCHMARK = FIXTURE_DIR / "jit_packaging_benchmark.py"
GENERATOR = FIXTURE_DIR / "generate_cmaf_sources.py"
SPEC = importlib.util.spec_from_file_location("jit_packaging_benchmark", BENCHMARK)
jit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = jit
SPEC.loader.exec_module(jit)


class AtomicPackageCacheTests(unittest.TestCase):
    def test_concurrent_fills_publish_one_complete_package(self):
        with tempfile.TemporaryDirectory(prefix="jit-cache-race-") as root:
            cache = jit.AtomicPackageCache(root)
            key = "a" * 64
            calls = []
            calls_lock = threading.Lock()
            entered, release = threading.Event(), threading.Event()

            def builder(staging):
                with calls_lock:
                    calls.append(1)
                entered.set()
                release.wait(5)
                (staging / "index.m3u8").write_text("#EXTM3U\n#EXT-X-ENDLIST\n")

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(cache.get_or_build, key, builder) for _ in range(8)]
                self.assertTrue(entered.wait(2))
                self.assertFalse((Path(root) / key).exists())
                release.set()
                results = [future.result(timeout=5) for future in futures]

            self.assertEqual(len(calls), 1)
            self.assertEqual(len({str(item[0]) for item in results}), 1)
            self.assertTrue((Path(root) / key / "index.m3u8").is_file())
            self.assertEqual(sum(1 for item in results if not item[1]), 1)
            self.assertEqual(sum(1 for item in results if item[1]), 7)

    def test_versioned_keys_and_explicit_invalidation(self):
        with tempfile.TemporaryDirectory(prefix="jit-cache-version-") as root:
            cache = jit.AtomicPackageCache(root)
            common = ("b" * 64, "hls", "profile-v1", "packager-v1", "ffmpeg-build")
            key_v1 = cache.make_key(*common)
            key_v2 = cache.make_key(common[0], common[1], "profile-v2",
                                    common[3], common[4])
            self.assertNotEqual(key_v1, key_v2)
            cache.get_or_build(key_v1, lambda path: (path / "index.m3u8").write_text("#EXTM3U\n"))
            self.assertTrue((Path(root) / key_v1).exists())
            cache.invalidate(key_v1)
            self.assertFalse((Path(root) / key_v1).exists())
            rebuilt, hit, _ = cache.get_or_build(
                key_v1, lambda path: (path / "index.m3u8").write_text("#EXTM3U\n")
            )
            self.assertFalse(hit)
            self.assertTrue((rebuilt / ".complete.json").is_file())

    def test_failed_jit_uses_prepackaged_fallback(self):
        with tempfile.TemporaryDirectory(prefix="jit-cache-fallback-") as root:
            fallback = Path(root) / "fallback"
            fallback.mkdir()
            (fallback / "index.m3u8").write_text("#EXTM3U\n#EXT-X-ENDLIST\n")
            result = jit.package_or_fallback(
                jit.AtomicPackageCache(Path(root) / "cache"), "c" * 64,
                lambda staging: (_ for _ in ()).throw(RuntimeError("simulated failure")),
                fallback,
            )
            self.assertEqual(result["mode"], "prepackaged-fallback")
            self.assertEqual(Path(result["path"]), fallback.resolve())

    def test_cache_residency_model_respects_ttl_and_request_spacing(self):
        resident, hits = jit.cache_residency_hours(24, 24, 2)
        self.assertAlmostEqual(resident, 24)
        self.assertEqual(hits, 23)
        resident, hits = jit.cache_residency_hours(3, 24, 1)
        self.assertAlmostEqual(resident, 3)
        self.assertEqual(hits, 0)


class TimelineTests(unittest.TestCase):
    def test_webvtt_cues_are_bounded_by_media_timeline(self):
        with tempfile.TemporaryDirectory(prefix="jit-subtitles-") as root:
            path = Path(root) / "captions.vtt"
            path.write_text(
                "WEBVTT\n\n00:00:00.500 --> 00:00:01.500\nfirst\n\n"
                "00:00:02.500 --> 00:00:03.500\nsecond\n",
                encoding="utf-8",
            )
            report = jit.validate_webvtt_timeline(path, 5.0)
            self.assertEqual(report["cue_count"], 2)
            self.assertEqual(report["cues"][0]["start_seconds"], 0.5)
            with self.assertRaises(ValueError):
                jit.validate_webvtt_timeline(path, 3.0)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and FFprobe are required for CMAF packaging tests",
)
class CMAFPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="synthetic-cmaf-")
        cls.sources = Path(cls.temp.name) / "sources"
        subprocess.run(
            [sys.executable, str(GENERATOR), "--output-dir", str(cls.sources)],
            check=True, capture_output=True, text=True, timeout=180,
        )
        cls.dataset = json.loads(
            (cls.sources / "dataset-manifest.json").read_text(encoding="utf-8")
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_copy_only_hls_and_dash_are_valid_and_deterministic(self):
        artifact = self.dataset["artifacts"][0]
        source = self.sources / artifact["filename"]
        for protocol, manifest_name in (("hls", "index.m3u8"), ("dash", "manifest.mpd")):
            hashes = []
            for run in range(2):
                output = Path(self.temp.name) / (protocol + "-" + str(run))
                report = jit.build_package(source, output, protocol)
                self.assertGreater(report["total_bytes"], 0)
                hashes.append(report["manifest_sha256"])
                self.assertTrue((output / manifest_name).is_file())
                self.assertTrue(report["audio_video_sync"]["passed"])
                self.assertLessEqual(
                    report["audio_video_sync"]["absolute_start_delta_seconds"], 0.25
                )
            self.assertEqual(hashes[0], hashes[1])


    def test_shared_prepackaged_cmaf_uses_one_media_object_set_for_hls_and_dash(self):
        artifact = self.dataset["artifacts"][0]
        source = self.sources / artifact["filename"]
        hashes = []
        for run in range(2):
            output = Path(self.temp.name) / ("shared-cmaf-" + str(run))
            report = jit.build_shared_cmaf(source, output)
            hashes.append((
                report["manifest_sha256"], report["hls_manifest_sha256"]
            ))
            self.assertGreater(report["shared_media_bytes"], 0)
            self.assertTrue(report["hls_validation"]["valid"])
            self.assertTrue(report["dash_validation"]["valid"])
            self.assertTrue(report["audio_video_sync"]["passed"])
            self.assertLessEqual(
                report["audio_video_segment_duration_delta_seconds"], 0.25
            )
            self.assertTrue(any(
                item["path"].endswith(".m4s")
                for item in report["shared_media_files"]
            ))
        self.assertEqual(hashes[0], hashes[1])

    def test_benchmark_report_compares_shared_cmaf_and_jit_cache_byte_hours(self):
        artifact = self.dataset["artifacts"][0]
        input_dir = Path(self.temp.name) / "single-input"
        input_dir.mkdir()
        source = self.sources / artifact["filename"]
        subtitle = self.sources / artifact["subtitle_filename"]
        file_shutil.copyfile(source, input_dir / artifact["filename"])
        file_shutil.copyfile(subtitle, input_dir / artifact["subtitle_filename"])
        (input_dir / "dataset-manifest.json").write_text(
            json.dumps({"dataset_id": "one-fixture", "artifacts": [artifact]}),
            encoding="utf-8",
        )
        output_dir = Path(self.temp.name) / "benchmark-output"
        report = jit.run_benchmark(
            input_dir, output_dir, repetitions=1, workload_hours=4,
            cache_ttl_hours=1, hls_requests=4, dash_requests=2,
        )
        result = report["fixtures"][0]
        self.assertGreater(result["prepackaged_cmaf"]["bytes"], 0)
        self.assertGreater(
            result["storage_comparison"]["prepackaged_cmaf_byte_hours"], 0
        )
        self.assertGreater(
            result["storage_comparison"]["jit_cache_byte_hours"], 0
        )
        self.assertFalse(report["request_path_reencoding"])
        self.assertTrue(
            (output_dir / "jit-packaging-benchmark.json").is_file()
        )

    def test_source_and_subtitle_checksums_and_timeline_are_stable(self):
        artifact = self.dataset["artifacts"][0]
        source = self.sources / artifact["filename"]
        subtitle = self.sources / artifact["subtitle_filename"]
        self.assertEqual(jit.sha256_file(source), artifact["sha256"])
        self.assertEqual(jit.sha256_file(subtitle), artifact["subtitle_sha256"])
        report = jit.validate_webvtt_timeline(subtitle, artifact["duration_seconds"])
        self.assertEqual(report["cue_count"], 2)


if __name__ == "__main__":
    unittest.main()
