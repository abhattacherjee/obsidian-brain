# Native runtime observations

Recorded on 2026-10-05. This ledger separates plugin discovery from running
handlers. It does not establish Codex parity.

## Local Claude format drift check

Run the read-only probe only against a source root you select explicitly:

```bash
python3 scripts/probe-claude-transcript-format.py --source-root /absolute/selected/projects
```

The probe does not run in CI or select a home directory. It reports aggregate
record/block type counts, malformed or unsupported schemas, and scan bounds.
It uses the production Claude parser, including exact verified metadata schemas.
It prints no source paths, session IDs, titles, messages, or tool values.
Symlinks are not followed. Defaults cap the scan at 1,000 files, 20,000 entries,
64 directory levels, 64 MiB, 1 MiB per line, and 10 seconds. Unknown formats,
unreadable/skipped sources, or exhausted bounds exit nonzero; `scan_complete`
is false. A clean result describes only the selected corpus and these bounds.

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

In the initial historical probe, both binaries accepted a local marketplace and installed version `3.8.0` in
separate disposable `CODEX_HOME` directories without authentication. The
marketplace uses `.claude-plugin/marketplace.json`. Installation alone did not
trust the handlers.

That historical `3.8.0` package discovered `hooks/hooks.json`. Its SessionEnd
handler had a one-second timeout; all four existing lifecycle handlers were
untrusted in the fresh homes. Project guidance did not select another hook file.
The current candidate has an explicit Codex descriptor and separate hook manifest;
these old observations do not describe or certify its installed dispatch.

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
| Cold startup and bounded recovery | Pending | Shared handlers are implemented and fixture subprocess checks exercise them. Measure final native interpreter startup, writer work, recovery bounds, and end deadline before acceptance |

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

## Shared capture implementation

On 2026-10-06, the user approved policy (b): well-formed unknown transcript
rows mark capture partial while known owned rows remain capturable. Malformed
known records and failed native ownership checks stay fail-closed. The approval
was verified from the trusted human review session. This policy choice does not
certify installed hook dispatch or full native acceptance.

The shared transcript reader and native adapters normalize visible records with
bounded byte reads and parser state. Reasoning, encrypted content, environment,
developer instructions, and internal HookPrompt are excluded. Stable native IDs
identify message mirrors. Byte-offset fallback IDs include the source generation,
so a rotated source cannot reuse another source's identity.

Each record is limited to 1 MiB, and a forward read consumes at most 4 MiB per
call. Opaque verification and draining stop at 256 MiB or 256 chunks under the
same deadline, with buffers no larger than 1 MiB. Larger sources stay visibly
pending. Oversized rows are drained without loading their bodies and retained
as generic unresolved references; verified later rows may still publish partial
capture. Recoverable mutation intents include their UTF8 content and metadata
within a 4 MiB limit; a 5 MiB whole-note mutation is explicitly unsupported.

A full fork can select a later child `session_meta` within the bounded batch.
Proven parent-owned records are excluded; ambiguous inherited history and missing
child metadata stay pending within the bounded search. Read-only provenance
verified the observed synthetic fork starts with its child `session_meta` at byte
0, ordinal 30. A larger inherited prefix is not an observed acceptance blocker.
Synthetic full-prefix tests exercise the defensive parser behavior.

`source_sessions` retains source descriptors, cursor state, native state,
completeness, first date, and message count. `native_events` retains scrubbed facts
below publication thresholds. Registered-source recovery rotates through a bounded
batch without scanning the full vault. Start/resume recovers before capture;
Stop captures before the retro gate; PreCompact captures and writes an immutable
snapshot in the legacy sessions folder by default, with explicit compact/clear
triggers and snapshot toggles; explicit SessionEnd flushes and marks the session
ended. `task_complete`
ends a turn; `turn_aborted` marks interruption. Completeness is a separate field.

`capture_revision` is SHA256 of the normalized managed capture region.
`applied_revision` is SHA256 of the whole published note. Deferred summaries will
bind `summary_revision` to the logical capture revision they summarized.

The explicit-host CLI wrapper records its entry time before handler work. Hooks
perform no AI calls, full-vault scans, or index rebuilds. Fixture subprocess checks
exercise this integration. They do not establish native desktop hook dispatch;
that live gate and plugin packaging remain pending.

## Sources

