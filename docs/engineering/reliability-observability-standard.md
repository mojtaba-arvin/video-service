# Reliability and observability engineering standard

- **Status:** Accepted cross-cutting engineering standard
- **Date:** 2026-11-19
- **Applies to:** Every runtime component, workflow, adapter, API, agent tool, deployment and engineering change

## 1. Non-negotiable principle

**Reliability and observability are product capabilities and release requirements, not post-launch enhancements.** No feature, optimization, provider integration, agent capability or scale initiative may weaken data integrity, recoverability, security, diagnosability or operational control.

A change is not complete merely because its happy path works. It must define how it fails, how the failure is detected, what state remains durable, how recovery proceeds, and what evidence an operator or automated system can use to verify the outcome.

When a proposed optimization conflicts with correctness, safe recovery or the ability to understand production behavior, preserve reliability and observability and defer the optimization.

## 2. Reliability invariants

1. **Durable acceptance:** once a request is acknowledged as accepted, its identity and execution intent are durable. PostgreSQL is the control-plane source of truth; Redis and brokers are projections/transports only.
2. **Explicit lifecycle:** Job, Workflow, Stage, Attempt, Artifact and dispatch state transitions are explicit, validated and auditable. Terminal states are not silently reopened.
3. **At-least-once reality:** delivery may repeat. Use durable claims, idempotent effects, deterministic artifact identities and bounded retries. Never claim exactly-once broker delivery.
4. **Recoverability:** define behavior for API crash, relay crash, broker outage, worker loss, process crash, cache loss, storage timeout, duplicate delivery, cancellation races and partial publication.
5. **No false success:** outputs are not successful or visible until validation and storage readback pass. Partial/cancelled artifacts remain unpublished.
6. **Bounded execution:** every external operation and media process has explicit deadlines, concurrency/resource bounds, cancellation behavior and cleanup semantics. Unknown/unbounded resource use is a release risk.
7. **Safe degradation:** transient dependency failure must not corrupt authoritative state. Fail closed for authorization, secret resolution, unsupported schema/operation versions and unsafe destinations.
8. **Compatibility:** in-flight work references immutable, versioned plans/contracts. Deployments must not silently reinterpret accepted work.
9. **Security is part of reliability:** secrets, tenant boundaries, audit records and safe callback/network behavior are required for trustworthy recovery and diagnosis.
10. **Rollback:** every behavior-changing release defines a safe rollback or forward-recovery path, including database and in-flight workflow compatibility.

## 3. Observability contract

Every asynchronous operation must be diagnosable from acceptance through final artifact publication.

### Correlation and identity

Propagate stable, non-secret identifiers across API, database events, outbox, broker, worker, media process, storage and callback boundaries:
- request/correlation ID;
- Job, Workflow, Stage and Attempt IDs;
- immutable plan/schema version;
- adapter/provider and bounded workload-class identifiers;
- trace/span context where supported.

Never use broker task IDs or provider IDs as the platform's authoritative identity. They may be retained as external references.

### Structured logs

- Emit structured events with timestamp, severity, component, operation, outcome, duration and correlation identifiers.
- Record actionable error codes and bounded diagnostic context; preserve provider-specific error details only in bounded, sanitized fields.
- Redact credentials, tokens, signed URLs, raw webhook secrets, private media paths and sensitive customer data before logs leave the process.
- Do not log full media payloads, arbitrary user metadata or unbounded FFmpeg command arguments.
- Log state transitions and recovery decisions, not just stack traces.

### Metrics

At minimum, instrument:
- accepted/completed/failed/cancelled Jobs and Stages;
- queue wait, execution duration, end-to-end completion and time-to-first-playable-output;
- retries, duplicate deliveries, claim conflicts, stale attempts and recovery outcomes;
- outbox pending age/depth, lease expiry, publish failures, dead/poison rows and relay lag;
- worker availability, active concurrency, process exit classifications and resource-limit terminations;
- CPU seconds, peak RSS, memory-duration, input/output bytes and measurement coverage;
- validation failures, readback failures, partial artifacts and publication outcomes;
- dependency latency/error rates for database, broker, cache and object storage;
- cost estimate coverage and unknown/unpriced dimensions;
- QoE metrics only when the corresponding player/CDN data source exists.

Metrics must use bounded-cardinality labels. Do not label metrics with Job IDs, user IDs, URLs, filenames, arbitrary errors or other unbounded values. Put high-cardinality identifiers in traces/logs or queryable event records.

Unknown measurements remain unknown/null and are accompanied by coverage metadata; never coerce missing telemetry to zero.

### Traces and events

