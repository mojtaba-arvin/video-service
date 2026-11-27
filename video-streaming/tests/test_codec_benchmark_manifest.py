"""Validation tests for the licensed codec-benchmark source manifest."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_codecs import (
    BenchmarkError,
    canonical_sha256,
    ensure_output_paths_available,
    load_manifest,
    probe_media,
    resource_measurement_fields,
    sha256_file,
)


class CodecBenchmarkManifestTests(unittest.TestCase):

    def test_configuration_fingerprint_is_stable_and_sensitive(self):
        configuration = {
            "target_vmaf": 93,
            "repetitions": 3,
            "source_hashes": ["a" * 64],
        }
        first = canonical_sha256(configuration)
        reordered = canonical_sha256({
            "source_hashes": ["a" * 64],
            "repetitions": 3,
            "target_vmaf": 93,
        })
        changed = canonical_sha256(dict(configuration, target_vmaf=94))

        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)

    def test_benchmark_output_never_overwrites_prior_evidence(self):
        with tempfile.TemporaryDirectory(prefix="codec-benchmark-output-") as directory:
            output_dir = Path(directory)
            ensure_output_paths_available(output_dir)
            (output_dir / "measurements.jsonl").write_text(
                '{"prior":"evidence"}\n', encoding="utf-8"
            )

            with self.assertRaisesRegex(BenchmarkError, "choose a new output directory"):
                ensure_output_paths_available(output_dir)

            self.assertEqual(
                (output_dir / "measurements.jsonl").read_text(encoding="utf-8"),
                '{"prior":"evidence"}\n',
            )

    def test_unmeasured_gpu_and_scratch_resources_remain_unknown(self):
        fields = resource_measurement_fields(
            cpu_seconds=None, peak_memory_bytes=1024,
            output_bytes=2048, output_bitrate=None
        )

        self.assertIsNone(fields["encode_cpu_seconds"])
        self.assertIsNone(fields["gpu_compute_seconds"])
        self.assertIsNone(fields["gpu_utilization_percent"])
        self.assertIsNone(fields["scratch_peak_bytes"])
        self.assertFalse(fields["resource_coverage"]["cpu_seconds"])
        self.assertFalse(fields["resource_coverage"]["gpu_compute_seconds"])
        self.assertFalse(fields["resource_coverage"]["output_bitrate"])
        self.assertTrue(fields["resource_coverage"]["output_storage_bytes"])
        self.assertEqual(fields["output_storage_bytes"], 2048)

    @patch("scripts.benchmark_codecs.run_command")
    def test_probe_media_returns_container_bitrate(self, run_command):
        run_command.return_value = Mock(stdout=json.dumps({
            "streams": [{
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "25/1",
                "codec_name": "h264",
            }],
            "format": {
                "duration": "12.0",
                "bit_rate": "4500000",
            },
        }))

        result = probe_media("ffprobe", "/tmp/reference.mp4", 30)

        self.assertEqual(result["bit_rate"], 4500000)
        self.assertEqual(result["duration_seconds"], 12.0)

    def test_manifest_resolves_sources_and_records_content_hashes(self):
        with tempfile.TemporaryDirectory(prefix="codec-benchmark-manifest-") as directory:
            root = Path(directory)
            media = root / "licensed-reference.mp4"
            media.write_bytes(b"representative-reference-content")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({
                "schema_version": 1,
                "dataset_id": "licensed-vod-reference-v1",
                "sources": [{
                    "id": "sports-clip-01",
                    "path": media.name,
                    "content_class": "sports",
                    "license": "internal-cleared-for-benchmarking",
                    "sha256": sha256_file(media),
                }],
            }), encoding="utf-8")

            manifest = load_manifest(manifest_path)
            source = manifest["sources"][0]

            self.assertEqual(manifest["dataset_id"], "licensed-vod-reference-v1")
            self.assertEqual(source["path"], str(media.resolve()))
            self.assertEqual(source["sha256"], sha256_file(media))
            self.assertEqual(source["content_class"], "sports")
            self.assertEqual(source["license"], "internal-cleared-for-benchmarking")

    def test_manifest_rejects_unlicensed_or_unclassified_sources(self):
        with tempfile.TemporaryDirectory(prefix="codec-benchmark-manifest-") as directory:
            root = Path(directory)
            media = root / "reference.mp4"
            media.write_bytes(b"reference")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({
                "schema_version": 1,
                "dataset_id": "incomplete",
                "sources": [{
                    "id": "clip-01",
                    "path": media.name,
                    "content_class": "documentary",
                }],
            }), encoding="utf-8")

            with self.assertRaisesRegex(BenchmarkError, "license"):
                load_manifest(manifest_path)

    def test_manifest_rejects_changed_source_hash(self):
        with tempfile.TemporaryDirectory(prefix="codec-benchmark-manifest-") as directory:
            root = Path(directory)
            media = root / "reference.mp4"
            media.write_bytes(b"reference")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({
                "schema_version": 1,
                "dataset_id": "hash-checked",
                "sources": [{
                    "id": "clip-01",
                    "path": media.name,
                    "content_class": "interview",
                    "license": "cleared",
                    "sha256": "0" * 64,
                }],
            }), encoding="utf-8")

            with self.assertRaisesRegex(BenchmarkError, "SHA-256 mismatch"):
                load_manifest(manifest_path)


if __name__ == "__main__":
    unittest.main()
