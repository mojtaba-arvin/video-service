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

The canonical task status is tracked in [Issue #54](https://github.com/mojtaba-arvin/video-service/issues/54). This table is a current snapshot; update it when evidence or priority changes.

| Priority | Outcome lane | Current increment / next proof | Status |
|---|---|---|---|
| P0 | Engineering foundation | Python 3.9 unit-test runner, generated VOD fixture validation, pinned Actions, CodeQL, dependency review, and Dependabot | **Done** — PR #55 established the runner; 17 tests now run in the expanded CI workflow |
| P0 | Configuration and portability | Remove tracked local environment values, add safe template and enforce ignore rules in CI | **Done** — PR #56 |
| P0 | Media correctness | Correct zero-dimension scaling and prove thumbnail rendering using FFmpeg + FFprobe | **Done** — PR #57 merged; 17 tests pass in CI, including eight FFmpeg/FFprobe render and validation integrations |
| P1 | Execution correctness | Validate HLS/DASH playlist manifests and expected renditions before publication; test non-zero FFmpeg exit, invalid/partial output, and cancellation | **Next** — thumbnail output validation is done in PR #59; playlist validation remains open |
| P1 | Process isolation | Supervise FFmpeg process groups/descendants; define timeout, termination, resource bounds, and cleanup semantics | Planned |
| P1 | API and outbound security | Redacted structured logs; caller authentication/authorization; SSRF-safe webhook delivery with DNS/redirect controls | Planned |
| P1 | Durable orchestration | Specify Job/Workflow/Stage/Attempt/Artifact states and idempotency; choose durable persistence through an ADR; test restart and cache loss | Planned |
| P1 | End-to-end VOD reliability | Exercise submit → execute → validate → publish → observe/cancel using isolated local services and storage test doubles | Planned |
| P2 | Cost intelligence | Capture stage-level wall/CPU/memory/disk/bytes and distinguish measured usage, estimated provider cost, and actual billing | Planned |
| P2 | Codec and packaging | Benchmark H.264/HEVC/AV1 and CMAF/fMP4 profiles on a fixed corpus; adopt only compatible, measured improvements | Planned |
| P2 | Agent interface | Define typed application operations first; expose the same services through gRPC/API and MCP without duplicating business logic | Planned |
| P3 | Hybrid cloud/edge | Add constrained edge leases and local incremental inspection only for workloads with measured locality benefit | Deferred pending workload evidence |
| P3 | QoE | Add privacy-reviewed session telemetry and correlate published asset versions with player/CDN outcomes | Deferred pending delivery/player integration |

Priority labels indicate current execution order, not a promise of a phase gate. Reorder them when a concrete failure, workload, or measurement changes their risk/value.

## First end-to-end target

The next meaningful platform milestone is not "finish the baseline phase". It is a repeatable VOD request path with these properties:

1. A caller submits a typed, validated operation and receives a stable job identifier.
2. The job and each stage/attempt have authoritative state that survives worker restart.
3. The existing execution backend can run the media operation without owning the domain state.
4. FFmpeg completion is checked and the output is probed/validated before it is published.
5. Publication is idempotent and the artifact reference is durable.
6. Status, progress, cancellation, and failure reason are queryable and tested.
7. External dependencies are local, mocked, or contract-tested so they do not block CI.

The initial proof can use the existing Celery path and a local filesystem/S3 test adapter. It should not require MCP, edge workers, GPUs, a new broker, or a multi-service rewrite.

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
