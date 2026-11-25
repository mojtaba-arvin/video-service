# Security boundary and webhook receiver release runbook

## Purpose and safety boundary

This runbook captures the production evidence required for Issue #73. Green CI proves the repository's test contracts; it does not configure the external identity provider, provision every tenant, or make an independently operated webhook receiver replay-safe.

Keep `TENANT_JOB_QUOTA_ENFORCEMENT_ENABLED=false` until the identity, operator, tenant-inventory and legacy-ownership gates below are complete. Do not treat sender-side delivery history as receiver-side replay protection.

## Gate 0 — pin the identity-provider trust configuration

The gRPC server starts only in OIDC mode and constructs its authenticator from deployment configuration. The issuer and JWKS URI are trusted deployment values; they are never discovered from an unverified token.

Configure these values from the identity provider's authoritative metadata:

| Setting | Required value |
|---|---|
| `GRPC_AUTH_MODE` | `oidc` |
| `OIDC_ISSUER` | Exact HTTPS issuer URL; must match the token `iss` |
| `OIDC_AUDIENCE` | Exact audience issued for this gRPC API |
| `OIDC_JWKS_URI` | HTTPS JWKS endpoint from the trusted issuer metadata |
| `OIDC_ALLOWED_ALGORITHMS` | Explicit asymmetric algorithms supported by the IdP; default is `RS256` |
| `OIDC_TENANT_CLAIM` | Tenant identity claim; default `tenant_id` |
| `OIDC_SUBJECT_CLAIM` | Principal identity claim; default `sub` |
| `OIDC_SCOPES_CLAIM` | Scope claim; default `scope` |
| `OIDC_ROLES_CLAIM` | Role claim; default `roles` |
| `OIDC_JWKS_TIMEOUT_SECONDS` | Bounded JWKS network timeout; default 5 seconds |
| `OIDC_CLOCK_LEEWAY_SECONDS` | Clock tolerance; default 0 |

The authenticator requires `exp`, `iss`, `aud` and the configured subject claim. Tenant identity must be a non-empty string. Scope and role claims may be whitespace-delimited strings or lists. Only asymmetric signing algorithms are accepted; HMAC algorithms such as HS256 are rejected.

Before deployment, verify with real, non-production IdP-issued tokens:

1. Correct issuer, audience, signature, expiry and tenant claim authenticate.
2. Wrong issuer/audience, expired token, missing tenant/subject, unknown key, unsupported algorithm and malformed claims fail closed.
3. JWKS endpoint outage or timeout fails closed; no static key or unsigned-token fallback exists.
4. A rotated signing key is accepted only after it appears in the configured trusted JWKS.
5. gRPC transport TLS is enabled and certificate rotation/expiry alerts are configured.

Do not place client secrets, private keys, bearer tokens or signed test tokens in this repository, runbook, CI variables visible to untrusted jobs, or issue comments.


### Where these settings belong

