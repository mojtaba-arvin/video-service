# Current-state architecture and repository review

## 1. Review scope

- Repository: `mojtaba-arvin/video-service`
- Branch inspected: `develop`
- Commit inspected: `d75bd59d8203277fc6a50440d3f0b34693958ee1`
- Latest commit visible in the inspected branch history: 2021-04-01
- Tree entries: 129
- Review method: static inspection of tracked files through GitHub.
- Not performed: local installation, runtime execution, test execution, load testing, security scanning, production configuration review, or cloud billing analysis.

This document records evidence from the inspected source. It does not claim that the code is currently deployed or operational.

## 2. What the repository implements

The existing service is an asynchronous **VOD processing microservice**:

- gRPC service contract in `video-streaming/video_streaming/grpc/protos/streaming.proto`.
- Celery orchestration using chains, groups, and chords.
- Redis-backed progress/status cache in `video-streaming/video_streaming/cache.py`.
- S3-compatible input/output operations in `core/services/s3.py`.
- FFmpeg/FFprobe processing tasks under `ffmpeg/tasks/`.
- HLS, DASH, fMP4/CMAF-related playlist options, codec/rendition configuration, thumbnails, and watermarking.
- Progress callbacks and per-output CPU time, memory, and elapsed-time samples.
- Job and output cancellation paths.
- Webhook notification after workflow completion.

The README describes the design as a no-web-framework gRPC service, using Redis, a broker (RabbitMQ or Redis), and S3-compatible storage.

## 3. Existing workflow (simplified)

1. Client submits a `JobRequest` through gRPC.
2. `CreateJobMixin` validates and expands requested outputs into Celery workflow signatures.
3. Input checks and S3 downloads are dispatched.
4. FFprobe analyzes the input.
5. Playlist, thumbnail, and optional watermark tasks run.
6. Output files/directories are uploaded to S3-compatible storage.
7. Redis status/progress is queried through `get_results`.
8. Optional webhook callback is invoked.
9. Client may request job/output cancellation.

The existing workflow is a valuable foundation for a VOD execution engine. It is not yet a general workflow platform with durable, versioned orchestration semantics.

## 4. Evidence-based capability assessment

| Capability | Evidence in current code | Assessment |
|---|---|---|
| Asynchronous job dispatch | Celery app and task graph | Implemented in legacy form |
| Typed remote contract | gRPC/protobuf | Present; not yet a versioned public API strategy |
| Object storage | boto3 S3 service | S3-compatible adapter exists |
| HLS/DASH output | playlist task and protobuf options | Present; verify behavior against current player/device requirements |
| fMP4 | playlist option and README | Present as a configuration path; CMAF interoperability needs tests |
| H.264/HEVC/VP9 options | protobuf enums and codec mapping | Present; AV1 is not represented in the inspected contract |
| Thumbnails/watermark | task modules and protobuf | Present |
| Progress | FFmpeg callback and Redis keys | Basic progress reporting exists |
| Resource usage | psutil callback and result fields | Basic CPU-time/memory sampling exists; not billing-grade accounting |
| Cancellation | revoke RPCs and callback process kill | Present; process-tree cleanup and race behavior need verification |
| Durable job history | Redis cache with TTL | Not established; Redis cache is not a durable job ledger |
| Tenant isolation/quotas | Not found in inspected contract and settings | Gap |
| Worker resource isolation | Docker service and local directories | Container exists; per-job CPU/memory/disk isolation not established |
| Cost attribution | elapsed/CPU/memory values | Partial signals only; no complete cost model |
| MCP | No MCP implementation found in inspected tree | Not implemented |
| Edge execution | No edge agent/scheduler found | Not implemented |
| Player QoE | No player telemetry ingestion found | Not implemented |
| Automated test suite | No test paths found in inspected 129-entry tree | Not found; verify local/other branches before treating as definitive |

## 5. Concrete findings requiring attention

### P0 — configuration correctness

In `video-streaming/video_streaming/settings.py`:

- `TASK_RETRY_FFMPEG_COMMAND_MAX` reads the `TASK_RETRY_BACKOFF_MAX` environment variable instead of its own variable. This makes the FFmpeg command retry limit configuration misleading.
- `S3_DEFAULT_OUTPUT_BUCKET_NAME` reads `S3_DEFAULT_INPUT_BUCKET_NAME` instead of `S3_DEFAULT_OUTPUT_BUCKET_NAME`. Separate input/output bucket configuration therefore does not behave as the setting name suggests.
- The `DEFAULT_PLAYLIST_IS_HLS` setting reads `DEBUG_PLAYLIST_IS_HLS`, which should be checked for an accidental name mismatch.

