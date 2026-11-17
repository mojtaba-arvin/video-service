# Project documentation

This documentation describes both the **verified current implementation** and the **proposed target architecture**. A capability described as a target, roadmap item, or design option is not necessarily implemented.

## Start here

1. [Product vision and architecture principles](architecture/vision.md) — what this project is becoming, the problems it addresses, product boundaries, and measurable outcomes.
2. [Current-state architecture review](architecture/current-state.md) — what exists in `develop`, what the code demonstrates, and the concrete reliability and security gaps found during repository inspection.
3. [Implementation roadmap](roadmap/README.md) — sequenced delivery stages, dependencies, acceptance gates, and explicit deferrals.

## Documentation rules

- **Evidence before claims:** distinguish repository facts, measured results, assumptions, and proposals.
- **No roadmap-as-feature:** planned capabilities must not be described as supported capabilities in README/API docs.
- **Contract before implementation:** public API, job state, retry, cancellation, and event semantics must be specified before replacing internals.
- **Reliability before optimization:** correctness, isolation, durable state, and observability precede GPU scheduling, edge execution, and distributed inference.
- **Portable by design:** keep provider-specific behavior behind replaceable adapters; avoid coupling the core domain to one cloud.
- **Measure economics:** record resource consumption and quality/latency outcomes before claiming cost savings.

## Status

The code reviewed for this document is the `develop` branch at commit `d75bd59d8203277fc6a50440d3f0b34693958ee1` (latest commit visible in the branch history: 2021-04-01). The review was a static inspection of repository files. It did not execute the application, run tests, inspect production infrastructure, or establish real-world performance.

Treat the roadmap as a proposal to guide implementation, not as a promise that every advanced capability will be delivered. Each stage must pass its acceptance criteria before the next stage is treated as complete.
