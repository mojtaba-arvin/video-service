# Parallel architecture research and benchmark strategy

- **Date:** 2026-11-18
- **Repository baseline:** `develop` at `37a6bce9e20bdf1cb5b69ca9dca80e4e5b69b3d9`
- **Purpose:** turn current external engineering evidence into decisions, experiments and acceptance criteria for the video orchestration platform.
- **Status:** research-informed proposals. They are not claims that the corresponding features are already implemented.

## 0. Verification of headline market and research claims

The planning figures supplied in the product brief are hypotheses, not
acceptance criteria. A source check on 2026-11-18 found:

| Claim in the planning brief | What current sources support | Engineering treatment |
|---|---|---|
| RTX 4090 rental around $0.33/GPU-hour | Provider comparisons list starting rates around $0.33, but prices vary by provider, region, tenancy/security tier, availability, host CPU/RAM and billing model. One October 2026 comparison found $0.33–$0.89 across seven providers. | Use a timestamped price-card snapshot. Compare the same completed workload and include CPU host, storage, network, idle time and interruption/retry waste. Hourly GPU sticker price is not a job cost. |
| MediaConvert around $4.95 per source hour | AWS pricing is based on output minutes and normalized multipliers for tier, codec, resolution, frame rate and quality. The current page lists Basic AVC at $0.0075 per normalized minute in the first tier; other combinations use different multipliers. There is no universal source-hour price. | Quote a concrete job configuration, count every output, and add storage/egress/CDN separately. Do not compare a GPU-hour with a universal MediaConvert source-hour price. |
| 62% Spot savings | AWS says Spot can save up to 90% versus On-Demand compute in some cases, but instances are interruptible. That is not a guarantee of 62% lower end-to-end media job cost. | Run interruption-aware experiments and report cost per successful output, queue delay, retry waste and latency. |
| TurboServe 37.2% cost and 37.5% latency improvement | The June 2026 preprint reports these results on streaming video-generation serving traces and clusters up to 64 NVIDIA B300 GPUs. It uses session placement, GPU provisioning, coalescing and migration. | Treat it as a candidate scheduling pattern for long-lived streaming-generation workloads, not a promised improvement for offline FFmpeg transcoding. |
| AV1 30% vs HEVC and 50% vs H.264 | A technical filing contains a provider's own average API-test figures on selected content/configurations. Codec efficiency varies by source, encoder, preset, rate control, hardware, quality metric and device support. | Benchmark on our own corpus at matched quality. Use VMAF v1 and the appropriate screen-class model, plus decode compatibility, encode cost and delivery cost. No default codec change based on generic percentages. |
| 76% AI Grid savings, 10–20x AIDP speed, 40–60% AIDP savings | I did not find sufficiently attributable primary benchmark material for these exact figures in this pass. | Do not publish or use for architecture sizing until a primary report/paper, methodology, hardware, sample size and reproduction path are identified. |
| 75% dashboard use, 45% direct API/database queries, 28% monitoring and 27% root-cause concerns | The exact survey, source and sampling methodology were not established in this pass. | Exclude these figures from product positioning until the underlying survey is identified and validated. |

### Primary references for this verification

- RTX 4090 provider comparison, October 17, 2026: https://www.amcompute.com/gpu-cloud-prices/rtx-4090
- Current GPU listings, November 18, 2026: https://perunitprice.com/gpu-cloud/
- AWS Elemental MediaConvert pricing and normalized output-minute model: https://aws.amazon.com/mediaconvert/pricing/
- AWS Spot best practices and interruption guidance: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/spot-best-practices.html
- TurboServe preprint (June 17, 2026): https://arxiv.org/abs/2606.19271
- Netflix VMAF and its 2026 VMAF v1 models: https://github.com/Netflix/vmaf and https://github.com/Netflix/vmaf/blob/master/resource/doc/models_v1.md
- Example technical filing containing the AV1 internal-test figures: https://ptacts.uspto.gov/ptacts/public-informations/petitions/1557886/download-documents?artifactId=LFvg5zEttzWXKCrc9ERCll6Wf3VsqlhGa1KW79DqQMJMcpgrbcIbkNQ

## 1. Executive decisions

