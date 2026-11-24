"""Tests for Attempt-scoped cgroup lifecycle and implicit process wiring."""

import subprocess
import sys
import unittest
from unittest import mock

from video_streaming.execution import attempt_scope
from video_streaming.execution.process_control import (
    run_cancellable_process,
    run_streaming_process,
)


class FakeAttemptCgroup:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.spawn_calls = 0
        self.cleanup_calls = 0
        self.terminate_calls = 0

    def spawn(self, command, stdin, stdout, stderr):
        self.spawn_calls += 1
        process = subprocess.Popen(
            command, stdin=stdin, stdout=stdout, stderr=stderr,
            start_new_session=True, bufsize=0,
        )
        process._video_service_process_group = True
        process._video_service_cgroup = self
        return process

    def wait_empty(self, timeout_seconds):
        return True

    def terminate(self):
        self.terminate_calls += 1

    def cleanup(self, timeout_seconds=5.0):
        self.cleanup_calls += 1


class AttemptCgroupScopeTests(unittest.TestCase):
    def test_disabled_scope_does_not_create_a_cgroup(self):
        with mock.patch.object(
                attempt_scope.settings, "ATTEMPT_CGROUP_V2_ENABLED", False):
            with mock.patch.object(attempt_scope, "AttemptCgroupV2") as factory:
                with attempt_scope.attempt_cgroup_scope("attempt-1") as active:
                    self.assertIsNone(active)
                    self.assertIsNone(attempt_scope.current_attempt_cgroup())
                factory.assert_not_called()

    def test_streaming_supervisor_uses_attempt_cgroup_implicitly(self):
        fake = FakeAttemptCgroup()
        lines = []
        with mock.patch.object(
                attempt_scope.settings, "ATTEMPT_CGROUP_V2_ENABLED", True):
            with mock.patch.object(
                    attempt_scope, "AttemptCgroupV2", return_value=fake):
                with attempt_scope.attempt_cgroup_scope("attempt-3"):
                    run_streaming_process(
                        [sys.executable, "-c", "print('attempt-scoped')"],
                        timeout_seconds=2,
                        on_line=lambda line, process: lines.append(line),
                    )
                self.assertEqual(lines, ["attempt-scoped"])
                self.assertEqual(fake.spawn_calls, 1)
                self.assertEqual(fake.cleanup_calls, 1)

    def test_process_supervisor_uses_attempt_cgroup_implicitly(self):
        fake = FakeAttemptCgroup()
        with mock.patch.object(
                attempt_scope.settings, "ATTEMPT_CGROUP_V2_ENABLED", True):
            with mock.patch.object(
                    attempt_scope, "AttemptCgroupV2", return_value=fake):
                with attempt_scope.attempt_cgroup_scope("attempt-2"):
                    run_cancellable_process(
                        [sys.executable, "-c", "pass"],
                        timeout_seconds=2,
                    )
                    self.assertEqual(fake.spawn_calls, 1)
                self.assertEqual(fake.cleanup_calls, 1)
                self.assertEqual(fake.terminate_calls, 0)
                self.assertIsNone(attempt_scope.current_attempt_cgroup())


if __name__ == "__main__":
    unittest.main()
