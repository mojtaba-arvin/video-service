# ADR 0010: Celery worker integration for durable Attempt leases

- **Status:** Implemented and CI-verified in PR #127; deployment/rollout pending
- **Date:** 2026-11-19
- **Parent issue:** #71
- **Depends on:** PR #123 (durable Attempt lease primitives)
- **Scope:** Integrating PostgreSQL lease ownership into the existing Celery/FFmpeg VOD pipeline without exposing lease tokens to the broker or Redis

## Decision summary

A lease token is process-local capability material. It is returned by `claim_attempt()`, held only in the worker that claimed the Attempt, and supplied to repository mutations and heartbeat renewal. It must never be included in Celery signatures/kwargs, task results, Redis projections, logs, traces, or durable event payloads.

The first integrated worker paths use `WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS` (default 60 seconds, bounded to 1–86400 seconds). The heartbeat renews at one third of the configured TTL unless a shorter interval is explicitly supplied.

The current task graph separates media processing from S3 publication. Therefore a single lease must not be passed from a processing task to an upload task. The intended model is two independently claimed Attempts for the same Stage:

1. A processing task claims the Stage, creates and validates an attempt-scoped local artifact, persists the validation report, and atomically closes its Attempt while leaving the Stage in VALIDATED.
2. The publication task independently claims a publication Attempt from VALIDATED, publishes only the validated artifact, performs remote readback/manifest checks, and atomically finishes the Stage.

This requires an explicit repository transition for completing a processing Attempt while preserving StageState.VALIDATED, plus a well-defined claim transition from VALIDATED to PUBLISHING. Do not implement this by weakening existing state guards or by marking the whole Stage finished before publication.

## Current verified boundary

PR #123 introduces lease columns, hashed one-time tokens, `claim_attempt()`, `renew_attempt_lease()`, fenced repository writes, and bounded expired-lease recovery. Its CI verifies repository-level behavior. It does not yet change live Celery task call sites or fence external side effects.

The shared FFmpeg process supervisor invokes usage callbacks while polling, checks cancellation and deadlines, and terminates the complete process tree when callbacks fail. Playlist generation now uses this supervisor directly rather than the `ffmpeg-streaming` library's internal monitor thread. The playlist timeout is bounded by `FFMPEG_PLAYLIST_TIMEOUT_SECONDS` (default 7,200 seconds); usage polling renews the lease even when FFmpeg emits no progress line. Blocking storage SDK calls still renew from transfer callbacks.

PR #127 now contains runtime code for thumbnail, watermark, and HLS/DASH playlist processing. Those workers claim processing leases, keep tokens process-local, renew from process-usage/progress callbacks, and close the processing Attempt after validation. Thumbnail, watermark, and playlist commands run under the process-tree supervisor; timeout, cancellation, callback failure, or lease loss terminates the full process tree before validation or repository completion.

The single-file and directory publishers independently claim publication Attempts. Single-file output and every HLS/DASH package object use immutable `.attempts/<publication_attempt_id>/...` keys. Package uploads renew from transfer callbacks, preserve manifest-last publication, and commit the authoritative Artifact pointer only under the publication lease. The gRPC result projection resolves AVAILABLE Artifacts into the existing `UploadedTo` bucket/key fields and aggregates metrics across processing/publication Attempts.

Orphan cleanup covers both package and single-file publication Attempts. HLS/DASH Attempts persist an inventory and exact prefix; single-file Attempts persist the exact immutable object key. The repository exposes `is_attempt_reclaimable()`: failed/cancelled Attempts, or retryable Attempts superseded by a newer Attempt, qualify only after the retention grace period; any AVAILABLE Artifact under the exact bucket/prefix blocks deletion. The cleanup helpers require this database-backed predicate and never list or delete a shared prefix. Two Celery tasks are registered: `cleanup_package_attempt` for a known Attempt and `cleanup_abandoned_packages` for the hourly janitor. The janitor runs through `CELERY_BEAT_SCHEDULE` and a dedicated production Circus Beat watcher, using a configurable one-day default retention grace period. Namespace metadata is persisted with each publication Attempt, so candidates are enumerated from PostgreSQL rather than bucket listings. PostgreSQL revalidates each candidate immediately before deletion, and a durable cleanup marker prevents repeat scans. The manual invocation and eligibility rules are documented in the operations runbook added by PR #127. CI passed on PR #127 head `9f9b82411b39fa1c3fb43f4a55c0a98af575132e` through the temporary stacked validation PR #128: PostgreSQL repository tests (Python 3.12), legacy runtime regression (Python 3.9), regression suite (Python 3.12), real FFmpeg thumbnail tests, synthetic HLS/DASH package harness, supervised watermark integration, full gRPC → Celery → FFmpeg → storage VOD integration, documentation links, dependency review, and required-check quality gates. This verifies the current VOD slice in CI; it does not mean it has been deployed or that every legacy workflow path has migrated.

## Required implementation increments

### A. Repository lifecycle contract

- Add an explicit operation to complete a processing Attempt after validation while retaining StageState.VALIDATED.
- Permit a fresh publication Attempt to claim a VALIDATED Stage only when no active Attempt exists and the previous processing Attempt is terminal.
- Define legal state transitions for retrying processing versus retrying publication. A publication retry must not rerun expensive processing when a valid, durable input artifact remains available.
- Ensure every mutation for a leased Attempt requires the matching token and checks expiry at mutation time.
- Preserve compatibility for legacy unleased attempts only during a bounded migration window; do not silently let a leased worker fall back to legacy mutation methods.
- Add PostgreSQL concurrency tests for simultaneous processing claims, simultaneous publication claims, processing completion racing with recovery, and stale worker mutation after publication ownership changes.

