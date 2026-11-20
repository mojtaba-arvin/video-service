---
name: agent-task-orchestration
description: Use when planning multi-role coding work, delegating repository tasks, integrating specialist findings, or preventing parallel-edit conflicts.
---

# Agent task orchestration

Read `AGENTS.md` and `docs/engineering/agent-engineering-standard.md`.

## Before assigning work
- Inspect the latest base, open PRs, branches, issues, ADRs and overlapping files.
- Define one outcome, acceptance criteria, non-goals, risk, dependencies and verification.
- Choose the smallest relevant role/profile and skill set.
- Assign exact mutable paths and one writer per shared surface.
- Parallelize read-only research/review only when tasks are independent.

## Handoff
Return Outcome, Evidence, Findings, Severity, Recommendation, Dependencies, Verification, Residual risks, Confidence and Files changed. Include exact paths/lines and test commands. Mark unverified statements as hypotheses.

## Integration
- Re-read changed files and independently verify critical claims.
- Resolve conflicting recommendations through evidence and repository contracts.
- Run tests after integrating changes; a specialist's green local test is not proof the integrated branch passes.
- Preserve commit history and attribution. Never force-push to simplify integration.
- Update issues and roadmap only when acceptance evidence changes.

## Runtime honesty
A profile is not a running agent. A skill is not an executable hook. Spawn independent agents only through an explicitly supported runtime and document the runtime/version. Otherwise orchestrate tools sequentially and do not claim parallel agent execution.
