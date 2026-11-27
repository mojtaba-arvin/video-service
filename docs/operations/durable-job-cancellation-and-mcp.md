# Durable Job cancellation and MCP interface

- **Status:** Pending CI and repository review
- **Scope:** Durable Job cancellation and a typed, authenticated MCP adapter
- **Depends on:** status application service (PR #198), Attempt leases, transactional outbox, OIDC/JWKS authentication

## Cancellation state machine

Job and Workflow cancellation are two-phase:

`QUEUED | RUNNING -> CANCEL_REQUESTED -> CANCELLED`

`FINISHED`, `FAILED`, and `CANCELLED` remain terminal. Repeated requests are idempotent. A request against an already terminal Job does not rewrite its outcome.

The cancellation request is one database transaction. It:

1. Locks Stage rows in ordinal order, then locks the Job row. This preserves the Stage-before-Job lock order used by claims, completion and Attempt acknowledgement.
2. Authorizes `video.jobs:cancel` against the immutable tenant, issuer and subject ownership tuple before changing state.
3. Moves Job and Workflow to `CANCEL_REQUESTED` and appends `JOB_CANCELLATION_REQUESTED`.
4. Marks stages without a running Attempt as `CANCELLED`, appending `STAGE_CANCELLED`. A Stage with a running Attempt remains active until its lease owner acknowledges termination.
5. Moves pending and processing outbox rows to `DEAD`, clears relay lease material, and records `job_cancel_requested`. A message already accepted by a broker can still arrive, but its consumer cannot claim a new Attempt after the Job state transition.

The request does not revoke or clear an active Attempt lease. The lease owner must terminate its process tree and call the existing lease-fenced `cancel_attempt()`. The acknowledgement persists Attempt and Stage cancellation, usage accounting, and lease cleanup. A worker that independently detects cancellation may initiate the same Job-wide request while holding its valid Attempt lease.

The Job becomes terminal `CANCELLED` and emits `JOB_CANCELLED` only when no `RUNNING` Attempts remain. If the last worker disappears, expired-lease recovery acknowledges cancellation instead of retrying the node. Completion and failure paths also check `CANCEL_REQUESTED`; they cannot publish a successful result, enqueue a retry, or resurrect the Job. Claim paths reject cancellation-requested Jobs.

This deliberately separates the operator's durable request from worker process termination. Redis force-stop markers and Celery revoke remain compatibility signals for legacy workers, not the source of truth for durable execution.

## MCP transport and authorization

The MCP adapter uses the official Python SDK over **Streamable HTTP only**. It does not expose stdio, because stdio does not carry the HTTP bearer-authentication boundary. The resource server validates JWTs through the existing configured OIDC/JWKS authenticator, with a dedicated MCP audience and the exact configured resource URL. The SDK's resource check is enabled.

Configure:

- `MCP_RESOURCE_URL`: exact externally visible HTTPS URL ending in `/mcp` (HTTP is allowed only on loopback for local development)
- `MCP_OIDC_AUDIENCE`: JWT audience for this MCP resource; defaults to `MCP_RESOURCE_URL`
- `MCP_HOST`, `MCP_PORT`: bind address and port
- `MCP_ALLOWED_HOSTS`: comma-separated exact Host values, including ports
- `MCP_ALLOWED_ORIGINS`: comma-separated allowed browser origins; empty disables browser-origin access

MCP submissions fail closed unless both `WORKFLOW_DURABLE_DISPATCH_ENABLED` and `TENANT_JOB_QUOTA_ENFORCEMENT_ENABLED` are true and the caller's tenant has a provisioned quota policy. MCP never overwrites existing output keys or creates buckets; outputs are create-only until a separately reviewed approval contract exists.\n\nThe deployment must also configure the existing `OIDC_ISSUER`, `OIDC_JWKS_URI`, `OIDC_ALLOWED_ALGORITHMS`, tenant/subject/scope/role claim names and clock/JWKS timeouts. Tokens must carry the MCP audience and the required per-tool scopes. The adapter converts only verified token claims into the shared `RequestPrincipal`.

Install the optional Python 3.12+ dependencies with `pip install -r requirements-mcp-py312.txt`, then run `python -m video_streaming.mcp` from the application environment. Terminate TLS at a trusted reverse proxy for non-loopback deployments and configure the exact Host/Origin allowlists. Do not expose the service directly to an untrusted network with the local defaults.

## Typed tools

| Tool | Required scope | Behavior |
|---|---|---|
| `video_probe` | `video.jobs:create` | Checks, downloads and probes one input; returns an asynchronous Job ID |
| `video_thumbnail` | `video.jobs:create` | Creates one thumbnail with typed timestamp and dimensions |
| `video_transcode` | `video.jobs:create` | Creates an HLS or DASH workflow with bounded codec/quality fields |
| `video_job_get` | `video.jobs:read` | Uses the shared `GetJobStatusService`; returns a sanitized status projection |
| `video_job_cancel` | `video.jobs:cancel` | Uses the shared `CancelJobService` and durable cancellation contract |

Submission delegates to `VideoOperationService` and `SubmitJobService`, reusing the existing versioned execution-plan validator and `build_vod_execution_plan`. The probe plan uses the existing allow-listed input check/download/analyze operations. Job creation uses the configured tenant quota policy and the transactional outbox. Caller-provided idempotency keys are hashed with the verified tenant/issuer/subject tuple before persistence, preventing cross-tenant collisions. Submission fails closed while `WORKFLOW_DURABLE_DISPATCH_ENABLED` is false; MCP does not bypass the rollout gate.

Tool schemas intentionally omit shell commands, arbitrary FFmpeg arguments, raw execution plans, callback URLs and provider credentials. The existing bounded resource profiles and retry policies are selected by the application service; callers cannot raise limits or request an overwrite. The status projection omits artifact URIs, event payloads, secret references and raw metadata. Tenant ownership and per-operation scopes remain enforced by the application services and repository, not by MCP tool descriptions.

## Compatibility

The gRPC revoke adapter authorizes every Job in the batch before side effects, then invokes `CancelJobService` before setting the legacy Redis force-stop marker and sending the Celery revoke signal. gRPC status maps `CANCEL_REQUESTED` to its existing revoked projection. The existing status-service implementation in PR #198 remains the shared status read path and is not copied into the MCP adapter.

## Required verification

CI must verify cancellation authorization and tenant isolation, duplicate requests, queued and active Jobs, claim/cancel races, outbox suppression, lease-fenced acknowledgement, retry suppression, expired-worker recovery, quota enforcement, MCP token/resource validation, tool schemas, asynchronous Job IDs, and gRPC compatibility. The branch is stacked on PR #198 and must not be merged into the default branch until PR #198 is merged and exact-head checks and repository review gates pass.
