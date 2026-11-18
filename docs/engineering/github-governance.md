# GitHub governance for this public repository

## Verified current state

As of 17 November 2026, the repository is public, with develop as the default
branch. GitHub API reports no active repository rulesets and reports develop
as unprotected. The CI workflow passing does not prevent direct pushes or
merges on its own.

GitHub documents branch protection rules and repository rulesets for public
repositories on GitHub Free. No paid plan is needed to protect this branch.
See the official [ruleset availability](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) and [ruleset management](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets) documentation.

## Recommended active ruleset for develop

Create one repository branch ruleset, set enforcement to Active, and target
develop. Configure:

- Require pull requests before merging.
- Require the single aggregate `Quality gates` status check from the CI workflow.
  Do not require conditional leaf jobs individually: docs-only changes
  intentionally skip runtime jobs, and the aggregate fails whenever a required
  job for the detected change class is not successful.
- For runtime changes, `Quality gates` requires the Python 3.9 regression
  suite, Python 3.12 regression suite, PostgreSQL repository tests on Python
  3.12, and the gRPC → Celery → FFmpeg → storage integration path. The Python
  3.9 PostgreSQL repository tests run inside that same integration job to
  avoid a second identical database container.
- For documentation-only changes, it requires the offline local-link validator
  and does not start Python/FFmpeg/PostgreSQL test runners.
- Keep Dependency Review as a separate security signal; require it only after
  confirming its behavior for both same-repository and fork-originated PRs.
- Require all PR conversations to be resolved.
- Require linear history.
- Block force pushes.
- Restrict branch deletion.
- Do not require an approving review while this remains a single-maintainer
  repository; a maintainer cannot approve their own PR.
- Keep bypass limited to repository administrators for documented emergencies.
  Do not grant routine bypass to automation.

Do not require CodeQL or dependency-review as merge checks until their behavior
has been verified on ordinary and fork-originated pull requests. They can
provide security feedback without blocking the initial contribution workflow.

The required status check must use the unique job name Unit tests (Python 3.9).
Avoid duplicate job/check names across workflows.

## Repository settings

1. Keep develop as the default branch.
2. Enable automatic deletion of head branches after PR merge.
3. Allow squash merging and disable merge commits. Rebase can be disabled too
   unless contributors have a demonstrated need for it.
4. Set the default GITHUB_TOKEN permission to read-only; each workflow declares
   the minimum permissions it needs.
5. Restrict Actions to GitHub-authored and verified actions used by this repo,
   or explicitly allow-list required actions.
6. Enable the dependency graph, Dependabot alerts, and Dependabot security
   updates. [Dependabot version updates are available for all repositories](https://docs.github.com/en/code-security/concepts/supply-chain-security/dependabot-version-updates).
7. Secret scanning [runs automatically and for free on public repositories](https://docs.github.com/en/code-security/how-tos/secure-your-secrets/detect-secret-leaks/enable-secret-scanning). Enable user-facing alerts and push protection where offered; these options are not all enabled automatically. The [repository security settings guide](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-security-and-analysis-settings-for-your-repository) describes the dependency-graph, Dependabot-alert, and push-protection switches.
8. Code scanning is available for public repositories. This repository already
   uses GitHub's low-maintenance [CodeQL default setup](https://docs.github.com/en/code-security/how-tos/find-and-fix-code-vulnerabilities/configure-code-scanning/configure-code-scanning).
   PR #61 produced a GitHub Advanced Security CodeQL check with no new alerts.
   Keep this default setup and do not add a second advanced workflow unless a
   concrete customization need justifies replacing it.
9. [Standard GitHub-hosted runner usage is free for public repositories](https://docs.github.com/en/billing/concepts/product-billing/github-actions); larger runners are billed. Keep media fixtures small and generate them at runtime.
   Advanced CodeQL analysis uses Actions workflows; standard hosted-runner
   usage remains free for this public repository.

## Agent customization

Shared, portable skills live in .agents/skills so they can be used by coding
agents that support the Agent Skills convention. GitHub Copilot-specific
custom agent profiles live in .github/agents. The project uses AGENTS.md for
cross-agent repository instructions and .github/copilot-instructions.md for
Copilot repository instructions. See GitHub's [agent skills](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/add-skills) and [custom agent profiles](https://docs.github.com/en/copilot/reference/custom-agents-configuration) documentation.

## Solo-maintainer merge policy

A focused branch is reviewed by the repository owner, all required checks must
pass, and the PR is squash-merged. An approval requirement is intentionally
omitted until an independent trusted reviewer exists. Revisit this policy when
a second maintainer joins.

## Branch cleanup

Delete only after checking open PRs and comparing the branch with develop.
Merged PR branches, abandoned experiments superseded by merged changes, and
historical branches with zero unique commits are candidates. Never delete
develop, an open PR head, or a branch with unique unreviewed work. Prefer the
GitHub automatic head-branch deletion setting for future merges.

## CI execution policy and cost controls

The CI workflow uses an always-running change-classification job and one stable
aggregate status check named `Quality gates`.

- Documentation-only updates run the local Markdown-link validator and skip
  media/database/runtime jobs. If any non-documentation path is included, the
  complete runtime suite still runs.
- Runtime changes run Python 3.9 and Python 3.12 regression suites in parallel,
  the VOD integration path, and PostgreSQL repository tests on Python 3.12.
  The Python 3.9 PostgreSQL repository suite is part of the integration job,
  reusing its existing database service.
- FFmpeg is installed only in the VOD integration job. The Python regression
  and PostgreSQL-only jobs do not install media binaries; the real thumbnail
  and HLS/DASH package suites run once in the FFmpeg job.
- The standalone synthetic-corpus generation step is omitted because the
  HLS/DASH harness creates and validates its own temporary fixtures.
- FFmpeg/FFprobe are checked before installation; apt update/install occurs
  only when the integration runner does not already provide both binaries.
- Pip caching remains keyed to the corresponding pinned requirement file.
- The change classifier fails closed: an unknown/empty diff runs all checks.
- Concurrency cancels superseded commits for the same PR/ref. Independent PRs
  and workflows remain isolated.
- The aggregate check is the branch-protection contract. Individual conditional
  jobs should not be required directly because a skipped job is expected for
  unrelated change classes.

Avoid reducing runtime coverage to make CI faster. Optimize duplicated setup,
redundant services and irrelevant test categories first.

## Workflow security and cost

- Pin third-party actions to full commit SHAs and let Dependabot update them.
- Keep permissions minimal and never use pull_request_target to execute
  untrusted PR code.
- Cancel superseded runs with concurrency groups.
- Do not upload generated media as Actions artifacts unless required.
- Use scheduled analysis and limit redundant jobs.
- Treat CI success as evidence only for the commands it actually ran.

## Administrative operations not committed here

Ruleset creation, branch protection, repository-level security switches,
merge-method settings, and automatic branch deletion are repository settings.
The connected GitHub integration can read these settings but does not expose
the write operations needed to apply them. GitHub requires repository admin
access (or the edit repository rules permission) to create or edit rulesets. They must be confirmed by a
repository administrator in GitHub settings.
