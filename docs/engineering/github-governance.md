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
- Require the Unit tests (Python 3.9) status check.
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
   updates. GitHub documents Dependabot version updates for all repositories.
7. Secret scanning runs automatically and for free on public repositories.
   Enable user-facing secret scanning alerts and push protection where offered;
   they are not all automatically enabled for public repositories.
8. Use either the committed CodeQL advanced workflow or GitHub's low-maintenance
   default setup, not both at once. GitHub recommends starting with default
   setup when custom configuration is unnecessary; this repository currently
   uses advanced setup to pin actions and control trusted PR analysis.
9. Standard GitHub-hosted runner usage is free for public repositories; larger
   runners are billed. Keep media fixtures small and generate them at runtime.
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
