"""Bounded process-tree termination for FFmpeg subprocesses."""

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
    pid = getattr(process, "pid", process)
    root = psutil.Process(int(pid))

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
