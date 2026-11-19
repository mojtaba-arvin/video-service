# ADR 0008: Job-scoped encrypted secret references for outbound delivery

- **Status:** Accepted design; implementation pending
- **Date:** 2026-11-19
- **Owners:** Video-service maintainers
- **Related:** [ADR 0002 — durable workflow state](0002-durable-workflow-state-and-typed-operations.md), [ADR 0004 — transactional dispatch](0004-transactional-dispatch-outbox-and-idempotent-execution.md), [ADR 0006 — reliability and observability](0006-reliability-and-observability-as-cross-cutting-constraints.md), [ADR 0007 — immutable execution plans](0007-immutable-execution-plan-persistence.md), [Issue #71](https://github.com/mojtaba-arvin/video-service/issues/71), [Issue #73](https://github.com/mojtaba-arvin/video-service/issues/73)

## Context

The legacy gRPC request accepts a caller-provided `webhook_url`. The current CreateJob path copies it into Redis `JOB_DETAILS`; the Celery webhook task later reads it from that cache entry. Redis is TTL-backed operational state, not an encrypted, durable secret store. A raw URL may contain bearer material or signed query parameters.

The immutable execution-plan contract deliberately rejects raw callback URLs and credentials. PR #106 persists validated plans atomically with Job/Workflow/Stage state, but it does not implement secret storage, resolve references, or change the live Celery dispatch path. A durable plan or future outbox message must never become a new place where plaintext credentials are copied.

The current service does not yet have a complete authenticated caller and tenant-ownership contract. Therefore Job scoping alone must not be described as tenant isolation, and secret resolution must not be exposed as a public API.

## Decision

Introduce a narrow, provider-neutral **Job Secret Store** for sensitive delivery material. It is not a general-purpose vault and does not own webhook network policy.

### 1. Reference and persistence contract

- Generate an unpredictable opaque reference (at least 128 bits of cryptographic randomness); the reference is an identifier, not a bearer credential.
- Store a typed secret record with: secret ID, Job ID, tenant/owner scope once defined by Issue #73, purpose, ciphertext, nonce, authenticated-encryption algorithm/version, wrapped data-encryption key, key-provider/key-version reference, creation/expiry/deletion timestamps, and audit metadata.
- The execution plan, Stage projection, workflow event, Redis task projection, Celery signature and outbox payload contain only the opaque reference. They never contain plaintext, ciphertext, wrapped keys, or key-provider credentials.
- Enforce uniqueness and referential integrity for (Job, purpose, secret version). Secret lookup must validate the expected Job, tenant/owner scope, purpose, active state and retention window.
- There is no public read/list endpoint for secret values. Only the internal delivery adapter may resolve a reference, and only immediately before use.

### 2. Encryption and key management

- Use authenticated encryption (AES-256-GCM or an equivalently reviewed AEAD construction) with a fresh nonce for each encryption.
- Use envelope encryption: a per-secret data-encryption key (DEK) encrypts the value; a replaceable Key Encryption Key (KEK) provider wraps the DEK.
- Production KEKs belong in a managed KMS/HSM adapter. The application stores only the wrapped DEK and a non-secret key identifier/version.
- A local/test key provider may be used for deterministic integration tests and explicitly configured single-node development. It must fail closed when production configuration is missing; no generated or default production key is allowed.
- Key rotation re-wraps DEKs under the new KEK version without logging or exporting plaintext. Decryption must support a bounded overlap window for old key versions.
- Never use a deterministic nonce, reuse a DEK/nonce pair, or silently fall back to plaintext when decryption fails.

### 3. Authorization and resolution

- Resolve with an explicit internal context containing Job ID, tenant/owner identity, purpose and requesting adapter.
- The store checks every scope field on every read; possession of an opaque reference alone is insufficient.
- Tenant/owner identity and authorization semantics must be established by Issue #73 before production secret resolution is enabled. Until then, no claim of tenant isolation or public multi-tenant safety is permitted.
- The delivery adapter receives the plaintext only in process memory for the shortest possible interval. Do not return it from service methods, persist it in task kwargs, attach it to exceptions, or log it.
- The adapter must apply the separate webhook destination policy (scheme/host/IP validation, DNS-rebinding-resistant connect behavior, redirect rejection, bounded timeouts, and delivery audit). Encryption does not mitigate SSRF.

### 4. Lifecycle, audit and retention

- Create the encrypted secret in the same database transaction as the accepted Job/Workflow state and its immutable plan reference. If the transaction fails, neither Job nor secret is committed.
- Record non-secret audit events for create, resolve attempt, denied resolve, rotate, expire and delete. Audit fields include Job/tenant scope, purpose, secret ID, key version and outcome—not secret contents or destination query strings.
- Define a retention policy tied to terminal Job state and callback retry/reconciliation windows. Expiry must be explicit and observable.
- Deletion removes ciphertext and wrapped DEK (cryptographic erasure); retain only a minimal non-sensitive tombstone where audit policy requires it.
- Secret resolution failure is a typed, non-retryable configuration/security error unless the key provider explicitly identifies a transient availability failure. Never substitute a different Job's secret.

### 5. Legacy webhook migration

1. New submissions are validated and encrypted before the Job transaction commits; only the opaque reference is stored in durable and cache projections.
2. New delivery code resolves the reference using Job and tenant/owner context.
3. During a bounded compatibility window, delivery may read the old raw `JOB_DETAILS.webhook_url` value only for already accepted in-flight Jobs created before the cutover. This fallback is measured, redacted and expires with the old Job TTL; it is not used for new Jobs.
4. Do not dual-write plaintext for new Jobs. Do not attempt to backfill an expired or missing legacy callback URL.
5. Remove the compatibility reader after the maximum legacy Job lifetime and retry horizon have elapsed and telemetry confirms no legacy reads remain.
6. Ensure mixed-version deployment behavior is explicit: old workers may continue to process only legacy payloads; new plan-driven work is not dispatched to old workers until their capability is known.

## Consequences

### Positive

- Durable plans and broker messages remain free of callback credentials.
- Secret resolution becomes auditable, scoped and independent of Redis TTL.
- Key-provider implementations can change without changing workflow contracts.
- Rotation, expiry and deletion have explicit semantics.

### Costs and limitations

- Requires a supported AEAD implementation and a managed production KEK provider.
- Adds a database migration, key-provider configuration, lifecycle operations and integration tests.
- Requires the caller/tenant authorization model and SSRF controls from Issue #73 before production activation.
- Legacy in-flight callbacks need a bounded compatibility path during rollout.

## Implementation gates

Do not activate secret-reference resolution in production until all gates pass:

1. Issue #73 establishes authenticated caller identity, Job ownership/tenant scope, authorization and outbound webhook policy.
2. A KMS-backed KEK provider and fail-closed configuration are available; local/test keys cannot be selected in production.
3. PostgreSQL stores encrypted values and wrapped DEKs with Job/tenant/purpose constraints; Job + secret + plan reference commit atomically.
4. Tests cover AEAD tampering, nonce uniqueness, wrong Job/tenant/purpose, unknown/retired key versions, rotation, expiry, deletion, rollback, denied access, and log/exception redaction.
5. Mixed-version and legacy TTL migration tests prove old accepted Jobs still complete without allowing new plaintext writes.
6. Delivery tests prove the resolved value is used only at the outbound boundary and is never included in Celery kwargs, events, snapshots, traces or logs.

Until these gates pass, the existing legacy callback path remains a known transitional risk; the new immutable-plan path continues to reject raw callback URLs and must not dispatch plans.
