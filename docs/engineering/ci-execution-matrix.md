# CI execution matrix and maintenance policy

The workflow at `.github/workflows/ci.yml` is designed around two principles:

1. Run the checks that prove the changed behavior, not the same setup repeatedly.
2. Make one stable aggregate check responsible for deciding whether the change
   class has passed.

This document describes the workflow on `develop`; it is not a promise that a
CI run proves production deployment or performance.

## Change classification

The `Detect changes` job always runs first. It compares the PR base/head or
push-before/current commit and produces two outputs:

- `runtime`: any code, workflow, dependency, build, configuration, test,
  fixture, or unclassified repository change.
- `docs`: documentation or agent-guidance content requiring link validation.

The classifier is intentionally fail-closed. If the diff base cannot be
resolved or no changed paths are returned, it enables both check classes.

A documentation-only change is limited to the explicit documentation paths
in the workflow, such as `docs/**`, README/CONTRIBUTING/SECURITY files,
`AGENTS.md`, agent skills/profiles, Copilot instructions, PR templates and
issue templates. Changes to a workflow, script, dependency file, application
source, test, fixture or other unknown path trigger the runtime checks.

## Execution matrix

| Change class | Jobs that run | Why |
|---|---|---|
| Docs-only | Detect changes → Documentation links → Quality gates | Validate changed repository-local Markdown targets without provisioning Python, FFmpeg, PostgreSQL or Redis |
| Runtime-only | Detect changes → Python 3.9 regression; Python 3.12 regression; VOD integration (Python 3.9); PostgreSQL repository tests (Python 3.12) → Quality gates | Preserve both Python test lanes and verify real media, persistence and integration behavior |
| Runtime + docs | All runtime checks plus Documentation links → Quality gates | A Markdown change does not suppress runtime checks when code/configuration also changed |
| Unknown/empty diff | Both change classes | Avoid accidentally skipping required checks |

### Runtime checks

**Legacy runtime regression — Python 3.9**

Runs the full standard-library test discovery and repository hygiene checks.
FFmpeg is not installed here; tests that require actual media binaries run in
the dedicated media job instead.

**Regression suite — Python 3.12**

Runs the full regression suite under the forward-compatibility test dependency
set. This is a compatibility test lane, not proof that the complete container
and deployed service have migrated from Python 3.9.

**VOD integration — Python 3.9**

Owns the single FFmpeg installation and the Redis/PostgreSQL service
containers. It runs:

- sensitive logging tests with runtime dependencies;
- thumbnail rendering and output-validation tests using real FFmpeg/FFprobe;
- synthetic HLS/DASH package generation and reference validation;
- PostgreSQL migrations and the Python 3.9 durable-workflow repository suite;
- the gRPC → Celery → FFmpeg → HLS/thumbnail → S3-compatible VOD integration
  test.

The synthetic HLS/DASH harness generates its own temporary source corpus.
There is no separate pre-generation step that repeats this work.

**PostgreSQL repository compatibility — Python 3.12**

Runs the durable workflow repository tests against a dedicated PostgreSQL
service under Python 3.12. The Python 3.9 repository tests share the existing
PostgreSQL service in the VOD integration job, avoiding a second identical
database container.

### Documentation links

The offline validator checks repository-local relative/root-relative Markdown
targets in changed Markdown files. External URLs and in-page anchors are not
fetched; that is intentional so the check remains deterministic and fast.
When the commit range is unavailable, it validates all tracked Markdown files.

## Required check and merge policy

The branch-protection contract is the single aggregate status check named
**Quality gates** from the **CI** workflow.

Do not require the conditional leaf jobs individually. A docs-only PR is
supposed to skip the Python/FFmpeg/PostgreSQL jobs; a runtime PR is supposed to
run them. The aggregate inspects their results according to the detected
change class and fails if any required job is failed, cancelled, or otherwise
not successful.

Dependency Review remains a separate security signal. It should be added as a
required check only after its behavior is confirmed for same-repository and
fork-originated PRs.

The current repository ruleset/branch protection still requires repository
owner/admin action. The connected GitHub workflow tooling used for engineering
changes does not expose a branch-ruleset write action. Once an administrator
configures protection for `develop`, require PRs, require `Quality gates`,
require resolved conversations and linear history, and block force pushes and
branch deletion. Do not set `Unit tests (Python 3.9)` or any other conditional
leaf job as an additional required check.

## Runtime and cost guardrails

- Third-party GitHub Actions are pinned to full commit SHAs.
- Workflow permissions default to read-only and are scoped per workflow.
- Same-PR superseded runs are cancelled; unrelated PRs have separate concurrency
  groups and must not cancel each other.
- Pip caches are keyed by the corresponding pinned requirement files.
- FFmpeg is installed only in the media integration job and only when the
  runner does not already have both FFmpeg and FFprobe.
- Generated media stays in temporary directories and is not committed or
  uploaded as an Actions artifact unless a future benchmark requires it.
- Preserve test coverage when improving speed. First remove duplicated setup,
  redundant service containers and irrelevant test categories.

## How to evaluate future CI changes

For any CI optimization, compare a fixed set of equivalent PRs:

1. docs-only;
2. one Python-only/runtime change;
3. a media-processing change;
4. a database/migration change;
5. a mixed code-and-docs change.

Record wall-clock time by job, queued time, cache hit/miss, runner minutes and
which test groups actually executed. A time-saving claim needs those
measurements, not just fewer YAML lines.

Before merging workflow changes, verify:

- docs-only changes skip runtime jobs but still pass `Quality gates`;
- code/runtime changes cannot skip either Python lane, the VOD integration
  path or PostgreSQL compatibility;
- changes to `.github/workflows`, requirements, scripts, Dockerfiles,
  migrations, protobufs, or tests are classified as runtime;
- missing diff metadata fails closed;
- cancellation affects only superseded runs for the same PR/ref;
- a failed/cancelled required leaf job makes `Quality gates` fail.