These are code-level findings; add regression tests before changing behavior.

### P0 — secrets and configuration hygiene

The tracked tree contains `video-streaming/video_streaming/.env.local`. The root `.gitignore` pattern `*.env` does not ignore a filename ending in `.env.local`.

The inspected values for the two S3 credential fields were short (two characters each), so this review did not establish that real credentials were exposed. Nevertheless, tracking local environment files is unsafe. Replace the committed file with a clearly non-secret example template, ignore local variants, and rotate any credential if a real value was ever committed. Review repository history before concluding that secret exposure never occurred.

### P1 — process execution and correctness

- `ffmpeg/tasks/create_playlist.py` contains a TODO noting that FFmpeg callback errors may not reliably prove the process completed successfully. Output validation must not rely on callback text or task completion alone.
- `ffmpeg/utils/ffmpeg_callback.py` samples the FFmpeg process PID. Child processes, descendants, and worker-level resource usage are not comprehensively accounted for.
- `ffmpeg/tasks/generate_thumbnail.py` has a likely scale-default bug: when `scale_height == 0`, it assigns `scale_width = -1` rather than setting `scale_height = -1`.
- Thumbnail generation's inspected path does not use the same explicit progress/process supervision pattern as playlist and watermark processing.
- Local input/output paths and cleanup behavior need adversarial tests for retries, worker crashes, concurrent jobs, and cancellation races.

### P1 — state and workflow reliability

- `RedisCache` stores job metadata/progress with a TTL. This is useful for transient progress but is not a durable source of truth for long-lived jobs, audit history, or recovery after cache loss.
- Celery task graphs are used as the workflow engine, but workflow definition/version, attempt history, idempotency, and artifact publication transactions are not represented as a separate durable domain model.
- Cancellation uses signals and process killing. The exact behavior under worker loss, process descendants, concurrent upload, and retry needs integration tests.
- Webhook delivery behavior needs explicit timeout, retry, signature, replay protection, and delivery-history semantics.

### P1 — API security and operational safety

- The gRPC server exposes create, result, and revoke operations. Authentication, tenant authorization, quotas, and per-resource access controls were not established in the inspected service contract.
- The job request includes a caller-supplied `webhook_url`; outbound webhook delivery must defend against SSRF, private/link-local destinations, DNS rebinding, redirects, and unbounded response times.
- `Streaming.create_job` prints the request, and `BaseTask.__call__` prints task arguments. Structured logging should redact secrets, signed URLs, and sensitive user data.
- S3 exception handling prints tracebacks and responses. Replace ad-hoc prints with structured, redacted logs and stable error codes.

These are review findings and threat-model requirements, not proof that the service is publicly exposed.

### P2 — dependency and deployment age

The inspected Dockerfile and requirements pin an older runtime stack, including Python 3.9.2, FFmpeg 4.1, Celery 4.4.7, grpcio 1.35.0, protobuf 3.14.0, and boto3 1.17.14.

Upgrade deliberately with compatibility tests and a reproducible image. Do not combine an untested dependency refresh with a large architecture rewrite.

### P2 — missing product surfaces

No MCP server, player telemetry ingestion, edge agent, dynamic GPU scheduler, AV1 contract, JIT packager, or end-to-end cost ledger was found in the inspected tree. These belong to the roadmap, not the current feature list.

## 6. Architectural constraints to preserve

- Keep FFmpeg/FFprobe behind a media execution adapter.
- Preserve the existing useful media operations while extracting their domain contracts from Celery task signatures.
- Keep object storage behind an adapter and avoid making MinIO-specific behavior part of the domain model.
- Preserve asynchronous submission and progress reporting, but move authoritative job state to durable persistence.
- Maintain gRPC compatibility deliberately; introduce API versions and migration policy before breaking protobuf changes.

## 7. Immediate verification checklist

Before declaring the current service production-ready or starting a major refactor:

- [ ] Reproduce the three settings issues with focused tests.
- [ ] Remove local environment files from tracking and inspect history for credentials.
- [ ] Add tests for FFmpeg exit status, output probing, thumbnail dimensions, cancellation, retries, and cleanup.
- [ ] Verify webhook SSRF defenses and authentication/authorization.
- [ ] Establish a local reproducible Docker build and smoke test.
- [ ] Inventory all branches, tags, open pull requests, and any deployment manifests not present in the inspected tree.
- [ ] Capture baseline throughput, queue wait, processing time, resource usage, and output correctness on a fixed media corpus.

## 8. Review conclusion

The repository has a meaningful legacy VOD pipeline, not a blank slate. Its strongest reusable assets are the gRPC contract, Celery workflow decomposition, FFmpeg task modules, S3 integration, progress tracking, and cancellation intent.

