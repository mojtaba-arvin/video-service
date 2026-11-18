"""Low-overhead worker resource measurements and transparent cost estimates."""

import math
import re
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
    """Return a validated immutable price card or the non-monetary RUNIT model.

    An operator-supplied price card must have an explicit model ID, version,
    ISO-style currency code and at least one positive finite rate. Partially
    configured cards fail closed instead of silently falling back to RUNIT.
    """
    model_id = str(
        getattr(settings, "WORKFLOW_PRICING_MODEL_ID", "") or ""
    ).strip()
    model_version = str(
        getattr(settings, "WORKFLOW_PRICING_MODEL_VERSION", "") or ""
    ).strip()
    currency = str(
        getattr(settings, "WORKFLOW_COST_CURRENCY", "USD") or ""
    ).strip().upper()
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
    custom_card_configured = bool(
        model_id or model_version or any(value != 0 for value in rates.values())
    )
    if custom_card_configured:
        if not model_id or not model_version:
            raise ValueError(
                "A custom price card requires WORKFLOW_PRICING_MODEL_ID "
                "and WORKFLOW_PRICING_MODEL_VERSION"
            )
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError(
                "WORKFLOW_COST_CURRENCY must be an uppercase three-letter code"
            )
        for name, value in rates.items():
            if not math.isfinite(value) or value < 0:
                raise ValueError(
                    "{} must be finite and non-negative".format(name)
                )
        if not any(value > 0 for value in rates.values()):
            raise ValueError("At least one custom price-card rate must be positive")
        return {
            "model_id": model_id,
            "model_version": model_version,
            "currency": currency,
            **rates,
        }

    return {
        "model_id": "relative-resource-index-v1",
        "model_version": "1",
        "currency": "RUNIT",
        "cpu_per_second": 1.0,
        "memory_gb_second": 0.25,
        "storage_gb": 0.01,
    }