The installed app-server schemas and sanitized fixtures provide discovery
evidence. Native behavior still requires the live checks above. The official
[hook documentation](https://learn.chatgpt.com/docs/hooks) describes the normal
`/hooks` trust flow and Stop JSON output. The official
[plugin documentation](https://developers.openai.com/plugins/build/plugins)
describes explicit Codex hook selection. These documents guide the probes;
they do not replace observed execution.

## Current parser policy and note format

The user approved policy (b) on 2026-10-06: well-formed unknown rows leave
capture partial while verified visible rows before and after can publish.
Safe type labels describe unresolved input. Malformed known records, unverified
identity, and ownership failures still block. Deferred rows remain private and
are replayed only after byte-range/hash validation. This decision does not
certify live client acceptance.

Claude Code `2.1.291` has exact verified schemas for `mode`, `ai-title`,
`atis-latch`, `relocated`, `continued-in`, `bridge-session`, `frame-link`,
`cost-state`, `worktree-state`, `file-history-delta`,
`artifact-autoreact-ledger`, and `artifact-comment-monitor`. Only matching
schemas are metadata; arbitrary scalar rows remain unknown.

Codex turn brackets determine current-turn ownership. Canonical visible final
answers are retained; mirrored transport records do not create duplicate facts.
Reads bound each batch and record, not the total lifetime conversation.

Native session names are `<date>-<project>-<provider>-<identity-hash16>.md`.
Provenance includes `agent_provider`, `agent_session_id`, `capture_state`,
`capture_completeness`, and `capture_revision`. Existing taxonomy stays intact.
The current Codex descriptor sets the SessionEnd timeout to three seconds.
The shared handler uses a 2.5-second budget measured from wrapper entry.
The earlier native three-second probe tested a temporary handler; final installed
deadline acceptance remains pending. `templates/session.md` is a note-format
blueprint, not a runtime input. Capture generates its provenance and managed
revision hashes; placeholders must not be copied into published notes.

Conversation text escapes wikilinks and flattens embedded line breaks into
readable separators. Its headings and checkboxes cannot become note structure.
Consecutive coordination messages render as one counted line; their individual
identity receipts remain private and unchanged.

New native snapshots record an immutable UTC `created_at` receipt. Readers use
that timestamp for ordering and display, with the legacy HHMMSS filename
fallback. Earlier hash-named snapshots without a recorded time remain unknown;
file mtimes and hash order cannot establish their chronology.

Retirement stops automatic source replay without acknowledging retained input.
When no source is scheduled, SessionStart and repeated SessionEnd name retained
input for review instead of inventing a pending recovery count. SessionStart
notices use the invoking canonical project root; another project does not
receive those warnings. Doctor still reports unresolved input. A missing
transcript at a new SessionStart does not inflate that count.

Session coverage uses the adapter's verified native identity and recorded
project, including records beyond the former 64 KiB prefix. An invoking
worktree cannot stand in for missing historical provenance. Record, batch and
deadline bounds still apply; incomplete or duplicate identities cannot authorize
reconstruction. Missing-note candidates get priority over covered sources, with
the selected project first. The directory/header label only schedules work;
the parser must verify the recorded project. Known input can span several
bounded batches within the same ten-second deadline. Covered transcript bodies
are not reparsed by this missing-note check. A capped global window reports its
unscanned sources and disables reconstruction. Use `--project` to select another
project for a bounded audit; a global result does not certify every source.

### Coordination migration limits

Doctor's clean result does not certify that legacy journal migration completed.
Its read-only inspection can miss an old index-adjacent journal or conflicting
prior receipts, including the reviewed 248 MB case. The old index-adjacent path
is `<index-parent>/.<index-filename>.coordination/state.sqlite3`; the earlier
path-keyed namespace is
`<coordination_root>/obsidian-brain/vaults/<SHA256(canonical-vault-path)>/state.sqlite3`.
The selected journal is
`<coordination_root>/obsidian-brain/vaults/<physical-vault-digest>/state.sqlite3`.
Writer migration checks vault identity and refuses conflicting rows. A large
journal can exceed its bounded budget. Doctor cannot choose a winning receipt
or repair these conflicts. Preserve every journal and its SQLite WAL/SHM
sidecars for operator review; do not delete or replace them based on a clean
doctor result.
Unknown-row partial capture remains visible pending under policy (b); doctor
rechecking does not acknowledge or suppress unresolved source records.
