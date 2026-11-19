# ADR 0002: Durable VOD workflow state and typed operations

- **Status:** Accepted
- **Date:** 2026-11-18
- **Scope:** Current VOD job submission, playlist/thumbnail stages, publication and reporting

## Context

Redis TTL-backed keys currently carry job identity, status, and output counters. A cache eviction or worker restart can therefore make an accepted job difficult to recover or report. Celery result IDs also risk becoming the accidental identity of a job. Cost/resource fields exist only as transient progress samples, while future API and MCP adapters need a stable operation contract.

## Decisions

### Source of truth and compatibility

- PostgreSQL is the authoritative store for `Job`, `Workflow`, `Stage`, `Attempt`, and `Artifact`.
- SQLAlchemy 2.0 is used for Python 3.9 runtime compatibility; Alembic is pinned to 1.16.5. Runtime migration to a newer Python remains a separate security/compatibility work item.
- Redis remains a short-lived progress and legacy response projection. Losing Redis must not lose the job identity, final stage state, validation report, artifact inventory, attempt metrics, or workflow events.
- Celery task IDs are recorded as optional execution references. They do not identify a job, stage, or attempt and can be replaced by another execution adapter.

### Identity and idempotency

- A job has a client idempotency key (or a generated key for legacy requests) and a deterministic request fingerprint.
- Reusing the same idempotency key with the same fingerprint returns the existing tracking ID. Reusing it with a different fingerprint is rejected.
- A workflow has one durable row per job. A stage has a stable key within that workflow. Each actual retry/recovery execution receives a new numbered attempt.
- A worker redelivery with the same execution reference may reattach to its current attempt; a distinct concurrent execution cannot create a second active attempt for the same stage.

### Allowed stage transitions

The first supported execution path uses these transitions:

`PENDING → PROCESSING → VALIDATED → PUBLISHING → FINISHED`

Failure may move a non-terminal stage to `RETRY_WAIT`, from which a new attempt returns it to `PROCESSING`. Failure without remaining retries moves it to `FAILED`. Cancellation moves an active stage to `CANCELLED`. Terminal states are not reopened.

The repository enforces these transitions under a row lock and appends an immutable workflow event in the same transaction. Stale running attempts can be moved to `RETRY_WAIT` from database timestamps without depending on Redis TTL.

### Artifacts and publication

- A structured validator report is stored as an Artifact of type `validation_report` and is associated with its stage and attempt.
- Each verified published playlist/segment/init object and the package inventory sidecar is recorded as an Artifact.
- A playlist stage becomes `FINISHED` only after all referenced objects pass storage readback and the root manifest is published last.
- The gRPC result projection reads durable state first; Redis may enrich it with live progress but does not override durable completion.

### Resource and cost reporting

Every completed, failed, retryable, cancelled, or recovered worker-loss attempt can retain its measured duration, CPU seconds, peak memory bytes, input/output bytes, and accounting evidence.

- A custom infrastructure price card requires a stable model ID, explicit immutable model version, currency, and finite non-negative rates. Partial card configuration fails closed. The exact rates and their units are snapshotted on the attempt; later configuration changes cannot silently re-price historical work.
- Without a custom card, `relative-resource-index-v1` version `1` reports `RUNIT`: 1.0 per CPU-second, 0.25 per GiB-second at measured peak RSS, and 0.01 per GiB of output bytes. These are normalized weights, not money, a provider bill, or a claim of cost accuracy.
- Each component records quantity, unit, rate, amount, and measurement coverage. Unknown metrics are never coerced to zero. Memory is estimated as measured peak RSS multiplied by the encoder processing interval; when that interval is unavailable, the full Attempt duration is used as a conservative fallback and the metric source remains visible in the accounting metadata. A partial estimate is retained for investigation, but the aggregate `total_estimated_cost` remains null unless every attempt is priced completely with the same currency, model ID, and model version.
- The snapshot explicitly excludes GPU compute, network ingress/egress, provider invoice reconciliation, idle/control-plane resources, power and cooling. Those dimensions must be instrumented and reconciled before a provider-bill total can be claimed.
- Failed/retryable and cancelled attempts can record observed spend. Worker-loss recovery records elapsed time but marks unobserved CPU/memory/output usage incomplete rather than inventing measurements.

### Typed internal operations

Core operation identifiers and dataclass request/result/error contracts live in the workflow package. gRPC and Celery are adapters around these operations. Future REST or MCP surfaces should call the same application contracts; they must not reimplement media business logic or expose FFmpeg command construction as the public domain API.

## Consequences

- Job identity and completed playlist state survive Redis cache loss.
- Retry attempts and worker-loss recovery are recorded independently of Celery result retention.
- Storage publication and validation evidence can be queried alongside resource measurements.
- Cost data is available from the first durable stage without inventing a dollar estimate.
- The initial implementation intentionally excludes tenant isolation, auth/RBAC, a public API redesign, and an MCP server.
- Thumbnail stages use durable attempts, validation artifacts, storage readback and are covered by the VOD E2E test. Watermark transformation and its separately published output are also wired to durable attempts, validation and readback; that branch still needs dedicated E2E coverage before it is claimed as end-to-end verified.
- The database-first create-then-dispatch sequence still has a narrow process-crash window between committing the job and publishing the Celery graph. A transactional dispatch/outbox mechanism is deferred until a durable dispatcher is introduced; Redis is not used to pretend that this gap is closed.

## Evidence and validation

- PostgreSQL migrations are exercised on Python 3.9 and 3.12 CI lanes.
- Repository tests cover idempotency conflicts, concurrent duplicate submission, forbidden transitions, validation artifacts, retry attempts, stale-attempt recovery, resource metrics, and immutable events.
- The VOD E2E test exercises gRPC → Celery → FFmpeg → HLS/thumbnail → S3-compatible storage and clears Redis before checking the final result again. Watermark-specific E2E is not yet covered.
