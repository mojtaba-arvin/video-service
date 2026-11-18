# ADR 0007: Persist immutable execution plans with workflow acceptance

- **Status:** Accepted
- **Date:** 2026-11-19
- **Scope:** Durable Job/Workflow/Stage creation and execution-plan versioning

## Context

The platform already has a versioned, transport-neutral execution-plan schema and a complete pure VOD DAG builder. The durable repository previously persisted Job, Workflow and Stage rows in one transaction, but did not persist the actual DAG, dependency edges, retry policies or resource profiles. A worker or future dispatcher therefore could not reconstruct the accepted work from durable state alone.

## Decision

When a caller supplies a validated execution plan, persist the following in the same PostgreSQL transaction:
- Job identity and idempotency fingerprint;
- Workflow identity and name;
- immutable execution-plan JSON;
- plan schema version and workflow definition version;
- canonical SHA-256 plan fingerprint;
- Stage projection rows derived from the plan nodes;
- the JOB_CREATED lifecycle event, containing the plan version and fingerprint but not the full plan.

The complete DAG remains the authoritative execution definition. Stage rows are thin query-friendly projections and must match the plan's ordered stage keys and operation identifiers. Plan parameters are not duplicated into Stage input/output JSON or public status snapshots; the full validated plan is retrieved only through the dedicated repository method and its fingerprint is verified.

The repository verifies the canonical fingerprint whenever a plan or Job snapshot is read. Plan JSON must pass the versioned validator, including cycle, operation, resource-profile, JSON-safety and sensitive-field checks. A plan's job identity must match the newly submitted Job. Replays with a generated replacement Job ID rebind only that identity to the already accepted Job before comparing fingerprints.

Idempotency rules:
- same idempotency key and same request fingerprint returns the original Job;
- a different plan for an already planned Job is rejected;
- a legacy accepted Job with no plan remains legacy on replay and is never silently upgraded;
- callers cannot supply a Stage projection that differs from the plan's node order, stage keys or operations.

## Compatibility and non-goals

Plan columns are nullable so existing accepted workflows remain readable. This decision does not add or activate an outbox, relay, broker consumer, secret store, or new dispatch behavior. It does not connect the plan builder to the gRPC CreateJob path or replace the current Celery Canvas execution graph.

Opaque secret references may be stored in a plan, but they are not credentials. A real secret-store implementation and authorization policy are still required before dispatch can resolve and use them.

## Consequences

- Accepted planned work can be reconstructed and fingerprint-verified after API or worker restarts.
- The Job/Workflow/Plan/Stage/Event acceptance boundary is atomic.
- In-flight workflows retain the exact plan and workflow version with which they were accepted.
- Legacy jobs remain compatible without speculative migration.
- The next safe increment is to resolve opaque secret references and add durable per-node execution claims. Only after those gates and broker-backed crash/replay tests pass may an outbox relay be enabled.

## Verification

PostgreSQL migration and repository integration tests cover plan round-trip, stage projection parity, idempotent replay, generated Job ID rebinding, legacy replay, conflicting plans and fingerprint tampering. Full dispatch/replay behavior remains unimplemented and is not claimed by this ADR.
