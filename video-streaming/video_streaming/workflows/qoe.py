"""Pure query-response projection for playback QoE reports.

The projection stays independent of transport libraries so it can be tested
in the minimal workflow regression environment and reused by future adapters.
"""


def playback_qoe_report_payload(report):
    """Convert the durable QoE report to stable, transport-neutral fields."""
    coverage = report.get("cost_coverage") or {}
    complete = bool(report["cost_estimate_complete"])
    partial_cost = report.get("partial_estimated_cost")
    return {
        "tracking_id": report["job_id"],
        "output_id": report["stage_key"],
        "stage_status": report["stage_status"],
        "session_count": report["session_count"],
        "playback_started_count": report["playback_started_count"],
        "average_startup_ms": int(round(report["average_startup_ms"] or 0)),
        "rebuffer_count": report["rebuffer_count"],
        "total_buffering_ms": report["total_buffering_ms"],
        "decoder_error_count": report["decoder_error_count"],
        "buffering_ratio_percent": report["buffering_ratio_percent"] or 0.0,
        "total_cpu_seconds": report["total_cpu_seconds"],
        # Proto3 scalars default to zero; the availability flag carries presence.
        "total_estimated_cost": (
            report["total_estimated_cost"] if complete else 0.0
        ) or 0.0,
        "cost_currency": report["cost_currency"] or "",
        "pricing_model_id": report["pricing_model_id"] or "",
        "cost_estimate_complete": complete,
        "priced_attempt_count": report["priced_attempt_count"],
        "total_attempt_count": report["total_attempt_count"],
        "total_estimated_cost_available": complete,
        "partial_estimated_cost": partial_cost or 0.0,
        "partial_estimated_cost_available": partial_cost is not None,
        "complete_attempt_count": coverage.get("complete_attempt_count", 0),
        "missing_cost_dimensions_by_attempt": (
            coverage.get("missing_dimensions_by_attempt") or {}
        ),
        "excluded_cost_dimensions": list(
            coverage.get("excluded_dimensions") or []
        ),
        "pricing_model_versions": list(
            coverage.get("pricing_model_versions") or []
        ),
    }
