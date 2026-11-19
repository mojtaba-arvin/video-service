---
name: durable-workflow-design
description: Use when designing jobs, workflows, stages, attempts, artifact records, retries, cancellation, idempotency, recovery, scheduling, or persistence adapters.
---

# Durable workflow design

Keep these domain concepts distinct:

- Job: caller-visible request and stable identity.
- Workflow: versioned execution definition selected for the job.
- Stage: typed operation with declared input and output contracts.
- Attempt: one execution with worker identity, timing, outcome, retry count,
  failure classification, and measured resource usage.
- Artifact: immutable validated output reference, checksum, media metadata,
  and publication state.

## Required invariants

1. Durable persistence is authoritative; Redis and broker are not a job ledger.
2. Assume at-least-once delivery. State transitions and side effects are idempotent.
3. A stage succeeds only after outputs satisfy its contract.
4. Publish only validated artifacts.
5. Cancellation is durable and has deterministic terminal semantics.
6. Retries are classified, bounded, deadline-aware, and recorded as attempts.
7. Duplicate scoped idempotency keys return the original job.
8. Worker, broker, or cache loss must not orphan an untracked workflow.
9. Use an outbox/inbox or equivalent transaction-safe dispatch pattern.
10. Tenant scope and authorization are enforced on every resource path.
11. Preserve stable application-owned correlation IDs across outbox, broker, worker, media, storage and callback boundaries.
12. Every state transition, claim conflict, retry, recovery, cancellation and terminal outcome must be queryable as a structured durable event.
13. Emit structured logs, bounded-cardinality metrics and trace context; never use high-cardinality IDs as metric labels.
14. Missing resource or cost measurements remain explicitly incomplete, not zero.
15. Define runbooks and safe replay/rollback semantics before enabling automatic dispatch or recovery.

## Failure-first design

Write transition tables and invariants before choosing persistence technology.
Test duplicate submissions/messages, crash windows, retry exhaustion, cancel
races, and recovery after Redis/broker restart. Celery is a replaceable execution
adapter, not the domain model. Record database/state-machine decisions as ADRs. Apply
`docs/engineering/reliability-observability-standard.md` to all workflow
changes; a happy-path test alone is insufficient.
