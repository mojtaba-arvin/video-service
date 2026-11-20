# ADR 0009: Provider-neutral gRPC identity and resource authorization

- **Status:** Accepted design; implementation pending
- **Date:** 2026-11-19
- **Related:** [ADR 0008 — encrypted Job secret references](0008-job-scoped-encrypted-secret-references.md), [ADR 0007 — immutable execution plans](0007-immutable-execution-plan-persistence.md), [Issue #73](https://github.com/mojtaba-arvin/video-service/issues/73)

## Context

The gRPC server currently creates its server without interceptors. TLS is enabled only when both server certificate and key are supplied; otherwise the listener is insecure. Supplying a CA bundle additionally enables client-certificate authentication, but TLS or mTLS alone does not establish application-level Job ownership or permission.

The public contract exposes create, read, revoke, output-revoke and playback-QoE operations. The current durable workflow schema does not establish a tenant/owner principal for every Job. Caller-provided tracking IDs, reference IDs and metadata are not proof of ownership. Secret resolution in ADR 0008 must not become available until this boundary exists.

The service has no selected external identity provider. The design must therefore define a portable trust contract without assuming a specific vendor or accepting caller-supplied tenant metadata.

## Decision

### 1. Authentication protocol

- Use OAuth 2.0 Bearer access tokens represented as JWTs issued by a configured trusted OpenID Connect issuer.
- Validate signature using keys obtained from the configured issuer's trusted JWKS endpoint; validate the exact configured issuer and audience, expiration, not-before when present, and an explicit allowed-algorithm list. Reject unsigned tokens, algorithm/key confusion, missing required claims, malformed tokens, unknown keys, and failed key refresh.
- Do not discover or trust an issuer from an unverified token. Do not accept an arbitrary JWKS URL supplied by a caller.
- Configuration is explicit: issuer, audience, JWKS URI, allowed algorithms, subject claim, tenant claim and optional role/scope claim mapping. Missing or invalid production configuration fails startup; there is no implicit anonymous production mode.
- Local development and tests may use an explicitly selected test authenticator. It must not be selected by production defaults or silently activated when verification fails.
- mTLS may be used as an additional transport/workload identity control. It does not replace bearer-token validation or resource authorization.

### 2. Principal contract

Authentication produces an immutable request-scoped principal with at least:
- trusted issuer;
- stable subject identifier;
- tenant identifier resolved only from a verified claim or a configured trusted claim-mapping policy;
- normalized scopes/roles;
- authentication method and token expiry.

The principal is attached to the gRPC request context by the authentication interceptor. Application services receive the principal explicitly. They must not parse raw metadata independently.

A caller-supplied tenant_id, owner_id, reference_id, tracking ID or custom metadata is never authoritative. Missing subject or tenant identity is rejected for tenant-scoped operations. No fallback to a global/default tenant is allowed.

### 3. Authorization and ownership

- Define one canonical ownership model for durable Jobs before adding tenant columns to related tables. At minimum, each newly accepted Job has an immutable owner tenant and creator subject.
- Every Job lookup, status read, cancellation, output revoke, artifact read, playback-session creation and QoE report checks the principal against the Job's owner and required action.
- Resource IDs are identifiers, not capabilities. Random IDs and unguessability do not replace authorization.
- Cross-tenant access returns a non-enumerating denial/not-found policy consistently; do not reveal whether another tenant's Job exists.
- Administrative access is a separate explicit permission with auditable reason and must not be inferred from a missing owner.
- Existing Jobs without ownership remain legacy-unowned. Do not silently assign them to a tenant based on reference ID, request metadata, IP address or current caller. Define a bounded migration/admin policy before enabling tenant-only access for them.
- Tenant identity and authorization are persisted with Job creation in the same transaction as durable workflow state.

### 4. Service and transport boundaries

- Apply authentication to every non-health RPC by default. Health endpoints have a separately documented exposure policy and must not reveal sensitive configuration or tenant data.
- Enforce authorization in shared application services as well as the gRPC adapter so future MCP/HTTP adapters cannot bypass it.
- Keep interceptors responsible for authentication and request context, not for embedding business-specific Job lookup rules.
- Authorization must run before secret resolution, quota consumption with side effects, or any operation that reveals resource state.
- All denials and authentication failures use stable gRPC status codes and redacted structured audit events.

### 5. Rollout and compatibility

1. Inventory all RPCs, durable Job creation paths, playback session records and internal/admin callers.
2. Add the principal contract and test authenticator, then the configured OIDC/JWKS verifier and fail-closed server configuration.
3. Add owner tenant/creator subject to new Job records with database constraints for new records; preserve an explicit legacy-unowned state.
4. Thread principal through shared application services and enforce per-operation authorization.
5. Define and execute a bounded legacy Job migration or privileged administrative policy.
6. Only after these steps pass, enable production secret resolution from ADR 0008 and tenant-scoped quotas.
7. Expose MCP or any new public adapter only by reusing the same application authorization services.

No production rollout is complete merely because the interceptor exists. Deployment configuration, issuer/audience mapping, key rotation/cache behavior, migration, observability and operational rollback must be verified.

## Implementation status (2026-11-19)

PR #117 implements a first gRPC vertical for this decision, but it is not
merged and does not complete production rollout:

- Configured OIDC/JWKS bearer verification with explicit issuer, audience,
  JWKS URI, asymmetric algorithm allow-list, required subject/tenant claims,
  and fail-closed verification.
- Request-scoped principal propagation and authentication of every non-health
  RPC; the gRPC listener requires TLS and complete OIDC trust configuration.
- Durable Job owner tenant, issuer and creator subject are persisted during
  Job creation. Job reads, cancellation, output revocation, playback-session
  creation/events and QoE reads enforce the relevant scope and owner boundary.
- Batch reads/cancellations are authorized in full before reads or side effects.

Remaining gates: PR #117 must pass all required CI and Dependency Review checks;
the deployed issuer, audience, claim mapping, JWKS/key-rotation behavior and TLS
certificates must be verified operationally; shared application-service
authorization for future non-gRPC adapters is not yet established; and legacy
unowned Jobs remain inaccessible pending an explicit migration/admin policy.
ADR 0008 secret resolution remains disabled until its own encryption, scoped
authorization and legacy webhook migration gates pass. Webhook SSRF hardening
and the plaintext Redis callback migration remain separate open risks.

Do not interpret this partial implementation as proof of production readiness
or as completion of Issue #73.

## Required verification

- Missing, malformed, expired, not-yet-valid, wrong-issuer and wrong-audience tokens are rejected.
- Disallowed algorithms, unknown signing keys, JWKS outage/refresh failure and key rotation behave fail-closed without accepting stale untrusted keys.
- Spoofed tenant metadata and caller-provided owner fields cannot alter the principal.
- A tenant cannot read, cancel, revoke outputs, start playback for, or retrieve QoE for another tenant's Job.
- Legacy-unowned Jobs follow only the explicit migration/admin policy.
- Health exposure is tested independently from protected RPCs.
- Authentication and authorization failures do not log tokens, raw metadata, signed URLs or request objects.
- The same authorization tests run through gRPC and the shared application-service boundary.

## Consequences

- Identity provider choice remains deployment configuration rather than a dependency in the domain model.
- Tenant ownership becomes explicit and auditable; legacy Jobs require deliberate treatment.
- Secret storage, quotas, MCP and other public adapters have a clear prerequisite.
- The service must reject production startup when trusted identity configuration is incomplete; this is an intentional availability trade-off in favor of fail-closed security.