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
4. **Concurrency control:** provision one quota-policy row per tenant. Lock that row with `SELECT ... FOR UPDATE`, count that tenant's nonterminal Job rows (`QUEUED` and `RUNNING`), compare against its configured `max_active_jobs`, and insert the new Job before commit. The policy-row lock serializes concurrent admissions for the same tenant; different tenants remain independent. PostgreSQL concurrency tests are the production acceptance boundary.
5. **Quota lifecycle:** the committed Job row itself is the reservation. It consumes one slot while its state is `QUEUED` or `RUNNING`; terminal transitions to FINISHED, FAILED or CANCELLED release capacity automatically because the authoritative Job state leaves the counted set. This avoids a second reservation state machine and counter drift.
6. **Fail closed:** when enforcement is enabled, a missing tenant policy is an admission error, not unlimited capacity. Do not invent a universal numeric limit. Limits are provisioned per tenant by an authorized operational process.
7. **Legacy data:** do not guess ownership or create reservations for existing unowned Jobs. Reconcile and drain them under the legacy-Job inventory gate. New public gRPC CreateJob requests must use the verified principal already required by the interceptor.
8. **Source of truth:** PostgreSQL reservation rows and tenant policy are authoritative. Redis may expose cached queue/usage projections but cannot grant admission.
9. **Rollout:** add a dedicated default-off enforcement setting. Before enabling it in an environment, apply the migration, provision and audit every active tenant's policy, verify terminal release paths and run concurrent PostgreSQL admission tests. Keep the durable-dispatch flag independently disabled until its own staging gates pass.

## Data model and transaction contract

- `workflow_tenant_quota_policies`: `tenant_id` primary key, nonnegative `max_active_jobs` (zero can suspend admission), policy version and timestamps.
- No separate reservation table or mutable active counter is required initially. The owned, nonterminal `workflow_jobs` rows are the reservations; count them while holding the tenant policy row lock. The existing Job state machine is the single release mechanism.

The quota check runs only after idempotency lookup has established that the request is genuinely new, and before the Job transaction commits. Because Job creation and terminal state transitions are already transactional, capacity is acquired and released with the authoritative Job state without a second lifecycle to reconcile.

## Acceptance tests

- At the exact configured limit, a new Job is rejected before either dispatch path; no Job, plan, secret, Stage or outbox row is committed.
- Concurrent requests for one tenant cannot exceed the limit; a PostgreSQL race test launches more contenders than remaining capacity and proves exactly the allowed number commit.
- Two tenants with independent policies do not block or consume one another's capacity.
- Same-owner idempotent replay does not consume capacity twice; cross-owner replay remains denied.
- QUEUED and RUNNING Jobs consume capacity across retries and worker loss; FINISHED, FAILED and CANCELLED Jobs no longer consume capacity.
- A rolled-back Job creation consumes no capacity; a rolled-back terminal transition leaves the Job counted.
- Missing policy fails closed only when quota enforcement is enabled; unowned legacy Jobs are not assigned an invented tenant.
- Logs and audit events contain tenant/Job references and decision codes only, never credentials, callback URLs or raw request payloads.

## Non-goals

This is a Job admission cap, not a complete resource scheduler. It does not claim per-minute request rate limits, byte/storage quotas, per-operation concurrency, CPU/GPU budgets, fairness, or Spot capacity selection. Those require measured workload profiles and separate policies under Issue #80.

## Consequences

The database transaction becomes the single admission/acceptance boundary for both dispatch modes. Tenant onboarding must provision a quota policy before quota enforcement is activated. Operational tooling must expose policy state and active owned-Job counts; a policy change must be reviewed and recorded.
