# ADR 0012: Tenant-owned creative project continuity and asset provenance

- **Status:** Accepted; Project/ProjectRevision and typed entity/reference persistence implemented; provenance and Job revision pinning remain open
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

- A Job pins its accepted project revision, workflow-definition version, execution-plan schema/version and fingerprint.
- Workflow upgrades apply to new Jobs by default. In-flight Jobs continue with their pinned version unless an explicit, validated migration creates a new immutable plan/revision and records the migration decision.
- Stage boundaries are resumable only when input identity, operation version, configuration revision and required parent artifacts match.
- Completed reusable artifacts survive cancellation and partial failure; publication state distinguishes provisional from committed/final outputs.

## Required acceptance evidence

- Cross-tenant and cross-project references are rejected at the database/application boundary.
- Renaming/reordering entities does not change their stable IDs; project revisions remain immutable.
- A Job continues to resolve the exact project revision and workflow/plan versions it accepted after later project edits and worker restart.
- Every derived Asset resolves to its parent hashes, operation/config versions, Attempt and validation evidence.
- Retry/replay cannot duplicate a logical output or overwrite a committed artifact lineage.
- Cancellation preserves completed reusable artifacts and marks incomplete outputs as provisional/unpublished.
- Retention/deletion tests prove tenant isolation, reference integrity and the documented deletion/tombstone policy.
- No provider-specific model payload or secret value leaks into persisted domain state.

## Consequences and implementation sequence

This ADR defines domain boundaries. PR #176 implements tenant-owned Project and immutable ProjectRevision persistence. PR #181 implements tenant/project-scoped Scene, Shot, Character and Asset records plus immutable revision membership references; public project gRPC APIs, artifact provenance and Job revision pinning remain open.

Implement in narrow increments:
1. **Implemented (PR #176):** tenant-owned Project and immutable ProjectRevision persistence with authorization and migration tests.
2. **Implemented (PR #181):** typed Scene/Shot/Character/Asset references, composite tenant/project constraints, immutable revision membership and same-tenant content-addressed Artifact linkage. Asset registration requires an existing AVAILABLE Artifact; source ingestion remains a separate increment.
3. Add append-only provenance linked to existing Artifact and Attempt records.
4. Add exact revision pinning to Job acceptance and resumable stage checks.
5. Add retention policy and deletion workflow only after customer/legal data classes and retention requirements are explicit.

Keep MCP exposure and model-provider integrations out of this increment. They must consume the same application services and authorization contracts after the domain persistence is proven.
