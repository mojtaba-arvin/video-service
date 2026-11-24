"""Attempt-scoped CPU and memory enforcement with cgroup v2.

The caller must pass a delegated cgroup-v2 directory whose cpu and memory
controllers are already enabled in cgroup.subtree_control. PID admission is
intentionally not claimed here: safely enforcing pids.max at process creation
requires an atomic CLONE_INTO_CGROUP-style launch path, not post-fork migration.
"""
import os
import signal
import subprocess
import sys
import time
import uuid


class CgroupV2Unavailable(RuntimeError):
    """Required cgroup v2 delegation or controller support is unavailable."""


class CgroupV2CleanupError(RuntimeError):
    """The attempt cgroup still contains processes and cannot be removed."""


def _write(path, value):
    with open(path, "w") as stream:
        stream.write(str(value))


def _read_key_values(path):
    values = {}
    with open(path, "r") as stream:
        for line in stream:
            key, value = line.split(None, 1)
            values[key] = int(value.strip())
    return values


class AttemptCgroupV2:
    """One cgroup per durable Attempt, with CPU/memory limits applied pre-exec.

    A gate process is attached to cgroup.procs before it is released to exec.
    This avoids the unbounded fork-to-exec workload window and does not use
    preexec_fn, which is unsafe in threaded workers. The gate itself is briefly
    forked outside the target cgroup; therefore pids.max is deliberately not
    configured or represented as an enforced limit by this implementation.
    """

    REQUIRED_CONTROLLERS = ("cpu", "memory")

    def __init__(self, delegated_root, cpu_max, memory_max, attempt_id=None):
        self.root = os.path.realpath(delegated_root)
        self.attempt_id = str(attempt_id or uuid.uuid4().hex)
        if not self.attempt_id or any(
                char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for char in self.attempt_id):
            raise ValueError("attempt_id must be a safe path component")
        if not isinstance(cpu_max, str) or len(cpu_max.split()) != 2:
            raise ValueError("cpu_max must be '<quota> <period>'")
        quota, period = cpu_max.split()
        if not quota.isdigit() or not period.isdigit() or int(quota) <= 0 or int(period) <= 0:
            raise ValueError("cpu_max quota and period must be positive integers")
        if not isinstance(memory_max, int) or isinstance(memory_max, bool) or memory_max <= 0:
            raise ValueError("memory_max must be a positive integer")

        controllers_path = os.path.join(self.root, "cgroup.controllers")
        subtree_path = os.path.join(self.root, "cgroup.subtree_control")
        if not os.path.isfile(controllers_path) or not os.path.isfile(subtree_path):
            raise CgroupV2Unavailable("delegated_root is not a cgroup v2 directory")
        with open(controllers_path, "r") as stream:
            available = set(stream.read().split())
        with open(subtree_path, "r") as stream:
            enabled = set(stream.read().split())
        missing = set(self.REQUIRED_CONTROLLERS) - available
        disabled = set(self.REQUIRED_CONTROLLERS) - enabled
        if missing or disabled:
            raise CgroupV2Unavailable(
                "required controllers unavailable={} disabled={}".format(
                    sorted(missing), sorted(disabled)
                )
            )

        self.path = os.path.join(self.root, "attempt-" + self.attempt_id)
        os.mkdir(self.path)
        try:
            _write(os.path.join(self.path, "cpu.max"), cpu_max)
            _write(os.path.join(self.path, "memory.max"), memory_max)
            required = (
                "cgroup.procs", "cgroup.events", "cgroup.kill",
                "cpu.stat", "memory.peak", "memory.events",
            )
            absent = [
                name for name in required
                if not os.path.exists(os.path.join(self.path, name))
            ]
            if absent:
                raise CgroupV2Unavailable(
                    "kernel lacks required cgroup files: {}".format(", ".join(absent))
                )
        except BaseException:
            os.rmdir(self.path)
            raise
        self._closed = False

    def spawn(self, command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
              stderr=subprocess.PIPE, env=None):
        """Start a command only after its gate process has joined this cgroup."""
        if self._closed:
            raise RuntimeError("attempt cgroup is closed")
        if not command or not isinstance(command, (list, tuple)):
            raise ValueError("command must be a non-empty argument sequence")
        read_fd, write_fd = os.pipe()
        os.set_inheritable(read_fd, True)
        helper = (
            "import os,sys; fd=int(sys.argv[1]); "
            "token=os.read(fd,1); os.close(fd); "
            "token == b'1' or os._exit(125); "
            "os.execvp(sys.argv[2], sys.argv[2:])"
        )
        try:
            process = subprocess.Popen(
                [sys.executable, "-c", helper, str(read_fd)] + list(command),
                stdin=stdin, stdout=stdout, stderr=stderr, env=env,
                pass_fds=(read_fd,), start_new_session=True, bufsize=0,
            )
        except BaseException:
            os.close(read_fd)
            os.close(write_fd)
            raise
        else:
            os.close(read_fd)

        try:
            _write(os.path.join(self.path, "cgroup.procs"), process.pid)
            os.write(write_fd, b"1")
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
        finally:
            os.close(write_fd)
        process._video_service_process_group = True
        process._video_service_cgroup = self
        return process

    def usage(self):
        """Return Attempt-wide kernel counters, not per-process measurements."""
        return {
            "cpu": _read_key_values(os.path.join(self.path, "cpu.stat")),
            "memory_peak_bytes": int(open(
                os.path.join(self.path, "memory.peak"), "r"
            ).read().strip()),
            "memory_events": _read_key_values(
                os.path.join(self.path, "memory.events")
            ),
        }

    def terminate(self):
        """Kill every process in this Attempt cgroup (including descendants)."""
        if not self._closed:
            _write(os.path.join(self.path, "cgroup.kill"), 1)

    def wait_empty(self, timeout_seconds):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        deadline = time.monotonic() + timeout_seconds
        events_path = os.path.join(self.path, "cgroup.events")
        while time.monotonic() < deadline:
            events = _read_key_values(events_path)
            if events.get("populated") == 0:
                return True
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        return False

    def cleanup(self, timeout_seconds=5.0):
        """Remove only after the kernel confirms the entire cgroup is empty."""
        if self._closed:
            return
        if not self.wait_empty(timeout_seconds):
            raise CgroupV2CleanupError(
                "attempt cgroup is still populated; refusing to remove it"
            )
        os.rmdir(self.path)
        self._closed = True
