# Product vision: AI-native, cost-aware video orchestration

## 1. Vision

Evolve this repository into a **portable orchestration layer for video processing and delivery** that humans, applications, and AI agents can invoke through stable contracts.

The platform should coordinate long-running media workflows with:

- predictable and explainable execution cost;
- bounded and observable latency;
- repeatable output quality;
- durable job state, safe retries, and cancellation;
- replaceable compute, storage, and delivery infrastructure;
- a stable machine-readable interface, including MCP.

The product is not an AI video-generation model, a CDN, or a replacement for FFmpeg. It is the control and execution layer that makes media workflows dependable and economically manageable across those systems.

## 2. Problem statement

### 2.1 Unpredictable processing and delivery economics

Video workloads vary greatly by duration, resolution, frame rate, codec, filter graph, number of renditions, and output destination. A fixed worker pool or a single encoding profile can waste CPU/GPU capacity, increase queue time, and multiply storage and CDN transfer costs.

**Product response:** estimate work where possible, enforce budgets and resource limits, schedule by workload class, measure actual consumption, and expose cost attribution per job and output. Cost optimization must be backed by comparable measurements rather than assumed savings.

### 2.2 Architecture-driven latency

Waiting for a complete upload, download, or preprocessing stage before useful work begins can increase time-to-first-result. Queue wait, worker cold starts, storage locality, and serial dependencies may dominate model or codec execution time.

**Product response:** support resumable/chunk-aware ingestion and incremental workflow stages where the media format and operation allow it. Separate queue delay, startup, processing, upload, and delivery latency. Edge execution is an optional later capability, not a prerequisite for a reliable first release.

### 2.3 Incomplete end-to-end observability

A server-side task status cannot explain the complete playback experience. Processing success does not prove that a player started quickly, avoided rebuffering, or decoded the result correctly.

**Product response:** correlate job and output lifecycle telemetry with delivery/CDN signals and, when an SDK/player integration exists, session-level QoE. Expose queryable events and metrics, not only dashboards.

### 2.4 Fragmented automation interfaces

Applications and agents should not need to construct FFmpeg command lines or learn each provider's API. Provider-specific integrations and agent tools otherwise create incompatible workflows.

**Product response:** define a versioned, typed video-operation contract and expose it through APIs and an MCP adapter. MCP is an interface to the platform, not the domain model or execution engine.

### 2.5 Operational complexity of FFmpeg at scale

FFmpeg processes can consume substantial CPU, memory, disk, file descriptors, and network bandwidth. Unbounded concurrency, weak process supervision, unsafe retries, and shared temporary directories can make failures affect unrelated jobs.

**Product response:** execute workloads in bounded, isolated workers; enforce per-job and per-tenant quotas; supervise process trees; make retries idempotent; and clean up temporary data reliably.

## 3. Product principles

1. **Correctness before throughput.** A completed job means validated artifacts exist and are durably recorded—not merely that a process exited.
2. **Durable state is authoritative.** Broker messages are delivery mechanisms; they are not the source of truth for job state.
3. **At-least-once execution, idempotent effects.** Assume workers can crash and messages can be redelivered. Make state transitions and artifact publication safe to repeat.
4. **Explicit budgets.** Enforce limits for duration, input size, output count, resolution, CPU/GPU, memory, disk, and execution time.
5. **Portable core, replaceable adapters.** Storage, queue, compute provider, telemetry exporter, and model integrations must not leak provider-specific details into the domain contract.
6. **Observable by default.** Every job, stage, attempt, and artifact has stable identifiers and correlated events.
7. **Quality is a constraint, not an afterthought.** Validate media metadata and decodeability; measure quality where a meaningful objective metric is available.
8. **Progressive delivery.** Ship a dependable VOD processing core before advanced live, edge, AI, or distributed-compute capabilities.
9. **Security at every boundary.** Authenticate callers, authorize resources, validate inputs, protect secrets, and treat user-supplied URLs and media as untrusted.
10. **Evidence-based claims.** Benchmark claims, savings percentages, and latency targets must include workload, hardware, configuration, sample size, and methodology.

