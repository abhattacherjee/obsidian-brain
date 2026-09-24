# Codex project configuration

Codex repository guidance is in [`AGENTS.md`](../AGENTS.md).
`hooks.json` registers the same Git policy scripts used by Claude Code, with
paths resolved from the current repository root. Codex's documented `Bash`
PreToolUse input and deny output match the contracts those scripts use.

These project policy hooks are separate from the session capture and recovery
hooks planned in the
[parity design](../docs/codex-claude-parity-design.md). The latter need
host-specific runtime adapters and are not registered yet.
