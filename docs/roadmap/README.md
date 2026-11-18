# Outcome backlog and delivery strategy

This document is a **live backlog of product outcomes**, not a mandatory phase sequence. The outcome areas are related but independently valuable; work is selected by current risk, value, dependency, and available evidence.

## Product outcome areas

| Outcome | What must become true | Evidence required |
|---|---|---|
| Durable orchestration | Jobs, workflows, stages, attempts, and artifacts survive process/broker/cache loss; retries, idempotency, and cancellation are deterministic | State-transition tests, crash/recovery tests, duplicate-submission and artifact-publication tests |
| Isolated, reproducible execution | FFmpeg/FFprobe run in bounded, supervised environments; outputs are verified before publication | Real media fixtures, exit-code/probe tests, process-tree and resource-limit tests, reproducible image builds |
| Cost intelligence | Each stage has attributable usage; scheduling and profile choices expose the cost/latency/quality trade-offs | Reproducible workload corpus, measured CPU/GPU/wall-time/bytes, transparent estimate versus reconciled billing |
| Codec and packaging | Profiles are versioned and selected against compatibility and quality constraints; packaging avoids unjustified duplication | Decoder/device compatibility, decode/manifest validation, quality and cost benchmarks |
| Agent interface | Human, application, and AI-agent callers use stable typed operations; MCP is an adapter to the same application services | Contract tests, schema compatibility, bounded operation tests, authorization and audit tests |
| Hybrid cloud/edge | Work is routed by explicit privacy, latency, bandwidth, cost, and capacity policy; incremental processing is safe | Edge lease/security tests and same-workload comparison against cloud-only processing |
| QoE and delivery intelligence | Processing outputs and delivery telemetry can be correlated to explain playback experience | Session-level event contract, privacy review, ingestion tests, CDN/player correlation |
| Engineering foundation | Every behavior change has tests; CI, security, observability, and reproducible development improve with each increment | Green required checks, isolated external adapters, ADRs for material choices, reproducible evidence |

## Selection policy

Choose the next increment using these questions, in order:

1. Does it remove a correctness, security, or data-loss risk in the current implementation?
2. Does it unlock a tested end-to-end capability or remove a known bottleneck?
3. Does it reduce uncertainty in a high-impact architectural choice?
4. Can it be shipped as a small, independently reviewable change with observable acceptance evidence?

Do not delay an independently useful capability just because another outcome lane has not been "completed". Do not build infrastructure for hypothetical scale. Prefer a modular monolith and replaceable adapters until workload evidence justifies additional services or providers.

## Live implementation backlog