1. Keep PostgreSQL as the authoritative workflow ledger. Add a transactional outbox in the same transaction as Job/Workflow/Stage creation; use a separate relay and idempotent consumers. Do not claim exactly-once delivery.
2. Treat Celery as an execution transport/backend, not as the durable workflow authority. Do not assume `acks_late` or worker-loss flags alone guarantee recovery.
3. Isolate FFmpeg as a supervised process tree with explicit wall-time, CPU, memory, PID and scratch-disk bounds. On Linux, cgroup v2 is a portable kernel primitive to evaluate before adopting a provider-specific worker system.
4. Treat current resource metrics and `RUNIT` as resource evidence/relative indices, not provider billing. A monetary estimate must carry a versioned price card and an explicit coverage status.
5. Make MCP a thin, versioned adapter over application services. The same authorization, quotas, audit and idempotency rules must apply to gRPC/HTTP and MCP.
6. Do not accept generic AV1/HEVC savings claims. Benchmark against the same representative corpus, matched perceptual quality and playback compatibility.
7. Use Spot/preemptible capacity only for work that can be safely interrupted and retried from durable checkpoints/artifacts. An interruption notice is an optimization signal, not a correctness guarantee.
8. Keep Edge/Cloud as an evidence-led lane. Start with bounded local inspection/frame selection experiments; do not build distributed infrastructure before measuring locality benefit.

## 2. Transactional outbox and durable dispatch

### Evidence

- AWS Prescriptive Guidance describes the dual-write failure: the database commit can succeed while event publication fails, or publication can happen while the database transaction later rolls back.
- The microservices.io pattern recommends writing the business object and outbox record in one database transaction, then relaying committed messages. The relay may publish duplicates, so consumers must be idempotent.

### Design decision

The current Job commit followed by `Celery.apply_async()` has a real crash window. Replace the dual write with:

1. A single PostgreSQL transaction writes Job, Workflow, Stage rows and one or more outbox rows.
2. A relay claims due rows using a lease/claim protocol (e.g. `FOR UPDATE SKIP LOCKED` where the supported database allows it), writes a bounded dispatch attempt, then publishes.
3. The relay marks a record delivered only after broker publication acknowledgement. If it crashes after publish but before marking delivered, it may publish again.
4. The consumer reconciles the message against the durable Stage/Attempt state and returns safely for an already-started or terminal stage.
5. Failed publishes use bounded exponential backoff, attempt limits, observable poison/dead-letter state and operator replay.

Do not hold a database transaction open while waiting for broker/network I/O. Do not claim exactly-once delivery: the design is at-least-once dispatch with idempotent effects.

### Acceptance tests

- Crash after the DB transaction commits but before the relay runs: the Job is eventually dispatched.
- Crash after broker publish but before outbox acknowledgement: redelivery occurs but creates no duplicate logical attempt/artifact.
- Broker unavailable for a bounded interval: Job remains durable and dispatch is retried.
- Two relay processes race: one lease owns each active row; lease expiry permits recovery.
- A poison message becomes visible and does not block unrelated dispatches.
- Job/Stage cancellation before dispatch prevents unnecessary execution.

## 3. Celery failure semantics

### Evidence

Celery task documentation explains acknowledgement and redelivery trade-offs. With late acknowledgement, a task may be delivered again after failure; tasks must be idempotent. Worker-loss settings can also lead to message loops if a task repeatedly kills its worker. Recent configuration documentation describes additional worker-child retry behavior in newer Celery releases, but this repository's PR #63 dependency uplift pins Celery 5.6.3. Do not rely on a newer setting without a deliberate compatibility upgrade.

### Design decision

- Keep attempt ownership, current state, deadlines and retry policy in PostgreSQL.
- Use Celery message/task IDs only as execution references.
- Keep FFmpeg task effects idempotent by attempt-scoped scratch paths and deterministic artifact identities.
- Reconcile stale attempts with a recovery worker, not Redis TTL.
- Configure broker acknowledgement/retry behavior only after testing the chosen worker pool and shutdown semantics.
- Add a broker-backed worker-process test; eager-mode E2E cannot reproduce all worker-loss behaviors.

### Acceptance tests

Kill a worker child, terminate the whole worker container, restart the broker, redeliver the same task and race two deliveries. Assert one authoritative successful attempt, no manifest pointing at incomplete artifacts, bounded retries, and a durable failure after retry exhaustion.

## 4. FFmpeg supervision and resource isolation

### Evidence

Linux cgroup v2 provides hierarchical process grouping and resource controllers. The CPU controller exposes `cpu.max`; memory control exposes `memory.max`; the PID controller exposes `pids.max`. These primitives are useful for a bounded worker design, but availability and delegation depend on the host/container runtime.

### Design decision

Start with a replaceable `ExecutionBackend` abstraction and a local supervised implementation:

- Launch FFmpeg in its own process group/session; capture exit code, stderr tail and wall-time.
- On timeout/cancellation, terminate the process group, wait a bounded grace period, then kill remaining descendants.
- Give each Attempt a private scratch directory and enforce disk quota/cleanup policy.
- Run each workload inside a bounded container or delegated cgroup with explicit CPU, memory and PID limits; avoid relying on worker-wide limits alone.
- Separate workload classes by expected resource profile (probe/thumbnail vs transcode) before increasing concurrency.
- Capture CPU time, peak RSS, wall time, input/output bytes, scratch high-water mark and (when available) GPU time.