- Trace the request and asynchronous hand-offs using propagated context where possible.
- Persist domain lifecycle events in the authoritative workflow store; traces are diagnostic telemetry, not the source of truth.
- Represent queue wait, execution, validation, storage readback, publication and notification as distinguishable spans/events.
- Sampling must not remove durable audit/state events or all traces for failures and slow requests.

## 4. SLOs, alerts and operational readiness

Do not invent numeric SLOs before a representative baseline exists. For each critical user journey, define an SLI, measurement window, workload class, exclusions and owner before setting a target.

Initial SLI candidates:
- accepted Jobs reaching a terminal state within a measured time window;
- accepted Jobs dispatched or moved to an explicit operator-visible failure state;
- duplicate deliveries that produce duplicate logical side effects (target: zero);
- validated artifacts successfully published and readable;
- cancellation-to-process-termination latency;
- outbox oldest-pending age and poison-message visibility;
- availability and latency of status/cancel/output APIs;
- completeness of per-attempt resource/cost measurements.

Alerts must be actionable and tied to an owner/runbook. Alert on symptoms and recovery risk (for example, growing oldest outbox age, repeated lease expiry, stuck attempts, rising publication/readback failures, process-limit termination spikes, missing telemetry, or exhausted worker capacity), not every isolated transient error.

Every critical dependency and recovery mechanism needs a runbook covering detection, impact, safe inspection, mitigation, replay/retry criteria, escalation and rollback. Manual replay must be authorized, audited and idempotent.

## 5. Failure-oriented testing requirements

Every change affecting state, side effects, process execution, provider integration or delivery must add tests appropriate to its failure modes. The baseline matrix includes:

- process termination before/after database commit and before/after broker acknowledgement;
- duplicate and concurrent message delivery;
- lease expiry, stale-owner updates and relay restart;
- broker/database/cache/storage unavailability and recovery;
- worker loss, non-zero FFmpeg exit, timeout, cancellation and descendant cleanup;
- CPU/memory/PID/disk limit enforcement where supported;
- malformed/unsupported schema and operation versions;
- secret resolution failure, authorization denial and cross-tenant access;
- storage write/readback mismatch, partial package and manifest-last publication;
- missing, delayed, malformed and duplicate telemetry;
- log-redaction regression and metric-cardinality checks.

Prefer deterministic unit/contract tests, then containerized integration tests, then a small number of end-to-end tests at real boundaries. Keep external dependencies isolated and CI cost bounded without skipping required coverage. A skipped required check is not a pass.

## 6. Architecture and change review checklist

Every PR or material design change must answer:
1. What durable state changes, and what is the transaction boundary?
2. What happens on duplicate delivery, crash, timeout, cancellation and dependency outage?
3. Which side effects are idempotent, and where is the durable execution claim acquired?
4. What are the resource/deadline/concurrency limits?
5. Which logs, metrics, traces and durable events prove progress and outcome?
6. How are secrets and tenant data protected from telemetry?
7. What compatibility guarantees apply to in-flight plans and persisted records?
8. How is the change tested under failure, and what CI gates are required?
9. How does an operator detect, mitigate, replay or roll back the change?
10. Which claims are implemented and measured versus proposed?

If these questions cannot be answered, keep the work behind a disabled feature flag or in a non-production path; do not silently expand production behavior.

## 7. Delivery order

Reliability and observability work is integrated into every roadmap lane. The immediate sequence is:
1. Complete the transport-neutral VOD DAG and keep the live dispatch path unchanged until durable persistence and idempotent claims exist.
2. Persist immutable plan/version and dispatch intent transactionally; add complete Stage/Attempt coverage and secret-reference resolution.
3. Implement outbox relay leases, bounded retry/poison state, cancellation handling and normalized lifecycle telemetry.
4. Add broker-backed crash/replay/duplicate tests and operational runbooks before relay activation.
5. Extend the same telemetry and reliability contract to storage/execution/media/delivery adapters, then establish measured SLOs.
6. Only then accelerate provider diversity, cost-aware scheduling, streaming and edge execution.

## 8. Current implementation boundary (2026-11-19)

This standard is a design and review requirement; it does not claim that all listed telemetry, SLOs, runbooks, resource limits or recovery mechanisms already exist.

Verified foundations include PostgreSQL-backed workflow state, immutable workflow events, attempt-level resource/accounting fields, sensitive-log redaction, bounded webhook behavior, and a VOD plan builder merged separately. Known gaps include the Job-to-dispatch crash window, incomplete durable representation/claims for every legacy graph node, full broker-backed crash/replay coverage, service authentication/authorization and tenant isolation, full process resource isolation, and end-to-end player/CDN telemetry.

Track each gap against its owning issue and close it only with merged code, tests and operational evidence.
