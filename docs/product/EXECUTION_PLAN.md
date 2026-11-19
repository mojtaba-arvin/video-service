# Product execution plan

- **Status:** Active product plan
- **Baseline:** `develop` at `c8460a751fc7a1167893891fac99896533598654`
- **Last reviewed:** 2026-11-19
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

**Governance action:** update the active `Protect develop` ruleset to require the stable aggregate `Quality gates` check. The current ruleset blocks deletion and non-fast-forward updates but does not require this check. This needs repository-admin capability.

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

Baseline on `develop`:
- PR #95 merged: direct streaming Watermark process-group CPU/RSS/duration metering.
- PR #96 merged: public QoE cost-coverage fields and generated-Protobuf integration coverage.
- PR #97 merged: bounded webhook connect/read waits, no automatic redirect/retry, deterministic cleanup.
- PR #98 merged: roadmap/governance/runtime status corrected.
- Current `develop` is `c8460a7` (full SHA in the baseline above); PR #105 merged the mandatory reliability/observability standard and ADR 0006.
- PR #99 merged the product execution plan; PR #100 merged the first versioned execution-plan contract.
- PR #104 merged the pure VOD plan builder; PR #106 merged atomic immutable execution-plan persistence after PostgreSQL migration/repository tests, Python 3.9/3.12 regression, full gRPC → Celery → FFmpeg → storage VOD integration, Documentation links, Dependency Review and aggregate `Quality gates` all passed on head `e42ef0e673bf14490eb66b1ca14afce941d506e3`.

Next execution order:
1. **#71 — complete VOD DAG builder (merged in PR #104).** The pure transport-neutral builder now represents preflight, input analysis, fan-in, watermark, HLS/DASH, thumbnails, uploads and callback references. Keep the live dispatch path unchanged.
2. **#71 — persist the immutable plan and version with the Job/Workflow/Stage transaction (PR #106 merged).** The plan snapshot, schema/workflow versions, fingerprint, Stage projections and JOB_CREATED event commit atomically; PostgreSQL, Python 3.9/3.12, full VOD E2E and Quality gates passed.
3. **#71 — implement an authorized secret-reference store and resolution contract.** Encrypt at rest; scope reads to tenant and Job; resolve only inside the delivery adapter; redact logs/errors; define rotation, audit, retention and deletion. Migrate the legacy Redis `JOB_DETAILS` webhook path without putting plaintext secrets into plan JSON, outbox payloads or broker messages.
4. **#71 — establish durable per-node execution claims and Attempt leases before outbox activation.** Cover concurrent claims, lease expiry/heartbeat, worker death, retries, cancellation, deterministic artifact keys and duplicate side-effect suppression. Then add broker-backed tests for commit-before-relay crash, publish-before-ack crash, broker outage and poison messages. Enable the relay only after all these pass.
5. **#73 — implement the service security boundary in parallel.** Start with an explicit caller/tenant identity contract and a DNS/IP-aware webhook policy test matrix. The merged redirect and timeout protection is only a partial slice.
6. **#72 — add real per-attempt cgroup/container resource enforcement.** First document Linux/container assumptions and expose a replaceable adapter; then test CPU, memory, PID and disk limits on CI workers capable of enforcing them.
7. **#74 — uplift the full runtime to Python 3.12.** Keep this isolated from workflow feature work and gate rollout on full image-level VOD E2E.
8. **#75/#82 — prepare benchmark/provider and CDN observability in parallel.** Execute representative codec tests only after licensed corpus approval; schema/fixture work can proceed meanwhile.
9. **#79/#84 — begin AI workflow state and incremental contracts after the durable plan model stabilizes.**
10. **#76 — implement MCP after the auth/authorization/quota layer is usable.**
11. **#80/#81/#83 — make measured go/no-go decisions after cost, workload and delivery baselines exist.**

The versioned execution-plan contract (PR #100), complete pure VOD DAG builder (PR #104), reliability/observability standard (PR #105), and atomic immutable plan persistence (PR #106) are merged. PR #106 passed PostgreSQL migration/repository tests, Python 3.9/3.12 regression, full VOD integration, Dependency Review and aggregate Quality gates. Next: tenant-scoped encrypted secret resolution, durable per-node claims/Attempt leases, then broker-backed crash/replay tests. Do not add or activate an outbox relay until every side-effecting node has a durable idempotent claim and all failure gates pass.

## 7. Explicit no-go decisions for now

- No "exactly once" delivery claim.
- No production codec change or savings percentage from synthetic media.
- No GPU/Spot scheduler without measured cost per successful output and retry/interruption economics.
- No general-purpose Edge fleet without a same-workload cloud baseline.
- No JIT packaging rollout without cold/warm latency and device/DRM compatibility evidence.
- No public MCP exposure before authentication, authorization, quotas, audit, and durable dispatch.
- No hard resource-limit claim from process-group sampling alone; cgroup/container enforcement must be tested.
- No "Python 3.12 migrated" claim from unit tests while the production-shaped image remains on Python 3.9.

This plan is intentionally outcome- and gate-based. The roadmap is adjusted when new evidence changes risk, product value, or implementation dependencies.
