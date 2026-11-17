# Copilot instructions

Read the repository-root AGENTS.md before changing code. It defines the
current VOD architecture, correctness and security boundaries, verification
commands, and task-specific skills in .github/skills/.

Keep changes narrow and evidence-backed. Preserve the Python 3.9 compatibility
baseline, prefer generated FFmpeg lavfi fixtures over downloaded media, add
tests in the same PR as behavior changes, and update Issue #54 when acceptance
status changes. Do not turn roadmap items into implemented-feature claims.
