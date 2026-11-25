"""Attempt-scoped CPU and memory enforcement with cgroup v2.

The caller must pass a delegated cgroup-v2 directory whose cpu, memory and
pids controllers are already enabled. Every workload process is created with
clone3(CLONE_INTO_CGROUP); there is deliberately no post-fork migration fallback.
"""
import os
import subprocess
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
    """One cgroup per Attempt; CPU, memory and PID limits apply at process creation.

    A small native launcher uses clone3(CLONE_INTO_CGROUP), so the target
    process is born inside the Attempt cgroup. Unsupported kernels or seccomp
    policies fail closed; there is no unsafe fork-then-migrate fallback.
    """

    REQUIRED_CONTROLLERS = ("cpu", "memory", "pids")

    def __init__(self, delegated_root, cpu_max, memory_max, attempt_id=None,
                 pids_max=64, helper_path="/usr/local/bin/video-cgroup-exec"):
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

        if not isinstance(pids_max, int) or isinstance(pids_max, bool) or pids_max <= 0:
            raise ValueError("pids_max must be a positive integer")
        if not isinstance(helper_path, str) or not helper_path:
            raise ValueError("helper_path is required")
        self.pids_max = pids_max
        self.helper_path = helper_path

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
            required = (
                "cgroup.procs", "cgroup.events", "cgroup.kill",
                "cpu.stat", "memory.peak", "memory.events",
                "pids.current", "pids.events",
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
        """Create the target directly inside this cgroup with atomic PID admission."""
        if self._closed:
            raise RuntimeError("attempt cgroup is closed")
        if not command or not isinstance(command, (list, tuple)):
            raise ValueError("command must be a non-empty argument sequence")
        if not os.path.isfile(self.helper_path) or not os.access(
                self.helper_path, os.X_OK):
            raise CgroupV2Unavailable(
                "atomic cgroup launcher is missing or not executable"
            )

        cgroup_fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            process = subprocess.Popen(
                [self.helper_path, str(cgroup_fd), "--"] + list(command),
                stdin=stdin, stdout=stdout, stderr=stderr, env=env,
                pass_fds=(cgroup_fd,), start_new_session=True, bufsize=0,
            )
        finally:
            os.close(cgroup_fd)

        process._video_service_process_group = True
        process._video_service_cgroup = self
        return process

    def usage(self):
        """Return Attempt-wide kernel counters, not per-process measurements."""
        with open(os.path.join(self.path, "memory.peak"), "r") as stream:
            memory_peak_bytes = int(stream.read().strip())
        return {
            "cpu": _read_key_values(os.path.join(self.path, "cpu.stat")),
            "memory_peak_bytes": memory_peak_bytes,
            "memory_events": _read_key_values(
                os.path.join(self.path, "memory.events")
            ),
            "pids_current": int(open(os.path.join(self.path, "pids.current"), "r").read().strip()),
            "pids_events": _read_key_values(os.path.join(self.path, "pids.events")),
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
