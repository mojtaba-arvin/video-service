# ADR 0010: Celery worker integration for durable Attempt leases

- **Status:** Partially implemented in PR #127; verification pending
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

The existing FFmpeg process supervisor invokes usage callbacks while polling. This is a suitable heartbeat hook for long-running supervised media commands, but it is not sufficient for blocking storage SDK calls or other operations that do not return to the polling loop.

PR #127 now contains runtime code for thumbnail, watermark, and HLS/DASH playlist processing. Those processing workers claim processing leases, keep tokens process-local, renew from FFmpeg progress callbacks, and close the processing Attempt after validation. If renewal fails inside the FFmpeg callback, the callback terminates the process tree before propagating the failure.

The single-file and directory publishers independently claim publication Attempts. Single-file output and every HLS/DASH package object use immutable `.attempts/<publication_attempt_id>/...` keys. Package uploads renew from transfer callbacks, preserve manifest-last publication, and commit the authoritative Artifact pointer only under the publication lease. The gRPC result projection resolves AVAILABLE Artifacts into the existing `UploadedTo` bucket/key fields and aggregates metrics across processing/publication Attempts.

The package publisher also exposes a guarded orphan-cleanup primitive. It only reads an Attempt's inventory and deletes keys inside that Attempt namespace, and it requires a caller-supplied database-backed reclaimability predicate. The repository now exposes `is_attempt_reclaimable()`: only failed/cancelled Attempts, or retryable Attempts superseded by a newer Attempt, can qualify after the grace period; any AVAILABLE Artifact under the exact bucket/prefix blocks deletion. The cleanup helper requires this database-backed predicate. Two Celery tasks are registered: `cleanup_package_attempt` for a known Attempt and `cleanup_abandoned_packages` for the scheduled janitor. The janitor runs hourly through `CELERY_BEAT_SCHEDULE` and a dedicated Circus Beat watcher, using a configurable one-day default retention grace period. Publication namespace metadata is persisted with each package Attempt so the janitor can enumerate candidates without listing shared S3 prefixes. PostgreSQL revalidates each candidate immediately before deletion. The manual invocation and eligibility rules are documented in the operations runbook added by PR #127. The code and tests are present in PR #127, but the branch has not been executed in CI or locally; none of these behaviors is yet verified by a passing test run.

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
3. Complete the playlist-processing and directory-publication integration in PR #127; run PostgreSQL plus real FFmpeg/S3-compatible integration tests before enabling it.
4. Extend the integration to every remaining workflow-managed task and verify no token appears in broker payloads, Redis, task results, logs or events.
5. Complete end-to-end stale-worker publication races, crash/replay tests, bounded retry accounting, and scheduled orphan cleanup with a database-backed retention predicate.
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
