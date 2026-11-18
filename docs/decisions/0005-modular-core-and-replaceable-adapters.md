# ADR 0005: Modular core and replaceable infrastructure adapters

- **Status:** Accepted architectural direction; implementation is incremental
- **Date:** 2026-11-19
- **Scope:** Internal module boundaries, provider replacement, independent team development, and safe evolution of the video platform

## Decision summary

The service will evolve as a **modular application core with explicit ports and replaceable adapters**. It will not be split into microservices pre-emptively.

A developer or team must be able to replace one infrastructure implementation, add a new provider, or evolve one product capability without rewriting unrelated modules. This is a target enforced through dependency direction, versioned contracts, compatibility tests, and staged rollout—not a claim that every current integration is already replaceable.

## Current architecture assessment

The repository already has useful foundations:

- PostgreSQL is the durable source of truth for Job, Workflow, Stage, Attempt, Artifact and workflow events.
- The versioned execution-plan contract is transport-neutral and deliberately contains no Celery imports.
- Workflow operation identifiers and request/result/error contracts are separated from gRPC transport.
- Celery task execution, FFmpeg processing, storage publication and gRPC request handling are identifiable implementation areas.
- Cost and QoE data are associated with durable attempts and published artifacts.

Important coupling remains and must be removed incrementally:

- The gRPC CreateJob handler currently constructs Celery Canvas signatures and directly dispatches them.
- S3-specific service calls and object naming are used by media and publication code.
- The durable Stage model currently covers output stages, while preflight, input inspection, fan-in and callback nodes are still assembled in the legacy in-memory graph.
- Some task contracts carry Celery result propagation semantics.
- Redis is still used for compatibility projections and transient request details; webhook URL handling needs a durable secret-reference design before dispatch plans can be replayed safely.

Therefore the architecture is **partially modular, not yet provider-independent end to end**.

## Target dependency direction

The dependency direction is inward:

- Transport adapters -> Application use cases -> Domain contracts
- Infrastructure adapters -> Domain/application ports

The domain and application layers must not import provider SDKs, Celery, FFmpeg command builders, cloud-specific clients, gRPC generated classes, or MCP SDK types.

The composition root is the only place that selects concrete implementations.

## Module ownership and boundaries

| Module / capability | Owns | Must not own |
|---|---|---|
| workflows | Job/Workflow/Stage/Attempt state, plan versions, transitions, claims, orchestration policy | Celery Canvas, provider SDKs, FFmpeg commands |
| media / ffmpeg | Media operation implementation, process supervision, validation | Job lifecycle policy, API authentication |
| storage | Object read/write/list/delete, multipart, checksums, readback | Workflow transitions or task scheduling |
| execution | Dispatch contract, worker lifecycle, concurrency and resource admission | Media business rules or public transport models |
| cost | Usage measurements, pricing snapshots, attribution and coverage | Provider scheduling decisions hidden in media tasks |
| qoe | Playback/session/CDN event contracts and aggregation | Player-specific transport implementation |
| api / grpc / future mcp | Authentication, request validation, DTO mapping and application-service invocation | Reimplementing workflow or media business logic |
| secrets | Secret references, scoped resolution, rotation and audit | Raw secret values in plans, logs, events or broker messages |

These are logical ownership boundaries first. They may remain Python packages in one deployable application until independent deployment is justified by measured scale, security isolation, or release cadence.

## Port and adapter contracts

Introduce ports only at real change boundaries. Initial contracts should be narrow and capability-oriented:

- ObjectStorage: head, get/read stream, put/write stream, list, delete, checksum/readback, multipart lifecycle.
- WorkflowRepository: transactional create, immutable plan persistence, state transition, attempt claim, event append and reconciliation.
- ExecutionDispatcher: publish a reference to a persisted plan version; it does not accept arbitrary Python objects.
- MediaOperationExecutor: execute one allow-listed operation with typed input/output, cancellation, deadline and resource envelope.
- SecretResolver: resolve an opaque, tenant-scoped reference for one authorized operation.
- UsageRecorder and PricingProvider: record measured quantities separately from pricing policy.
- PlaybackEventSink: accept normalized session events independently of a specific player or CDN.

Avoid a generic plugin framework, dynamic Python imports from user data, or arbitrary executable task names. Provider registration must be explicit and allow-listed.

## Compatibility and change policy

