"""Low-overhead worker resource measurements and transparent cost estimates."""

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


def configured_pricing(settings):
    """Return explicit provider rates or a clearly non-monetary resource index.

    The default is a normalized comparison unit, not money or a provider bill.
    It makes resource consumption cost-shaped from the first stage while
    avoiding a fabricated USD estimate before an operator configures a rate
    card.
    """
    model_id = getattr(settings, "WORKFLOW_PRICING_MODEL_ID", "")
    rates = {
        "cpu_per_second": float(
            getattr(settings, "WORKFLOW_CPU_COST_PER_SECOND", 0.0)
        ),
        "memory_gb_second": float(
            getattr(settings, "WORKFLOW_MEMORY_GB_SECOND_COST", 0.0)
        ),
        "storage_gb": float(
            getattr(settings, "WORKFLOW_STORAGE_GB_COST", 0.0)
        ),
    }
    if model_id and any(value > 0 for value in rates.values()):
        return {
            "model_id": model_id,
            "currency": getattr(settings, "WORKFLOW_COST_CURRENCY", "USD"),
            **rates,
        }

    return {
        "model_id": "relative-resource-index-v1",
        "currency": "RUNIT",
        "cpu_per_second": 1.0,
        "memory_gb_second": 0.25,
        "storage_gb": 0.01,
    }
