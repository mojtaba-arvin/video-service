"""Low-overhead worker resource measurements for durable workflow attempts."""

import resource


def capture_child_cpu_seconds():
    """Snapshot process-child CPU time; Celery prefork workers isolate tasks."""
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return float(usage.ru_utime + usage.ru_stime)


def child_cpu_seconds_since(start_value):
    """Return non-negative child CPU consumed since the supplied snapshot."""
    return max(
        0.0,
        capture_child_cpu_seconds() - float(start_value),
    )
