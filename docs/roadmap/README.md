# Implementation roadmap

This roadmap translates the product vision into dependency-ordered delivery. It is intentionally outcome-driven: a phase is complete only when its acceptance gate is met and evidence is recorded.

## Delivery rules

- Work from a dedicated feature branch and keep each phase reviewable.
- Add tests with the behavior change; do not defer all testing to the end.
- Preserve a working VOD path throughout the migration.
- Prefer a modular monolith and replaceable adapters over premature microservices.
- Do not introduce Kubernetes, a new broker, a new database, GPU scheduling, or an AI framework without a measured need.
- Update the current-state document when implementation materially changes.
- Record benchmarks with source media, hardware, versions, command/profile, repetitions, and raw results.
- Never mark a target capability as implemented based only on documentation.

## Phase 0 — establish a safe baseline

**Goal:** make the current service reproducible and remove immediate correctness/security risks.

Deliver:
- Reproducible local Docker build and smoke test.
- Automated test harness for configuration, API validation, workflow state, FFmpeg execution, and S3 adapter behavior.
- Fix and test the retry-limit, output-bucket, and playlist-setting configuration defects.
- Stop tracking local environment files; add a safe example template and ignore rules; review history for exposed secrets.
- Structured redacted logging; remove request/argument dumps.
- Webhook URL validation and outbound network policy.
- Authentication/authorization design for the gRPC boundary.
- Dependency inventory and supported runtime upgrade plan.

**Exit gate:** clean build; configuration regression tests pass; secret hygiene is verified; critical request and webhook security tests pass; one VOD job can be submitted, observed, cancelled, and completed in a repeatable smoke test.

## Phase 1 — define contracts and durable job state

**Goal:** decouple product semantics from Celery and Redis implementation details.

Deliver:
- Domain entities: Job, Workflow, Stage, Attempt, Artifact, UsageRecord, Event.
- Explicit state machine and allowed transitions.
- Idempotency key and duplicate-submission behavior.
- Durable job/artifact metadata store selected through an ADR.
- Transactional outbox or equivalent reliable event publication strategy.
- Versioned API contract and compatibility policy for gRPC.
- Stable error taxonomy and retryability classification.
- Job events and progress stream/polling contract.
- Migration/dual-read strategy from current Redis status keys.

**Exit gate:** worker restart and Redis cache loss do not erase authoritative job state; duplicate requests do not duplicate published artifacts; retries and cancellation are deterministic under integration tests.

## Phase 2 — isolate and supervise media execution

**Goal:** make FFmpeg execution bounded, safe, and observable.

Deliver:
- MediaExecutor interface with FFmpeg/FFprobe implementation.
- Per-job scratch directories with ownership, quotas, and cleanup.
- Process-group/tree supervision, timeout, graceful termination, and forced kill.
- CPU, memory, disk, file descriptor, and wall-clock limits.
- Admission control and bounded concurrency by workload class.
- Output validation using FFprobe and decode/manifest checks before publication.
- Atomic artifact publication and orphan cleanup.
- Failure injection tests for worker crash, disk full, process hang, partial upload, and cancellation races.

**Exit gate:** resource limits are enforced; no partial artifact is marked ready; worker loss is recoverable; cancellation terminates the full process tree; scratch artifacts are cleaned or reconciled.

## Phase 3 — cost and performance intelligence

**Goal:** measure first, then optimize scheduling and encoding.

Deliver:
- Per-stage wall time, CPU time, peak memory, disk, bytes in/out, retries, and worker class.
- Optional GPU utilization/time metrics where supported.
- Cost model separating measured usage, estimated provider cost, and reconciled billing.
- Benchmark corpus and reproducible profile matrix for codec, resolution, preset, and quality.
- Queue scheduling by resource profile and deadlines.
- Tenant quotas and budget enforcement.
- Cost/quality/latency comparison reports.

