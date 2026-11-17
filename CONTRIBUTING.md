# Contributing

## Change policy

- Work on a focused branch; do not push feature work directly to `develop`.
- Keep each pull request to one independently reviewable behavior or vertical increment.
- A behavior change must include regression tests that fail before the fix and pass after it.
- Pull requests targeting `develop` must have all required CI checks green before merge.
- Do not merge while checks are pending, failing, or unavailable. A green check is evidence for the commands it actually ran, not for untested runtime or production behavior.
- Record material architecture or compatibility decisions in a short ADR under `docs/decisions/`.
- Do not claim performance, cost, or quality improvements without a reproducible benchmark and its workload definition.

## Running the regression tests

The initial test suite uses Python's standard-library `unittest` runner and the same `python-decouple` version pinned by the service:

```sh
# Install FFmpeg and FFprobe using your platform package manager first.
python -m pip install -r requirements-test-py39.txt
# For the forward-compatibility test environment, use requirements-test-py312.txt.
python video-streaming/tests/fixtures/synthetic-vod-v1/generate.py --output-dir /tmp/synthetic-vod-v1
PYTHONPATH=video-streaming python -m unittest discover -s video-streaming/tests -v
```

GitHub Actions runs this command for pull requests targeting `develop` and for pushes to `develop`. A separate Python 3.12 job runs the same regression suite.

The CI job now validates configuration, environment-file tracking rules, and thumbnail rendering/output validation through real FFmpeg and FFprobe executions. It still does not validate Celery orchestration, Redis, RabbitMQ, S3 publication, container builds, gRPC authentication, webhook security, or a complete VOD job. The legacy service baseline is Python 3.9, which reached end of life on 2025-10-31. CI keeps a Python 3.9 backward-compatibility job and separately runs the regression suite on Python 3.12. The Python 3.12 job covers only the current tests; it does not prove that the full service and all runtime requirements are compatible. Upgrade the production/runtime baseline in a dedicated, tested dependency and container migration.


## Agent-assisted development

Read the repository-root AGENTS.md before using an AI coding agent. It defines
the verified architecture boundaries and local verification commands. Use the
task-specific skills under .agents/skills/ for FFmpeg regression testing,
durable workflow design, MCP/agent contracts, cost and codec benchmarking,
and security reviews. These are process guides, not implemented capabilities.

## GitHub automation and enforcement

CI runs the regression suite and validates the generated synthetic corpus.
CodeQL and dependency review provide additional security feedback; Dependabot
proposes dependency and Actions updates. See
docs/engineering/github-governance.md for the recommended repository settings.

The current workflow's green check is not itself branch protection. The
develop branch must have an active ruleset requiring the Unit tests
(Python 3.9) check before GitHub technically blocks an unverified merge.