The highest-value next step is **not** to add advanced AI/edge/GPU features immediately. It is to establish correctness, secure boundaries, durable state, process isolation, automated tests, and measurable baselines. Those foundations make later cost-aware scheduling, MCP, and edge execution safe rather than cosmetic.


## 9. Verified implementation updates (2026-11-17)

The original sections above describe the inspected legacy baseline at commit
`d75bd59d8203277fc6a50440d3f0b34693958ee1`. The following changes have since
been merged to `develop`:

| Change | Evidence | Current status |
|---|---|---|
| FFmpeg retry-limit configuration | `TASK_RETRY_FFMPEG_COMMAND_MAX` now reads its own environment key; five regression tests cover it | Merged in PR #55 |
| Independent input/output buckets | Output bucket now reads `S3_DEFAULT_OUTPUT_BUCKET_NAME`; covered by regression test | Merged in PR #55 |
| Playlist environment compatibility | `DEFAULT_PLAYLIST_IS_HLS` is canonical; `DEBUG_PLAYLIST_IS_HLS` remains a fallback | Merged in PR #55 |
| Test and CI baseline | Python 3.9 `unittest` job runs on Ubuntu 24.04 for PRs and pushes to develop | Merged in PR #55 |
| Environment file hygiene | Tracked `.env.local` removed; non-secret `.env.example` added; `.env` and `.env.*` ignored except the template | Merged in PR #56 |

The CI suite now runs 17 tests: five isolated settings regressions, four scale-normalization checks, and eight FFmpeg render/output-validation integration checks. CI also generates and FFprobe-validates a separate synthetic VOD fixture corpus. It still does not cover the complete gRPC → Celery → FFmpeg → object-storage VOD path.

### Environment history finding

Ten historical revisions containing `.env.local` were inspected. The S3
access-key and secret-key fields were empty where present. The broker/result
backend URLs contained fixed development credentials and internal RabbitMQ/Redis
hostnames. They appear to be development placeholders, but repository history
cannot establish whether an external/shared service still accepts them. Treat
those historic values as exposed and rotate them anywhere still active. The
current tracked local environment file has been removed; Git history has not
been rewritten, and no external service credentials were changed.

### Current media correctness work

The thumbnail scale-default defect was fixed in PR #57: width and height are normalized independently and the renderer is covered by actual FFmpeg/FFprobe integration tests. The fix is merged to `develop`.

### Current verification boundary

The latest fully merged baseline includes configuration tests, environment tracking checks, and thumbnail rendering integration. It does not yet establish durable job state, FFmpeg process-tree isolation, playlist artifact validation/publication safety, webhook SSRF defenses, authenticated tenant boundaries, or player QoE. Those remain unimplemented or unverified
rather than implied by the roadmap.


### Repository automation additions (2026-11-17)

A follow-up governance branch adds full-SHA-pinned checkout/setup actions,
concurrency cancellation, a generated synthetic VOD regression corpus,
existing CodeQL default analysis, pull-request dependency review, Dependabot update
schedules, a security reporting policy, and a pull request template.

Five repository agent skills now define procedures for FFmpeg regression
testing, durable workflow design, MCP/agent interface design, codec/cost
benchmarking, and security-boundary review. These skills guide contributors
and coding agents; they do not mean that MCP, durable orchestration, or billing
intelligence has already been implemented.

The synthetic-vod-v1 corpus is generated from FFmpeg lavfi sources at test
time. It covers short landscape, portrait-with-audio, and low-resolution
sources. It is designed for deterministic correctness tests, not perceptual
quality or production cost claims.

The regression suite now also runs under Python 3.12. The legacy service and
its runtime image still target Python 3.9, which reached end of life on
2025-10-31. Python 3.12 test-suite success does not prove runtime dependency or
container compatibility; migrating the service runtime remains an explicit
security and maintenance item.

The CodeQL and dependency-review workflows are not yet required merge checks.
GitHub repository administration also remains necessary: current inspection
found no active ruleset and develop is unprotected. The connected GitHub
integration does not expose write access to branch-protection or repository
security settings.


## 10. Verified implementation update (2026-11-18)

The historical review above describes the legacy baseline at commit
`d75bd59d8203277fc6a50440d3f0b34693958ee1`. It must not be read as the
current implementation. The following evidence was verified against
`develop` at `37a6bce9e20bdf1cb5b69ca9dca80e4e5b69b3d9` on
2026-11-18.

### Merged implementation

