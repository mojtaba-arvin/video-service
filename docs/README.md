# Project documentation

This documentation describes both the **verified current implementation** and the **proposed target architecture**. A capability described as a target, roadmap item, or design option is not necessarily implemented.

## Start here

- [Reliability and observability engineering standard](engineering/reliability-observability-standard.md) — mandatory failure, telemetry, SLO, test and operational-readiness requirements.
- [ADR 0006: Reliability and observability](decisions/0006-reliability-and-observability-as-cross-cutting-constraints.md) — accepted cross-cutting architecture constraints.
- [ADR 0008: Job-scoped encrypted secret references](decisions/0008-job-scoped-encrypted-secret-references.md) — opaque references, envelope encryption, authorization, rotation, retention and legacy webhook migration.
- [ADR 0009: Provider-neutral gRPC identity and resource authorization](decisions/0009-provider-neutral-grpc-identity-and-resource-authorization.md) — OIDC/JWT trust configuration, immutable principal, tenant ownership, fail-closed rollout and legacy Job policy.
- [ADR 0007: Immutable execution-plan persistence](decisions/0007-immutable-execution-plan-persistence.md) — atomic plan snapshots, fingerprint verification and legacy compatibility.

1. [Product vision and architecture principles](architecture/vision.md) — product direction, problems, boundaries and measurable outcomes.
2. [Current-state architecture review](architecture/current-state.md) — what exists in `develop` and the verified reliability/security gaps.
3. [Implementation roadmap](roadmap/README.md) — outcome lanes, current priorities and explicit deferrals.
4. [GitHub governance](engineering/github-governance.md) — rulesets, security settings and repository-plan constraints.
5. [CI execution matrix](engineering/ci-execution-matrix.md) — change-aware jobs, aggregate required check and CI cost controls.
6. [Codec and cost benchmark](engineering/codec-cost-benchmark.md) — licensed source manifests, matched-VMAF runs, raw measurements and cost limitations.
7. [Parallel architecture research and delivery strategy](research/2026-11-18-parallel-architecture-and-benchmarks.md) — six-goal alignment, research evidence and parallel engineering lanes.
8. [Agent engineering guide](../AGENTS.md) — shared instructions and portable skills.
9. [GitHub Copilot specialist agents](../.github/agents/) — focused profiles for media engineering, read-only security review, and orchestration/MCP design.
10. [Transactional dispatch outbox (ADR 0004)](decisions/0004-transactional-dispatch-outbox-and-idempotent-execution.md) — durable dispatch and at-least-once/idempotent recovery semantics.
11. [VOD package publication invariants (ADR 0001)](decisions/0001-vod-package-validation-and-publication.md) — validation, manifest-last publication and retry behavior.
12. [Durable workflow state and typed operations (ADR 0002)](decisions/0002-durable-workflow-state-and-typed-operations.md) — PostgreSQL source of truth, state transitions, resource/cost accounting and application contracts.

## Documentation rules

- **Evidence before claims:** distinguish repository facts, measured results, assumptions, and proposals.
- **No roadmap-as-feature:** planned capabilities must not be described as supported capabilities in README/API docs.
- **Contract before implementation:** public API, job state, retry, cancellation, and event semantics must be specified before replacing internals.
- **Reliability before optimization:** correctness, isolation, durable state, and observability precede GPU scheduling, edge execution, and distributed inference.
- **Portable by design:** keep provider-specific behavior behind replaceable adapters; avoid coupling the core domain to one cloud.
- **Measure economics:** record resource consumption and quality/latency outcomes before claiming cost savings.

## Status

The original static architecture review was performed against `develop` at `d75bd59d8203277fc6a50440d3f0b34693958ee1` (2021-04-01). The repository has since received tested changes; see the implementation updates in the current-state review. The review was a static inspection of repository files. It did not execute the application, run tests, inspect production infrastructure, or establish real-world performance.

Treat the roadmap as a live, evidence-backed backlog, not as a sequence of mandatory phases. Capabilities are selected and delivered by current risk, value, dependencies, and evidence.
