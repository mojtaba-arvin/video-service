"""Deterministic workload admission, fairness and economics policy tests."""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from video_streaming.workflows.scheduling import (
    AdmissionStatus,
    CapacityKind,
    CapacityPool,
    PoolEconomics,
    TenantConcurrencyBudget,
    TenantConcurrencyUsage,
    WorkloadRequirements,
    fair_queue_order,
    select_capacity,
    summarize_workload_economics,
)


NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)


def cpu_pool(pool_id="cpu-a", **overrides):
    values = {
        "pool_id": pool_id,
        "kind": CapacityKind.CPU,
        "workload_classes": frozenset(["media_transform", "metadata"]),
        "capabilities": frozenset(["cpu", "ffmpeg"]),
        "locality": "eu-central",
        "privacy_levels": frozenset(["standard", "confidential"]),
        "cpu_capacity_units": 8,
        "available_cpu_units": 8,
        "memory_bytes": 8 * 1024 ** 3,
        "disk_bytes": 100 * 1024 ** 3,
        "adapter_enabled": True,
    }
    values.update(overrides)
    return CapacityPool(**values)


def budget(max_attempts=3, cpu_units=8):
    return TenantConcurrencyBudget(max_attempts, cpu_units)


class Request:
    def __init__(self, tenant_id, job_id, enqueued_at, deadline_at=None):
        self.tenant_id = tenant_id
        self.job_id = job_id
        self.enqueued_at = enqueued_at
        self.deadline_at = deadline_at


