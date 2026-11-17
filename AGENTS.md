# Repository agent guide

## Product reality

This repository incrementally modernizes a legacy asynchronous VOD service.
The implemented baseline uses gRPC, Celery, Redis, S3-compatible storage,
FFmpeg, and FFprobe. Durable orchestration, typed agent operations/MCP, cost
attribution, optional edge execution, and playback QoE are target capabilities,
not implemented features unless code and tests prove otherwise.

Inspect the current code and tests before changing behavior. Treat
docs/architecture/current-state.md as a dated review, not a substitute for
reading implementation details.

## Engineering invariants

1. Preserve existing VOD behavior unless a tested change intentionally replaces it.
2. Keep behavior, tests, documentation, and Issue #54 acceptance status in the same increment.
3. Never report success until expected artifacts are validated and publication is safe.
4. Redis TTL keys and broker messages are not authoritative durable workflow state.
5. Treat media, metadata, paths, webhook URLs, storage references, and callbacks as untrusted input.
6. Use subprocess argument arrays, never interpolated shell commands. Supervise process trees and bound time, memory, disk, and concurrency.
7. Never expose arbitrary shell, FFmpeg command lines, filter graphs, filesystem paths, or unrestricted URL fetching as an agent tool.
8. Keep provider details behind replaceable adapters. Do not add microservices, Kubernetes, a new broker, or an orchestration framework without workload evidence and an ADR.
9. Never claim cost, latency, quality, or throughput gains without reproducible benchmarks and raw measurements.
10. Never commit generated video binaries, secrets, production URLs, customer media, or copyrighted sample media without redistribution rights.

## Verification

Install test dependencies and FFmpeg/FFprobe:

~~~sh
python -m pip install -r requirements-test-py39.txt
# For the forward-compatibility test environment, use requirements-test-py312.txt.
python video-streaming/tests/fixtures/synthetic-vod-v1/generate.py --output-dir /tmp/synthetic-vod-v1
PYTHONPATH=video-streaming python -m unittest discover -s video-streaming/tests -v
~~~

The legacy service baseline is Python 3.9, which reached end of life on
2025-10-31. Keep its regression job temporarily as a backward-compatibility
check, not as a security-supported runtime target. A separate Python 3.12 job
checks only the current regression suite; it does not prove that the full
service and its runtime dependencies are Python 3.12 compatible. Upgrade the
runtime through a dedicated dependency/container compatibility increment.

## Task-specific skills

Use the relevant procedural guide under .agents/skills/:

- ffmpeg-regression-testing — media behavior, generated fixtures, FFprobe,
  failure, cancellation, and process cleanup.
- durable-workflow-design — Job/Workflow/Stage/Attempt/Artifact semantics,
  retry policy, idempotency, and crash recovery.
- mcp-agent-interface — typed tools, async jobs, authorization, budgets, and
  agent-facing contracts.
- cost-codec-benchmarking — corpus design, repeatable profile comparisons,
  resource metrics, and billing reconciliation.
- security-boundary-review — untrusted media, subprocesses, SSRF, secrets,
  tenancy, and artifact access.

Skills are guidance, not evidence that the corresponding platform capability
exists.

GitHub Copilot specialist profiles are defined under .github/agents/: the
media pipeline engineer can implement and test changes, the security auditor
is read-only, and the orchestration/MCP architect focuses on contracts and
state design. Keep reviewer profiles read-only and grant execute/edit access
only to agents that must change code or run tests.
