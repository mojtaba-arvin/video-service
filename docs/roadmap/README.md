# Outcome backlog and delivery strategy

This document is a **live backlog of product outcomes**, not a mandatory phase sequence. The outcome areas are related but independently valuable; work is selected by current risk, value, dependency, and available evidence.

The delivery sequence, release gates, parallel workstreams, product metrics, and
go/no-go decisions are maintained in the
[Product execution plan](../product/EXECUTION_PLAN.md). This roadmap remains
the issue-level outcome index and current implementation status.

## Strategic-goal alignment (2026-11-19)

The six ratings are engineering estimates on a 0–5 maturity scale based on
merged code and verified CI evidence. They are not delivery percentages or
production-readiness claims.

| Goal | Maturity | Verified implementation | Remaining distance |
|---|---:|---|---|
| 1. AI-native long-running orchestration | 2.5/5 | PostgreSQL Job/Workflow/Stage/Attempt/Artifact/Event state; typed internal operations; async VOD and durable publication | Transactional outbox, versioned workflow definitions, project/scene/character continuity, incremental input/output and resumable creative runs |
| 2. Cost intelligence | 2/5 | Merged: per-attempt CPU/time/memory/bytes, immutable price-card ID/version/rate snapshots, component coverage, partial spend for failed/retryable/cancelled attempts, explicit worker-loss gaps, RUNIT fallback and matched-VMAF H.264/HEVC/AV1 benchmark harness (PR #93); direct process-group CPU/RSS/duration samples for Watermark (PR #95); public gRPC report coverage fields (PR #96). No production codec default or cost-saving claim is derived from the harness alone | Run the harness on licensed representative media; meter GPU/egress/retention/idle resources; reconcile to provider invoices; then implement cost-aware admission/scheduling and Spot economics |
| 3. Edge–cloud collaboration | 0.5/5 | Research and target architecture only | Real workload baseline, edge preprocessing prototype, secure leases, cloud fallback and measured benefit |
| 4. Portable MCP server | 1/5 | Typed application-operation request/result/error contracts | MCP server, stable tool schemas, OAuth audience validation, resource authorization, budgets and client contract tests |
| 5. Observability and QoE | 2.5/5 | Merged manifest-bound playback sessions, idempotent player events, queryable QoE report and public gRPC cost-coverage fields (PR #96) | Player client/SDK, CDN adapter, delivery correlation, query/aggregation APIs, cohorts and percentiles |
| 6. FFmpeg scale and isolation | 2/5 | Package validation/publication safety; bounded process-tree cancellation/deadlines for Thumbnail (PR #87) and Watermark (PR #92), plus direct streaming Watermark process-group CPU/RSS/duration metering (PR #95) merged | Per-attempt cgroup/container CPU/RAM/PID/disk limits, supervision for every library-launched FFmpeg process, admission/concurrency control and broker-backed worker-loss tests |

The simple mean is approximately 1.7/5. It is a planning signal only; the six
outcomes have different dependencies and should not be treated as one linear
project percentage.

## Cross-cutting reliability and observability standard

Reliability and observability are mandatory release constraints across every outcome lane, not a separate phase. Each PR must define failure/recovery behavior, stable correlation, sanitized structured logs, bounded-cardinality metrics, trace propagation, failure-oriented tests and operator recovery. Numeric SLOs are set only after representative baselines exist. See the [engineering standard](../engineering/reliability-observability-standard.md) and [ADR 0006](../decisions/0006-reliability-and-observability-as-cross-cutting-constraints.md).

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
below reflects evidence on `develop` as of 2026-11-19; open PR work is
identified separately and is not counted as merged.

| Priority | Outcome lane | Verified state | Next acceptance evidence |
|---|---|---|---|
| P0 | Engineering foundation | **Partial** — pinned CI actions, Python 3.9/3.12 regression lanes, synthetic VOD fixtures, Dependency Review, Dependabot, agent guidance and stable aggregate `Quality gates` check (PR #88). Active `Protect develop` ruleset currently blocks deletion/non-fast-forward only; it does not yet require `Quality gates` | Repository admin must make the single stable `Quality gates` check a required status check; keep it green |
| P0 | Configuration and repository hygiene | **Done** — settings regressions and safe environment template/ignore checks (PR #55/#56) | Confirm historic development broker credentials are no longer accepted by any shared service |
| P0 | Thumbnail correctness | **Done** — actual FFmpeg render plus FFprobe checks before completion (PR #57/#59) | Add failure/cancellation coverage to the complete workflow |
| P0 | HLS/DASH correctness and publication | **Done** — package graph validation, expected rendition checks, bounded metadata probing, storage readback and manifest-last publication (PR #62/#64) | Add adversarial tests for cleanup/partial failure as the package contract evolves |
| P0 | Durable orchestration | **Plan contract, complete pure VOD DAG builder, and atomic immutable plan persistence merged (PR #100/#104/#106)** — Job/Workflow/plan/version/fingerprint/Stage projections/event are persisted together; PR #106 passed PostgreSQL repository/migration, Python 3.9/3.12 regression, full VOD integration and aggregate Quality gates; live gRPC/Celery dispatch remains unchanged | Implement tenant/Job-authorized encrypted secret-reference storage and durable per-node claims/leases; add broker-backed crash/replay tests before enabling a relay |
| P0 | End-to-end VOD reliability | **Partial** — gRPC → Celery → FFmpeg → HLS/thumbnail → S3-compatible test double; final state survives Redis cache clearing (PR #63/#68) | Add broker-backed worker process tests for worker loss, non-zero FFmpeg exit, retry, cancellation and watermark output |
| P0 | FFmpeg execution isolation | **Partial — PR #87/#92/#95 merged** — Thumbnail and Watermark use bounded deadlines and terminate process groups/descendants on cancellation; streaming Watermark records direct process-group CPU/RSS/duration; real FFmpeg/FFprobe tests run in CI | Extend metering/supervision to every third-party library-launched FFmpeg path; add cgroup/container CPU/memory/PID/disk bounds, admission control and adversarial cleanup tests |
| P0 | Security boundary | **Partial — PR #85/#97 merged** — sensitive task/request/provider/callback values are redacted; webhook connect/read timeouts are bounded, automatic redirects/retries are disabled, and response/pool cleanup is deterministic | Caller authentication/authorization, tenant boundaries, quotas, DNS/IP-aware SSRF protection, signed/replay-protected webhooks, delivery history and abuse tests |
| P0 | Runtime and image maintenance | **Open — target Python 3.12** — Python 3.9 reached EOL on 2025-10-31 and 3.10 reached EOL on 2026-10-01; CI Python 3.12 tests are not evidence that the application image/runtime has migrated | Upgrade gevent/greenlet, pyzmq/circus and remaining pins; build the full Python 3.12 + FFmpeg image; run migrations, broker-backed worker smoke tests and full VOD E2E inside that image; then retire the legacy lane |
| P1 | QoE and delivery intelligence | **Foundation merged — PR #69/#96** — manifest-bound sessions, idempotent events, active-buffering reports and public gRPC cost-coverage fields are in develop | Add client/SDK example, provider-neutral CDN adapter, delivery/manifest-version correlation and richer query/aggregation APIs |
| P1 | Cost intelligence | **Merged — PR #68/#93** — stage duration, CPU, peak memory, bytes, immutable price snapshots, component coverage, failure/cancel/retry/worker-loss accounting and explicit RUNIT semantics. No provider-bill claim | Run against licensed representative media; instrument GPU, network egress, storage retention and idle resources; reconcile estimates to provider invoices; then apply cost-aware admission and scheduling |
| P1 | Codec and packaging efficiency | **Benchmark harness merged — PR #93** — HLS/DASH and H.264/HEVC/VP9 options plus software H.264/HEVC/AV1 matched-VMAF runner with source provenance, raw results, repeat-run variance and environment capture. No production defaults changed; no licensed representative run yet | Run the harness on approved content classes, record repeat-run variance and decoder/player compatibility, then decide profile/default changes from measured trade-offs |
| P1 | Agent interface | **Foundation only** — typed internal operation identifiers/request/result/error contracts exist (PR #68); no MCP server | Stabilize application contracts and authorization first; implement MCP as a thin adapter and contract-test with real MCP clients |
| P1 | AI/creative workflow state | **Not implemented** — generic VOD state exists, but versioned creative flows and scene/character/project continuity do not | Define typed project/scene/asset state, provenance and workflow-version policy; test deterministic replay (Issue #79) |
| P1 | Cost-aware scheduler | **Not implemented** — no capacity scheduler or GPU/Spot policy | Start with workload classes, CPU admission and cost/latency measurement before GPU/Spot selection (Issue #80) |
| P1 | JIT packaging | **Research only** — fMP4/CMAF is supported; request-time packaging is not | Prototype packaging of pre-encoded assets; compare storage savings, cold latency, cache behavior and player/DRM constraints (Issue #81) |
| P1 | Playback/CDN correlation | **QoE base merged; CDN link open** | Add delivery telemetry adapter and query API with privacy constraints (Issue #82) |
| P2 | Edge–cloud | **Research only** — no edge agent or scheduler exists | Prove benefit on one real workload before secure leases/routing (Issue #83) |
| P1 | Incremental ingestion/output | **Not implemented** — baseline remains file-oriented VOD | Define chunk identity, checksums, provisional/final artifacts, backpressure and resume semantics (Issue #84) |

Priority is execution guidance, not a requirement to finish one lane before
starting another. Independent research, test design and implementation can
proceed concurrently when they have stable contracts and do not contend for
the same files or migration/protobuf ownership.

## Strategic goal to issue mapping

| Product objective | Implementation/research track |
|---|---|
| AI-native orchestration | #71 transactional outbox; #79 versioned creative state; #84 incremental workflows |
| Cost intelligence | #75 cost/codec benchmark; #80 dynamic capacity scheduling; #81 JIT packaging |
| Edge–cloud | #83 one-workload proof |
| MCP | #76 secure MCP server |
| QoE and observability | #69 merged session telemetry; #82 CDN/player correlation |
| FFmpeg scale | #72 process supervision and limits |
| Cross-cutting foundations | #73 security; #74 supported runtime |

## First end-to-end target

The next milestone is a **failure-tested, cost-observable VOD vertical**. Immutable execution-plan persistence is now merged (PR #106); the next missing safety contracts are secret resolution and durable per-node claims. The live dispatch path is still the legacy gRPC/Celery graph. The target vertical is:

1. A caller submits a typed request with a stable idempotency key.
2. PostgreSQL persists the Job, Workflow, immutable plan/version/fingerprint,
   Stage projections and creation event in one transaction (merged in PR #106).
3. A secret reference resolves only inside an authorized delivery adapter;
   plaintext credentials never enter plan snapshots or broker payloads.
4. PostgreSQL persists dispatch intent atomically; a lease-based relay
   publishes at least once only after durable per-node claims and idempotent
   side-effect contracts exist.
5. A bounded worker owns a supervised FFmpeg process tree and records an
   Attempt with measurements.
6. The output is probed, package-validated and read back from object storage
   before the root manifest is published and the Artifact is marked available.
7. Job status, progress, cancellation and failure reason are queryable from
   durable state after cache loss and worker restart.
8. Resource usage, cost-estimate coverage and output quality are available
   through a stable application contract.

The current in-process E2E test is a strong deterministic regression harness,
but it is not a substitute for broker-backed worker-loss tests or live
provider billing. The merged QoE test exercises server ingestion, not a real
player or CDN. MCP, GPU, Edge, DRM and JIT packaging are not prerequisites
for this first reliability milestone, but remain independent product outcomes.

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

The legacy service container targets Python 3.9. Python 3.9 reached end of
life on 2025-10-31 and Python 3.10 reached end of life on 2026-10-01; neither
receives upstream security updates. Python 3.12 remains in security support
through 2028-10 ([Python version status](https://devguide.python.org/versions/)).
CI retains Python 3.9 regression coverage for backward compatibility and
runs a separate Python 3.12 suite, but neither proves that the application
image/runtime has migrated.

Research on the pinned stack identified old gevent and PyZMQ versions as
migration blockers; see [Issue #74](https://github.com/mojtaba-arvin/video-service/issues/74)
for compatibility evidence and the selected target. Next acceptance
increment: uplift the complete dependency set, rebuild the container from
pinned inputs, run migrations and broker-backed worker smoke tests, and run
the complete VOD path inside the Python 3.12 image before retiring the legacy
lane.
