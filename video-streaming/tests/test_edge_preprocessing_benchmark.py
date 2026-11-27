"""Unit tests for the edge preprocessing benchmark recovery contract."""
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPT))
import benchmark_edge_preprocessing as benchmark


class EdgePreprocessingBenchmarkTests(unittest.TestCase):
    def test_sampling_rate_respects_interval_and_frame_bound(self):
        self.assertAlmostEqual(benchmark.sampling_rate(60, 5, 8), 8 / 60)
        self.assertEqual(benchmark.sampling_rate(10, 2, 8), 0.5)

    def test_sampling_rate_rejects_invalid_policy(self):
        for values in ((0, 5, 8), (60, 0, 8), (60, 5, 0)):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    benchmark.sampling_rate(*values)

    def test_disconnect_falls_back_to_cloud_and_preserves_identity(self):
        calls = []

        def edge():
            calls.append("edge")
            raise benchmark.EdgeDisconnectedError("offline")

        def cloud():
            calls.append("cloud")
            return {"source_sha256": "same-source"}

        result = benchmark.run_with_cloud_fallback(edge, cloud)
        self.assertEqual(calls, ["edge", "cloud"])
        self.assertEqual(result["selected_path"], "cloud_fallback")
        self.assertEqual(result["result"]["source_sha256"], "same-source")

    def test_non_disconnect_failure_does_not_fallback(self):
        def edge():
            raise ValueError("invalid input")

        with self.assertRaises(ValueError):
            benchmark.run_with_cloud_fallback(
                edge, lambda: self.fail("unexpected cloud fallback")
            )


if __name__ == "__main__":
    unittest.main()
