# ADR 0010 — Atomic tenant admission and Job quota reservations

- **Status:** Accepted design; implementation pending
- **Date:** 2026-11-25
- **Owners:** Service maintainers
- **Related:** Issue [#73](https://github.com/mojtaba-arvin/video-service/issues/73), [#80](https://github.com/mojtaba-arvin/video-service/issues/80), ADR 0009

## Context

The gRPC boundary now establishes a verified `RequestPrincipal`, and durable Jobs persist its tenant, issuer and subject. However, there is no atomic per-tenant admission reservation before a newly accepted Job enters either the legacy Celery path or the feature-gated durable outbox path.

A read-count-then-dispatch check is unsafe: concurrent CreateJob requests can all observe capacity and exceed the limit. Redis counters are also unsuitable as the authority because Redis is a projection/cache and can be cleared independently of PostgreSQL.

## Decision

1. **Identity source:** quota ownership comes only from the authenticated `RequestPrincipal.tenant_id`. Never infer a tenant from `reference_id`, Job ID, idempotency key, callback URL, task arguments or caller-supplied metadata.
2. **Reservation unit:** one active reservation per accepted Job, independent of the number of outputs, stages or Celery messages. Idempotent replay returns the existing Job and must not consume another reservation.
3. **Atomic boundary:** create the reservation in the same PostgreSQL transaction as Job, Workflow, immutable plan, Stages, secret envelopes and optional dispatch-outbox intent. A quota denial rolls back the entire acceptance; neither legacy dispatch nor durable outbox publication may occur.
4. **Concurrency control:** provision one quota-policy row per tenant. Lock that row with `SELECT ... FOR UPDATE`, count that tenant's ACTIVE reservation rows, compare against its configured `max_active_jobs`, and insert the new reservation before commit. The locked policy row serializes concurrent admissions for the same tenant; different tenants remain independent. PostgreSQL concurrency tests are the production acceptance boundary.
5. **Quota lifecycle:** hold the reservation while the Job is QUEUED, RUNNING or RETRY_WAIT. Release it exactly once in the same transaction that makes the Job terminal (FINISHED, FAILED or CANCELLED). Keep a released reservation record for audit; never decrement a counter outside the owning transaction.
6. **Fail closed:** when enforcement is enabled, a missing tenant policy is an admission error, not unlimited capacity. Do not invent a universal numeric limit. Limits are provisioned per tenant by an authorized operational process.
7. **Legacy data:** do not guess ownership or create reservations for existing unowned Jobs. Reconcile and drain them under the legacy-Job inventory gate. New public gRPC CreateJob requests must use the verified principal already required by the interceptor.
8. **Source of truth:** PostgreSQL reservation rows and tenant policy are authoritative. Redis may expose cached queue/usage projections but cannot grant admission.
9. **Rollout:** add a dedicated default-off enforcement setting. Before enabling it in an environment, apply the migration, provision and audit every active tenant's policy, verify terminal release paths and run concurrent PostgreSQL admission tests. Keep the durable-dispatch flag independently disabled until its own staging gates pass.

## Data model and transaction contract

- `workflow_tenant_quota_policies`: `tenant_id` primary key, positive `max_active_jobs`, policy version and timestamps.
- `workflow_job_admission_reservations`: one row per `job_id`, tenant ID, state (`ACTIVE` / `RELEASED`), creation and release timestamps; unique Job ownership and an index on (tenant_id, state).
- No denormalized active counter is required initially. Counting ACTIVE reservations while holding the tenant policy row lock avoids counter drift and makes repair/audit straightforward. Add a counter only if measured admission throughput requires it and a reconciliation mechanism is provided.

The reservation is created only after the idempotency lookup has established that the request is genuinely new, and before the transaction commits. Every terminal Job transition uses one shared idempotent release helper. A missing reservation for a newly admitted owned Job is an invariant violation to surface and alert on; it must not be silently repaired by decrementing or inventing state.

## Acceptance tests

- At the exact configured limit, a new Job is rejected before either dispatch path; no Job, plan, secret, Stage or outbox row is committed.
- Concurrent requests for one tenant cannot exceed the limit; a PostgreSQL race test launches more contenders than remaining capacity and proves exactly the allowed number commit.
- Two tenants with independent policies do not block or consume one another's capacity.
- Same-owner idempotent replay does not consume capacity twice; cross-owner replay remains denied.
- Reservations remain active through retry and worker loss, then release once on FINISHED, FAILED or CANCELLED.
- Duplicate terminal transitions do not double-release; rollback of a terminal transition preserves the reservation.
- Missing policy fails closed only when quota enforcement is enabled; unowned legacy Jobs are not assigned an invented tenant.
- Logs and audit events contain tenant/Job references and decision codes only, never credentials, callback URLs or raw request payloads.

## Non-goals

This is a Job admission cap, not a complete resource scheduler. It does not claim per-minute request rate limits, byte/storage quotas, per-operation concurrency, CPU/GPU budgets, fairness, or Spot capacity selection. Those require measured workload profiles and separate policies under Issue #80.

## Consequences

The database transaction becomes the single admission/acceptance boundary for both dispatch modes. Tenant onboarding must provision a quota policy before quota enforcement is activated. Operational tooling must expose policy state, active reservations, oldest reservation age and auditable release/reconciliation; a manual repair must be reviewed and recorded.
