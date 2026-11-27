# ADR 0011: Explainable workload admission and capacity selection

- **Status:** Accepted policy contract; runtime activation pending durable reservation and operational evidence
- **Date:** 2026-11-28
- **Related:** Issues #75 and #80; ADRs 0005, 0006 and 0010

## Context

The service has provider-neutral workload classes (#152), Attempt resource
telemetry and immutable price snapshots, Attempt-scoped CPU/memory enforcement
(#168), and atomic per-tenant active-Job quotas (#170). These are not a worker
scheduler. In particular, active-Job quotas do not bound concurrent Stage
Attempts or their aggregate CPU reservation. The durable dispatch path also
remains feature-gated pending deployed broker, identity and recovery acceptance.

The repository does not yet have a licensed representative corpus with
matched workload profiles and reconciled provider invoices. Therefore hourly
instance price, generic benchmark claims, and unmeasured resource estimates
must not be used as evidence of lower workload cost.

## Decision

1. **Separate classification, admission and capacity selection.** Workload
   classification remains derived from the typed operation contract. A
   provider-neutral policy consumes explicit resource requirements, tenant
   budget/usage, compatible pool capabilities, locality/privacy constraints,
   deadlines, queue age and measured cohort economics.
2. **CPU is the initial safe capacity class.** CPU admission is deterministic
   and bounded by tenant concurrent-attempt and CPU-unit budgets plus available
   global CPU capacity. A denial/defer decision must be explainable with stable
   reason codes. An active-Job quota is not a substitute for these budgets.
3. **No guessed economics.** A hard monetary budget requires a measured
   cost-per-successful-output value from the same currency and a representative
   cohort. If the estimate is missing, defer rather than treating it as zero.
   Cost includes failed/retried work; hourly instance price is not a ranking
   metric. Report p50/p95 end-to-end latency alongside cost.
4. **Fairness is tenant round-robin.** The policy orders each tenant's queue
   by deadline then age, and emits at most one item per tenant per round.
   Missing tenant identity is rejected; it is never mapped to a shared tenant.
5. **Locality and privacy are hard constraints.** A pool that does not satisfy
   the requested locality, privacy level, workload class, capabilities or
   resource envelope is ineligible. Unknown capabilities fail closed.
6. **GPU and preemptible capacity remain replaceable adapters.** Neither is
   eligible unless its adapter is explicitly enabled and the relevant
   idempotency, checkpoint and recovery guarantees are verified. No GPU/Spot
   provider is enabled by this policy increment.
7. **No runtime activation by configuration alone.** The policy core is
   deterministic and independently testable. Wiring it into production
   dispatch requires a PostgreSQL-atomic per-tenant Attempt reservation and
   release lifecycle, a cross-tenant fair dispatcher, exact-head concurrency
   tests, and operational tenant-policy provisioning. The existing durable
   dispatch feature flag remains unchanged.

## Consequences

- Decisions can be unit-tested and audited without binding domain contracts to
  Celery or a cloud provider.
- The policy reports cost per successful output and p50/p95 latency from
  measured observations; incomplete cost remains unknown.
- This increment does not claim production admission enforcement, worker
  provisioning, cost savings, GPU support, or Spot recovery.
- Follow-up work must add atomic Attempt reservations before production
  concurrency enforcement. It must not reinterpret the existing active-Job
  quota as an Attempt budget.
