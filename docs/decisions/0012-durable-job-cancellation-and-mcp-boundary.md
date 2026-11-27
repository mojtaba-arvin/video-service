# ADR 0012: Durable Job cancellation and MCP operation boundary

- **Status:** Proposed; implementation and operational gates pending
- **Date:** 2026-11-28
- **Related:** [Issue #76](https://github.com/mojtaba-arvin/video-service/issues/76), [Issue #73](https://github.com/mojtaba-arvin/video-service/issues/73), [PR #198](https://github.com/mojtaba-arvin/video-service/pull/198), [ADR 0009](0009-provider-neutral-grpc-identity-and-resource-authorization.md), [ADR 0010](0010-celery-attempt-lease-worker-integration.md), [ADR 0004](0004-transactional-dispatch-outbox-and-idempotent-execution.md)

## Context

The status application service is being extracted in PR #198. Its `GetJobStatusService`
authorizes the complete batch before loading durable snapshots; the gRPC adapter
continues to map those snapshots into protobuf and Redis-compatible responses.
This ADR treats that service as the existing implementation boundary. It must
not be copied into an MCP-specific service.

The repository already has a lease-fenced `WorkflowRepository.cancel_attempt()`.
That method is a worker acknowledgement for one active Stage/Attempt. It clears
the Attempt lease, records `STAGE_CANCELLED`, and currently may transition the
owning Job/Workflow to `CANCELLED`. It is not a caller's durable Job-level
cancellation request.

The current gRPC revoke adapter authorizes a Job, writes a Redis force-stop
marker, and sends a best-effort Celery revoke. It does not atomically update
durable Job state, cancel pending Stages, or suppress durable outbox dispatch.
The existing outbox has `PENDING`, `PROCESSING`, `PUBLISHED`, and `DEAD`
states. Redis and Celery are transports/projections, not cancellation authority.

Issue #73 remains open. Production OIDC issuer/audience/JWKS configuration,
complete tenant admission/quota policy provisioning, and operational security
acceptance are not yet proven. Therefore a protocol adapter may be developed
behind a disabled feature gate, but public MCP exposure is not authorized by
this ADR.

## Decision

### 1. Job cancellation is a durable request followed by worker acknowledgement

Introduce an explicit non-terminal `CANCEL_REQUESTED` Job and Workflow state.
The public cancellation use case is idempotent and distinct from
`cancel_attempt()`.

| Current Job state | Cancellation request | Durable result |
|---|---|---|
| `QUEUED`, no active Attempts | Accepted | Cancel pending Stages and undispatched outbox rows; finalize Job/Workflow as `CANCELLED` in the same transaction |
| `RUNNING` | Accepted | Set Job/Workflow to `CANCEL_REQUESTED`; cancel pending Stages and suppress pending dispatch; leave active Attempts leased until acknowledged or recovered |
| `CANCEL_REQUESTED` | Repeated request | Return the existing cancellation state; do not append duplicate request events or re-run side effects |
| `CANCELLED` | Repeated request | Return an already-terminal result |
| `FINISHED` or `FAILED` | Request | Do not rewrite the authoritative terminal outcome; return an already-terminal result |

The application result must distinguish `accepted`, `already_requested`,
`already_terminal`, and `termination_pending`. A successful request means
the durable intent committed; it does not claim that every OS process has
already exited.

The cancellation mutation must re-check Job ownership and
`video.jobs:cancel` inside the same repository transaction that changes state.
The adapter must not authorize using a Redis key, caller-supplied tenant ID,
tracking ID, or opaque Job ID alone. Missing, legacy-unowned, and foreign Jobs
retain the same non-enumerating denial behavior.

### 2. Locking, claims, and cancellation races

Keep the existing Stage -> Attempt -> Job lock order used by worker
acknowledgement and lease-fenced mutations. A Job-level cancellation transaction
must acquire affected Stage rows and their active Attempt rows in deterministic
key/ID order before locking the Job row. Outbox rows are locked before these
workflow rows when they are part of the same transaction, matching the relay's
outbox-first claim boundary. All claim, completion, recovery, and cancellation
paths must be reviewed together before implementation; no path may introduce
an inverse lock order.

Every `claim_attempt()` must check the Job state while holding the Job lock,
not only when `require_dependencies` is enabled. A claim that serializes after
`CANCEL_REQUESTED` must fail before creating an Attempt or allowing an
operation side effect. A claim that commits before the cancellation transaction
is an active Attempt and must be included in the cancellation fan-out.

A completion that commits before the cancellation request may win and preserve
its terminal outcome. If cancellation commits first, the worker must observe
the durable cancellation signal before validation, artifact registration,
publication, or another externally visible side effect. Cancellation must not
make an already-published Artifact disappear or rewrite a successful/failed
terminal Job.

### 3. Stage and Attempt behavior

- Pending and retry-wait Stages transition to `CANCELLED` during the request
  transaction. No new retry may be scheduled for a cancellation-requested Job.
- Active processing/publishing Stages remain active until their lease-owning
  worker cooperatively stops and calls the existing lease-fenced
  `cancel_attempt()`. The raw lease token remains process-local and is never
  sent through MCP, gRPC, Redis, broker payloads, logs, or events.
- Refactor `cancel_attempt()` so one worker acknowledgement does not mark a
  multi-Attempt Job terminal while sibling Attempts remain active. It records
  the Stage/Attempt cancellation and finalizes the Job/Workflow only after no
  active Attempts remain and all non-terminal Stages are cancelled.
- If an Attempt lease expires after Job cancellation was requested, recovery
  records a cancelled Attempt/Stage rather than reopening the Stage as
  retryable. Recovery remains the authority for an abandoned lease; a stale
  worker cannot acknowledge or mutate after lease expiry.
- A cancellation request never resets a retry budget or reopens a terminal
  Stage/Job. A cancellation-requested Job is not eligible for retry, replay,
  or dispatch.

### 4. Outbox and legacy Celery behavior

Extend the durable outbox state constraint with an explicit `CANCELLED`
state through a forward-only migration. In the cancellation transaction,
pending rows are cancelled and processing rows are fenced by clearing their
lease fields and moving them to `CANCELLED`; published rows remain historical
facts and dead rows remain dead. The relay must reject a publish/ack attempt
whose row is no longer in its claimed state with the same lease token.

A relay may already have handed a message to the broker when cancellation
commits. This unavoidable in-flight race is safe only if the worker's durable
claim checks Job state and refuses to create an Attempt or execute side
effects after `CANCEL_REQUESTED`. Do not claim exactly-once dispatch.

The gRPC revoke adapter must call the same Job cancellation application
service as MCP. After the durable commit, it may set the legacy Redis force-stop
marker and send Celery revoke as best-effort acceleration for already-running
legacy workers. Those signals are not authoritative and their failure must not
roll back the durable request. Preserve protobuf response shapes and existing
status projection mapping. Do not introduce an authorization bypass for
legacy-unowned Jobs.

### 5. Durable events and idempotency

Reuse append-only `WorkflowEventRecord`; do not create a second audit ledger.
Record bounded, structured events:

- `JOB_CANCEL_REQUESTED`: one event for the first successful transition,
  with the authenticated actor identity represented by safe stable fields
  and a bounded reason code (never raw tokens or arbitrary request metadata).
- `STAGE_CANCELLED` and `ATTEMPT_CANCELLED`: worker/recovery acknowledgements
  tied to the Stage and Attempt.
- `JOB_CANCELLED`: one terminal event when all active Attempts are terminal
  and all Stages are terminal.

Serialize request/finalization through the Job row lock and check the current
state before appending events so duplicate requests, acknowledgements, and
recovery cannot append duplicate terminal transitions. Event payloads must not
contain provider credentials, signed URLs, media paths, raw FFmpeg arguments,
or bearer tokens.

### 6. Shared application services and MCP boundary

Keep the transport-neutral application contracts in
`video_streaming.workflows.application`:

- `SubmitJobService` remains the only submit/admission path; it owns the
  existing authorization, ProjectRevision scope checks, idempotency lookup,
  and durable Job creation.
- `GetJobStatusService` from PR #198 remains the only status-read path.
- Add a `RequestJobCancellationService` that accepts an explicit trusted
  principal and Job ID, delegates to the durable repository transaction, and
  returns a typed cancellation result. It must not import gRPC, MCP, Celery,
  Redis, or protobuf modules.
- Typed operation submission must use the existing versioned operation and
  execution-plan contracts. MCP must never create Celery signatures or access
  repositories directly.

The initial versioned MCP tool surface is limited to:

| Tool | Contract |
|---|---|
| `video_probe` | Typed input reference and bounded probe options; return bounded metadata or a durable Job ID when execution exceeds the synchronous probe budget |
| `video_thumbnail` | Typed input reference, bounded timestamps/count and output profile; return a durable Job ID |
| `video_transcode` | Allow-listed codec/container/profile/preset/quality options; return a durable Job ID |
| `video_job_get` | Reuse `GetJobStatusService`; return a bounded status snapshot and opaque Artifact references |
| `video_job_cancel` | Reuse `RequestJobCancellationService`; return durable cancellation state, never claim process termination before worker acknowledgement |

Tool schemas are deterministic, explicitly versioned, bounded, and filtered
only by the verified principal's scopes. Re-check resource authorization on
every call; a Job ID or Artifact reference is not a capability. Long-running
operations return durable Job IDs immediately and clients poll status with
bounded requests. Do not expose shell commands, arbitrary FFmpeg arguments or
filter graphs, filesystem paths, unrestricted URL fetching, provider
credentials, raw bearer tokens, or media blobs.

The MCP adapter authenticates its own inbound resource audience and constructs
the same immutable request principal used by application services. It must not
forward the inbound token to storage, Celery, or another provider. For HTTP
transport, implement the selected MCP authorization specification and validate
issuer, audience, expiry, allowed signing algorithms, and required claims
against configured trust. Do not infer identity or tenant from MCP connection
state or caller arguments.

### 7. Quotas, audit, and exposure gates

Before exposing any submit tool, verify that quota admission is atomic with
durable Job creation and occurs before dispatch. Reuse tenant quota policies
and audit records; do not add an MCP-local quota counter. A missing policy or
quota overflow fails closed with a stable, non-sensitive error. Do not enable
`TENANT_JOB_QUOTA_ENFORCEMENT_ENABLED` until the active-tenant policy
inventory and legacy ownership reconciliation required by Issue #73 are
complete.

Keep the MCP server/route feature flag disabled by default. Public exposure
requires all of the following evidence:

1. Issue #73's production OIDC issuer, audience, JWKS rotation and TLS
   configuration are verified operationally; no anonymous fallback exists.
2. Every active tenant has an explicit quota policy and the admission path is
   tested to reject overflow before dispatch.
3. Shared application authorization and non-enumerating tenant isolation pass
   through gRPC and MCP adapters.
4. Durable cancellation migration, claim/cancel/lease-recovery races, outbox
   suppression, terminal event idempotency, and gRPC compatibility pass.
5. The MCP client integration verifies versioned deterministic discovery,
   typed schemas, asynchronous Job IDs, bounded polling, cancellation results,
   and denial for foreign/legacy-unowned Jobs.
6. Required CI, Dependency Review, repository review, exact-head checks, and
   staging rollback/operational acceptance pass. No skipped required check is
   treated as success.

## Required test matrix

- Cancellation before dispatch: pending Stages and outbox rows become
  cancelled; no Attempt is claimed.
- Concurrent claim/cancel: either claim commits first and is acknowledged as
  active, or cancellation commits first and claim is rejected; no third state.
- Concurrent cancellation requests: one request event and one terminal event.
- Multiple active Attempts: first acknowledgement does not finalize the Job;
  final acknowledgement/recovery does.
- Cancellation during retry wait: no retry or replay is scheduled.
- Lease expiry after request: recovery cancels rather than retries; stale token
  cannot mutate the Attempt.
- Relay claimed before cancellation: publish acknowledgement is fenced; any
  already-delivered message cannot claim an Attempt.
- Completion racing with cancellation: the transaction that serializes first
  determines the durable outcome without terminal-state rewriting.
- Tenant isolation: foreign, expired, wrong-scope, missing, and legacy-unowned
  Job IDs are denied without revealing existence or changing quota/state.
- Quota: missing policy and overflow fail before dispatch; successful admission
  is atomic with Job creation and auditable.
- gRPC: protobuf revoke response compatibility, batch authorization before
  side effects, and best-effort Celery/Redis acceleration after durable commit.
- MCP client: stable versioned schemas, scope-filtered discovery, async Job IDs,
  status polling, cancellation idempotency, bounded arguments and output
  sanitization.

## Consequences and rollout

This is a design prerequisite, not evidence that durable cancellation or an MCP
server exists. Implementation must proceed in separate reviewable increments:
(1) durable cancellation state machine and repository migration/tests;
(2) shared cancellation application service and gRPC adapter migration;
(3) typed MCP adapter and client integration behind a disabled flag;
(4) production OIDC/quota/staging acceptance and explicit enablement approval.

PR #198 remains the sole owner of the status-service extraction. The follow-up
must build on its branch/merge result and must not copy, rewrite, or silently
change its implementation.