class SchedulingTests(unittest.TestCase):
    def requirements(self, **overrides):
        values = {
            "workload_class": "media_transform",
            "cpu_units": 2,
            "memory_bytes": 1024,
            "disk_bytes": 2048,
            "capabilities": frozenset(["ffmpeg"]),
            "locality": "eu-central",
            "privacy_level": "confidential",
            "enqueued_at": NOW - timedelta(seconds=30),
            "expected_runtime_seconds": 10,
        }
        values.update(overrides)
        return WorkloadRequirements(**values)

    def test_unknown_workload_class_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "registered workload class"):
            self.requirements(workload_class="unregistered")

    def test_disabled_cpu_adapter_is_not_selected(self):
        decision = select_capacity(
            self.requirements(), [cpu_pool(adapter_enabled=False)], budget(),
            TenantConcurrencyUsage(), 8, NOW,
        )
        self.assertEqual(decision.status, AdmissionStatus.NO_COMPATIBLE_CAPACITY)
        self.assertIn("PROVIDER_ADAPTER_DISABLED", decision.reason_codes)

    def test_cpu_only_workload_never_selects_gpu_only_pool(self):
        gpu = CapacityPool(
            pool_id="gpu-only",
            kind=CapacityKind.GPU,
            workload_classes=frozenset(["media_transform"]),
            capabilities=frozenset(["gpu", "ffmpeg"]),
            locality="eu-central",
            privacy_levels=frozenset(["confidential"]),
            cpu_capacity_units=8,
            available_cpu_units=8,
            adapter_enabled=True,
            recovery_contract_verified=True,
            supports_idempotency=True,
            supports_checkpoints=True,
            supports_recovery=True,
        )
        decision = select_capacity(
            self.requirements(), [gpu], budget(), TenantConcurrencyUsage(),
            global_available_cpu_units=8, now=NOW,
        )
        self.assertEqual(decision.status, AdmissionStatus.NO_COMPATIBLE_CAPACITY)
        self.assertIn("CPU_ONLY_WORKLOAD", decision.reason_codes)

    def test_gpu_and_spot_remain_disabled_without_recovery_contract(self):
        gpu = CapacityPool(
            pool_id="gpu",
            kind=CapacityKind.GPU,
            workload_classes=frozenset(["media_transform"]),
            capabilities=frozenset(["gpu", "ffmpeg"]),
            locality="eu-central",
            privacy_levels=frozenset(["confidential"]),
            cpu_capacity_units=8,
            available_cpu_units=8,
            adapter_enabled=True,
        )
        decision = select_capacity(
            self.requirements(gpu_required=True, capabilities=frozenset(["gpu"])),
            [gpu], budget(), TenantConcurrencyUsage(), 8, NOW,
        )
        self.assertIn("RECOVERY_CONTRACT_UNVERIFIED", decision.reason_codes)
        self.assertIn("INTERRUPTION_GUARANTEES_INCOMPLETE", decision.reason_codes)

    def test_tenant_concurrency_and_cpu_budgets_are_enforced(self):
        concurrency = select_capacity(
            self.requirements(), [cpu_pool()], budget(2, 8),
            TenantConcurrencyUsage(active_attempts=2, reserved_cpu_units=2),
            8, NOW,
        )
        self.assertEqual(concurrency.status, AdmissionStatus.REJECTED_TENANT_CONCURRENCY)

        cpu = select_capacity(
            self.requirements(cpu_units=4), [cpu_pool()], budget(4, 5),
            TenantConcurrencyUsage(active_attempts=1, reserved_cpu_units=2),
            8, NOW,
        )
        self.assertEqual(cpu.status, AdmissionStatus.REJECTED_CPU_CAPACITY)

    def test_global_cpu_capacity_defers_without_claiming_or_overbooking(self):
        decision = select_capacity(
            self.requirements(cpu_units=3), [cpu_pool()], budget(),
            TenantConcurrencyUsage(), 2, NOW,
        )
        self.assertEqual(decision.status, AdmissionStatus.DEFERRED)
        self.assertIn("GLOBAL_CPU_CAPACITY_EXHAUSTED", decision.reason_codes)

    def test_budget_rejects_using_cost_per_successful_output_not_hourly_price(self):
        economics = PoolEconomics(
            cost_per_successful_output=Decimal("0.42"),
            p50_latency_seconds=20,
            p95_latency_seconds=35,
            successful_outputs=100,
            corpus_id="licensed-cohort-v1",
            measured_at=NOW,
        )
        decision = select_capacity(
            self.requirements(max_cost=Decimal("0.30")),
            [cpu_pool(economics=economics)], budget(),
            TenantConcurrencyUsage(), 8, NOW,
        )
        self.assertEqual(decision.status, AdmissionStatus.REJECTED_BUDGET)
        self.assertIn("COST_BUDGET_EXCEEDED", decision.reason_codes)

    def test_budgeted_work_is_deferred_when_measured_economics_are_missing(self):
        decision = select_capacity(
            self.requirements(max_cost=Decimal("1.00")),
            [cpu_pool()], budget(), TenantConcurrencyUsage(), 8, NOW,
        )
        self.assertEqual(decision.status, AdmissionStatus.DEFERRED)
        self.assertIn("COST_ESTIMATE_UNAVAILABLE", decision.reason_codes)

    def test_deadline_locality_and_privacy_are_hard_eligibility_constraints(self):
        wrong_locality = cpu_pool(locality="us-east")
        decision = select_capacity(
            self.requirements(), [wrong_locality], budget(),
            TenantConcurrencyUsage(), 8, NOW,
        )
        self.assertIn("LOCALITY_MISMATCH", decision.reason_codes)

        private_pool = cpu_pool(privacy_levels=frozenset(["standard"]))
        decision = select_capacity(
            self.requirements(), [private_pool], budget(),
            TenantConcurrencyUsage(), 8, NOW,
        )
        self.assertIn("PRIVACY_POLICY_MISMATCH", decision.reason_codes)

        late = select_capacity(
            self.requirements(deadline_at=NOW + timedelta(seconds=5)),
            [cpu_pool()], budget(), TenantConcurrencyUsage(), 8, NOW,
        )
        self.assertIn("DEADLINE_AT_RISK", late.reason_codes)

    def test_measured_economics_rank_by_cost_per_successful_output(self):
        expensive = PoolEconomics(
            Decimal("0.50"), 10, 20, 100, "cohort-a", NOW
        )
        cheaper = PoolEconomics(
            Decimal("0.20"), 12, 25, 100, "cohort-a", NOW
        )
        decision = select_capacity(
            self.requirements(), [
                cpu_pool("fast-but-expensive", economics=expensive),
                cpu_pool("lower-total-cost", economics=cheaper),
            ], budget(), TenantConcurrencyUsage(), 8, NOW,
        )
        self.assertEqual(decision.status, AdmissionStatus.ADMITTED)
        self.assertEqual(decision.pool_id, "lower-total-cost")
        self.assertIn("MEASURED_COST_PER_SUCCESSFUL_OUTPUT", decision.reason_codes)

    def test_fair_queue_round_robins_tenants_and_respects_tenant_deadlines(self):
        requests = [
            Request("tenant-a", "a1", NOW - timedelta(seconds=60)),
            Request("tenant-a", "a2", NOW - timedelta(seconds=30),
                    NOW + timedelta(seconds=10)),
            Request("tenant-a", "a3", NOW - timedelta(seconds=20)),
            Request("tenant-b", "b1", NOW - timedelta(seconds=50)),
            Request("tenant-c", "c1", NOW - timedelta(seconds=40)),
        ]
        ordered = fair_queue_order(requests)
        self.assertEqual(
            [item.job_id for item in ordered],
            ["a2", "b1", "c1", "a1", "a3"],
        )

    def test_fair_queue_rejects_unowned_work_instead_of_inventing_tenant(self):
        with self.assertRaisesRegex(ValueError, "tenant_id"):
            fair_queue_order([Request("", "job", NOW)])

    def test_interruption_pool_requires_every_recovery_guarantee(self):
        for missing_guarantee in (
                "supports_idempotency", "supports_checkpoints", "supports_recovery"):
            values = {
                "pool_id": "spot",
                "kind": CapacityKind.SPOT,
                "workload_classes": frozenset(["media_transform"]),
                "capabilities": frozenset(["cpu", "ffmpeg"]),
                "locality": "eu-central",
                "privacy_levels": frozenset(["confidential"]),
                "cpu_capacity_units": 8,
                "available_cpu_units": 8,
                "adapter_enabled": True,
                "recovery_contract_verified": True,
                "supports_idempotency": True,
                "supports_checkpoints": True,
                "supports_recovery": True,
            }
            values[missing_guarantee] = False
            spot = CapacityPool(**values)
            decision = select_capacity(
                self.requirements(), [spot], budget(),
                TenantConcurrencyUsage(), 8, NOW,
            )
            self.assertIn(
                "INTERRUPTION_GUARANTEES_INCOMPLETE", decision.reason_codes
            )

    def test_spot_is_eligible_only_after_adapter_and_recovery_evidence(self):
        spot = CapacityPool(
            pool_id="spot",
            kind=CapacityKind.SPOT,
            workload_classes=frozenset(["media_transform"]),
            capabilities=frozenset(["cpu", "ffmpeg"]),
            locality="eu-central",
            privacy_levels=frozenset(["confidential"]),
            cpu_capacity_units=8,
            available_cpu_units=8,
            memory_bytes=8 * 1024 ** 3,
            disk_bytes=100 * 1024 ** 3,
            adapter_enabled=True,
            recovery_contract_verified=True,
            supports_idempotency=True,
            supports_checkpoints=True,
            supports_recovery=True,
        )
        decision = select_capacity(
            self.requirements(), [spot], budget(),
            TenantConcurrencyUsage(), 8, NOW,
        )
        self.assertEqual(decision.status, AdmissionStatus.ADMITTED)
        self.assertEqual(decision.pool_id, "spot")

    def test_duplicate_queue_job_identity_is_rejected(self):
        duplicate = Request("tenant-a", "same-job", NOW)
        with self.assertRaisesRegex(ValueError, "duplicate queued job identity"):
            fair_queue_order([duplicate, duplicate])

    def test_cost_per_successful_output_includes_failed_and_retry_cost(self):
        summary = summarize_workload_economics([
            {"total_cost": "2.00", "successful_outputs": 1, "latency_seconds": 10},
            {"total_cost": "1.00", "successful_outputs": 0, "latency_seconds": None},
            {"total_cost": "3.00", "successful_outputs": 2, "latency_seconds": 20},
        ])
        self.assertEqual(summary["cost_per_successful_output"], Decimal("2.00"))
        self.assertEqual(summary["p50_latency_seconds"], 20)
        self.assertEqual(summary["p95_latency_seconds"], 20)
        self.assertTrue(summary["cost_coverage_complete"])

    def test_missing_cost_is_not_zero_and_percentiles_remain_explicit(self):
        summary = summarize_workload_economics([
            {"total_cost": "1.00", "successful_outputs": 1, "latency_seconds": 10},
            {"total_cost": None, "successful_outputs": 1, "latency_seconds": 30},
        ])
        self.assertIsNone(summary["total_cost"])
        self.assertIsNone(summary["cost_per_successful_output"])
        self.assertEqual(summary["p50_latency_seconds"], 10)
        self.assertEqual(summary["p95_latency_seconds"], 30)
        self.assertFalse(summary["cost_coverage_complete"])


if __name__ == "__main__":
    unittest.main()
