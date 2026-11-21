"""Tests for failure-isolated abandoned package cleanup sweeps."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from video_streaming.ffmpeg.tasks import cleanup_package_attempt as cleanup_task


class _Repository:
    def __init__(self):
        self.cleaned = []

    def list_reclaimable_package_attempts(self, **kwargs):
        return [
            {
                "attempt_id": "attempt-bad",
                "kind": "package",
                "bucket": "output",
                "prefix": "vod/.attempts/attempt-bad",
                "inventory_key": "vod/.attempts/attempt-bad/.manifest.artifacts.json",
            },
            {
                "attempt_id": "attempt-good",
                "kind": "package",
                "bucket": "output",
                "prefix": "vod/.attempts/attempt-good",
                "inventory_key": "vod/.attempts/attempt-good/.manifest.artifacts.json",
            },
        ]

    def is_attempt_reclaimable(self, *args, **kwargs):
        return True

    def mark_package_attempt_cleaned(self, attempt_id, bucket, prefix):
        self.cleaned.append((attempt_id, bucket, prefix))


class CleanupPackageAttemptTests(unittest.TestCase):
    def test_one_candidate_failure_does_not_abort_remaining_sweep(self):
        repository = _Repository()
        task_self = SimpleNamespace(s3_service=object())
        with patch.object(
            cleanup_task, "get_workflow_repository", return_value=repository
        ):
            with patch.object(
                cleanup_task,
                "cleanup_abandoned_attempt_package",
                side_effect=[
                    RuntimeError("provider URL must not be exposed"),
                    {"deleted": True},
                ],
            ) as cleanup:
                result = cleanup_task.cleanup_abandoned_packages.run(
                    task_self, limit=2
                )

        self.assertEqual(cleanup.call_count, 2)
        self.assertEqual(repository.cleaned, [
            ("attempt-good", "output", "vod/.attempts/attempt-good")
        ])
        self.assertEqual(result, {
            "candidates": 2,
            "deleted": 1,
            "failed": 1,
            "skipped": 0,
        })


if __name__ == "__main__":
    unittest.main()