The canonical engineering work queue is tracked in
[Issue #54](https://github.com/mojtaba-arvin/video-service/issues/54). Status
below reflects evidence on `develop` as of 2026-11-18; open PR work is
identified separately and is not counted as merged.

| Priority | Outcome lane | Verified state | Next acceptance evidence |
|---|---|---|---|
| P0 | Engineering foundation | **Done** — pinned CI actions, Python 3.9/3.12 regression lanes, synthetic VOD fixture generation, Dependency Review, Dependabot and agent guidance (PR #61) | Keep required checks green; establish repository ruleset with owner/admin access |
| P0 | Configuration and repository hygiene | **Done** — settings regressions and safe environment template/ignore checks (PR #55/#56) | Confirm historic development broker credentials are no longer accepted by any shared service |
| P0 | Thumbnail correctness | **Done** — actual FFmpeg render plus FFprobe checks before completion (PR #57/#59) | Add failure/cancellation coverage to the complete workflow |
| P0 | HLS/DASH correctness and publication | **Done** — package graph validation, expected rendition checks, bounded metadata probing, storage readback and manifest-last publication (PR #62/#64) | Add adversarial tests for cleanup/partial failure as the package contract evolves |
| P0 | Durable orchestration | **Core merged; dispatch gap open** — PostgreSQL Job/Workflow/Stage/Attempt/Artifact/Event state, explicit transitions, idempotency and stale-attempt recovery (PR #68) | Implement transactional outbox + dispatcher; prove no lost dispatch across DB commit/process crash and idempotent relay redelivery |
| P0 | End-to-end VOD reliability | **Partial** — gRPC → Celery → FFmpeg → HLS/thumbnail → S3-compatible test double; final state survives Redis cache clearing (PR #63/#68) | Add broker-backed worker process tests for worker loss, non-zero FFmpeg exit, retry, cancellation and watermark output |
| P0 | FFmpeg execution isolation | **Partial — open PR #78** — FFmpeg callback now terminates descendant processes with bounded graceful/forced escalation and a real parent/child test | Extend process-tree guarantees to every FFmpeg execution path; enforce deadlines and CPU/memory/PID/disk bounds; add cgroup/container limits and adversarial cleanup tests |
| P0 | Security boundary | **Partial — open PR #77** — raw task args, gRPC request payloads, provider exception details and callback URLs are removed from logs | Caller authentication/authorization, tenant boundaries, quota and webhook SSRF controls; then signatures, replay protection, delivery history and abuse tests |
| P0 | Runtime and image maintenance | **Open** — Python 3.12 regression suite passes, but the application/runtime image remains on Python 3.9 | Inventory dependencies; migrate the full runtime/container to a supported Python version; execute complete container VOD smoke test |
| P1 | QoE and delivery intelligence | **Open PR #69** — durable session/event/report design is on a separate branch, not yet in develop | Pass CI after event-payload idempotency, active-buffering and cost-coverage fixes; merge only after review; then add player SDK and CDN signals |
| P1 | Cost intelligence | **Partial foundation** — stage duration, CPU, peak memory, byte counts and model-labelled estimates persisted in PR #68; RUNIT is not money | Named/versioned price cards; include GPU, disk, storage retention and egress; report coverage and distinguish estimates from reconciled billing |
| P1 | Codec and packaging efficiency | **Partial foundation** — HLS/DASH, H.264/HEVC/VP9 options and fMP4 path exist; AV1 is not yet established by the current contract/runtime | Benchmark representative corpus at matched quality using VMAF and decode/compatibility checks; adopt codec/profile changes only with measured economics |
| P1 | Agent interface | **Foundation only** — typed internal operation identifiers/request/result/error contracts exist (PR #68); no MCP server | Stabilize application contracts and authorization first; implement MCP as a thin adapter and contract-test with real MCP clients |
| P2/P3 | Hybrid cloud/edge | **Deferred, research active** — no edge agent or scheduler is implemented | Benchmark locality/bandwidth/latency/privacy benefit on a target workload before building edge leases or routing |
| P2 | Playback and delivery correlation | **Research / integration gap** — QoE ingestion is not equivalent to CDN telemetry | Join session metrics to delivery configuration and CDN/player outcomes without collecting unnecessary identity data |

Priority is execution guidance, not a requirement to finish one lane before
starting another. Independent research, test design and implementation can
proceed concurrently when they have stable contracts and do not contend for
the same files or migration/protobuf ownership.

## First end-to-end target

The next milestone is a **failure-tested, cost-observable VOD vertical**:

1. A caller submits a typed request with a stable idempotency key.
2. PostgreSQL persists the Job, Workflow, Stage and an outbox dispatch record
   in one transaction.
3. A relay leases and publishes the dispatch at least once; duplicate delivery
   is safe because state transitions and side effects are idempotent.
4. A bounded worker owns a supervised FFmpeg process tree and records an
   Attempt with measurements.
5. The output is probed, package-validated and read back from object storage
   before the root manifest is published and the Artifact is marked available.
6. Job status, progress, cancellation and failure reason are queryable from
   durable state after cache loss and worker restart.
7. Resource usage, cost-estimate coverage and output quality are available
   through a stable application contract.

The current in-process E2E test is a strong deterministic regression harness,
but it is not a substitute for broker-backed worker-loss tests or live
provider billing. MCP, GPU, Edge, DRM and JIT packaging are not prerequisites
for this first reliability milestone.

## Delivery and governance rules

- Work on a focused branch; every PR represents one independently reviewable behavior or vertical increment.
- No behavioral change without regression or integration tests.
- PRs targeting `develop` require green CI; do not merge while checks are pending, failing, or absent.
- External systems used in tests must be mocked, containerized, or isolated behind replaceable adapters.
- Record architecture choices that constrain future work in a short ADR under `docs/decisions/`.
- Keep the implementation and tests in the same PR; don't defer test coverage to a later "testing phase".
- Preserve the working VOD path; avoid broad dependency upgrades combined with architecture rewrites.
- Any claimed performance, cost, quality, or latency improvement needs a reproducible benchmark with source media, tool/runtime versions, hardware, parameters, repetitions, and raw results.
- Track actual usage separately from provider-price estimates and reconciled billing.
- Update [Issue #54](https://github.com/mojtaba-arvin/video-service/issues/54) when the chosen increment, acceptance criteria, or priority changes.
- Keep docs aligned with the repository: proposed, experimental, and merged capabilities must be clearly distinguished.

## Explicitly deferred unless evidence changes

Advanced spot/preemptible scheduling, live migration, distributed inference, mandatory Kubernetes, live video, DRM/multi-DRM, just-in-time packaging, and player SDK integration are not prerequisites for the initial reliable VOD platform. Revisit them only when their dependencies, a target workload, and a measurable success criterion are established.


## Additional P0 — runtime security baseline

The legacy service currently targets Python 3.9. Python 3.9 reached end of
life on 2025-10-31 and no longer receives official security updates
([Python version status](https://devguide.python.org/versions/)). CI now keeps
a Python 3.9 regression check for backward compatibility and adds a separate
Python 3.12 regression-suite run. The latter is not evidence that the full
service, its old runtime dependencies, or its container is compatible.

Next acceptance increment: inventory the runtime image and all pinned
dependencies, select a supported Python target, prove compatibility with
gRPC/Celery/Redis/S3/FFmpeg integrations, rebuild the container, and run a
complete VOD smoke test before changing the production baseline.