## 4. Target capability model

### A. Workflow orchestration and durable state

- Versioned job and workflow definitions.
- Explicit states for job, stage, attempt, and artifact.
- Durable persistence independent of broker retention.
- Retry classification, bounded retry policy, backoff, deadlines, and idempotency keys.
- Cooperative cancellation with process-tree termination and cleanup.
- Incremental events and progress reporting.
- Composable stages for ingest, inspect, transform, validate, package, publish, and notify.

### B. Cost and resource intelligence

- Resource profiles by workload class.
- Admission control and per-tenant concurrency quotas.
- CPU/GPU selection based on measured throughput and cost.
- Spot/preemptible workers only after checkpointing, retry, and artifact publication are safe.
- Per-stage usage: CPU time, peak memory, GPU time, wall time, temporary disk, bytes transferred, stored bytes, and provider charges where available.
- Cost estimates clearly labeled as estimates; actual cost reconciled from usage and provider billing data.
- Cost/quality/latency comparisons for encoding profiles.

### C. Encoding and packaging efficiency

- Explicit codec and rendition profiles, initially retaining broadly compatible H.264.
- Add HEVC and AV1 only after compatibility, encoder availability, performance, and quality are tested.
- Evaluate CMAF/fMP4 and HLS/DASH packaging choices against target player/device requirements.
- Separate encoding from packaging where doing so reduces duplicate work.
- Consider just-in-time packaging only when measured storage savings outweigh compute, latency, cache, DRM, and operational complexity.

### D. Edge and streaming execution

- Optional edge agents for local inspection, keyframe/scene sampling, and selective preprocessing.
- Transfer only data required by downstream stages when semantics and quality permit.
- Route work between edge and cloud using explicit latency, privacy, resource, and cost policies.
- Keep batch VOD and live/real-time pipelines as distinct workload classes with different SLOs.

### E. Stable automation and MCP interface

- Typed operations such as `video_probe`, `video_trim`, `video_merge`, `video_transcode`, `video_thumbnail`, and `video_add_text`.
- Validated inputs and explicit output artifact contracts.
- Asynchronous job submission, status, cancellation, and event retrieval.
- MCP tools map to the same versioned application/domain contracts as the regular API.
- Tool descriptions communicate limits, side effects, cost implications, and whether an operation is synchronous or asynchronous.

### F. End-to-end observability and QoE

- Correlated traces and structured events across API, queue, worker, FFmpeg, storage, and delivery.
- Metrics for queue wait, startup, stage duration, retries, failure causes, throughput, resource use, and artifact publication.
- Player/session telemetry (startup time, rebuffer events and duration, playback errors, rendition changes) only through an explicit player/SDK integration.
- Join playback sessions to published asset/version and delivery configuration without collecting unnecessary personal data.
- Queryable telemetry API and export formats.

## 5. Reference architecture (logical, not yet implemented)

1. **Control plane:** authenticated API, tenant/resource authorization, validation, admission control, job creation and cancellation.
2. **Durable metadata store:** jobs, workflow versions, stage attempts, idempotency records, artifacts, usage records, and audit events.
3. **Scheduler and event transport:** dispatch eligible stages to queues/workers; broker is not authoritative state.
4. **Execution plane:** isolated CPU/GPU workers with FFmpeg/FFprobe and bounded local scratch storage.
5. **Storage adapters:** S3-compatible object storage initially; provider-neutral artifact references and multipart transfer.
6. **Validation and publication:** inspect outputs, verify expected renditions/manifests, publish atomically, and record immutable artifact metadata.
7. **Observability plane:** metrics, logs, traces, lifecycle events, and cost attribution.
8. **Integration plane:** versioned API, webhooks/event delivery, SDKs, and MCP server.
9. **Optional edge plane:** registered edge agents with constrained capabilities and secure work leasing.
10. **Optional playback plane:** player telemetry ingestion and QoE correlation.

