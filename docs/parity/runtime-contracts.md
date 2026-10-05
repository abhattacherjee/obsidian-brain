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
| Hook trust | Observed in CLI | User approved four temporary probe handlers; native `/hooks` trusted only those handlers |
| Start and resume | Observed in CLI | Native `source` values `startup`, `resume`, and `fork`; event IDs match rollout metadata |
| Stop and loop protection | Observed in CLI | Block JSON caused one `PROBE_CONTINUED` response, followed by `stop_hook_active=true` and `{}` |
| Compaction | Observed in CLI | Native `trigger=manual`; rollout contains a `compacted` record |
| End and deadline | Partly observed in CLI | `reason=other` on exit; a four-second handler did not reach its completion marker with the native three-second timeout. Exact cutoff time was not measured |
| Fork and interruption | Observed in CLI | Fork created a different thread ID; interrupted tool output retained the fork ID and native UI reported interruption |
| Skill resource handoff | Observed in CLI | Installed skill resolved its adjacent `probe.py` and package descriptor independently of cwd |
| Native AI execution | Observed | CLI used the user's configured `gpt-6.1-sol`; desktop used its existing task settings. Both ran synthetic identity checks |
| Desktop lifecycle | Partly observed | Authorized temporary desktop task's tool ID matched rollout metadata; task archived after the check. Desktop hook-manager access remains denied by computer-use tooling |
| Nested Claude inside Codex | Startup observed | Claude Code `2.1.289` emitted a different native session ID while inheriting the Codex thread ID; the isolated probe timed out after 45 seconds without completion |
| Cold startup and bounded recovery | Pending | Final runtime handlers do not exist yet; measure their interpreter startup, writer work, recovery bounds, and end deadline after implementation |

## CLI execution evidence

`tests/fixtures/hosts/codex-cli-0.159.0-alpha.12.1-lifecycle.jsonl` records
allowlisted fields from the disposable observer. Every event's session ID was
checked against the referenced rollout's `session_meta.payload.id` before export.
The Stop block and continuation were also visible in the native CLI.

Native hook commands had `PLUGIN_ROOT` and `PLUGIN_DATA`, but no
`CODEX_THREAD_ID`. The installed skill's tool shell had the matching
`CODEX_THREAD_ID`, but neither plugin environment variable. Skill resource paths
must therefore resolve from the installed skill location; hook paths can use
the hook-specific resource handoff.

The nested Claude startup fixture records only allowlisted metadata. Its native
Claude session ID differs from the inherited Codex ID. This establishes why
explicit host selection must take precedence over an inherited thread variable.
The probe's 45-second timeout does not establish Claude AI completion, End hook
dispatch, or a reason for the timeout.

SessionStart on resume was observed when the resumed thread began its next
turn. Opening the resume UI alone did not establish dispatch. Fork history
retained the parent's old tool output; the new tool invocation returned the
fork's ID. An adapter must use current native identity, not inherited messages.

The timeout probe logged its entry, slept for four seconds, and would have
written `timeout-completed` afterward. Native exit completed without that
marker. The logged `handler_elapsed_ms` covers handler work before log writing;
it excludes interpreter startup, fsync, and the deliberate sleep. It is not a
measurement of the final capture implementation's deadline.

## Sources

The installed app-server schemas and sanitized fixtures provide discovery
evidence. Native behavior still requires the live checks above. The official
[hook documentation](https://learn.chatgpt.com/docs/hooks) describes the normal
`/hooks` trust flow and Stop JSON output. The official
[plugin documentation](https://developers.openai.com/plugins/build/plugins)
describes explicit Codex hook selection. These documents guide the probes;
they do not replace observed execution.
