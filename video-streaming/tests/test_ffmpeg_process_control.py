"""Tests for bounded FFmpeg process-tree termination."""

import subprocess
import sys
import time
import unittest

import psutil

from video_streaming.execution.process_control import terminate_process_tree


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

    @staticmethod
    def _is_running(pid):
        try:
            proc = psutil.Process(pid)
            return proc.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False


if __name__ == "__main__":
    unittest.main()
