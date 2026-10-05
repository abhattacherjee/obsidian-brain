# Native runtime observations

Recorded on 2026-10-05. This ledger separates plugin discovery from running
handlers. It does not establish Codex parity.

## Installed clients

| Client | Version | Evidence |
| --- | --- | --- |
| Codex CLI | `0.159.0-alpha.12.1` | `tests/fixtures/hosts/codex-cli-0.159.0-alpha.12.1-hooks.json` |
| Desktop bundled CLI | `0.159.0-alpha.12.1` | `tests/fixtures/hosts/codex-desktop-0.159.0-alpha.12.1-hooks.json` |

The desktop fixture comes from its bundled app-server binary. It is not proof
that a desktop conversation dispatched a hook. An earlier CLI observation was
`0.155.1`; the CLI changed during this session. The committed fixtures were
recorded again against the current binary.

## Discovery and installation

Both binaries accepted a local marketplace and installed version `3.8.0` in
separate disposable `CODEX_HOME` directories without authentication. The
marketplace uses `.claude-plugin/marketplace.json`. Installation alone did not
trust the handlers.

The current package discovers `hooks/hooks.json`. Its SessionEnd handler has a
one-second timeout; all four existing lifecycle handlers were untrusted in the
fresh homes. Project guidance did not select a different plugin hook file.

A disposable probe package with `.codex-plugin/plugin.json` and an explicit
`"hooks": "./hooks/codex-hooks.json"` selected its three-second SessionEnd
handler in both binaries. The probe's default `hooks/hooks.json` handler was
absent from those listings. Explicit hook selection therefore replaced default
discovery in this experiment.

`hooks/list` returned event name, source path, enabled state, timeout, handler
hash, and trust status. The fixture validator checks these fields and always
reports `runtime_verified: false`. A trusted listing is still not execution
evidence.

## Live acceptance gates

| Contract | Status | Evidence or remaining check |
| --- | --- | --- |
| No-auth installation | Observed | Both isolated native binaries installed the package |
| Explicit Codex manifest selection | Observed | Both selected the probe hook file and its timeout |
| Hook trust | Pending | Native review UI reached; execution approval pending |
| Start and resume | Pending | Capture native payload and rollout identity |
| Stop and loop protection | Pending | Exercise JSON blocking output and `stop_hook_active` |
| Compaction | Pending | Capture native event and transcript layout |
| End and deadline | Pending | Measure native dispatch and bounded handler exit |
| Fork and interruption | Pending | Record identity and interrupted transcript behavior |
| Skill resource handoff | Pending | Run an installed skill and resolve its package root |
| Native AI execution | Pending | Use the client's normal authentication and model |
| Desktop lifecycle | Pending | Computer-use access to the desktop app was denied; a temporary test task requires the user's explicit request |
| Cold startup and bounded recovery | Pending | Measure the installed handler after native trust |

## Sources

The installed app-server schemas and sanitized fixtures provide discovery
evidence. Native behavior still requires the live checks above. The official
[hook documentation](https://learn.chatgpt.com/docs/hooks) describes the normal
`/hooks` trust flow and Stop JSON output. The official
[plugin documentation](https://developers.openai.com/plugins/build/plugins)
describes explicit Codex hook selection. These documents guide the probes;
they do not replace observed execution.
