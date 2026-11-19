# Agent engineering standard

**Status:** Active repository operating standard  
**Applies to:** Human contributors, GitHub Copilot agents, and any future delegated coding-agent runtime

## 1. Purpose and capability boundary

Use focused roles to improve review quality and reduce context switching without creating parallel edits to shared contracts. A profile or skill is an instruction set; it is not proof that an agent runtime can spawn an independent session. Delegation is enabled only when the selected runtime explicitly supports it and the configured handoff is observable.

The repository's currently documented specialist profiles are:
- `.github/agents/orchestrator.agent.md` — plan, assign, integrate and report.
- `.github/agents/security-auditor.agent.md` — read-only security review.
- `.github/agents/media-pipeline-engineer.agent.md` — media implementation and regression verification.
- `.github/agents/orchestration-and-mcp-architect.agent.md` — durable workflow and typed tool contracts.

Skills under `.agents/skills/` are reusable procedures. Load only the task-relevant skills; do not dump every skill into every agent context.

## 2. Role and permission matrix

| Role | Owns | Permissions | Must not |
|---|---|---|---|
| Orchestrator | scope, dependency graph, branch/PR plan, integration and final evidence | read/search; delegate only if runtime supports it | silently implement a specialist's review; claim unavailable delegation |
| Researcher | repository/history/API/compatibility evidence | read/search | edit implementation or treat design prose as runtime proof |
| Architect | contracts, state transitions, ADR and migration/rollback design | read/search; propose edits only when assigned | introduce distributed infrastructure without workload evidence |
| Implementer | one assigned vertical slice and its tests | edit/execute only in assigned paths | edit another owner's files or broaden scope |
| Security reviewer | threat model, exploitability, residual risk | read/search only | modify code or approve own implementation |
| Test/CI engineer | test strategy, CI diagnosis, reproducibility | read/search/execute; edit workflow only when assigned | convert skipped checks into passes |
| Documentation engineer | user/operator docs and traceability | edit only assigned docs | describe target capabilities as shipped |

Default to one writer per shared file, interface, migration, protobuf contract, or state machine. Security review and implementation must be separate roles. The orchestrator integrates; it does not bypass review.

## 3. Task lifecycle

1. **Discover:** read `AGENTS.md`, relevant skills, current-state docs, owning Issue/ADR, latest `develop`, open PRs and branches. Search for overlapping work and unmerged predecessors.
2. **Frame:** write one outcome, explicit non-goals, acceptance criteria, affected paths, risk class, dependencies, rollback and verification plan.
3. **Design:** inspect implementation and tests; document state transitions and compatibility before persistence/API/security changes. Record material decisions in an ADR.
4. **Assign:** split only independent deliverables. Name one owner per mutable surface and define exact file/path boundaries and handoff dependencies.
5. **Implement:** one reviewable vertical slice; tests and docs in the same change. Avoid speculative refactors and unrelated formatting.
6. **Verify:** run focused tests, relevant full suites and every required CI lane. Inspect logs for the actual failure cause. Record commands, counts, skipped tests and environment limits.
7. **Review:** independent read-only security/architecture review for high-risk changes; resolve findings or record explicit accepted residual risk.
8. **Integrate:** compare with latest base, inspect changed files, check PR overlap, preserve commit history and attribution, then create/update one focused PR.
9. **Close the loop:** update Issue/ADR/roadmap only when acceptance evidence changes. Report shipped, unverified and deferred items separately.

## 4. Mandatory handoff contract

Every delegated task and every specialist review must return this structure:

- **Outcome:** the exact assigned outcome.
- **Evidence:** repository paths, line ranges, commit/PR links, test output or source references.
- **Findings:** facts and defects; distinguish confirmed from inferred.
- **Severity:** Blocker / High / Medium / Low / Informational, with rationale.
- **Recommendation:** smallest safe next action.
- **Dependencies:** required decisions, owners and blocked tasks.
- **Verification:** commands/checks run, results, test counts and skipped lanes.
- **Residual risks:** what was not proven, including runtime/deployment unknowns.
- **Confidence:** High / Medium / Low and why.
- **Files changed:** exact paths and ownership; empty for read-only reviews.