**Exit gate:** every completed job has attributable usage; estimates are labeled and explainable; baseline and optimized profiles are compared on the same corpus; no savings claim is made without reproducible results.

## Phase 4 — codec ladder and packaging strategy

**Goal:** reduce storage and delivery cost without compromising playback compatibility.

Deliver:
- Versioned rendition/codec profiles.
- H.264 baseline with compatibility matrix.
- HEVC and AV1 experimental profiles gated by encoder availability and target device support.
- Quality evaluation (for example VMAF where appropriate) plus decode, audio, and manifest validation.
- Evaluate CMAF/fMP4 packaging and shared encoded segments.
- Evaluate just-in-time packaging through a measured ADR, including cache hit rate, packaging latency, storage reduction, DRM, and operational costs.

**Exit gate:** each promoted profile has quality, compatibility, speed, and cost evidence; fallback behavior is defined; JIT is adopted only if total cost and latency improve for a named workload.

## Phase 5 — stable agent interface and MCP

**Goal:** expose video operations to applications and AI agents without exposing FFmpeg internals.

Deliver:
- Versioned application-level operations: probe, trim, merge, transcode, thumbnail, text/watermark overlay, and packaging as supported.
- Typed schemas, constraints, limits, and explicit artifact references.
- Asynchronous submit/status/cancel/event operations.
- MCP server mapping to the same application service layer.
- Tool annotations/descriptions for destructive effects, cost implications, duration, and supported formats.
- Authentication and tenant scoping; rate limits and request budgets.
- Contract tests and examples for a reference MCP client.

**Exit gate:** MCP and regular API invoke the same business logic; invalid inputs fail before expensive execution; tool calls are auditable and bounded; compatibility tests protect published schemas.

## Phase 6 — edge and incremental workflows

**Goal:** reduce ingest latency and unnecessary transfer for workloads that benefit from locality.

Deliver:
- Edge agent registration, identity, capability advertisement, and secure job leasing.
- Small set of safe edge tasks (probe, metadata extraction, keyframe/scene sampling) first.
- Content hashes and artifact references to avoid duplicate transfer.
- Explicit routing policy based on privacy, latency, bandwidth, cost, and local capacity.
- Offline/reconnect behavior and lease expiry.
- Compare edge/cloud execution against cloud-only baseline.

**Exit gate:** edge agents cannot access unrelated jobs or storage; interrupted leases recover safely; edge processing has measured net benefit for a named workload.

## Phase 7 — playback QoE and delivery feedback

**Goal:** connect processing and delivery decisions to the actual playback experience.

Deliver:
- Player/SDK telemetry contract and privacy policy.
- Session IDs and published asset/version correlation.
- Startup, rebuffering, playback errors, rendition switches, and session completion events.
- Ingestion API, retention controls, queryable events, and export.
- Join CDN/delivery metrics with processing profile and artifact version.
- Quality/cost experiments with guardrails.

**Exit gate:** end-to-end traces can explain a playback failure or degradation; telemetry is privacy-reviewed; profile changes are evaluated against QoE and cost together.

## Cross-cutting release gates

Every phase must satisfy:

- security review of new trust boundaries;
- migration and rollback plan;
- observability for new states and failure modes;
- automated unit, integration, and contract tests;
- documentation and runnable examples;
- benchmark evidence where performance or cost is claimed;
- explicit statement of what is implemented, experimental, or deferred.

## Priority order

The order is deliberate:

1. Baseline and security.
2. Durable state and stable contracts.
3. Execution isolation and output correctness.
4. Measurement and cost attribution.
5. Codec and packaging optimization.
6. MCP and agent experience.
7. Edge execution.
8. Player QoE feedback.

MCP can be prototyped earlier as a thin adapter, but it should not become a second source of business logic or expose unsafe legacy task signatures. Advanced spot scheduling, live migration, distributed inference, and live video are deferred until their prerequisites and workload demand are demonstrated.
