# ADR 0011: Attempt-scoped scratch disk isolation

- **Status:** Accepted design constraints; runtime backend selection pending worker-host evidence
- **Date:** 2026-11-25
- **Related:** [Issue #72](https://github.com/mojtaba-arvin/video-service/issues/72), [Issue #71](https://github.com/mojtaba-arvin/video-service/issues/71)

## Context

The existing Attempt cgroup v2 integration enforces CPU, memory and PID limits. cgroup v2 does not expose a per-cgroup hard limit for filesystem bytes. The `io.max` controller limits I/O rates; it is not a disk-capacity quota and must not be presented as one.

The legacy media task paths use worker-configured download and processed roots and request/job-derived directories. Those roots are shared by work beyond one live Attempt. Measuring or deleting a shared root cannot establish Attempt ownership, and a periodic directory walk cannot enforce a hard ceiling because a writer can exceed the sampled size between polls.

## Decision

1. **Do not claim disk isolation from cgroup v2.** CPU, memory and PID enforcement remain separate from disk-capacity enforcement.
2. **Do not use periodic `du`/directory-size polling as a hard quota.** It may later provide best-effort telemetry only after the measured tree is exclusively owned by one Attempt.
3. **Make scratch ownership Attempt-scoped.** Every transient download and intermediate must live beneath a unique Attempt root. Durable/reusable artifacts and final outputs must be stored separately and must not be removed by scratch cleanup.
4. **Enforce capacity at the filesystem boundary.** The runtime adapter may use a filesystem project quota (for example, a supported XFS/ext4 project-quota mount) or a per-Attempt bounded filesystem/volume. The concrete backend must be selected only after inspecting the actual worker filesystem, mount options, container privileges and quota support.
5. **Fail closed when enforcement is requested but unavailable.** There is no silent fallback to a shared unbounded directory or sampled-size check.
6. **Constrain cleanup to the owning Attempt.** Resolve and validate the Attempt root beneath the configured scratch root, reject symlink/path escapes, and remove it only after the Attempt process cgroup is confirmed empty. Never recursively delete a worker-wide or request-wide shared root.

## Required runtime contract

The implementation must expose:
- a stable Attempt scratch-root identity;
- configured hard byte limit and the enforcement backend/capability result;
- current and high-water allocated bytes when the backend can report them;
- a typed quota-exceeded outcome;
- cleanup result and failure reason.

Unknown or unsupported measurements remain unknown, not zero. High-water telemetry is not a substitute for an enforced quota.

## Acceptance tests

- Two concurrent Attempts cannot read, write, account or clean each other's scratch files.
- A process writing beyond the configured byte limit is blocked or fails with a typed quota-exceeded outcome before exhausting the worker filesystem.
- A retry gets a new scratch root and cannot delete a prior Attempt's committed/reusable artifacts.
- Cancellation and worker loss leave no unbounded process writing into the scratch tree; cleanup waits for process-tree termination.
- A symlink or path traversal cannot redirect cleanup outside the configured scratch root.
- A worker mount without the selected quota capability fails closed when a hard limit is configured.
- CI exercises the actual filesystem quota backend where privileged Linux support is available; mocks alone are not acceptance evidence.

## Rollout

No disk-quota feature flag is enabled by this ADR. Keep `ATTEMPT_CGROUP_V2_ENABLED=false` until the production delegated cgroup root/controllers are verified, and keep disk enforcement disabled until the worker filesystem backend passes the acceptance tests on the production-shaped host.
