"""Tests for bounded FFmpeg process-tree termination."""

import subprocess
import sys
import time
import unittest

import psutil

from video_streaming.execution.process_control import (
    ProcessUsageAccumulator,
    terminate_process_tree,
)


class ProcessTreeTerminationTests(unittest.TestCase):

    def test_terminates_parent_and_nested_child_processes(self):
        child_code = "import time; time.sleep(60)"
        parent_code = (
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', {!r}]); "
            "time.sleep(60)"
        ).format(child_code)
        parent = subprocess.Popen(
            [sys.executable, "-c", parent_code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            root = psutil.Process(parent.pid)
            deadline = time.monotonic() + 5
            descendants = []
            while time.monotonic() < deadline:
                try:
                    descendants = root.children(recursive=True)
                except psutil.NoSuchProcess:
                    break
                if descendants:
                    break
                time.sleep(0.02)

            self.assertTrue(descendants, "child process did not start")
            child_pids = [item.pid for item in descendants]

            terminate_process_tree(parent, grace_period=0.5)
            parent.wait(timeout=2)

            for child_pid in child_pids:
                self.assertFalse(self._is_running(child_pid))

        finally:
            if parent.poll() is None:
                terminate_process_tree(parent, grace_period=0.5)
                parent.wait(timeout=2)

    def test_is_idempotent_when_process_already_exited(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process.wait(timeout=2)

        terminate_process_tree(process, grace_period=0.1)

    def test_deadline_terminates_parent_and_child_processes(self):
        import tempfile
        from pathlib import Path

        from video_streaming.execution.process_control import (
            run_cancellable_process,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            pid_path = Path(temp_dir) / "child.pid"
            child_code = (
                "import os, time; "
                "temporary = {!r}; target = {!r}; "
                "open(temporary, 'w').write(str(os.getpid())); "
                "os.replace(temporary, target); "
                "time.sleep(60)"
            ).format(str(pid_path) + ".tmp", str(pid_path))
            parent_code = (
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', {!r}]); "
                "time.sleep(60)"
            ).format(child_code)

            with self.assertRaises(subprocess.TimeoutExpired):
                run_cancellable_process(
                    [sys.executable, "-c", parent_code],
                    timeout_seconds=0.5,
                    poll_interval=0.02,
                    terminate_grace_period=0.2,
                )

            self.assertTrue(pid_path.exists(), "child process did not start")
            child_pid = int(pid_path.read_text())
            self.assertFalse(self._is_running(child_pid))

    def test_cooperative_cancellation_terminates_process_tree(self):
        import tempfile
        from pathlib import Path

        from video_streaming.execution.process_control import (
            ProcessExecutionCancelled,
            run_cancellable_process,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            pid_path = Path(temp_dir) / "child.pid"
            child_code = (
                "import os, time; "
                "temporary = {!r}; target = {!r}; "
                "open(temporary, 'w').write(str(os.getpid())); "
                "os.replace(temporary, target); "
                "time.sleep(60)"
            ).format(str(pid_path) + ".tmp", str(pid_path))
            parent_code = (
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', {!r}]); "
                "time.sleep(60)"
            ).format(child_code)

            with self.assertRaises(ProcessExecutionCancelled):
                run_cancellable_process(
                    [sys.executable, "-c", parent_code],
                    timeout_seconds=5,
                    poll_interval=0.02,
                    should_cancel=pid_path.exists,
                    terminate_grace_period=0.2,
                )

            self.assertTrue(pid_path.exists(), "child process did not start")
            child_pid = int(pid_path.read_text())
            self.assertFalse(self._is_running(child_pid))

    def test_non_zero_exit_preserves_command_error(self):
        from video_streaming.execution.process_control import run_cancellable_process

        with self.assertRaises(subprocess.CalledProcessError) as context:
            run_cancellable_process(
                [sys.executable, "-c", "import sys; print('bad'); sys.exit(7)"],
                timeout_seconds=2,
            )

        self.assertEqual(context.exception.returncode, 7)
        self.assertIn(b"bad", context.exception.output)

    def test_streaming_runner_reports_process_group_usage(self):
        from video_streaming.execution.process_control import run_streaming_process

        usage_samples = []
        result = run_streaming_process(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(0.3)",
            ],
            timeout_seconds=2,
            poll_interval=0.02,
            on_line=lambda line, process: None,
            on_usage=usage_samples.append,
        )

        self.assertEqual(result, 0)
        self.assertTrue(usage_samples)
        self.assertTrue(any(
            sample["peak_memory_bytes"] for sample in usage_samples
        ))
        self.assertGreaterEqual(
            usage_samples[-1]["memory_duration_ms"], 200
        )

    def test_each_subprocess_has_a_stable_unique_usage_identity(self):
        from video_streaming.execution.process_control import (
            run_cancellable_process,
            run_streaming_process,
        )

        identities = []
        for runner in (
                run_cancellable_process,
                run_streaming_process,
                run_cancellable_process):
            samples = []
            if runner is run_streaming_process:
                runner(
                    [sys.executable, "-c", "import time; time.sleep(0.12)"],
                    timeout_seconds=2,
                    poll_interval=0.02,
                    on_line=lambda line, process: None,
                    on_usage=samples.append,
                )
            else:
                runner(
                    [sys.executable, "-c", "import time; time.sleep(0.12)"],
                    timeout_seconds=2,
                    poll_interval=0.02,
                    on_usage=samples.append,
                )

            self.assertTrue(samples)
            process_ids = {sample["process_id"] for sample in samples}
            self.assertEqual(len(process_ids), 1)
            self.assertTrue(all(
                sample["process_pid"] > 0 for sample in samples
            ))
            identities.append(process_ids.pop())

        # Models FFmpeg followed by multiple FFprobe invocations: even if the
        # operating system reuses a PID, process accounting must not merge them.
        self.assertEqual(len(set(identities)), 3)

    def test_streaming_runner_delivers_lines_as_they_arrive(self):
        from video_streaming.execution.process_control import run_streaming_process

        lines = []
        result = run_streaming_process(
            [sys.executable, "-c", "print('first'); print('second')"],
            timeout_seconds=2,
            poll_interval=0.02,
            on_line=lambda line, process: lines.append(line),
        )

        self.assertEqual(result, 0)
        self.assertEqual(lines, ["first", "second"])

    def test_streaming_deadline_kills_child_after_parent_exits(self):
        import tempfile
        from pathlib import Path

        from video_streaming.execution.process_control import run_streaming_process

        with tempfile.TemporaryDirectory() as temp_dir:
            pid_path = Path(temp_dir) / "orphan.pid"
            child_code = (
                "import os, time; "
                "temporary = {!r}; target = {!r}; "
                "open(temporary, 'w').write(str(os.getpid())); "
                "os.replace(temporary, target); "
                "time.sleep(60)"
            ).format(str(pid_path) + ".tmp", str(pid_path))
            parent_code = (
                "import subprocess, sys; "
                "subprocess.Popen([sys.executable, '-c', {!r}])"
            ).format(child_code)

            with self.assertRaises(subprocess.TimeoutExpired):
                run_streaming_process(
                    [sys.executable, "-c", parent_code],
                    timeout_seconds=0.5,
                    poll_interval=0.02,
                    on_line=lambda line, process: None,
                    terminate_grace_period=0.2,
                )

            self.assertTrue(pid_path.exists(), "child process did not start")
            self.assertFalse(self._is_running(int(pid_path.read_text())))

    def test_streaming_cancellation_is_checked_without_new_output(self):
        import tempfile
        from pathlib import Path

        from video_streaming.execution.process_control import (
            ProcessExecutionCancelled,
            run_streaming_process,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            pid_path = Path(temp_dir) / "child.pid"
            child_code = (
                "import os, time; "
                "temporary = {!r}; target = {!r}; "
                "open(temporary, 'w').write(str(os.getpid())); "
                "os.replace(temporary, target); "
                "time.sleep(60)"
            ).format(str(pid_path) + ".tmp", str(pid_path))
            parent_code = (
                "import subprocess, sys; "
                "subprocess.Popen([sys.executable, '-c', {!r}]); "
                "time.sleep(60)"
            ).format(child_code)

            with self.assertRaises(ProcessExecutionCancelled):
                run_streaming_process(
                    [sys.executable, "-c", parent_code],
                    timeout_seconds=5,
                    poll_interval=0.02,
                    on_line=lambda line, process: None,
                    should_cancel=pid_path.exists,
                    terminate_grace_period=0.2,
                )

            self.assertTrue(pid_path.exists(), "child process did not start")
            self.assertFalse(self._is_running(int(pid_path.read_text())))

    def test_cancellable_runner_reports_process_tree_cpu_and_peak_memory(self):
        from video_streaming.execution.process_control import run_cancellable_process

        samples = []
        code = (
            "import time\n"
            "payload = bytearray(16 * 1024 * 1024)\n"
            "deadline = time.monotonic() + 0.4\n"
            "value = 0\n"
            "while time.monotonic() < deadline:\n"
            "    value += 1\n"
        )
        run_cancellable_process(
            [sys.executable, "-c", code],
            timeout_seconds=3,
            poll_interval=0.05,
            on_usage=samples.append,
        )

        self.assertTrue(samples)
        self.assertGreater(max(item["cpu_seconds"] for item in samples), 0)
        self.assertGreater(
            max(item["memory_duration_ms"] for item in samples), 0
        )
        self.assertGreaterEqual(
            max(item["peak_memory_bytes"] for item in samples),
            16 * 1024 * 1024,
        )

    def test_usage_accumulator_preserves_known_cumulative_samples(self):
        usage = ProcessUsageAccumulator()
        usage.record({
            "process_id": "ffmpeg",
            "cpu_seconds": 0.5,
            "peak_memory_bytes": 512,
            "memory_duration_ms": 100,
        })
        # A later partial sample must not erase earlier valid observations.
        usage.record({
            "process_id": "ffmpeg",
            "cpu_seconds": 0.75,
            "peak_memory_bytes": None,
            "memory_duration_ms": None,
        })
        usage.record({
            "process_id": "ffprobe",
            "cpu_seconds": 0.25,
            "peak_memory_bytes": 128,
            "memory_duration_ms": 40,
        })

        # CPU sums across distinct subprocesses; RSS remains a peak, while
        # per-process lifetimes are additive.
        self.assertEqual(usage.aggregate(), {
            "cpu_seconds": 1.0,
            "peak_memory_bytes": 512,
            "memory_duration_ms": 140,
        })

    def test_usage_accumulator_distinguishes_unknown_from_measured_zero(self):
        usage = ProcessUsageAccumulator()
        self.assertEqual(usage.aggregate(), {
            "cpu_seconds": None,
            "peak_memory_bytes": None,
            "memory_duration_ms": None,
        })

        usage.record({
            "process_id": "zero-usage",
            "cpu_seconds": 0.0,
            "peak_memory_bytes": 0,
            "memory_duration_ms": 0,
        })
        self.assertEqual(usage.aggregate(), {
            "cpu_seconds": 0.0,
            "peak_memory_bytes": 0,
            "memory_duration_ms": 0,
        })

    @staticmethod
    def _is_running(pid):
        try:
            proc = psutil.Process(pid)
            return proc.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False


    def test_usage_callback_failure_terminates_managed_process_tree(self):
        import tempfile
        from pathlib import Path

        from video_streaming.execution.process_control import (
            run_cancellable_process,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            pid_path = Path(temp_dir) / "lease-lost-child.pid"
            child_code = (
                "import os, time; "
                "temporary = {!r}; target = {!r}; "
                "open(temporary, 'w').write(str(os.getpid())); "
                "os.replace(temporary, target); "
                "time.sleep(60)"
            ).format(str(pid_path) + ".tmp", str(pid_path))
            parent_code = (
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', {!r}]); "
                "time.sleep(60)"
            ).format(child_code)
            callback_count = [0]

            def lose_lease_after_child_starts(_usage):
                callback_count[0] += 1
                if pid_path.exists():
                    raise RuntimeError("durable lease renewal failed")

            with self.assertRaisesRegex(
                    RuntimeError, "durable lease renewal failed"):
                run_cancellable_process(
                    [sys.executable, "-c", parent_code],
                    timeout_seconds=5,
                    poll_interval=0.05,
                    on_usage=lose_lease_after_child_starts,
                )

            self.assertGreater(callback_count[0], 0)
            self.assertTrue(pid_path.exists(), "child process did not start")
            self.assertFalse(self._is_running(int(pid_path.read_text())))



if __name__ == "__main__":
    unittest.main()
