# Remote branch cleanup audit

**Snapshot:** 18 November 2026  
**Repository:** mojtaba-arvin/video-service  
**Protected working branch:** develop  
**Develop HEAD:** e92a31b27eaaf7e0d082550fa3aea493bf1d9ba5  
**Branch count at snapshot:** 16 (including develop)

Deletion rule: delete only a branch whose associated PR is confirmed merged, or whose comparison against develop proves zero unique commits and zero file diff. A closed-but-unmerged PR is not sufficient. Branch age is not a criterion.

## Branch-by-branch result

| Branch | HEAD | Comparison with develop | PR evidence | Decision |
|---|---|---|---|---|
| develop | e92a31b27eaaf7e0d082550fa3aea493bf1d9ba5 | baseline | protected working branch | **KEEP** |
| feat/config-baseline-ci | 13240c538180f34dcbe0cbea2397acf5a2c0d7b7 | 6 ahead / 9 behind; changes are in merged PR | #55 merged at fd3ef556 | DELETE candidate |
| chore/env-example-hygiene | 09595b72b618eafdfe9bdcbb9f0ba2267f8ec7cf | 5 ahead / 8 behind; changes are in merged PR | #56 merged at aaf72994 | DELETE candidate |
| fix/thumbnail-scale-and-render-tests | 77bc1704ad36658d974a677d6f2c9ddedc7ce15f | 5 ahead / 7 behind; changes are in merged PR | #57 merged at f5ca8034 | DELETE candidate |
| docs/outcome-oriented-backlog | c3d6b73b9e82d1ceb21d9c1fb5695fdc630811be | 9 ahead / 7 behind; changes are in merged PR | #58 merged at 9ae98bb2 | DELETE candidate |
| feat/validate-thumbnail-artifacts | 01d246970858df11f4db42f075e7894ba862e223 | 4 ahead / 6 behind; changes are in merged PR | #59 merged at a11dd491 | DELETE candidate |
| docs/sync-ci-and-backlog-status | 697e78744e9f6c3a4b7c59c595a05fc2230eb2c2 | 3 ahead / 4 behind; changes are in merged PR | #60 merged at ef748a3f | DELETE candidate |
| chore/github-governance-agent-dataset | 5ffe7e23dc832fe597294c2adc27633e99097010 | 66 ahead / 3 behind; changes are in merged PR | #61 merged at cd3a6ea5 | DELETE candidate |
| feat/validate-streaming-packages | 6cabad58673e0f7d610de99731fc10e8ba47c3e4 | 16 ahead / 2 behind; squash/merge result is present on develop | #62 merged at 6017d9e4 | DELETE candidate |
| test/grpc-celery-vod-e2e | 23e7ae0721844a342a0bafa03359d272e10080d5 | 29 ahead / 1 behind; merged changes are present on develop | #63 merged at e92a31b2 (current develop HEAD) | DELETE candidate |
| feature/celery | 5bcfcbf5b991c24e05feaa7b7938904c16010b1b | 0 ahead / 19 behind; empty file diff | Closed historical PRs #7, #8, #9, #30, #32, #34, #36–#39, #47; no unique commit remains | DELETE candidate |
| feature/ffmpeg | 515143bbf451e01e669a1652f1ffde079b74a582 | 0 ahead / 14 behind; empty file diff | Closed historical PRs #13–#29, #33, #40, #43, #44, #48–#50; no unique commit remains | DELETE candidate |
| feature/grpc | 2dbde2ed5789f0303d6e8d07fa0ae23525a1578f | 0 ahead / 22 behind; empty file diff | Closed historical PRs #1–#6, #10–#12, #18, #31, #35, #41, #42, #45, #46; no unique commit remains | DELETE candidate |
| docs/ai-native-video-orchestration-strategy | b9bc50490eb58102b7ba2a8b8b119249c1afeb81 | 5 ahead / 9 behind; documentation diff remains | #53 closed, **not merged**; replaced by #58 but not proven identical | **KEEP pending content-level supersession review** |
| feat/durable-workflow-state | 8d5a6b85555d91669639d78877358e4794481f36 | 75 ahead / 0 behind; substantial unique code | No PR | **KEEP** |
| feat/package-publication-invariants | 9048d06b9657fc50ffc0c71241fdcfbd87e1ecb0 | 53 ahead / 0 behind; substantial unique code | #64 open | **KEEP** |

## Summary

- 16 branches observed.
- 12 are deletion candidates: nine branches associated with merged PRs (#55–#63) and three historical branches with zero unique commits and empty diffs against develop.
- Four must remain: develop; the unmerged/replaced documentation branch pending content comparison; the unreviewed durable-workflow branch; and the open PR #64 branch.
- PR #53 is closed but not merged. It is deliberately excluded from deletion.
- PR #64 is open and deliberately excluded from deletion.
- No branch was deleted during this audit because the connected GitHub actions available in this session expose branch reads and ref updates, but not the GitHub DELETE-ref operation. The audit does not simulate deletion by moving refs.

## Safe deletion set

`feat/config-baseline-ci`, `chore/env-example-hygiene`, `fix/thumbnail-scale-and-render-tests`, `docs/outcome-oriented-backlog`, `feat/validate-thumbnail-artifacts`, `docs/sync-ci-and-backlog-status`, `chore/github-governance-agent-dataset`, `feat/validate-streaming-packages`, `test/grpc-celery-vod-e2e`, `feature/celery`, `feature/ffmpeg`, `feature/grpc`.

The deletion operation should use exact branch names and be performed only after a fresh HEAD check against the SHAs recorded above. Retain develop.