These are **service runtime configuration values**, not values that can be inferred from this repository. Supply them to the deployed gRPC service through its approved runtime configuration mechanism (for example, the deployment platform's environment/configuration store). The checked-in `video-streaming/video_streaming/.env.example` intentionally leaves `OIDC_ISSUER`, `OIDC_AUDIENCE` and `OIDC_JWKS_URI` blank; never deploy those placeholders.

As of this runbook revision, the repository has CI and Dependency Review workflows but no GitHub Actions deployment workflow or declared GitHub Environment. Therefore, do **not** create an arbitrary GitHub Environment or set repository Environment variables solely to configure OIDC. GitHub Environment variables are appropriate only if a later deployment workflow explicitly targets that Environment and maps those variables into the service's runtime configuration. A successful `gh variable set` would not by itself configure the running service.

The IdP must first provide its exact issuer and audience for this gRPC API and its trusted HTTPS JWKS URI. If the IdP is not yet selected/configured, keep the production OIDC rollout blocked; do not substitute a guessed issuer, audience, or JWKS URL.

### Selected reference provider

[ADR 0010](../decisions/0010-keycloak-reference-oidc-provider.md) selects **Keycloak 26.6+ with Organizations** as the reference OIDC provider. This is an architectural selection, not evidence that a Keycloak instance exists or that production is configured. Use a dedicated realm and gRPC API audience; issue tenant-scoped access tokens in exactly one selected organization context. Keycloak's native organization claim is an object, while this service currently requires a top-level string tenant_id. A tested mapper/adapter must emit the selected immutable organization ID as tenant_id before production rollout. Never use organization:* or accept an ambiguous multi-organization token for this API. The exact issuer, audience and JWKS URI remain unavailable until the instance is provisioned.

## Gate 1 — operator identity and least privilege

The quota-administration RPCs require the verified principal to carry the exact scope `video.tenant.quota.admin`. The public creative Project API separately uses `video.projects:create`, `video.projects:read` and `video.projects:write`; grant these only to tenant-scoped application identities that need Project/Revision or typed-entity access. The target tenant, cap and mandatory reason are request data; operator issuer and subject are derived from the verified token and persisted in the audit record.

Create a dedicated operator identity or tightly controlled operator group:

- Grant `video.tenant.quota.admin` only to named operational identities that require tenant quota administration.
- Do not grant this scope to ordinary API clients, workers, customer users or every service account.
- Keep application permissions separate from IdP administration privileges.
- Verify a non-operator receives PERMISSION_DENIED for policy provisioning, inventory and audit-history RPCs.
- Verify the authorized operator can provision a policy and that the audit record contains the verified issuer, subject, previous value, new value, version and reason.

The operator must use a short-lived token issued for the configured audience. Revoke the scope promptly when an operator changes role or leaves the team.

## Gate 2 — provision and reconcile every active tenant

Before enabling quota enforcement:

1. Export the authoritative active-tenant inventory from the customer/account system.
2. Export the service's existing Job ownership inventory, including legacy Jobs with missing owner identity.
3. Reconcile tenant IDs exactly; investigate unknown, duplicate, orphaned and unowned Jobs. Do not guess an owner or silently assign a default cap.
4. Agree and record a reviewed `max_active_jobs` policy for every active tenant.
5. Provision each policy through the authenticated `provision_tenant_job_quota_policy` RPC with a non-empty operational reason.
6. Read back the policy inventory and audit history; reconcile the results against the source tenant inventory.
7. Repeat the reconciliation immediately before activation and define an owner/process for new-tenant provisioning and policy changes.

The current inventory RPC is bounded to at most 1,000 rows and does not expose pagination. If the active tenant population exceeds that limit, use an authoritative external inventory plus bounded tenant-specific verification; do not interpret a truncated response as a complete inventory.

Only after every active tenant has an explicit policy, legacy ownership is reconciled, operator audit is verified and missing-policy behavior is tested should a separately reviewed release change enable `TENANT_JOB_QUOTA_ENFORCEMENT_ENABLED=true`.

Rollback: disable new quota enforcement, preserve policy/audit history, and investigate any Job admitted during the rollout window. Do not delete policy rows or rewrite historical audit records to make inventory counts match.

## Gate 3 — receiver-side webhook replay protection

The sender signs the exact transmitted body bytes with HMAC-SHA256 and sends these headers:

- `X-Video-Webhook-Version`
- `X-Video-Webhook-Timestamp`
- `X-Video-Webhook-Delivery-Id`
- `X-Video-Webhook-Signature`

The Delivery ID is deterministic for one Job and Stage and is reused on retries. The timestamp is fresh per attempt. Signature verification checks the exact body, Delivery ID and timestamp, using a bounded clock-skew window. This authenticates and time-bounds a request; it does **not** prevent replay on its own.

The receiving application must implement durable, atomic deduplication in its own database:

1. Read the raw request bytes once; verify the HMAC against those exact bytes before parsing or transforming the payload.
2. Validate the timestamp and Delivery ID. Enforce a maximum request-body size and the receiver's tenant/endpoint authorization.
3. Begin one database transaction.
4. Insert a receipt row with a unique key such as `(receiver_tenant_id, endpoint_id, delivery_id)`, plus the payload digest and received timestamp.
5. If the unique key already exists with the same payload digest, treat it as a duplicate and return the receiver's documented successful acknowledgement without repeating the business effect.
6. If the same Delivery ID exists with a different digest, reject and alert; never overwrite the original receipt.
7. Apply the business side effect in the same transaction as the receipt insert. Commit both together.
8. If the transaction fails, roll back both receipt and side effect so a later sender retry can safely try again.
9. Retain receipt keys for at least the maximum sender retry/replay horizon and the receiver's required audit period. Define deletion only under the receiver's own approved retention policy.

A separate "check then insert" followed by an out-of-transaction side effect is unsafe: concurrent requests can both pass the check, or a crash can persist the receipt without the effect (or the effect without the receipt). Use a unique constraint and one transaction.

Acceptance rehearsal must send the same signed delivery concurrently and after a simulated receiver crash. Exactly one business effect may commit; duplicates with the same digest must be acknowledged without repeating it; a different digest under the same ID must be rejected. The sender remains at-least-once and must not claim end-to-end exactly-once delivery.

## Gate 4 — release evidence

Attach a sanitized evidence record for each gate:

- exact service commit and image digest;
- IdP issuer, audience and JWKS URI (no credentials or tokens);
- identity/claim test matrix and result;
- operator identity inventory and scope grant/revocation test;
- active-tenant source count, service inventory count, reconciliation exceptions and approved policy export;
- quota audit sample with sensitive identifiers appropriately redacted;
- receiver schema/unique constraint, transaction implementation reference and duplicate/concurrency/crash test results;
- reviewer, timestamp, change/incident ticket and rollback decision.

No feature flag is enabled by this document. Activation requires a separate reviewed release decision based on the evidence above.
