# AGENTS.md

Read [CLAUDE.md](CLAUDE.md) for the repository's shared development, security,
architecture, and Git Flow rules. Shared runtime rules live there; native
storage and execution are selected by explicit host adapters.

## Codex guidance

- Run `./scripts/commit-preflight.sh` before committing. It runs the pytest
  coverage gate and the repository checks.
- Codex parity is specified in
  [docs/codex-claude-parity-design.md](docs/codex-claude-parity-design.md).
  The shared runtime and plugin packaging are implemented on this branch,
  but full parity has not shipped. Current-client binding and native Desktop
  dispatch remain unverified. See [acceptance evidence](docs/parity/acceptance-evidence.md).
- Native Codex context requires `codex-cli` or `codex-desktop` explicitly.
  The launcher declares `OB_CLIENT` before starting the native client; registered
  hooks pass that value as `--client`. CLI launches require
  `OB_CLIENT=codex-cli codex --no-daemon`. Registered capture refuses shared-server
  ancestors, unreadable ancestry, and Desktop dispatch. Verify hook process
  ancestry and arguments independently before claiming native binding.
  Do not infer the current frontend from a thread's creation metadata or a
  daemon's environment. Missing identity skips native hook capture; no source is retained.
- Keep host-specific instructions in this file. Put shared rules in
  `CLAUDE.md` and link to them here so the two instruction files stay in step.
