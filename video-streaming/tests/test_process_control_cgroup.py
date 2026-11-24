"""Integration contract tests for cgroup-aware process supervision."""
import subprocess
import sys
import unittest

from video_streaming.execution.process_control import (
    run_cancellable_process,
    run_streaming_process,
)


class FakeAttemptCgroup:
    def __init__(self):
        self.spawn_calls = 0
        self.terminated = False

    def spawn(self, command, stdin, stdout, stderr):
        self.spawn_calls += 1
        process = subprocess.Popen(
            command, stdin=stdin, stdout=stdout, stderr=stderr,
            start_new_session=True, bufsize=0,
        )
        process._video_service_process_group = True
        process._video_service_cgroup = self
        return process

    def usage(self):
        return {
            "cpu": {"usage_usec": 250000},
            "memory_peak_bytes": 8192,
            "memory_events": {"oom_kill": 0},
            "pids_current": 1,
        }

    def terminate(self):
        self.terminated = True

    def wait_empty(self, timeout_seconds):
        return True


class CgroupProcessSupervisorTests(unittest.TestCase):
    def test_cancellable_runner_launches_inside_attempt_cgroup(self):
        cgroup = FakeAttemptCgroup()
        samples = []
        run_cancellable_process(
            [sys.executable, "-c", "pass"],
            timeout_seconds=2,
            on_usage=samples.append,
            attempt_cgroup=cgroup,
        )
        self.assertEqual(cgroup.spawn_calls, 1)
        self.assertTrue(samples)
        self.assertGreaterEqual(samples[-1]["cpu_seconds"], 0.25)
        self.assertGreaterEqual(samples[-1]["peak_memory_bytes"], 8192)

    def test_streaming_runner_launches_inside_attempt_cgroup(self):
        cgroup = FakeAttemptCgroup()
        samples = []
        lines = []
        run_streaming_process(
            [sys.executable, "-c", "print('bounded')"],
            timeout_seconds=2,
            on_line=lambda line, process: lines.append(line),
            on_usage=samples.append,
            attempt_cgroup=cgroup,
        )
        self.assertEqual(cgroup.spawn_calls, 1)
        self.assertEqual(lines, ["bounded"])
        self.assertTrue(samples)
        self.assertGreaterEqual(samples[-1]["cpu_seconds"], 0.25)
        self.assertGreaterEqual(samples[-1]["peak_memory_bytes"], 8192)


if __name__ == "__main__":
    unittest.main()