1. **Stable identity:** Job, Workflow, Stage, Attempt and Artifact identities are application-owned and never derived from Celery task IDs, provider object IDs, or queue message IDs.
2. **Versioned contracts:** Execution-plan schema, operation input/output schemas, events, API DTOs and persisted metadata carry explicit versions where compatibility is required.
3. **Additive evolution:** New optional fields and operations are additive. Removing or changing field meaning requires a new schema/operation version and a migration/compatibility window.
4. **Unknown operation behavior:** A worker that does not support an operation or schema version must reject it visibly before side effects; it must never guess or silently reinterpret it.
5. **In-flight immutability:** A Job references an immutable plan version. Deploying new workflow code does not mutate already accepted Jobs. Old plan readers/executors remain available until in-flight work drains or an explicit migration is performed.
6. **Provider capability negotiation:** Each adapter declares capabilities and limits (formats, codecs, streaming, multipart, cancellation, resource classes). The scheduler selects only compatible adapters.
7. **Idempotency boundary:** All external side effects pass through durable Stage/Attempt claims and deterministic artifact identities. Provider retries are not assumed to be exactly once.
8. **Data ownership:** PostgreSQL remains the control-plane source of truth. Redis and broker messages are projections/transports, not authoritative workflow state.
9. **Security boundary:** Credentials never enter execution plans, outbox payloads, logs or general events. Plans carry opaque, scoped references.
10. **Observability contract:** Adapters emit normalized lifecycle, usage and failure events while retaining provider-specific diagnostic fields in bounded metadata.

## Safe provider replacement procedure

A provider is replaceable only after the following sequence:

1. Define or confirm the provider-neutral port and capability model.
2. Implement the new adapter behind the same port; do not alter domain contracts for vendor details.
3. Run the shared adapter contract suite against both old and new implementations.
4. Run parity tests on a representative, licensed workload and compare output validation, latency, resource usage, cancellation and failure semantics.
5. Enable the new adapter for a small explicit cohort or workload class.
6. Observe normalized metrics and provider-specific failure rates; preserve a fast rollback to the previous adapter.
7. Change the default only after the acceptance window passes. Keep the old adapter until no in-flight work depends on it.
8. Remove the old adapter only after persisted data, plans, artifacts and operational procedures no longer reference it.

## Independent developer and team workflow

- Each module has one documented owner boundary, public internal interfaces, contract tests and migration responsibility.
- Cross-module changes begin with a contract change and compatibility tests, then implementation changes in dependent modules.
- Shared schema/protobuf/plan changes are serialized through one integration owner; independent adapter and test work may proceed in parallel.
- A feature branch must not change another module's private implementation to bypass its public contract.
- CI runs focused module tests plus impacted contract suites; the aggregate quality gate remains mandatory before merge.
- Branches are merged through PRs. No direct pushes to protected integration branches and no force-push to shared branches.

## Rollout sequence

### A. Make the workflow contract executable

- Complete the pure VOD plan builder for the entire legacy graph.
- Persist immutable plan versions with Job/Workflow/Stage and dispatch intent.
- Establish durable consumer claims for every side-effecting node.
- Introduce the outbox relay and Celery adapter only after duplicate/crash tests pass.

### B. Extract high-value infrastructure ports

- Storage port first, because storage is used by input, output, validation and publication.
- Execution dispatcher and operation executor next, preserving Celery as the first adapter.
- Secret resolution and outbound callback policy before replayable callback nodes.
- Usage, pricing and playback event sinks remain independent contracts.

### C. Enable provider diversity only when justified

- Add a second storage, execution, or compute adapter with contract parity tests.
- Add GPU/Spot/edge adapters only after admission, cancellation, metering and cost attribution work.
- Split a module into a separate deployable service only when measured scaling, isolation or release independence justifies the operational cost.

## Non-goals and guardrails

- No microservice split solely to claim scalability.
- No plugin marketplace or arbitrary runtime code loading.
- No assumption that all providers support identical codecs, cancellation, streaming or consistency.
- No in-flight plan mutation during deployment.
- No provider switch without contract parity, representative workload evidence and rollback.
- No claim that current S3/Celery integrations are already fully replaceable.

## Consequences

- Teams can work independently against explicit module contracts.
- Provider replacement becomes a bounded adapter change rather than a domain rewrite.
- Compatibility and migration costs become visible before deployment.
- The modular monolith remains simpler to operate while retaining a path to selective service extraction.
- Some short-term work is required to remove direct Celery and S3 dependencies from application/domain code.
- The transactional outbox remains gated on a complete executable VOD plan and idempotent side effects; this ADR does not authorize premature dispatch replay.

## Related decisions

- ADR 0002: Durable VOD workflow state and typed operations.
- ADR 0004: Transactional dispatch outbox and idempotent execution plans.
- Issue #71: Durable Job-to-Celery dispatch gap.
