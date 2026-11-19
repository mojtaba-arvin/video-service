# Branch lifecycle audit — 2026-11-19

**Snapshot date:** 2026-11-19  
**Repository:** `mojtaba-arvin/video-service`  
**Integration branch:** `develop`  
**Develop HEAD:** `a058583a3a57f03f7cffc82d80decd059a208bb6` (PR #101)  
**Remote branch count:** 27, including `develop`  
**Open pull requests at audit time:** 0

## Decision policy

- Never merge a branch merely because it is ahead of `develop`; compare its actual file delta, PR history, tests and compatibility first.
- A branch is a deletion candidate only when its associated PR is confirmed merged, or comparison proves zero commits ahead and an empty file diff.
- A closed-but-unmerged PR is not merge evidence.
- Keep unreviewed divergent implementation branches until their unique behavior is compared with the merged implementation.
- This audit is read-only. No branch refs were deleted or rewritten.

## Branch disposition

| Branch | Ahead / behind `develop` | Evidence / relationship | Decision |
|---|---:|---|---|
| `develop` | baseline | Current integration branch | **KEEP** |
| `chore/branch-cleanup-audit` | 1 / 22 | Old 18-Nov audit; superseded by this snapshot | **DELETE after this audit is merged and rechecked** |
| `chore/env-example-hygiene` | 5 / 30 | PR #56 merged | DELETE candidate |
| `chore/github-governance-agent-dataset` | 66 / 25 | PR #61 merged | DELETE candidate |
| `ci/fast-doc-aware-quality-gates-2026-11-18` | 0 / 15 | Empty diff; no unique commits | DELETE candidate |
| `docs/ai-native-video-orchestration-strategy` | 5 / 31 | PR #86 merged; stale divergent docs branch | DELETE candidate after fresh merged-content check |
| `docs/outcome-oriented-backlog` | 9 / 29 | PR #58 merged | DELETE candidate |
| `docs/parallel-platform-research-2026-11-18` | 0 / 17 | Empty diff; no unique commits | DELETE candidate |
| `docs/parallel-research-and-progress-2026-11-18` | 7 / 17 | Unique research files; Issue #71 references this research path | **KEEP until research is reviewed and migrated to develop** |
| `docs/research-and-roadmap-v2` | 0 / 16 | Empty diff; no unique commits | DELETE candidate |
| `docs/sync-ci-and-backlog-status` | 3 / 26 | PR #60 merged | DELETE candidate |
| `feat/config-baseline-ci` | 6 / 31 | PR #55 merged | DELETE candidate |
| `feat/durable-workflow-state` | 98 / 22 | 39-file divergent code delta; core durable workflow later merged through PR #68, but branch has not had a complete content-level salvage review | **KEEP for code-level comparison; do not merge wholesale** |
| `feat/package-publication-invariants` | 54 / 22 | PR #64 merged | DELETE candidate |
| `feat/validate-streaming-packages` | 16 / 24 | PR #62 merged | DELETE candidate |
| `feat/validate-thumbnail-artifacts` | 4 / 28 | PR #59 merged | DELETE candidate |
| `feature/celery` | 0 / 41 | Empty diff; no unique commits | DELETE candidate |
| `feature/ffmpeg` | 0 / 36 | Empty diff; no unique commits | DELETE candidate |
| `feature/grpc` | 0 / 44 | Empty diff; no unique commits | DELETE candidate |
| `fix/ffmpeg-process-tree-supervisor-v2` | 0 / 16 | Empty diff; no unique commits | DELETE candidate |
| `fix/redact-sensitive-operational-logs-2026-11-18` | 11 / 17 | PR #85 merged | DELETE candidate after fresh merged-content check |
| `fix/thumbnail-scale-and-render-tests` | 5 / 29 | PR #57 merged | DELETE candidate |
| `fix/watermark-deadline-and-process-supervision` | 7 / 12 | PR #91 closed but not merged; similar later work exists in PR #92 | **KEEP pending explicit comparison with PR #92** |
| `fix/watermark-deadline-and-process-supervision-v2` | 8 / 11 | PR #92 merged | DELETE candidate after fresh merged-content check |
| `security/redacted-operational-logs-v2` | 0 / 16 | Empty diff; no unique commits | DELETE candidate |
| `test/grpc-celery-vod-e2e` | 29 / 23 | PR #63 merged | DELETE candidate |
| `test/synthetic-vod-hls-dash-harness` | 0 / 21 | Empty diff; no unique commits; PR #66 merged | DELETE candidate |

Ahead/behind values are GitHub comparison counts relative to the current `develop` HEAD. For divergent, squash-merged branches, a nonzero ahead count does not prove that the behavior is missing from `develop`; verify the merged PR and file content before deletion.

## Merge decision

There were **no open PRs** at the time of this snapshot, so there was no existing PR eligible for merge. The divergent branches are not safe to merge wholesale: several are old snapshots of code already merged through later PRs, while the durable-workflow and research branches still require targeted content review.

The current integration point is PR #101 at `a058583a3a57f03f7cffc82d80decd059a208bb6`. PR #100 introduced the versioned transport-neutral execution-plan contract; PR #101 synchronized the execution plan and roadmap.

## Cleanup result and limitations

- **9 branches** have zero commits ahead and an empty diff; they are deletion candidates under the stated rule.
- Additional branches are deletion candidates because their associated PRs are merged, subject to a fresh HEAD check and merged-content verification.
- **4 divergent branches** are explicitly retained for targeted review: the research branch, durable-workflow branch, closed-unmerged watermark branch, and this superseded audit branch until the new audit is integrated.
- No branch deletion is reported as completed. The available connected GitHub operations in this session do not expose a DELETE-ref action; do not simulate deletion by force-updating refs.
- The repository has an active `Protect develop` ruleset according to PR #98, but it does not require the aggregate `Quality gates` check. Required-check enforcement still needs repository-admin configuration.
