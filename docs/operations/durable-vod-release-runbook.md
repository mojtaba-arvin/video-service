# Durable VOD release and rollback runbook

## Status and safety boundary

This runbook describes the release gates for the durable VOD implementation
currently integrated on `develop`. The latest relevant merged baseline is
`8811c9bee0629e8e765122ee01c4005bf9a40f5c` (PR #160); its PR CI run
[45374558976](https://github.com/mojtaba-arvin/video-service/actions/runs/45374558976)
passed the required `Quality gates`, Python 3.9/3.12 regression and full VOD
integration lanes, PostgreSQL workflow tests, and Python 3.12 container build.

This is **not production activation approval**. The durable dispatch feature
gate defaults to false, and the gRPC `CreateJob` path continues to use
`legacy_celery`. Do not start the relay, schedule its Beat task, enqueue
durable dispatch, or change the feature gate as part of a routine deployment.

CI uses ephemeral PostgreSQL/Redis and test doubles for S3/KMS adapters. It
does not verify a live AWS account, IAM trust/policies, KMS key policy,
production bucket configuration, network egress, or operational alarms.

## Gate 0 — immutable release candidate

Before any environment work:

1. Record the exact release commit and image digest. Do not deploy a moving
   branch or tag.
2. Confirm the release candidate's CI and Dependency Review are successful
   for that exact commit.
3. Confirm the deployed API, worker, migration job, and scheduler images are
   built from compatible revisions.
4. Keep `WORKFLOW_DURABLE_DISPATCH_ENABLED=false`. Keep CreateJob on
   `legacy_celery`.
5. Confirm operators can reach PostgreSQL, broker, object storage, KMS/STS
   diagnostics, and redacted service logs without granting broad application
   privileges.

A passing CI run on a different SHA is not evidence for this candidate.

## Gate 1 — database and migration rehearsal

Use the same release image and migration command intended for production:

```sh
cd video-streaming
alembic -c alembic.ini upgrade head
```

Run this first against a production-like staging database restored from a
sanitized backup. Record the starting and ending Alembic revisions, duration,
lock impact, row counts, and rollback/forward-recovery decision.

The workflow migrations are additive, but that alone does not prove a safe
production migration. Verify schema/index creation, database capacity,
connection limits, backup/restore, and compatibility with the currently
deployed legacy API and workers.

Do not downgrade by dropping workflow or outbox tables after any durable rows
have been written. Migrations 0006–0010 now fail closed before removing tenant
ownership, encrypted Job secrets, Attempt lease state, Outbox rows, or workflow
dependency edges (PR #160). The PostgreSQL CI lane now also creates a separate,
empty rehearsal database and executes the complete Alembic chain:
`upgrade head → downgrade base → upgrade head`. It verifies that representative
workflow tables are absent at base and that the final revision is
`0011_tenant_job_quotas`. This proves empty-database schema-chain
reversibility on PostgreSQL; it does **not** prove rollback with populated
production data, deployment rollback safety, or application compatibility with
a downgraded schema. Prefer rolling application components back while
retaining additive state, then use a reviewed forward migration if correction
is needed.

## Gate 2 — accepted legacy Job and callback inventory

Before any future dispatch cutover, produce an auditable inventory of Jobs
accepted by the legacy path, including at minimum:

- Job identity, tenant ownership, creation/acceptance time and terminal state;
- queued/running/retrying work and outstanding Celery task identifiers;
- callback URL/secret representation and whether delivery is still pending;
- input/output object references and publication state;
- cancellation state and any external side effects already completed.

Define and rehearse explicit handling for each non-terminal category:
finish on the legacy path, safely drain, or migrate through a reviewed,
idempotent conversion. Never infer that persisted plan snapshots mean a
legacy Job has already been dispatched durably. Do not copy plaintext callback
URLs into migration reports, logs, task arguments, or tickets.

Cutover is blocked until there are no ambiguous in-flight Jobs and the
reconciliation report is reviewed. The runbook does not prescribe an automatic
bulk migration because that requires environment-specific evidence and
business policy.

## Gate 3 — AWS identity, KMS, and storage validation

Use a dedicated non-production AWS account or isolated staging resources and
least-privilege roles. Do not use production secrets in CI or paste credentials
into command history.

Validate and retain redacted evidence for:

1. API workload identity can call STS and has only the required
   `kms:GenerateDataKey` permission on the configured key.
2. The callback worker assumes the exact configured role and passes
   `GetCallerIdentity`; the role has `kms:Decrypt` only for the intended key.
3. KMS key policy and IAM policy enforce the required encryption-context
   conditions (`purpose=webhook.callback` for destination URLs and
   `purpose=webhook.signing` for HMAC keys, `algorithm=AES-256-GCM`, and
   required identity/context keys).
4. Wrong role, wrong key, missing context, revoked/disabled key, STS outage,
   and KMS denial fail closed. No plaintext fallback is allowed.
5. S3 permissions cover only required buckets/prefixes and operations.
   Conditional no-replace writes work for both single-object and multipart
   completion paths; conflicting bytes fail closed.
6. Read-back checksum/size checks, versioned package prefixes, cleanup policy,
   and bucket lifecycle/retention behavior match the operational policy.
7. Network egress and callback URL validation prevent access to loopback,
   private, link-local, and metadata destinations except explicitly isolated
   test configurations.

Record account/region, role names, key ARN aliases, policy revision, test
identity, and sanitized request IDs. Never record access keys, plaintext
callback URLs, ciphertext payloads, or decrypted data keys.

## Gate 4 — broker and worker recovery rehearsal

In staging, exercise the actual deployed broker and worker topology, not only
the isolated CI harness:

- relay process loss before and after broker acceptance;
- broker outage and recovery;
- duplicate dispatch and concurrent consumers;
- worker process termination while an Attempt lease is RUNNING;
- lease expiry, redelivery, retry budget exhaustion and DEAD-letter handling;
- Job cancellation before dispatch and before irreversible side effects;
- webhook timeout and crash after remote 2xx but before local receipt commit.

The expected delivery contract is at-least-once. Consumers and side-effect
adapters must enforce durable claims and stable idempotency. A Celery task ID
is not a deduplication guarantee. Webhook receivers must deduplicate
`Idempotency-Key` to avoid duplicate remote effects.

The current OS-process acceptance test uses explicit redelivery after SIGKILL;
it does not prove automatic broker requeue/visibility-timeout behavior. This
staging gate must validate the selected broker's real redelivery semantics.

## Gate 5 — observability and operator readiness

Before activation, define measured baselines and alert thresholds for:

- pending outbox count and oldest pending age;
- relay publish, retry, lease-expiry and DEAD counts;
- workflow queue age, Attempt duration, retry rate and lease expiry;
- per-operation failure rate and cancellation latency;
- S3 publication conflicts, checksum/read-back failures and orphaned prefixes;
- KMS/STS authorization failures and webhook delivery outcomes.

Verify redaction in logs/traces/metrics. Run the documented outbox CLI
(`stats`, `dead`, `inspect`, audited `replay`) with an operator identity.
Manual replay is only for inspected DEAD rows with a recorded incident/ticket
reason; never replay poison messages blindly.

Assign an on-call owner, escalation path, dashboard, alert routing, retention
policy, backup owner, and a tested incident procedure.

## Gate 5A — Linux cgroup v2 process-isolation acceptance

The required CI job `Linux cgroup v2 kernel acceptance (Python 3.12)` runs on
an Ubuntu 24.04 host as root. It creates a dedicated child cgroup, enables only
the CPU and memory controllers needed by the test, and exercises the actual
kernel interface through `AttemptCgroupV2` and the process supervisor.

The test verifies CPU quota throttling via `cpu.stat`, memory-limit OOM
accounting via `memory.events`, termination of a detached descendant via
`cgroup.kill`, and refusal to remove a populated cgroup. The cgroup is removed
only after the kernel reports `populated 0`.

This is evidence for the CI host's Linux kernel and delegated cgroup setup; it
does not prove the production host has the same delegation or limits. Before
enabling `ATTEMPT_CGROUP_V2_ENABLED` in production, provision and verify the
service user's delegated root and controller policy on the actual worker host.
PID admission and disk quotas remain unimplemented and must not be claimed as
enforced. The durable Workflow executor also remains a separate integration
gate until its Attempts are wrapped in the same cgroup lifecycle.

## Gate 6 — staged activation (separate approval)

Only after Gates 0–5 are evidenced and reviewed:

1. Approve a written cutover plan and maintenance/rollback window.
2. Confirm all accepted legacy Jobs are reconciled and no callback/side effect
   can be lost or duplicated by the cutover.
3. Deploy compatible API, worker, scheduler and migration revisions with the
   feature gate still off.
4. Enable durable dispatch for a controlled staging cohort only; monitor
   outbox, worker, artifact and webhook signals.
5. Verify end-to-end create, execute, publish, callback, cancellation, retry,
   duplicate delivery, and recovery behavior.
6. Expand only after explicit release approval and successful observation.
7. Keep a tested means to stop new durable dispatch while allowing in-flight
   work to drain safely.

No activation is authorized by this document or by green CI alone.

## Rollback and stop conditions

Stop new durable dispatch immediately if plan/handler mismatch, unexpected
duplicate side effects, unexplained outbox growth, repeated lease churn,
secret-resolution failures, publication corruption, or unbounded retry/dead
letter growth occurs.

Do not delete outbox rows, workflow plans, Attempts, or artifact references to
make rollback appear clean. Preserve evidence, disable new durable dispatch,
allow or deliberately reconcile in-flight work, and roll back compatible
application components. Keep additive schema until a separately reviewed
retention/migration decision is made.

## Evidence record

For each gate, attach: exact commit/image digest, environment and region,
timestamp, operator, command/test procedure, redacted output or dashboard,
pass/fail decision, linked incident/change ticket, and reviewer. Mark a gate
complete only when evidence exists; a checklist tick without evidence is not
approval.