This is a logical decomposition. It does not mandate a specific database, broker, orchestration framework, cloud, or deployment platform. Those choices require workload evidence and architecture decision records.

## 6. Explicit product boundaries and sequencing

The initial product should focus on **asynchronous VOD workflows**: ingest from object storage, inspect, transcode/package, validate, publish, and report status.

The following are not assumed to exist or to be part of the first delivery:

- live transcoding or real-time surveillance;
- distributed inference or an AI model-serving platform;
- live migration of running GPU sessions;
- guaranteed spot-instance savings;
- DRM and multi-DRM packaging;
- player SDK and session QoE analytics;
- Kubernetes as a mandatory deployment target.

These can be evaluated as later workload-specific extensions.

## 7. Success measures

Targets must be set after establishing a reproducible baseline. Report at least:

| Dimension | Measurement |
|---|---|
| Reliability | Job success rate, artifact validation rate, duplicate publication rate, recovery rate |
| Latency | Queue wait, worker startup, per-stage duration, time to first usable output, end-to-end p50/p95/p99 |
| Cost | Cost per source-video minute and per output-minute, CPU/GPU seconds, bytes transferred, storage per delivered minute |
| Quality | Decode/manifest validation, resolution/frame-rate correctness, audio sync, objective quality metric where applicable |
| Capacity | Throughput by workload class, bounded concurrency, queue age, resource saturation |
| Operations | Mean time to detect/recover, retry rate, cancellation completion time, orphaned scratch artifacts |
| Agent usability | Tool schema validity, task completion rate, actionable errors, contract compatibility |
| QoE (later) | Startup time, rebuffer ratio, playback failure rate, quality switches per session |

No numeric performance or savings target is accepted until the benchmark workload and measurement procedure are documented.

## 8. Six strategic objectives and traceability

The product direction is executed as six connected but independently valuable
outcomes:

1. **AI-native long-running orchestration:** versioned workflow definitions,
   durable runs, composable stages, project/scene/character/asset continuity,
   provenance, resumability and incremental output.
2. **First-class cost intelligence:** measured usage, explicit price-card
   versions, cost/quality/latency trade-offs, admission control, CPU/GPU policy
   and interruption-aware Spot capacity.
3. **Edge–cloud collaboration:** selective local preprocessing and semantic
   sampling, with cloud fallback and measurable privacy/bandwidth/latency
   benefits.
4. **Portable MCP interface:** stable typed video tools over application
   contracts, with authentication, authorization, quotas and audit applied
   consistently across protocols.
5. **End-to-end observability and QoE:** correlate server workflow, immutable
   output version, CDN delivery and player-session experience through a
   queryable API.
6. **FFmpeg execution at scale:** process-tree supervision, per-attempt
   CPU/memory/PID/disk bounds, fair concurrency, worker-loss recovery and
   safe artifact cleanup.

Security, runtime support, test infrastructure and governance are cross-cutting
prerequisites for all six outcomes.

## 9. Claims requiring independent validation

Industry figures supplied in planning discussions (codec-efficiency percentages,
Spot discounts, GPU rental rates, distributed-inference savings, survey
percentages and latency improvements) are hypotheses, not verified properties
of this repository. The research note
`docs/research/2026-11-18-parallel-architecture-and-benchmarks.md` records
which claims have identifiable sources and which remain unsubstantiated.

Codec gains vary by content, encoder, preset, quality target, hardware and
playback support. Cloud prices vary by provider, region, tenancy and time.
MediaConvert uses normalized output-minute pricing; GPU sticker price is not
comparable to a provider's end-to-end job bill. Product claims must identify
a current source and be reproduced or qualified with a project-specific
benchmark.
