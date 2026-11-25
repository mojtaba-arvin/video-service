# Product execution plan

- **Status:** Active product plan
- **Baseline:** `dc93324abc8f8ee6e70489764c06f1ab1ddbc20a` (merged PR #173)
- **Last reviewed:** 2026-11-25
- **Parent backlog:** [Issue #54](https://github.com/mojtaba-arvin/video-service/issues/54)
- **Planning rule:** Sequence by dependency and risk; execute independent workstreams in parallel.

This plan turns the repository's six strategic outcomes into an executable, evidence-led delivery system. It is not a date-based promise: no delivery dates or percentage estimates are invented without throughput, capacity, or representative workload data.

## 1. Product outcome and North Star

Build a portable, secure, AI-native video execution platform that turns a validated request into one or more verified, published assets, while preserving state across retries and failures and making quality, latency, and resource cost explainable.

**North Star:** successful, validated, published outputs per unit of fully attributable resource cost, constrained by playback-quality and delivery-latency objectives.

The first operational baseline must measure at least:
- successful published outputs / accepted Jobs;
- accepted Jobs with no dispatch after database commit;
- duplicate logical Attempts or Artifacts after repeated delivery;
- queue wait, execution duration, and time to first playable manifest;
- p50/p95 latency and failure/retry rate by workload class;
- CPU-seconds, peak RSS, memory-duration, bytes, and later GPU/egress/storage;
- complete cost-estimate coverage and variance against provider billing;
- playback startup, rebuffering, decoder failures, and later CDN delivery signals.

Do not set numeric performance or cost targets until a reproducible baseline exists. Once measured, product targets must specify workload, environment, sample size, percentile, and acceptance window.

## 2. Delivery principles

1. **Protect data and accepted work first.** A committed Job must not vanish; a duplicate message must not duplicate logical side effects.
2. **One durable control plane.** PostgreSQL owns Job, Workflow, Stage, Attempt, Artifact, Event, plan-version and dispatch state. Redis is only a projection/cache.
3. **At-least-once transport, idempotent effects.** Never claim exactly-once broker delivery.
4. **Security before exposure.** MCP, public APIs, callbacks, and tenant-aware scheduling depend on authenticated identity, resource authorization, quotas, and safe outbound requests.
5. **Measurements before optimization.** No codec default, GPU policy, Spot policy, JIT rollout, or Edge architecture based on generic savings claims.
6. **Replaceable adapters, not speculative microservices.** Keep a modular application core; introduce new infrastructure only when measured workload needs it.
7. **Behavior and evidence ship together.** Every implementation PR includes tests, operational implications, docs, and rollback considerations.
8. **Unknown is not zero.** Incomplete metrics, costs, QoE, and provider data remain explicitly incomplete.
9. **Reliability and observability are release gates.** Every change defines failure/recovery behavior, stable correlation, structured telemetry, bounded metric cardinality, redaction, relevant failure tests and operator recovery. Set numeric SLOs only after representative baselines exist; follow the mandatory [engineering standard](../engineering/reliability-observability-standard.md).

## 3. Release sequence and exit gates

### Gate A — Reliable VOD execution (P0)

Issues: [#71](https://github.com/mojtaba-arvin/video-service/issues/71), [#72](https://github.com/mojtaba-arvin/video-service/issues/72), plus the reliability slice of [#54](https://github.com/mojtaba-arvin/video-service/issues/54).

Deliver:
- a complete immutable, schema-versioned execution plan that represents every node and dependency in the existing gRPC-to-Celery graph (preflight, input checks/download/inspection, fan-out/fan-in, output operations, validation, publication, and notification);
- database transactionally persisted Job/Workflow/Stage/plan/outbox intent;
- lease-based relay with bounded exponential retries, poison/dead state, cancellation suppression, and observable history;
- durable consumer claims before side effects, deterministic artifact keys, attempt-scoped scratch, and safe duplicate handling;
- process isolation/limits for FFmpeg, with typed limit failures and cleanup;
- real broker/worker crash tests, including crash after publish but before outbox acknowledgement.

**Exit gate:** a database-committed Job is always eventually dispatched or moves to an explicit terminal/operator-visible state; duplicate broker delivery never creates concurrent logical Attempts or duplicate published Artifacts; a killed or resource-limited process tree cannot exceed its configured envelope or survive cancellation.

**Implementation safety rule:** do not add an outbox row that dispatches the current in-memory legacy graph until the entire graph has a durable versioned representation and every stage has an idempotent claim contract. An outbox table alone would make replay of a partially idempotent graph unsafe.

### Gate B — Secure and supported service (P0, parallel with Gate A)

Issues: [#73](https://github.com/mojtaba-arvin/video-service/issues/73), [#74](https://github.com/mojtaba-arvin/video-service/issues/74), and the repository-governance action.

Deliver:
- caller authentication and request-scoped authorization for create/read/cancel/output/Artifact operations;
- an explicit ownership/tenant model and audit trail before exposing cross-resource operations;
- quotas/admission checks before dispatch;
- outbound webhook policy rejecting loopback, private, link-local, multicast, reserved and metadata destinations; redirect rejection; DNS-rebinding-resistant resolution-to-connect behavior; bounded requests; signed callbacks and replay protection;
- Python 3.12 application/container migration, upgraded dependencies/build tools, generated gRPC modules, and pinned reproducible image;
- migration, broker-backed worker, PostgreSQL, Redis, S3-compatible storage and full VOD smoke tests inside the new image.

**Exit gate:** unauthorized or cross-tenant requests cannot access resources; SSRF and replay tests pass; webhook delivery is bounded and auditable; the production-shaped Python 3.12 image completes the full VOD path and migrations.

**Governance state (2026-11-24):** the active `Protect develop` ruleset now requires pull requests and the stable aggregate `Quality gates` check, preserves deletion/non-fast-forward protection, and has no bypass actors. Verify the required check on a PR targeting `develop`; keep this gate green.

### Gate C — Measurable quality and economics (P1)

Issues: [#75](https://github.com/mojtaba-arvin/video-service/issues/75), [#80](https://github.com/mojtaba-arvin/video-service/issues/80), [#82](https://github.com/mojtaba-arvin/video-service/issues/82).

Deliver:
- run the matched-VMAF H.264/HEVC/AV1 harness against a licensed, representative and versioned media corpus, with repeated runs and player/device compatibility;
- record GPU seconds, network ingress/egress, storage byte-hours/retention, idle/control-plane overhead and actual provider invoice line items;
- report raw measurements, estimated cost, coverage gaps, and reconciled billing as distinct data products;
- add provider-neutral CDN delivery events correlated to the exact published manifest/artifact version and player session;
- add queryable windows/cohorts and privacy-conscious percentiles;
- add deterministic CPU admission and per-tenant concurrency controls after baselines exist; only then evaluate GPU pools and Spot/preemptible capacity.

**Exit gate:** benchmark results are reproducible and licensed; all comparisons share the same input/quality conditions; cost per successful published output is explainable and reconciled; scheduler decisions are measurable and auditable; CDN/player gaps are surfaced as missing data, not zero failures.

**External dependency:** representative codec benchmarking cannot be truthfully completed without legally approved source media. Until that corpus exists, keep the harness and synthetic correctness tests ready and do not claim codec savings.

### Gate D — AI-native workflow and agent interface (P1)

Issues: [#79](https://github.com/mojtaba-arvin/video-service/issues/79), [#84](https://github.com/mojtaba-arvin/video-service/issues/84), [#76](https://github.com/mojtaba-arvin/video-service/issues/76).

Deliver:
- immutable, versioned workflow definitions and explicit in-flight upgrade policy;
- typed project/scene/shot/character/asset references and provenance, with retention and tenant ownership;
- resumable stage boundaries and incremental input/output with chunk identity, checksums, ordering, backpressure, and provisional-vs-final artifact states;
- stable application services and authorization reused by gRPC and MCP;
- secure MCP tools for bounded probe, thumbnail, transcode, status and cancellation; long-running calls return durable Job IDs.

**Exit gate:** workflow-version changes cannot silently mutate in-flight Jobs; retries preserve provenance and completed artifacts; incremental outputs cannot be mistaken for final outputs; MCP contract/client tests prove authorization, quotas, and shared application-service behavior.

**Dependency:** do not expose MCP tools before the service identity/authorization and quota layer from Gate B exists. Do not build a second orchestration engine inside MCP.

### Gate E — Evidence-backed delivery extensions (P1/P2)

Issues: [#81](https://github.com/mojtaba-arvin/video-service/issues/81), [#83](https://github.com/mojtaba-arvin/video-service/issues/83).

- JIT packaging: prototype packaging-only transformation of pre-encoded CMAF assets; compare storage byte-hours, cold/warm latency, cache behavior, player/device and DRM constraints. Preserve pre-packaged fallback.
- Edge-cloud: select one target workload, measure cloud-only baseline, then test bounded edge preprocessing and cloud fallback with identical source/policy. Do not build a generalized edge fleet before a positive measured result.

**Exit gate:** each extension must outperform the baseline on its selected workload after compute, storage, transfer, operational, security and compatibility costs are counted. Otherwise document the no-go decision and keep the simpler architecture.

## 4. Parallel workstream operating model

Work in parallel when contracts are stable and write ownership does not overlap. Coordinate migrations, protobuf schema and shared runtime configuration to prevent merge conflicts.

| Workstream | Primary ownership | Parallel deliverables | Coordination rule |
|---|---|---|---|
| A. Durable plan/outbox | Workflow domain, repository, migrations, dispatch/relay | Plan schema, failure matrix, broker harness | One owner for workflow migrations; never activate partial legacy-plan replay |
| B. Security | AuthN/AuthZ, webhook destination policy, audit/quota | SSRF test harness, tenant threat model | Publish auth context/ownership contract before MCP and scheduler consume it |
| C. Runtime | Dockerfiles, dependency locks, generated-code compatibility | Python 3.12 image and smoke tests | Avoid unrelated architecture changes; keep old lane until image E2E passes |
| D. FFmpeg isolation | Process supervisor, cgroup/container adapter, scratch lifecycle | Resource-limit tests and workload classes | Share typed Attempt resource/error contract with A; don't add a second metering schema |
| E. Cost/QoE | Benchmark harness, metric adapters, report queries | Corpus readiness, provider-neutral CDN schema | No production tuning until measurement coverage and data rights pass |
| F. AI/MCP | Workflow definitions, provenance, shared application services | Creative state schema and MCP contract tests | Implement after A/B contracts; MCP remains a thin adapter |

Every parallel PR must name its owning issue, affected contracts, files/migrations it owns, tests, and rollback path. If two workstreams need the same migration/protobuf file, sequence that change under one branch rather than merging conflicting edits concurrently.

## 5. Product operating cadence

### At the start of an increment
- Pick the smallest deliverable that closes a verified acceptance gap.
- Record user/customer value, risk removed, dependencies, explicit non-goals, owner, and acceptance tests in the Issue.
- Check for an existing implementation or open PR before creating another branch.

### During implementation
- Keep architecture research and test design parallel with coding.
- Use test-first failure cases for data loss, duplicate delivery, cross-tenant access, process escape, and incomplete metrics.
- Prefer real integration boundaries in CI; mock only external services that cannot be run deterministically.
- Avoid unbounded matrix expansion: run narrow contract tests locally/early, then the required runtime and end-to-end lanes before merge.

### Before merge
- Require stable aggregate `Quality gates` plus Dependency Review.
- No merge while required checks are pending, failing, missing, or skipped contrary to the change classifier.
- Verify generated Protobuf and migration outputs from source definitions.
- Update roadmap and Issue status using merged evidence, not branch existence.
- Document security limitations and external prerequisites.

### At each product review
- Report: merged outcomes, CI evidence, operational risk closed, baseline/metric changes, open blockers, decisions needed, and the next smallest high-value increment.
- Reprioritize on new evidence. Do not preserve a roadmap item merely because implementation has started.
- Close an issue only when every acceptance criterion is verified; otherwise split a completed slice and keep remaining work open.

## 6. Current portfolio state and next execution order

Baseline on `develop` as verified on 2026-11-25 (post-merge state):

- **PR #151 merged** as `ead3c17de2f74223f25908f05f2600574b515b41`: transactional dispatch outbox, PostgreSQL Attempt leases/recovery, artifact handoff, KMS envelope secret storage and opt-in durable gRPC CreateJob path. CI and Dependency Review passed on the merged head. Durable dispatch remains disabled by default; live KMS/STS/IAM, legacy callback Job reconciliation and deployed broker redelivery remain release gates.
- **PR #152 merged** as `0ec1a13a4e7a9c1ca2dfcfe1399761ea758a1c1b`: provider-neutral workload classification for typed operations, including `NETWORK_IO` webhook delivery.
- **PR #153 merged** as `a2ce4d8afe4d82241025c9bc77e5eb9d08c80803`: reusable versioned creative workflow definitions, canonical fingerprints, DAG validation and Job-specific instantiation.
- **PR #154 merged** as `4062b6998b5fd6b1e0bd7b01de900f01f4137663`: Python 3.12.11 Alpine 3.22 production image, refreshed pins, same-base FFmpeg/FFprobe and startup Protobuf generation. Image/import/media-tool smoke and CI passed; full broker-backed VOD and migrations inside this image remain operational acceptance work.
- **PR #156/#168 merged** as `d546607e20ea6fcec4859688ebed9286bcf0b3da` / `06a53f51b1fad0ea1dd0a1eaac6ffeb2c69097ba`: Attempt-scoped cgroup v2 CPU/memory limits are wired through durable playlist, thumbnail and watermark processing; kernel counters persist on success and failure.
- **PR #171 merged** as `c54f60a3c43acfecae4c68bc047a7e74fa417443`: native `clone3(CLONE_INTO_CGROUP)` admission enforces per-Attempt `pids.max` from process creation. The Linux acceptance job verifies CPU, memory/OOM, PID rejection, descendant termination and cleanup. Disk quotas and production delegated-root verification remain open.
- **PR #173 merged** as `dc93324abc8f8ee6e70489764c06f1ab1ddbc20a`: ADR 0011 defines hard per-Attempt scratch-disk isolation constraints. This is a design decision only; no disk quota backend or runtime flag is enabled.
- **PR #157 merged** as `f75a332ef6a06d2230ef147f593b9e535e7b9f01`: full VOD + Redis/Celery recovery matrix on Python 3.9 and 3.12.
- **PR #170 merged** as `6c375c52dc28d181c9d5ea69c291ceecd640044b`: atomic per-tenant active-Job admission uses the authenticated tenant for both legacy and durable CreateJob paths. The flag remains off until every tenant has an explicit policy; operator provisioning and policy-change audit history are not yet delivered.
- **Governance #148:** ruleset `Protect develop` now requires PRs and `Quality gates`, with no bypass actors. Issue #148 is closed; PR #171 demonstrated the required `Quality gates` check on a PR targeting `develop`.
- **Security #73 remains partial**: OIDC gRPC authentication, request principals, scope checks, durable Job ownership, DNS-pinned SSRF-resistant webhook transport, bounded timeouts, redirect rejection, sensitive-log redaction, HMAC callback signing and atomic tenant quotas are merged. PR #175 added audited quota-policy provisioning/inventory repository primitives; PR #178 added redacted sender-side webhook attempt history; PR #180 now exposes quota provisioning, policy inventory, quota audit inspection and Job-owner-scoped webhook history over authenticated gRPC. Still open: production issuer/audience/JWKS and admin-scope issuance, receiver-side atomic Delivery-ID replay suppression, provisioning every active tenant and legacy-resource reconciliation.
- **Durable dispatch rollout remains disabled**: CI does not establish production KMS/STS/IAM policy, live S3 behavior, legacy callback migration or automatic broker redelivery in the deployed topology.

Next execution order:

1. **#72 — close the remaining resource-isolation gap.** PID admission is now atomic and tested with `clone3(CLONE_INTO_CGROUP)`. ADR 0011 records the disk-isolation constraints. Implement a real per-Attempt filesystem quota backend only after worker mount/quota capability evidence; verify delegated CPU/memory/PID controllers and limits on the actual worker host. Keep the feature flag off until this evidence exists.
2. **#73 — complete security operations.** Configure the production OIDC issuer/audience/JWKS and ensure only the operator identity receives `video.tenant.quota.admin`; provision and reconcile an explicit policy for every active tenant through the new RPCs. Keep `TENANT_JOB_QUOTA_ENFORCEMENT_ENABLED=false` until that inventory is complete and legacy ownership is reconciled. Document and test the receiver's atomic Delivery-ID dedup transaction in the receiving system; sender-side history is already merged in PR #178.
3. **#71 — complete operational acceptance.** Keep `WORKFLOW_DURABLE_DISPATCH_ENABLED=false`; rehearse migrations and the complete broker-backed VOD path inside the exact Python 3.12 production image, then verify live KMS/STS/IAM, legacy callback inventory/drain, deployed broker redelivery, worker-loss recovery, observability and rollback in staging before activation.
4. **#79 — complete the P0 long-running workflow contract.** PR #153 already provides reusable versioned creative definitions and canonical fingerprints; ADR 0012 now defines the missing tenant-owned Project/Revision/Scene/Shot/Character/Asset and immutable provenance boundaries. Implement persistence and revision pinning in stages, then retention/deletion only after customer/legal policy is explicit.
5. **#80 — implement deterministic CPU admission and tenant fairness** after quota provisioning/audit and resource metrics are operational; defer GPU/Spot policies.
6. **#75/#82 — prepare licensed benchmark corpus and CDN telemetry correlation in parallel**, with privacy/retention controls.
7. **#76 — implement MCP only after shared authorization, quota and audit contracts are complete.**
8. **#84 — design incremental ingestion/output after artifact identity, resumability and backpressure contracts stabilize.**
Issue #74 is closed: the required Python 3.12 migration CI, migration-chain checks and full VOD compatibility lanes passed. The remaining exact production-image/staging rehearsal is tracked under #71, not as an open runtime-migration implementation issue.

## 7. Explicit no-go decisions for now

- No "exactly once" delivery claim.
- No production codec change or savings percentage from synthetic media.
- No GPU/Spot scheduler without measured cost per successful output and retry/interruption economics.
- No general-purpose Edge fleet without a same-workload cloud baseline.
- No JIT packaging rollout without cold/warm latency and device/DRM compatibility evidence.
- No public MCP exposure before authentication, authorization, quotas, audit, and durable dispatch.
- No hard resource-limit claim from process-group sampling alone; cgroup/container enforcement must be tested.
- No claim of fully validated Python 3.12 runtime migration until migrations and the complete broker-backed VOD path pass inside the production image.

This plan is intentionally outcome- and gate-based. The roadmap is adjusted when new evidence changes risk, product value, or implementation dependencies.

## 7. Creative project continuity increment (Issue #79)

**Merged:** PR #176, squash commit `f9d7f9959cb4baaa8ec30bb451787f67d547cac6` (2026-11-25). The full required CI matrix passed on head `878a76bf657e584950a038e7918d457e763d239e`, including PostgreSQL repository integration, migration downgrade safety, both Python 3.9/3.12 VOD paths, cgroup v2 acceptance, regression, documentation links and Dependency Review.

Delivered:
- Tenant-owned Project roots derive tenant and creator identity from the verified principal.
- ProjectRevision snapshots use canonical JSON, recursive string-key validation, SHA-256 identity and monotonic per-project revision numbers.
- Composite `(project_id, tenant_id)` constraints prevent cross-tenant revision attachment; PostgreSQL triggers reject revision UPDATE and DELETE.
- Reads/writes require explicit `video.projects:*` scopes and foreign-tenant access is denied without revealing resource existence.
- Migration `0013_creative_project_revisions` follows `0012_tenant_quota_audit`; downgrade refuses to discard project/revision history.

Next #79 increments, in order:
1. Typed, tenant-constrained Scene/Shot/Character/Asset references that point to a Project and, where required, an exact ProjectRevision.
2. Append-only provenance for derived artifacts (source artifact IDs/hashes, operation/version, redacted prompt/config revision, validation evidence and Attempt).
3. Pin each Job to an exact ProjectRevision and verify that pin during retry/resume; keep workflow-definition and execution-plan versions separate.
4. Define customer/legal retention policy before any retention automation or deletion path.

Do not add model-provider payloads to the domain schema, mutable revision updates, guessed retention periods, or duplicate the existing versioned workflow-definition/execution-plan contracts.


## 8. Webhook delivery history increment (Issue #73)

**Merged:** PR #178, squash commit `9790f80eed8a4f8be8e62f146fb468ef65f7be13` (2026-11-25). The required Quality gates and Dependency Review passed after synchronizing the branch with the latest `develop` head. The full matrix passed, including Python 3.9/3.12 VOD integration, PostgreSQL, Redis/Celery recovery, OS-process worker kill/restart, S3 publication and KMS envelope contracts.

The implementation reuses append-only `WorkflowEventRecord` rather than introducing a duplicate table. Sender attempts emit STARTED and terminal SUCCEEDED/FAILED events tied to the persisted Job, Stage and Attempt. The payload allowlist stores only stable Delivery ID, bounded HTTP status and a fixed failure code; it never stores destination URLs, headers, request/response bodies, signatures or secrets. History reads require `video.jobs:read`, exact Job-owner identity and a limit of 1–100 events.

A STARTED event without a terminal event means the external outcome is unknown after a crash; do not infer success/failure or claim exactly-once delivery. Retry preserves the stable Delivery ID. Receiver-side atomic deduplication remains an external integration requirement. No automatic retention/deletion policy is introduced until customer/legal policy is explicit.


## 9. Operator security operations increment (Issue #73)

**Merged:** PR #180, squash commit `c986bae25651d14021e2d34b95af154587a0401b` (2026-11-25). The service now exposes authenticated gRPC operations for quota provisioning, bounded current-policy inventory, bounded quota audit-history inspection, and bounded Job-owner-scoped webhook delivery history.

Quota administration derives operator issuer/subject from the verified OIDC RequestPrincipal and requires `video.tenant.quota.admin`; callers supply only target tenant, cap and mandatory reason. Audit history is visible only to that scope. Webhook history uses `video.jobs:read` and exact Job-owner identity. The feature flag remains disabled: this code does not configure the external IdP, prove production issuer/audience/JWKS, provision every active tenant or implement receiver-side replay suppression.
