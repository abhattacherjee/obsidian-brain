# Codex project configuration

Codex repository guidance is in [`AGENTS.md`](../AGENTS.md).
`hooks.json` registers the same Git policy scripts used by Claude Code, with
paths resolved from the current repository root. Outside a Git worktree, the
commands warn and allow the tool call because this repository's policy cannot
identify a target checkout.

The [Codex hook documentation](https://learn.chatgpt.com/docs/hooks), checked
on 2026-09-24 with local `codex-cli 0.155.1`, shows a hook command using
`$(git rev-parse --show-toplevel)`, states that commands run with the session
`cwd`, and defines the `Bash` PreToolUse deny JSON. The registration test
checks the allow, deny, and missing-worktree command outcomes. An installed
client run is still needed before treating this as proof of hook loading.

These project policy hooks are separate from the session capture and recovery
hooks planned in the
[parity design](../docs/codex-claude-parity-design.md). The latter need
host-specific runtime adapters and are not registered yet.
