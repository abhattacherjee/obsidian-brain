# AGENTS.md

Read [CLAUDE.md](CLAUDE.md) for the repository's shared development, security,
architecture, and Git Flow rules. Its `.claude-plugin/` paths, `~/.claude/`
configuration, and `claude -p` commands describe the current implementation;
do not replace them with Codex paths unless the corresponding code exists.

## Codex guidance

- Run `./scripts/commit-preflight.sh` before committing. It runs the pytest
  coverage gate and the repository checks.
- Codex parity is specified in
  [docs/codex-claude-parity-design.md](docs/codex-claude-parity-design.md).
  The shared runtime, Codex lifecycle hooks, and Codex plugin packaging in
  that design have not shipped. Do not claim parity from this repository's
  Codex project guidance alone.
- Keep host-specific instructions in this file. Put shared rules in
  `CLAUDE.md` and link to them here so the two instruction files stay in step.
