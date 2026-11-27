# ADR 0012: Tenant-owned creative project continuity and asset provenance

- **Status:** Accepted; tenant-owned continuity, typed references, immutable provenance, Job revision pinning and authenticated APIs implemented; customer/legal retention policy remains open
- **Date:** 2026-11-25
- **Related:** [Issue #79](https://github.com/mojtaba-arvin/video-service/issues/79), [ADR 0002](0002-durable-workflow-state-and-typed-operations.md), [ADR 0007](0007-immutable-execution-plan-persistence.md)

## Context

The platform already persists immutable, versioned execution plans and provides reusable creative workflow definitions with canonical fingerprints and Job-specific instantiation. Those contracts preserve how a Job executes, but they do not by themselves provide a durable, tenant-owned creative project model across multiple Jobs.

A long-running creative project needs stable references to scenes, shots, characters and assets; explicit revision history; and traceable relationships between generated outputs and the inputs/operations that produced them. Copying the entire project into every prompt or task would create inconsistent snapshots, unbounded payloads and unclear ownership.

## Decision

### 1. Separate project state from execution state

- A **Project** is a tenant-owned creative workspace and the durable root for creative continuity.
- **Scene**, **Shot**, **Character** and **Asset** are typed, project-scoped entities with stable opaque IDs. Names and ordering are mutable presentation attributes, never identity.
- A **ProjectRevision** is an immutable snapshot/sequence boundary for a set of creative decisions and entity references. Mutable working state must not rewrite a revision already referenced by an accepted Job.
- A **Job** references the exact project revision it consumes. A later project edit creates a new revision; it does not silently change an in-flight Job.
- Execution plan and workflow-definition versions remain separate from project revision. A Job pins all three identities independently.

### 2. Keep references typed and tenant-scoped

- Persist tenant_id on the Project root and enforce project ownership through database constraints/queries on every child reference.
- A Shot references its Scene; project membership must be checked for both sides. Character and Asset references must resolve within the same Project and tenant.
- Do not accept caller-supplied tenant IDs as authority. Derive ownership from the authenticated principal, as in ADR 0009/0010.
- Do not encode entity relationships as arbitrary JSON prompt blobs or provider-specific IDs.

### 3. Make provenance an immutable artifact lineage

Every derived Asset must be traceable to:
- its parent/input Asset IDs and immutable content hashes;
- the operation identifier and operation-contract version;
- the pinned workflow-definition version and execution-plan fingerprint;
- the relevant project revision and typed Scene/Shot/Character references;
- a redacted prompt/configuration revision identifier (not raw credentials or secret values);
- validation result/evidence and creation Attempt.

Provenance records are append-only. A retry may create a new physical artifact, but it must not overwrite or erase the lineage of an already committed artifact. Existing Attempt-scoped object-key and PostgreSQL Artifact publication rules remain authoritative.

### 4. Preserve privacy and retention boundaries

- Project, revision, continuity and provenance rows inherit the authenticated tenant ownership boundary.
- Define retention by data class (project state, prompt/config references, source assets, generated assets, audit/provenance) and legal/customer policy before enabling automated deletion.
- A retention deletion must not leave dangling references that appear valid; use explicit tombstone/deletion state and preserve the minimum audit evidence required by policy.
- Do not invent a universal retention duration. Do not persist raw personal/character reference media or prompts beyond the configured, documented policy.
- Provider/model request payloads stay behind replaceable adapters and are not part of the public domain contract.

### 5. Version and resume semantics

- Creative workflow definitions are provider-neutral, schema-validated DAG templates with a canonical SHA-256 fingerprint. A Job acceptance transaction persists the complete validated execution-plan snapshot, schema version, workflow version and fingerprint. Stage rows are projections of that immutable plan, not an alternate source of parameters.
- A Job pins its accepted ProjectRevision and workflow/plan snapshot independently. Workflow upgrades apply only to new Jobs. An idempotent replay with a different plan fingerprint or workflow version is rejected; it cannot rewrite an in-flight Job. Existing accepted Jobs continue from their persisted plan after worker restart.
- There is deliberately no in-place or automatic migration of an accepted Job. If an operator needs to move work to a newer workflow, the safe supported behavior is to submit a new Job against the new immutable definition and explicitly retain the original Job and its artifacts for audit/reuse. A future migration operation must be separately authorized, validate compatibility of completed outputs and remaining stages, persist a new immutable plan plus an append-only migration decision, and preserve the original plan; it must not be inferred from deployment or worker version changes.
- A worker that does not support a Stage's persisted operation-contract version fails closed before invoking the operation adapter. It must not reinterpret the Stage using its current handler.
- Stage boundaries are resumable only when the persisted plan fingerprint, operation-contract version, stable input identity, configuration revision, dependency state and required parent Artifacts match. Completed stages are returned as already completed; expired leases are recovered through durable retry state and a new Attempt. Reusable validated Artifacts remain addressable across retries and worker restarts.
- Operation implementations are injected through an explicit allow-listed handler registry. Provider SDK payloads, credentials and agent-framework types do not enter the workflow/domain contracts.
- Completed reusable artifacts survive cancellation and partial failure; publication state distinguishes provisional from committed/final outputs. Cancellation of a running Attempt is lease-fenced. A Job-level cancellation request/dispatch-suppression API is a separate workflow increment and must not be inferred from Attempt cancellation.

## Required acceptance evidence

- Cross-tenant and cross-project references are rejected at the database/application boundary.
- Renaming/reordering entities does not change their stable IDs; project revisions remain immutable.
- A Job continues to resolve the exact project revision and workflow/plan versions it accepted after later project edits and worker restart.
- Every derived Asset resolves to its parent hashes, operation/config versions, Attempt and validation evidence.
- Retry/replay cannot duplicate a logical output or overwrite a committed artifact lineage.
- Cancellation preserves completed reusable artifacts and marks incomplete outputs as provisional/unpublished.
- Retention/deletion tests prove tenant isolation, reference integrity and the documented deletion/tombstone policy.
- No provider-specific model payload or secret value leaks into persisted domain state.

## Implementation status (verified 2026-11-28)

The core continuity and provenance scope is implemented on `develop`:

1. **PR #153:** provider-neutral, versioned creative workflow-definition contracts, canonical fingerprints, DAG validation and Job-specific plan instantiation.
2. **PR #176:** tenant-owned Project roots and immutable, canonical ProjectRevision snapshots with SHA-256 identity and database-enforced immutability.
3. **PR #181:** typed tenant/project-scoped Scene, Shot, Character and Asset records; immutable revision membership; composite ownership constraints; and same-project Shot→Scene / Asset→Artifact relationships.
4. **PR #184:** optional immutable Job→ProjectRevision pins; idempotency replay pin checks; append-only, sealed Attempt-bound Asset provenance with parent content hashes, operation-contract version, workflow/plan fingerprint and validation evidence. PostgreSQL guards reject late lineage mutation and cycles.
5. **PR #187:** authenticated, tenant-scoped gRPC project/revision and typed-entity operations.
6. **Existing durable workflow implementation:** persists and verifies each Job's immutable execution-plan snapshot and workflow version; Stage rows are projections; retry attempts, lease expiry recovery, dependency artifacts, deterministic side-effect idempotency keys and cancellation fencing are covered by repository/executor tests.

The in-flight version policy is intentionally **pin, do not auto-migrate**. A new workflow version is selected for new Jobs. Replaying an existing idempotency key with a different plan/version is rejected. There is no in-place migration API; a future explicit migration must create a new immutable plan and append-only migration decision without rewriting the original Job history.

### Remaining policy gate

Customer/legal owners must approve retention and deletion policy by data class (project state, source/generated media, prompt/configuration references, provenance and audit evidence), including legal hold, deletion scope and minimum audit preservation. Until that policy is approved, no universal retention duration, automated deletion, or delete API is introduced. Existing tenant ownership, scoped authorization, non-enumerating reads and immutable history remain enforced.

Asset registration still requires an existing AVAILABLE content-addressed Artifact; source ingestion remains #84. No provider-specific model payloads, raw prompts, credentials or agent-framework objects are persisted in domain contracts.