Do not implement Shared UDP Mux or thousands of concurrent FFmpeg processes without a measured file-descriptor/network bottleneck and a reproducible load test.

### Acceptance tests

- timeout kills FFmpeg and every descendant;
- cancellation cannot leave an encoder or uploader alive;
- memory/PID/CPU limits produce classified failures and do not starve unrelated jobs;
- scratch cleanup is safe across retry and cannot delete another Attempt's files;
- concurrency does not exceed the configured resource budget.

## 5. Cost intelligence: usage, estimates and bills are different data

### Required dimensions

Persist raw, attributable usage before converting it to money:

- CPU seconds and wall-clock seconds;
- peak/integrated memory (document which one; peak RSS multiplied by wall time is only an approximation);
- GPU seconds and accelerator type, where available;
- scratch disk high-water mark and duration;
- input/output/egress bytes;
- retained storage byte-hours/byte-months;
- provider/region/instance/price-card identity and billing source.

### Design decision

- Every price card is immutable/versioned and includes provider, region, machine/resource, currency, unit, effective period and source.
- Estimated cost records its input metrics and price-card version.
- Reconciled billing is a separate record/source; never silently overwrite estimates.
- Report coverage: priced attempts / all attempts and priced dimensions / all required dimensions.
- Do not return a partial subtotal under a field named `total_cost`. Return `null` for incomplete totals or expose a clearly labelled partial subtotal and coverage.
- Use `RUNIT` only as a normalized internal resource index, not USD/EUR or invoice data.

### Experiments

Replay the same corpus across at least two CPU worker classes and one GPU class where available. Compare cost per source-minute, output-minute, queue delay, p50/p95 processing latency and quality. Publish raw data and the exact price-card revision.

## 6. Codec and packaging benchmark strategy

### Hypothesis, not assumption

AV1 may reduce bitrate at a matched perceptual-quality target, but the result depends on content, encoder implementation/preset, hardware, playback device support and encode time. A bitrate reduction alone does not prove a lower total cost if encoding consumes substantially more compute or creates compatibility/rebuffering problems.

### Benchmark protocol

- Pin FFmpeg version/build configuration and encoder implementation (e.g. libx264, libx265, libsvtav1).
- Use a representative corpus: landscape, portrait, low motion, high motion, animation, grain/noise, dark scenes, audio-heavy and multiple resolutions/durations.
- Compare H.264, HEVC and AV1 at matched VMAF target bands; record VMAF model/version, bitrate, file size, encode wall time, CPU/GPU time, peak memory and decode compatibility.
- Run repeated measurements on fixed hardware and record thermal/parallel-load conditions.
- Validate HLS/DASH/CMAF packaging, manifests, audio sync and player compatibility.
- Include CDN egress/storage and decode support in the economic conclusion.
- Retain H.264 as broad-compatibility fallback until device/player evidence supports profile changes.

Do not publish an “AV1 is X% better” project claim without corpus, target-quality, encoder, hardware and compatibility results.

## 7. MCP as a stable adapter

### Evidence and design implications

The MCP 2026-07-28 specification emphasizes tool capability declaration, consistent list results and authorization security. Servers should validate token audience and should not accept token passthrough. Tool discovery should be deterministic within the same authorization context.

### Design decision

- Keep operation schemas/versioning in the application layer; MCP tool handlers call those same services.
- Start with safe, useful tools: `video_probe`, `video_thumbnail`, `video_transcode`, `video_job_status`, `video_cancel`.
- Add destructive or expensive operations only with budget estimation, explicit limits and caller authorization.
- Return durable Job IDs and typed status handles for long-running calls; do not keep MCP requests open for entire transcodes.
- Enforce caller identity, tenant/resource scope, quota, audit and output ownership in the service layer, not only in tool descriptions.
- Bind access tokens to the MCP server audience; never blindly forward an upstream bearer token.
- Keep tool names and JSON schemas stable and deterministic; version breaking changes explicitly.
- Design agent-facing errors as actionable, bounded, non-secret details.

### Acceptance tests

Test tool-list stability, schema validation, authorization isolation, expired/wrong-audience tokens, budget limits, idempotent submission and status/cancel behavior through an actual MCP client.

## 8. Spot and preemptible scheduling

Cloud Spot capacity is interruptible; AWS documents a two-minute interruption notice in its EC2 Spot guidance, but the system must still tolerate interruption without relying on receiving the notice.

Use Spot for:
- independent, short/restartable encodes or stages;
- work whose inputs and completed artifacts are already durable;
- tasks with bounded checkpoints or clean restart semantics.