### B. Processing worker integration

- Replace `start_attempt()` in each workflow-managed processing task with `claim_attempt()`.
- Use a stable bounded lease owner such as worker identity plus process identity; keep Celery task ID only as external execution metadata.
- Keep the raw token in a local closure/object owned by that task invocation.
- Extend the FFmpeg/FFprobe usage callback to renew the lease at a bounded cadence. A failed renewal raises a dedicated lease-lost exception.
- When lease loss is detected, terminate the complete supervised process tree before any validation, artifact registration, cache status update, or retry scheduling.
- For non-FFmpeg work, use bounded operation timeouts and a heartbeat mechanism that can actually stop or prevent the next side effect. Do not add a background heartbeat thread unless its shutdown, database-session ownership, and cancellation races are tested.
- Store scratch output under an attempt-specific local directory. A retry must never reuse a path that an older still-running process can overwrite.

### C. Publication worker integration

- Claim a distinct publication Attempt inside the upload task; never serialize or forward the processing token.
- Before upload, verify the referenced validation artifact belongs to the expected Job, Stage and completed processing Attempt.
- Upload to an attempt-scoped immutable key, not directly to the stable public key. This is implemented for single-file and HLS/DASH package publishers in PR #127.
- Verify remote object size/readback. For HLS/DASH, verify package dependencies and publish the root manifest last. This is implemented in the package publisher; checksum verification remains a separate enhancement.
- Do not promote by overwriting a shared stable key. Keep immutable attempt-scoped keys and atomically update the authoritative Artifact pointer in PostgreSQL.
- Check lease ownership before every externally visible publication boundary and immediately before final repository completion.
- A stale publisher may leave an unreferenced attempt-scoped object, but it must never overwrite another Attempt's key or commit the authoritative Artifact pointer. PR #127 adds inventory-scoped cleanup guarded by a database predicate; scheduling, retention grace period, and production DB predicate wiring remain outstanding.

### D. Failure and retry semantics

- Lease loss is not an ordinary media failure and must not be recorded as a successful retryable completion by the stale worker.
- Worker cancellation and lease loss both stop the process tree, but remain distinguishable durable outcomes.
- A timeout while renewing the lease is ambiguous: the worker must fail closed and stop side effects; it must not assume the lease is still owned.
- Expired-lease recovery is the only authority that reopens an abandoned leased Attempt. A worker cannot resurrect an expired lease.
- Retry budgets are bounded and shared across Celery retries and durable recovery; prevent an unbounded loop where each redelivery creates a fresh Attempt.
- Never report exactly-once delivery. The target is at-least-once task delivery with one authoritative lease holder and idempotent external effects.

## Acceptance test matrix

| Scenario | Required result |
|---|---|
| Two workers claim the same processing Stage | Exactly one claim succeeds |
| Processing worker renews during a long FFmpeg run | Lease remains live; token is never serialized |
| Processing lease expires while FFmpeg runs | Process tree is terminated; stale worker cannot validate or register artifacts |
| Worker crashes after local output but before validation commit | Recovery creates a new Attempt; partial local output is not trusted |
| Processing completes and upload task is redelivered | Publisher claims independently; processing is not repeated |
| Two publishers race | One owns the publication lease; only its artifact pointer can become authoritative |
| Publisher loses lease during S3 upload | Upload remains attempt-scoped/unreferenced; no stable pointer or stage completion |
| Storage write succeeds but readback fails | Stage remains non-terminal; bounded retry/reconciliation applies |
| Worker crashes after upload but before DB completion | Retry discovers/reconciles deterministic staging object and completes idempotently |
| Cancellation races with heartbeat/publication | No new public artifact becomes visible after cancellation wins |
| Lease renewal/database unavailable | Worker fails closed and stops before another side effect |
| Legacy unleased job executes during migration | Compatibility path is explicit, observable and time-bounded |

## Rollout gates

1. Merge and deploy the repository lease primitives only after their required review and migration-order checks.
2. Implement and test repository processing/publication lifecycle transitions.
3. Complete the playlist-processing and directory-publication integration in PR #127; implementation and CI verification are complete. Deployment still requires the isolated rollout and operational monitoring gates below.
4. Migrate any remaining workflow-managed task families outside this VOD slice and verify no token appears in broker payloads, Redis, task results, logs or events.
5. Keep extending crash/replay and cancellation-race coverage as new side-effect paths are added; the current VOD slice includes stale publisher fencing, post-upload/pre-commit recovery, readback failure, local scratch loss/requeue, and scheduled orphan cleanup with a database-backed retention predicate.
6. Enable for an isolated test cohort; observe lease-renewal failures, expired attempts, publication conflicts and orphan artifacts.
7. Only after all stage classes satisfy the acceptance matrix may Issue #71 outbox dispatch/replay be activated.

## Explicit non-goals

- This ADR does not claim production rollout, complete workflow coverage, a scheduled orphan janitor, or an outbox relay.
- It does not authorize passing raw lease tokens between Celery tasks.
- It does not assume S3-compatible providers all support the same conditional-write semantics.
- It does not claim that repository fencing alone prevents a stale process from writing files or remote objects.

## Related references

- [ADR 0004: Transactional dispatch outbox and idempotent execution plans](0004-transactional-dispatch-outbox-and-idempotent-execution.md)
- [ADR 0005: Modular core and replaceable infrastructure adapters](0005-modular-core-and-replaceable-adapters.md)
- [Reliability and observability engineering standard](../engineering/reliability-observability-standard.md)
- [Issue #71](https://github.com/mojtaba-arvin/video-service/issues/71)
- [PR #123](https://github.com/mojtaba-arvin/video-service/pull/123)
