# Codex project configuration

Codex repository guidance is in [`AGENTS.md`](../AGENTS.md).
`hooks.json` registers the same Git policy scripts used by Claude Code, with
paths resolved from the current repository root. Outside a Git worktree, the
commands warn and allow the tool call because this repository's policy cannot
identify a target checkout.

The [Codex hook documentation](https://learn.chatgpt.com/docs/hooks), checked
on 2026-10-05 with local `codex-cli 0.159.0-alpha.12.1`, shows a hook command using
`$(git rev-parse --show-toplevel)`, states that commands run with the session
`cwd`, and defines the `Bash` PreToolUse deny JSON. The registration test
checks the allow, deny, and missing-worktree command outcomes. An installed
client run is still needed before treating this as proof of hook loading.

These project policy hooks are separate from the plugin's registered
`hooks/codex-hooks.json`, selected by `.codex-plugin/plugin.json`. The plugin
hooks currently skip every event without retaining a source because current
CLI/Desktop client binding is unverified. Their installation does not establish
capture parity. See [installation and status](../README.md#codex-installation)
and [native evidence](../docs/parity/acceptance-evidence.md).