Do not use Spot initially for:
- uncheckpointed monolithic jobs whose only state is local scratch;
- critical control-plane services (outbox relay, DB, authorization);
- any operation whose retry can publish duplicate or partial output.

Measure interruption rate, wasted compute, retry cost, queue delay and end-to-end latency before claiming savings.

## 9. Edge/Cloud collaboration

Start with a constrained edge experiment rather than a distributed execution mesh:

1. Define one real workload where upload bandwidth, privacy or response latency is the bottleneck.
2. Run metadata inspection and bounded frame/scene sampling close to the source.
3. Transfer only required derived frames/metadata, while preserving source hashes and provenance.
4. Compare edge and cloud-only execution for bytes transferred, time-to-first-result, energy/resource use, accuracy/quality and operational failure rate.
5. Add signed work leases and an edge agent only if the measured benefit exceeds coordination, security and support cost.

Edge should be an optional execution backend, not a special domain model or mandatory deployment.

## 10. Parallel work lanes and safe integration

| Lane | Scope | Parallel output | Shared boundary / merge dependency |
|---|---|---|---|
| A — durable dispatch | Outbox, relay, retry and recovery | PostgreSQL outbox + relay + crash tests | Own outbox migration/repository/dispatcher files; integrate with Workflow repository through a small explicit interface |
| B — process execution | FFmpeg supervisor, limits, cleanup | PR #78 merged: callback uses bounded process-tree termination and a parent/child regression test | Add cgroup/container limits, per-attempt quotas and deadline supervision across all FFmpeg paths |
| C — security | redacted logs, webhook SSRF, auth/tenant/quotas | New PR #85 removes raw task/request/provider/webhook values from logs | Coordinate identity/tenant contract before changing gRPC/protobuf; next add SSRF and authorization controls |
| D — economics | price cards and benchmark harness | raw dataset + reproducible report + coverage-aware API | Do not modify attempt persistence fields until usage schema is agreed |
| E — agent interface | typed contracts + MCP adapter | MCP tool manifest + client contract tests | Depends on app service contracts and authorization, not on Celery signatures |
| F — QoE | playback event correctness and player integration | Manifest-bound sessions and event/report ingestion merged in PR #69 | Add client example/SDK and CDN correlation in Issue #82; keep delivery telemetry schema independent |
| G — codec | H.264/HEVC/AV1 benchmark | VMAF report + compatibility matrix | Depends on pinned FFmpeg build and corpus; no default codec change without results |
| H — edge | local sampling and optional edge worker proof | same-workload edge/cloud experiment | Deferred until a workload hypothesis and baseline exist |
| I — runtime | Python and image migration | dependency inventory + full image smoke test | Keep compatibility uplift separate from orchestration refactor |

### Integration rules

- One owner/agent per shared file, migration chain, protobuf contract or schema at a time.
- Parallelize research, isolated code, tests and documentation; serialize merges to shared interfaces.
- Each PR includes its tests, risks, rollback note and evidence.
- Use a merge queue/required checks when repository administration allows it; otherwise manually verify all checks before merge.
- No generated media in source control; store benchmark outputs as CI artifacts or versioned external data with hashes.
- Record benchmark commands, runtime versions, hardware, settings, repetitions and raw measurements.
- No “done” status based solely on a design document or green unit test that does not exercise the relevant failure boundary.

## 11. Source references

The conclusions above are distilled from these primary technical references, checked on 2026-11-18:

1. AWS Prescriptive Guidance, Transactional outbox pattern: https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html
2. microservices.io, Transactional outbox: https://microservices.io/patterns/data/transactional-outbox.html
3. Celery task acknowledgement and idempotency: https://docs.celeryq.dev/en/stable/userguide/tasks.html
4. Celery worker-loss configuration and caveats: https://docs.celeryq.dev/en/stable/userguide/configuration.html
5. Linux kernel, cgroup v2: https://docs.kernel.org/admin-guide/cgroup-v2.html
6. Model Context Protocol, 2026-07-28 server tools: https://modelcontextprotocol.io/specification/2026-07-28/server/tools
7. Model Context Protocol authorization security considerations: https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations
8. AWS EC2 Spot best practices: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/spot-best-practices.html
9. Netflix VMAF: https://github.com/Netflix/vmaf
10. FFmpeg external libraries and build options: https://www.ffmpeg.org/general.html
11. Linux kernel cgroup v2 resource controls: https://docs.kernel.org/6.18/admin-guide/cgroup-v2.html
12. AWS Elemental MediaConvert pricing: https://aws.amazon.com/mediaconvert/pricing/
13. TurboServe streaming video-generation scheduling paper: https://arxiv.org/abs/2606.19271
