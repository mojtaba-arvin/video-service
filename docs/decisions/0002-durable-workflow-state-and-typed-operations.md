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

Every completed attempt records elapsed time, CPU seconds, peak memory bytes, input/output bytes, artifact count, and an estimated resource cost.

- If an operator configures a named price card, the estimate uses its explicitly configured CPU, memory, and storage rates/currency.
- Otherwise, the default `relative-resource-index-v1` reports `RUNIT`: 1.0 per CPU-second, 0.25 per GB-second of peak-memory allocation, and 0.01 per GB of output. These are normalized weights, not money, a provider bill, or a claim of cost accuracy.
- The price model ID and unit are persisted with every estimate so values from different models are not silently compared.

### Typed internal operations

Core operation identifiers and dataclass request/result/error contracts live in the workflow package. gRPC and Celery are adapters around these operations. Future REST or MCP surfaces should call the same application contracts; they must not reimplement media business logic or expose FFmpeg command construction as the public domain API.

## Consequences

- Job identity and completed playlist state survive Redis cache loss.
- Retry attempts and worker-loss recovery are recorded independently of Celery result retention.
- Storage publication and validation evidence can be queried alongside resource measurements.
- Cost data is available from the first durable stage without inventing a dollar estimate.
- The initial implementation intentionally excludes tenant isolation, auth/RBAC, a public API redesign, and an MCP server.
- Thumbnail stages also use durable attempts and file publication verification. Watermark transformation remains on the legacy task lifecycle until it is explicitly instrumented.
- The database-first create-then-dispatch sequence still has a narrow process-crash window between committing the job and publishing the Celery graph. A transactional dispatch/outbox mechanism is deferred until a durable dispatcher is introduced; Redis is not used to pretend that this gap is closed.

## Evidence and validation

- PostgreSQL migrations are exercised on Python 3.9 and 3.12 CI lanes.
- Repository tests cover idempotency conflicts, concurrent duplicate submission, forbidden transitions, validation artifacts, retry attempts, stale-attempt recovery, resource metrics, and immutable events.
- The VOD E2E test exercises gRPC → Celery → FFmpeg → HLS/thumbnail → S3-compatible storage and clears Redis before checking the final result again.
