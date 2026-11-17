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
