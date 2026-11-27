"""Deterministic, explainable workload admission and capacity selection.

This module is a provider-neutral policy core. It does not provision workers,
reserve database capacity, or claim that measured economics exist. Callers must
supply an authoritative tenant usage/budget snapshot and measured pool
observations. Missing measurements remain unknown; they are never treated as
zero. Production dispatch must pair this policy with an atomic reservation.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from math import ceil
from typing import Optional, Tuple


class CapacityKind(str, Enum):
    CPU = "cpu"
    GPU = "gpu"
    SPOT = "spot"


class AdmissionStatus(str, Enum):
    ADMITTED = "admitted"
    DEFERRED = "deferred"
    REJECTED_BUDGET = "rejected_budget"
    REJECTED_TENANT_CONCURRENCY = "rejected_tenant_concurrency"
    REJECTED_CPU_CAPACITY = "rejected_cpu_capacity"
    NO_COMPATIBLE_CAPACITY = "no_compatible_capacity"


def _aware(value, name):
    if value is not None and (
            not isinstance(value, datetime)
            or value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("{} must be timezone-aware".format(name))


def _positive_int(value, name, allow_zero=False):
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError("{} must be an integer >= {}".format(name, minimum))


def _decimal(value, name):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("{} must be a finite decimal".format(name))
    if not result.is_finite() or result < 0:
        raise ValueError("{} must be a finite non-negative decimal".format(name))
    return result


@dataclass(frozen=True)
class WorkloadRequirements:
    workload_class: str
    cpu_units: int = 1
    memory_bytes: Optional[int] = None
    disk_bytes: Optional[int] = None
    capabilities: frozenset = field(default_factory=frozenset)
    gpu_required: bool = False
    locality: Optional[str] = None
    privacy_level: str = "standard"
    enqueued_at: Optional[datetime] = None
    deadline_at: Optional[datetime] = None
    expected_runtime_seconds: Optional[float] = None
    max_cost: Optional[Decimal] = None
    cost_currency: str = "USD"

    def __post_init__(self):
        if not isinstance(self.workload_class, str) or not self.workload_class.strip():
            raise ValueError("workload_class is required")
        _positive_int(self.cpu_units, "cpu_units")
        for name in ("memory_bytes", "disk_bytes"):
            value = getattr(self, name)
            if value is not None:
                _positive_int(value, name, allow_zero=True)
        if not isinstance(self.capabilities, (set, frozenset)):
            raise TypeError("capabilities must be a set")
        if not isinstance(self.gpu_required, bool):
            raise TypeError("gpu_required must be a boolean")
        if not isinstance(self.privacy_level, str) or not self.privacy_level.strip():
            raise ValueError("privacy_level is required")
        _aware(self.enqueued_at, "enqueued_at")
        _aware(self.deadline_at, "deadline_at")
        if self.expected_runtime_seconds is not None and (
                isinstance(self.expected_runtime_seconds, bool)
                or self.expected_runtime_seconds <= 0):
            raise ValueError("expected_runtime_seconds must be positive")
        if self.max_cost is not None:
            object.__setattr__(self, "max_cost", _decimal(self.max_cost, "max_cost"))
        if not isinstance(self.cost_currency, str) or len(self.cost_currency) != 3:
            raise ValueError("cost_currency must be a three-letter code")


@dataclass(frozen=True)
class PoolEconomics:
    """Measured cohort results; hourly instance price is deliberately absent."""

    cost_per_successful_output: Decimal
    p50_latency_seconds: float
    p95_latency_seconds: float
    successful_outputs: int
    corpus_id: str
    measured_at: datetime
    currency: str = "USD"

    def __post_init__(self):
        object.__setattr__(
            self, "cost_per_successful_output",
            _decimal(self.cost_per_successful_output, "cost_per_successful_output"),
        )
        _positive_int(self.successful_outputs, "successful_outputs")
        if self.p50_latency_seconds < 0 or self.p95_latency_seconds < self.p50_latency_seconds:
            raise ValueError("latency percentiles must be non-negative and ordered")
        if not isinstance(self.corpus_id, str) or not self.corpus_id.strip():
            raise ValueError("corpus_id is required for measured economics")
        _aware(self.measured_at, "measured_at")
        if not isinstance(self.currency, str) or len(self.currency) != 3:
            raise ValueError("currency must be a three-letter code")


@dataclass(frozen=True)
class CapacityPool:
    pool_id: str
    kind: CapacityKind
    workload_classes: frozenset
    capabilities: frozenset
    locality: str
    privacy_levels: frozenset
    cpu_capacity_units: int
    available_cpu_units: int
    memory_bytes: Optional[int] = None
    disk_bytes: Optional[int] = None
    startup_delay_seconds: float = 0.0
    economics: Optional[PoolEconomics] = None
    adapter_enabled: bool = False
    recovery_contract_verified: bool = False
    supports_idempotency: bool = False
    supports_checkpoints: bool = False
    supports_recovery: bool = False

    def __post_init__(self):
        if not isinstance(self.pool_id, str) or not self.pool_id.strip():
            raise ValueError("pool_id is required")
        if not isinstance(self.kind, CapacityKind):
            object.__setattr__(self, "kind", CapacityKind(self.kind))
        _positive_int(self.cpu_capacity_units, "cpu_capacity_units")
        _positive_int(self.available_cpu_units, "available_cpu_units", allow_zero=True)
        if self.available_cpu_units > self.cpu_capacity_units:
            raise ValueError("available_cpu_units cannot exceed pool capacity")
        if self.startup_delay_seconds < 0:
            raise ValueError("startup_delay_seconds cannot be negative")
        if not isinstance(self.workload_classes, (set, frozenset)):
            raise TypeError("workload_classes must be a set")
        if not isinstance(self.capabilities, (set, frozenset)):
            raise TypeError("capabilities must be a set")
        if not isinstance(self.privacy_levels, (set, frozenset)):
            raise TypeError("privacy_levels must be a set")


@dataclass(frozen=True)
class TenantConcurrencyBudget:
    max_concurrent_attempts: int
    max_cpu_units: int

    def __post_init__(self):
        _positive_int(self.max_concurrent_attempts, "max_concurrent_attempts")
        _positive_int(self.max_cpu_units, "max_cpu_units")


@dataclass(frozen=True)
class TenantConcurrencyUsage:
    active_attempts: int = 0
    reserved_cpu_units: int = 0

    def __post_init__(self):
        _positive_int(self.active_attempts, "active_attempts", allow_zero=True)
        _positive_int(self.reserved_cpu_units, "reserved_cpu_units", allow_zero=True)


@dataclass(frozen=True)
class SchedulingDecision:
    status: AdmissionStatus
    pool_id: Optional[str]
    reason_codes: Tuple[str, ...]
    queue_age_seconds: Optional[float] = None
    estimated_cost_per_successful_output: Optional[Decimal] = None
    expected_finish_at: Optional[datetime] = None


def _pool_compatibility_reasons(requirements, pool):
    reasons = []
    if requirements.workload_class not in pool.workload_classes:
        reasons.append("WORKLOAD_CLASS_UNSUPPORTED")
    if not requirements.capabilities.issubset(pool.capabilities):
        reasons.append("CAPABILITY_MISMATCH")
    if requirements.locality and pool.locality != requirements.locality:
        reasons.append("LOCALITY_MISMATCH")
    if requirements.privacy_level not in pool.privacy_levels:
        reasons.append("PRIVACY_POLICY_MISMATCH")
    if requirements.memory_bytes is not None and (
            pool.memory_bytes is None or pool.memory_bytes < requirements.memory_bytes):
        reasons.append("MEMORY_CAPACITY_INSUFFICIENT")
    if requirements.disk_bytes is not None and (
            pool.disk_bytes is None or pool.disk_bytes < requirements.disk_bytes):
        reasons.append("DISK_CAPACITY_INSUFFICIENT")
    if pool.available_cpu_units < requirements.cpu_units:
        reasons.append("POOL_CPU_CAPACITY_EXHAUSTED")

    if pool.kind == CapacityKind.CPU and requirements.gpu_required:
        reasons.append("GPU_REQUIRED")
    if pool.kind == CapacityKind.GPU and not requirements.gpu_required:
        reasons.append("CPU_ONLY_WORKLOAD")
    if pool.kind in (CapacityKind.GPU, CapacityKind.SPOT):
        if not pool.adapter_enabled:
            reasons.append("PROVIDER_ADAPTER_DISABLED")
        if not pool.recovery_contract_verified:
            reasons.append("RECOVERY_CONTRACT_UNVERIFIED")
        if not (
                pool.supports_idempotency
                and pool.supports_checkpoints
                and pool.supports_recovery):
            reasons.append("INTERRUPTION_GUARANTEES_INCOMPLETE")
    return reasons


def _queue_age(requirements, now):
    if requirements.enqueued_at is None:
        return None
    return max(0.0, (now - requirements.enqueued_at).total_seconds())


def _finish_time(requirements, pool, now):
    if pool.economics is not None:
        runtime = pool.economics.p95_latency_seconds
    else:
        runtime = requirements.expected_runtime_seconds
    if runtime is None:
        return None
    return now + timedelta(seconds=pool.startup_delay_seconds + runtime)


def select_capacity(
        requirements, pools, tenant_budget, tenant_usage,
        global_available_cpu_units, now=None):
    """Select a compatible pool and return a bounded, auditable explanation.

    The default safe path is CPU-only. GPU and Spot pools require an explicitly
    enabled adapter plus verified idempotency, checkpoint and recovery support.
    A hard cost budget cannot be evaluated from hourly price or missing data.
    """
    now = now or datetime.now(timezone.utc)
    _aware(now, "now")
    _positive_int(global_available_cpu_units, "global_available_cpu_units", allow_zero=True)
    age = _queue_age(requirements, now)

    if tenant_usage.active_attempts >= tenant_budget.max_concurrent_attempts:
        return SchedulingDecision(
            AdmissionStatus.REJECTED_TENANT_CONCURRENCY, None,
            ("TENANT_CONCURRENCY_LIMIT",), age,
        )
    if tenant_usage.reserved_cpu_units + requirements.cpu_units > tenant_budget.max_cpu_units:
        return SchedulingDecision(
            AdmissionStatus.REJECTED_CPU_CAPACITY, None,
            ("TENANT_CPU_BUDGET_EXHAUSTED",), age,
        )
    if requirements.cpu_units > global_available_cpu_units:
        return SchedulingDecision(
            AdmissionStatus.DEFERRED, None, ("GLOBAL_CPU_CAPACITY_EXHAUSTED",), age,
        )

    compatible = []
    rejected_reasons = set()
    for pool in pools:
        reasons = _pool_compatibility_reasons(requirements, pool)
        if reasons:
            rejected_reasons.update(reasons)
            continue
        if requirements.max_cost is not None:
            economics = pool.economics
            if economics is None or economics.currency != requirements.cost_currency:
                rejected_reasons.add("COST_ESTIMATE_UNAVAILABLE")
                continue
            if economics.cost_per_successful_output > requirements.max_cost:
                rejected_reasons.add("COST_BUDGET_EXCEEDED")
                continue
        finish_at = _finish_time(requirements, pool, now)
        if requirements.deadline_at is not None:
            if finish_at is None:
                rejected_reasons.add("DEADLINE_ESTIMATE_UNAVAILABLE")
                continue
            if finish_at > requirements.deadline_at:
                rejected_reasons.add("DEADLINE_AT_RISK")
                continue
        economics = pool.economics
        cost = (
            economics.cost_per_successful_output
            if economics is not None else None
        )
        compatible.append((
            cost is None, cost if cost is not None else Decimal("0"),
            finish_at or datetime.max.replace(tzinfo=timezone.utc),
            pool.startup_delay_seconds, pool.pool_id, pool, finish_at,
        ))

    if not compatible:
        if "COST_BUDGET_EXCEEDED" in rejected_reasons:
            status = AdmissionStatus.REJECTED_BUDGET
        elif "POOL_CPU_CAPACITY_EXHAUSTED" in rejected_reasons or "GLOBAL_CPU_CAPACITY_EXHAUSTED" in rejected_reasons:
            status = AdmissionStatus.DEFERRED
        elif "DEADLINE_AT_RISK" in rejected_reasons or "COST_ESTIMATE_UNAVAILABLE" in rejected_reasons or "DEADLINE_ESTIMATE_UNAVAILABLE" in rejected_reasons:
            status = AdmissionStatus.DEFERRED
        else:
            status = AdmissionStatus.NO_COMPATIBLE_CAPACITY
        return SchedulingDecision(
            status, None, tuple(sorted(rejected_reasons or {"NO_COMPATIBLE_POOL"})), age,
        )

    compatible.sort(key=lambda item: item[:5])
    _, _, _, _, _, selected, finish_at = compatible[0]
    reasons = ["COMPATIBLE_CPU_CAPACITY"]
    if selected.economics is None:
        reasons.append("SAFE_FALLBACK_UNMEASURED_ECONOMICS")
    else:
        reasons.append("MEASURED_COST_PER_SUCCESSFUL_OUTPUT")
    if age is not None:
        reasons.append("QUEUE_AGE_CONSIDERED")
    if requirements.deadline_at is not None:
        reasons.append("DEADLINE_FEASIBLE")
    if requirements.locality:
        reasons.append("LOCALITY_MATCHED")
    reasons.append("PRIVACY_POLICY_MATCHED")
    return SchedulingDecision(
        AdmissionStatus.ADMITTED,
        selected.pool_id,
        tuple(reasons),
        age,
        selected.economics.cost_per_successful_output if selected.economics else None,
        finish_at,
    )


def fair_queue_order(requests):
    """Return tenant-round-robin order; preserve deadline/age order per tenant.

    Each item must expose tenant_id, enqueued_at and optional deadline_at.
    The oldest tenant head starts the rotation; one item per tenant is emitted
    per round so a burst from one tenant cannot occupy the entire dispatch list.
    """
    grouped = {}
    for request in requests:
        tenant = getattr(request, "tenant_id", None)
        enqueued = getattr(request, "enqueued_at", None)
        deadline = getattr(request, "deadline_at", None)
        if not isinstance(tenant, str) or not tenant.strip():
            raise ValueError("every queued request requires tenant_id")
        _aware(enqueued, "enqueued_at")
        _aware(deadline, "deadline_at")
        if enqueued is None:
            raise ValueError("every queued request requires enqueued_at")
        grouped.setdefault(tenant, []).append(request)

    for tenant in grouped:
        grouped[tenant].sort(key=lambda item: (
            getattr(item, "deadline_at", None) or datetime.max.replace(tzinfo=timezone.utc),
            item.enqueued_at,
            getattr(item, "job_id", ""),
        ))
    tenant_order = sorted(
        grouped,
        key=lambda tenant: (grouped[tenant][0].enqueued_at, tenant),
    )
    ordered = []
    while tenant_order:
        next_round = []
        for tenant in tenant_order:
            ordered.append(grouped[tenant].pop(0))
            if grouped[tenant]:
                next_round.append(tenant)
        tenant_order = next_round
    return ordered


def _nearest_rank(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, int(ceil(percentile * len(ordered))) - 1)]


def summarize_workload_economics(observations):
    """Aggregate repeated matched-cohort runs without hiding incomplete costs.

    Each observation contains total_cost (including failed/retried work),
    successful_outputs and optional successful-output latency_seconds.
    """
    if not observations:
        return {
            "sample_count": 0,
            "successful_outputs": 0,
            "total_cost": None,
            "cost_per_successful_output": None,
            "p50_latency_seconds": None,
            "p95_latency_seconds": None,
            "cost_coverage_complete": False,
        }
    total_cost = Decimal("0")
    cost_complete = True
    successes = 0
    latencies = []
    for item in observations:
        if not isinstance(item, dict):
            raise TypeError("observations must contain dictionaries")
        count = item.get("successful_outputs", 0)
        _positive_int(count, "successful_outputs", allow_zero=True)
        successes += count
        cost = item.get("total_cost")
        if cost is None:
            cost_complete = False
        else:
            total_cost += _decimal(cost, "total_cost")
        latency = item.get("latency_seconds")
        if latency is not None and count:
            if isinstance(latency, bool) or float(latency) < 0:
                raise ValueError("latency_seconds must be non-negative")
            latencies.extend([float(latency)] * count)
    return {
        "sample_count": len(observations),
        "successful_outputs": successes,
        "total_cost": total_cost if cost_complete else None,
        "cost_per_successful_output": (
            total_cost / Decimal(successes)
            if cost_complete and successes else None
        ),
        "p50_latency_seconds": _nearest_rank(latencies, 0.50),
        "p95_latency_seconds": _nearest_rank(latencies, 0.95),
        "cost_coverage_complete": cost_complete,
    }
