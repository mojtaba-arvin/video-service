"""Attempt-scoped cgroup v2 lifecycle and process-launch context."""

from contextlib import contextmanager
from contextvars import ContextVar

from video_streaming.execution.cgroup_v2 import (
    AttemptCgroupV2,
    CgroupV2CleanupError,
)

_CURRENT_ATTEMPT_CGROUP = ContextVar(
    "video_streaming_attempt_cgroup", default=None
)


def current_attempt_cgroup():
    """Return the cgroup owned by the current processing Attempt, if any."""
    return _CURRENT_ATTEMPT_CGROUP.get()


@contextmanager
def attempt_cgroup_scope(attempt_id):
    """Own one cgroup for every FFmpeg/FFprobe child in a processing Attempt.

    The feature is opt-in. When enabled, missing delegation/controller support
    fails closed rather than silently running an unbounded process. All child
    processes are terminated and reaped before the cgroup directory is removed.
    """
    # Import settings only when a task explicitly enters the opt-in scope.
    # Process supervisors are also used by isolated FFmpeg helpers that do not
    # have a project .env file loaded.
    from video_streaming import settings

    if not settings.ATTEMPT_CGROUP_V2_ENABLED:
        yield None
        return

    cgroup = AttemptCgroupV2(
        delegated_root=settings.ATTEMPT_CGROUP_V2_ROOT,
        cpu_max=settings.ATTEMPT_CGROUP_V2_CPU_MAX,
        memory_max=settings.ATTEMPT_CGROUP_V2_MEMORY_MAX_BYTES,
        attempt_id=attempt_id,
    )
    token = _CURRENT_ATTEMPT_CGROUP.set(cgroup)
    try:
        yield cgroup
    finally:
        try:
            if not cgroup.wait_empty(0.05):
                cgroup.terminate()
                if not cgroup.wait_empty(
                        settings.ATTEMPT_CGROUP_V2_CLEANUP_TIMEOUT_SECONDS):
                    raise CgroupV2CleanupError(
                        "Attempt cgroup remained populated after termination"
                    )
            cgroup.cleanup(
                timeout_seconds=settings.ATTEMPT_CGROUP_V2_CLEANUP_TIMEOUT_SECONDS
            )
        finally:
            _CURRENT_ATTEMPT_CGROUP.reset(token)
