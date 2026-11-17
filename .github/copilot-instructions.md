# Copilot instructions

Read the repository-root AGENTS.md before changing code. It defines the
current VOD architecture, correctness and security boundaries, verification
commands, and task-specific skills in .agents/skills/.

Keep changes narrow and evidence-backed. Preserve the legacy Python 3.9
backward-compatibility check while treating it as end-of-life; do not claim the
full service is Python 3.12 compatible from the test-harness job alone. Prefer
generated FFmpeg lavfi fixtures over downloaded media, add
tests in the same PR as behavior changes, and update Issue #54 when acceptance
status changes. Do not turn roadmap items into implemented-feature claims.

Use the specialist profiles under .github/agents/ for focused media-pipeline,
read-only security-review, or orchestration/MCP design tasks when appropriate.
