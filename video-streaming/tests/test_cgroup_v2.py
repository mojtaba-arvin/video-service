"""Unit tests for fail-closed attempt cgroup v2 management."""
import os
import tempfile
import unittest
from unittest import mock

from video_streaming.execution.cgroup_v2 import (
    AttemptCgroupV2,
    CgroupV2CleanupError,
    CgroupV2Unavailable,
)


class AttemptCgroupV2Tests(unittest.TestCase):
    def _delegated_root(self, root, enabled="cpu memory pids"):
        for name, value in (
            ("cgroup.controllers", "cpu memory pids"),
            ("cgroup.subtree_control", enabled),
        ):
            with open(os.path.join(root, name), "w") as stream:
                stream.write(value)
        return root

    def _remove_fake_cgroup_files(self, attempt):
        for name in os.listdir(attempt.path):
            os.remove(os.path.join(attempt.path, name))

    def _attempt(self, root):
        with mock.patch(
                "video_streaming.execution.cgroup_v2.os.path.exists",
                return_value=True):
            attempt = AttemptCgroupV2(
                root, cpu_max="50000 100000",
                memory_max=256 * 1024 * 1024, pids_max=64,
                attempt_id="attempt-123",
            )
        for name, value in (
            ("cgroup.procs", ""),
            ("cgroup.events", "populated 0\nfrozen 0\n"),
            ("cgroup.kill", ""),
            ("cpu.stat", "usage_usec 1250\nuser_usec 1000\nsystem_usec 250\n"),
            ("memory.peak", "4096"),
            ("memory.events", "low 0\nhigh 0\nmax 1\noom 0\noom_kill 0\n"),
            ("pids.current", "2"),
        ):
            with open(os.path.join(attempt.path, name), "w") as stream:
                stream.write(value)
        return attempt

    def test_fails_closed_when_controller_is_not_enabled(self):
        with tempfile.TemporaryDirectory() as root:
            self._delegated_root(root, enabled="cpu memory")
            with self.assertRaises(CgroupV2Unavailable):
                AttemptCgroupV2(root, "50000 100000", 1024, 8)

    def test_applies_all_three_limits(self):
        with tempfile.TemporaryDirectory() as root:
            self._delegated_root(root)
            with mock.patch(
                    "video_streaming.execution.cgroup_v2.os.path.exists",
                    return_value=True):
                attempt = AttemptCgroupV2(root, "50000 100000", 1024, 8)
            try:
                for name, expected in (
                    ("cpu.max", "50000 100000"),
                    ("memory.max", "1024"),
                    ("pids.max", "8"),
                ):
                    with open(os.path.join(attempt.path, name), "r") as stream:
                        self.assertEqual(stream.read(), expected)
            finally:
                self._remove_fake_cgroup_files(attempt)
                os.rmdir(attempt.path)

    def test_usage_exposes_kernel_counters(self):
        with tempfile.TemporaryDirectory() as root:
            self._delegated_root(root)
            attempt = self._attempt(root)
            usage = attempt.usage()
            self.assertEqual(usage["cpu"]["usage_usec"], 1250)
            self.assertEqual(usage["memory_peak_bytes"], 4096)
            self.assertEqual(usage["memory_events"]["oom_kill"], 0)
            self.assertEqual(usage["pids_current"], 2)
            self._remove_fake_cgroup_files(attempt)
            os.rmdir(attempt.path)

    def test_cleanup_refuses_populated_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            self._delegated_root(root)
            attempt = self._attempt(root)
            with mock.patch.object(attempt, "wait_empty", return_value=False):
                with self.assertRaises(CgroupV2CleanupError):
                    attempt.cleanup(timeout_seconds=0.01)
            self.assertTrue(os.path.isdir(attempt.path))
            self._remove_fake_cgroup_files(attempt)
            os.rmdir(attempt.path)

    def test_cleanup_only_removes_empty_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            self._delegated_root(root)
            attempt = self._attempt(root)
            self._remove_fake_cgroup_files(attempt)
            # cgroupfs exposes virtual control files; the temporary-directory
            # fixture models them as ordinary files, so remove those first.
            with mock.patch.object(attempt, "wait_empty", return_value=True):
                attempt.cleanup()
            self.assertFalse(os.path.exists(attempt.path))
            self.assertTrue(attempt._closed)


if __name__ == "__main__":
    unittest.main()
