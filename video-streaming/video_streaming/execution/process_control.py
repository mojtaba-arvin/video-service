"""Bounded process-tree termination for FFmpeg subprocesses."""

import subprocess
import time

import psutil


class ProcessTreeTerminationError(RuntimeError):
    """Raised when one or more descendants survive forced termination."""


def terminate_process_tree(process, grace_period=1.0):
    """Terminate a process and its descendants, escalating to kill if needed.

    The root is stopped first to prevent it from spawning more children. The
    descendant snapshot is captured before termination, and all waits are
    bounded so a stuck process cannot block a Celery worker indefinitely.

    Args:
        process: A subprocess.Popen object or a PID.
        grace_period: Maximum seconds allowed for graceful and forced waits.

    Raises:
        ProcessTreeTerminationError: A process remains alive after SIGKILL.
        psutil.AccessDenied: The worker lacks permission to stop a process.
    """
    poll = getattr(process, "poll", None)
    if callable(poll) and poll() is not None:
        return

    pid = getattr(process, "pid", process)
    try:
        root = psutil.Process(int(pid))
    except psutil.NoSuchProcess:
        return

    try:
        descendants = root.children(recursive=True)
    except psutil.NoSuchProcess:
        return

    targets = descendants + [root]

    try:
        root.terminate()
    except psutil.NoSuchProcess:
        pass

    for child in reversed(descendants):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass

    _, alive = psutil.wait_procs(targets, timeout=grace_period)

    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass

    _, alive = psutil.wait_procs(alive, timeout=grace_period)
    if alive:
        remaining_pids = sorted(proc.pid for proc in alive)
        raise ProcessTreeTerminationError(
            "Processes survived forced termination: {}".format(
                ", ".join(str(item) for item in remaining_pids)
            )
        )


class ProcessExecutionCancelled(RuntimeError):
    """Raised when a cooperative cancellation check requests process shutdown."""


def run_cancellable_process(
        command,
        timeout_seconds,
        should_cancel=None,
        poll_interval=0.2,
        terminate_grace_period=1.0):
    """Run a command with a deadline and cooperative process-tree cancellation.

    Both stdout and stderr are drained while polling, so a verbose child cannot
    block on a full pipe. Timeout and cancellation terminate the complete
    process tree before returning control to the caller.

    Raises:
        ProcessExecutionCancelled: when should_cancel returns True.
        subprocess.TimeoutExpired: when the execution deadline is reached.
        subprocess.CalledProcessError: when the command exits non-zero.
        ProcessTreeTerminationError: when descendants survive forced shutdown.
    """
    if timeout_seconds is None or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be greater than zero")

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    started_at = time.monotonic()

    try:
        while True:
            if should_cancel is not None and should_cancel():
                terminate_process_tree(
                    process,
                    grace_period=terminate_grace_period,
                )
                stdout, stderr = process.communicate()
                raise ProcessExecutionCancelled(
                    "FFmpeg execution was cancelled"
                )

            elapsed = time.monotonic() - started_at
            remaining = timeout_seconds - elapsed
            if remaining <= 0:
                terminate_process_tree(
                    process,
                    grace_period=terminate_grace_period,
                )
                stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(
                    command,
                    timeout_seconds,
                    output=stdout,
                    stderr=stderr,
                )

            try:
                stdout, stderr = process.communicate(
                    timeout=min(poll_interval, remaining)
                )
                break
            except subprocess.TimeoutExpired:
                # communicate() can be called again after TimeoutExpired; it
                # continues draining the same pipes without losing output.
                continue

        if process.returncode:
            raise subprocess.CalledProcessError(
                process.returncode,
                command,
                output=stdout,
                stderr=stderr,
            )
        return stdout, stderr

    except BaseException:
        if process.poll() is None:
            terminate_process_tree(
                process,
                grace_period=terminate_grace_period,
            )
        if process.poll() is None:
            # terminate_process_tree already escalates to SIGKILL; this bounded
            # wait reaps the Popen child and closes both captured pipes.
            try:
                process.communicate(timeout=terminate_grace_period)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        raise