| Capability | Current evidence | Boundary / remaining work |
|---|---|---|
| CI and engineering controls | Python 3.9 and 3.12 regression lanes, generated synthetic media fixtures, pinned Actions, dependency review, Dependabot and repository agent guidance (PR #61) | Python 3.12 lane is regression coverage, not proof that the full service image/runtime is compatible |
| Thumbnail correctness | FFmpeg rendering and FFprobe validation reject missing, empty, corrupt and dimension-mismatched artifacts (PR #59) | Does not prove process-tree isolation or broad resource limits |
| HLS/DASH publication | Structured package validation checks manifests, rendition references, media artifacts and metadata; publication verifies storage dependencies and publishes root manifests last (PR #62/#64) | Namespace-safe garbage collection of unreachable old remote media remains deferred |
| End-to-end VOD smoke path | Real local gRPC server, Celery signatures/chains, FFmpeg/FFprobe, HLS generation, in-memory cache/storage doubles and callback server (PR #63) | It is an in-process/eager integration harness, not a broker-backed worker crash/restart or live cloud-storage test |
| Durable workflow state | PostgreSQL Job/Workflow/Stage/Attempt/Artifact/Event records; explicit transitions, idempotency, stale-attempt recovery, validation reports and resource measurements (PR #68) | Job commit and Celery graph dispatch still have a crash window; a transactional outbox/dispatcher is not implemented |
| Durable VOD vertical | Playlist and thumbnail stages use durable attempts, validation, publication readback and persisted artifacts; E2E retrieves final state after clearing Redis (PR #68) | Watermark-specific E2E, worker-loss recovery through a real worker process, cancellation and non-zero FFmpeg exit/retry coverage remain open |

PR #68's verified CI evidence on its reviewed head was 54 regression tests
on Python 3.9 (7 skipped), 54 on Python 3.12 (7 skipped), five PostgreSQL
repository tests on each Python version, two VOD E2E tests on Python 3.9,
and a passing Dependency Review. These are CI test results, not production
deployment or performance evidence.

### Merged QoE increment

PR #69 is merged into `develop` in commit
`94c57d6b3f709ff7d7d4f294113bd7a9151f1790`. It adds PostgreSQL-backed
playback sessions and append-only player-event ingestion bound to the exact
completed Stage and manifest Artifact. The contract covers startup, buffering,
decoder errors and playback end. CI passed all five jobs, including the full
gRPC → Celery → FFmpeg → storage E2E path and PostgreSQL tests on Python 3.9
and 3.12.

The pre-merge review found and fixed important correctness issues: active
buffering was omitted from live reports, partially priced attempts could be
presented as a total, and duplicate event IDs did not compare semantic
payloads. A further regression test verifies that active buffering updates
both duration and rebuffer count.

This is durable ingestion/reporting infrastructure, not a player SDK, CDN
integration, percentile/SLA view or automated adaptive-bitrate system.

### Current priority gaps

1. Transactional outbox/dispatcher for the Job-to-Celery dual-write window.
2. FFmpeg process-tree cancellation is merged via PR #78. The callback now
   uses bounded graceful/forced descendant termination; supervision for every
   command path, deadlines, CPU/memory/PID/disk limits and cgroup isolation
   remain open.
3. Sensitive log redaction is being delivered in new PR #85. Authentication,
   authorization, tenant isolation and quotas, plus SSRF-safe webhook
   delivery with DNS/IP/redirect controls, remain open.
4. Full failure-path integration: worker loss, non-zero FFmpeg exit, retry,
   cancellation, duplicate delivery and watermark output.
5. Supported Python runtime and reproducible container migration. Python 3.9
   reached end of life; the Python 3.12 regression lane does not establish
   complete application compatibility.
6. Measured, versioned monetary cost attribution across CPU/GPU, storage,
   network transfer and provider billing; current resource metrics and RUNIT
   are not a cloud invoice.
7. Versioned public application contracts and an MCP adapter. Typed internal
   operation contracts are a foundation, not an MCP implementation.
8. Codec-quality/cost benchmark, then evidence-based AV1/HEVC decisions.
9. Versioned AI/creative workflow definitions, project continuity state,
   incremental ingestion, JIT packaging, cost-aware scheduling, CDN/player
   correlation and edge execution remain independent backlog outcomes.
   Issues #79–#84 define scoped acceptance criteria.

### Operational and governance limits

The CI harness and the connected GitHub integration do not establish that a
production deployment exists. GitHub repository rulesets/branch protection and
some repository security settings require owner/admin access. Those settings
should be enabled, but they do not block independent engineering work on
isolated branches.

The authoritative work queue remains
[Issue #54](https://github.com/mojtaba-arvin/video-service/issues/54).
