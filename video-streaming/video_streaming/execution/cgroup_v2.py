"""Attempt-scoped cgroup v2 resource enforcement.

The caller must pass a delegated cgroup-v2 directory whose cpu, memory and
pids controllers are already enabled in cgroup.subtree_control. This module
never changes host-wide controller policy and fails closed when delegation or
required kernel interfaces are unavailable.
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
    """One cgroup per durable Attempt, with limits applied before exec.

    Use `spawn` instead of subprocess.Popen: a tiny Python gate process starts,
    is moved into cgroup.procs, and only then execs the requested command.
    This avoids the unbounded fork-to-attach window and does not use
    preexec_fn (which is unsafe in threaded workers).
    """

    REQUIRED_CONTROLLERS = ("cpu", "memory", "pids")

    def __init__(self, delegated_root, cpu_max, memory_max, pids_max,
                 attempt_id=None):
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
        for name, value in (("memory_max", memory_max), ("pids_max", pids_max)):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("{} must be a positive integer".format(name))

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
            _write(os.path.join(self.path, "pids.max"), pids_max)
            required = ("cgroup.procs", "cgroup.events", "cgroup.kill",
                        "cpu.stat", "memory.peak", "memory.events", "pids.current")
            absent = [name for name in required
                      if not os.path.exists(os.path.join(self.path, name))]
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
        finally:
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
        """Return kernel counters without converting unavailable data to zero."""
        return {
            "cpu": _read_key_values(os.path.join(self.path, "cpu.stat")),
            "memory_peak_bytes": int(open(
                os.path.join(self.path, "memory.peak"), "r"
            ).read().strip()),
            "memory_events": _read_key_values(
                os.path.join(self.path, "memory.events")
            ),
            "pids_current": int(open(
                os.path.join(self.path, "pids.current"), "r"
            ).read().strip()),
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
            with open(events_path, "r") as stream:
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