A handoff without evidence is a hypothesis, not a completed task. The receiver must independently validate critical claims before integration.

## 5. Parallelism and conflict control

Parallelize discovery, independent test design, documentation research and read-only review. Serialize:
- schema/migration and repository interface changes;
- protobuf/API/authentication contracts;
- shared state-machine and retry/cancellation semantics;
- common CI workflow or dependency-manifest edits;
- changes to the same files or generated assets.

Before parallel work, record file ownership and dependency edges. If two tasks need the same contract, architect first, then assign implementation. Never ask multiple writers to edit the same branch concurrently. Integrate in dependency order and rerun affected tests after integration.

## 6. Security and reliability gates

For security-sensitive work, require the security-boundary-review skill and an independent read-only review. For durable workflow changes, require durable-workflow-design and explicit duplicate, crash, retry, lease-expiry, cancellation and recovery behavior. For media changes, require synthetic fixtures, FFprobe validation and process cleanup coverage.

Never:
- put credentials, bearer tokens, signed URLs or raw webhook secrets in plans, outbox payloads, broker messages, logs or test artifacts;
- expose arbitrary shell, FFmpeg commands, filter graphs, filesystem paths or unbounded URL fetching as agent tools;
- claim exactly-once delivery;
- activate automatic dispatch/replay before durable claims and idempotent side effects cover every dispatched stage;
- call CI green equivalent to production readiness;
- rewrite shared branch history or alter historical authorship to fix attribution.

## 7. GitHub and change management

- Inspect open PRs, issue ownership, branch divergence and overlapping files before creating a branch.
- Use a dedicated branch and one outcome-oriented PR for each independent deliverable.
- Keep commit messages factual and scoped; avoid vague messages and unrelated commit stacking.
- Do not merge with pending/failed required checks or unresolved blocking review threads.
- Preserve existing commits; prefer additive corrective commits.
- Verify the final remote head, commit identity where available, changed-file list, PR checks and review state.
- Update issue acceptance checkboxes only when the corresponding evidence exists.

## 8. Hooks, skills and runtime integration

Skills are portable procedures; profiles are role prompts; hooks are runtime lifecycle automation. A hook is effective only if the chosen agent runtime loads and executes it. Do not add nominal hook files for an unsupported runtime or describe them as enforced controls.

When adopting a runtime:
1. identify its documented profile, delegation, permission, hook and tool syntax;
2. pin the runtime/version assumptions in its integration documentation;
3. start with read-only reviewer permissions and explicit writer path ownership;
4. add hooks only for deterministic checks (format, tests, secret scan, handoff validation);
5. test hook execution with a deliberate failing fixture and prove it blocks/records the event;
6. keep CI as the authoritative enforcement boundary.

If no delegation runtime is available, the orchestrator may sequence tools and maintain handoffs, but must describe that honestly as tool orchestration—not independent subagents.

## 9. Evaluation and continuous improvement

Track outcomes, not agent count:
- first-pass acceptance rate and review rework;
- escaped regressions and security findings;
- test/CI failure diagnosis accuracy;
- duplicate/overlapping PRs avoided;
- handoff completeness and evidence quality;
- time blocked on shared contracts;
- token/runtime cost per accepted change.

After each material failure, convert the lesson into the narrowest durable control: regression test, skill rule, profile instruction, CI check or ADR. Do not add generic rules without a concrete failure mode and a way to verify compliance.

## 10. Completion report

Every completed increment reports: outcome, exact commits/PRs, changed paths, tests/CI, review state, remaining risks, issue status, and the next dependency-ready task. Distinguish implemented, merged, deployed and production-verified states.
