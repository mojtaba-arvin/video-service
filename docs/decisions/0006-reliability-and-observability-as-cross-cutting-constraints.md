# ADR 0006: Reliability and observability as cross-cutting architecture constraints

- **Status:** Accepted
- **Date:** 2026-11-19
- **Scope:** All modules, adapters, workflows, APIs, agent tools, runtime changes and releases

## Context

The platform is evolving toward long-running, provider-independent and agent-driven media workflows. That increases the number of asynchronous hand-offs, external side effects, failure modes and independently changing components. Modularity alone does not make a system operable: without durable lifecycle state, bounded execution, correlation, useful telemetry and tested recovery, independent components become independently opaque failure domains.

Reliability and observability must therefore be designed into contracts and release gates rather than added after features ship.

## Decision

Adopt the [Reliability and observability engineering standard](../engineering/reliability-observability-standard.md) as a mandatory cross-cutting constraint.

The platform must preserve these invariants:
- durable acceptance and PostgreSQL as the control-plane source of truth;
- explicit versioned lifecycle state for Job/Workflow/Stage/Attempt/Artifact/dispatch;
- at-least-once delivery with idempotent side effects, never an exactly-once assumption;
- deterministic recovery for crashes, retries, cancellation, duplicate delivery and dependency outages;
- validated/read-back artifacts before publication and no false success;
- bounded deadlines, concurrency and resource use for external operations and media processes;
- safe degradation, fail-closed security boundaries and in-flight contract compatibility;
- stable correlation across request, workflow, attempt, worker, storage and delivery;
- structured, sanitized logs; bounded-cardinality metrics; traces and durable domain events;
- actionable SLOs, alerts and runbooks based on measured baselines;
- failure-oriented tests and a documented rollback/forward-recovery path for every material change.

Reliability and observability take precedence over optimization or feature breadth when they conflict. No numeric SLO, cost saving, throughput or quality claim may be introduced without representative measurement and a documented measurement method.

## Consequences

- Every material PR and architecture decision must state failure behavior, durable-state implications, telemetry, security/redaction, tests and operational recovery.
- Every asynchronous hand-off must preserve stable application-owned identifiers and correlation context; broker/provider identifiers remain secondary references.
- High-cardinality identifiers belong in traces, logs or durable event records, not metric labels.
- Missing measurements remain explicitly incomplete, never zero.
- CI must test the failure modes relevant to a change; skipped required checks are not successful evidence.
- Operational runbooks and alert ownership become part of feature completion.
- This ADR does not claim the standard is fully implemented. The existing dispatch crash window, incomplete durable node claims, broker-backed crash/replay tests, auth/tenant boundaries, process isolation and player/CDN telemetry remain open work.

## Rollout

Apply the standard immediately to new work and incrementally to existing modules. First priority is the durable VOD plan/dispatch path, then process isolation and security, followed by storage/execution adapter telemetry and QoE/CDN correlation. Do not activate transactional outbox replay until the complete graph and idempotent side-effect gates pass.

## Related decisions

- ADR 0002: Durable VOD workflow state and typed operations.
- ADR 0004: Transactional dispatch outbox and idempotent execution plans.
- ADR 0005: Modular core and replaceable infrastructure adapters.
- Issue #71: Durable Job-to-Celery dispatch gap.
