"""End-to-end HLS/DASH validation over every synthetic VOD fixture."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "synthetic-vod-v1"
GENERATOR = FIXTURE_DIR / "generate.py"
HARNESS = FIXTURE_DIR / "package_harness.py"


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and FFprobe are required for HLS/DASH package harness tests",
)
class SyntheticVODPackageHarnessTests(unittest.TestCase):
    def test_all_synthetic_fixtures_produce_valid_hls_and_dash_packages(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-vod-package-harness-") as temp:
            root = Path(temp)
            sources, packages = root / "sources", root / "packages"
            subprocess.run(
                [sys.executable, str(GENERATOR), "--output-dir", str(sources)],
                check=True, capture_output=True, text=True, timeout=120,
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT / "video-streaming")
            subprocess.run(
                [
                    sys.executable, str(HARNESS),
                    "--input-dir", str(sources),
                    "--output-dir", str(packages),
                ],
                check=True, capture_output=True, text=True, timeout=300, env=env,
            )
            report = json.loads((packages / "package-validation-report.json").read_text(encoding="utf-8"))
            self.assertEqual(len(report["fixtures"]), 3)
            for fixture in report["fixtures"]:
                self.assertEqual(fixture["hls"]["protocol"], "hls")
                self.assertEqual(fixture["dash"]["protocol"], "dash")
                self.assertEqual(fixture["hls"]["renditions"], 2)
                self.assertEqual(fixture["dash"]["renditions"], 2)
                self.assertGreaterEqual(fixture["hls"]["segments"], 2)
                self.assertGreaterEqual(fixture["dash"]["segments"], 2)
                self.assertEqual(len(fixture["hls"]["manifest_sha256"]), 64)
                self.assertEqual(len(fixture["dash"]["manifest_sha256"]), 64)
                self.assertGreater(fixture["hls"]["file_count"], 0)
                self.assertGreater(fixture["dash"]["file_count"], 0)


if __name__ == "__main__":
    unittest.main()
