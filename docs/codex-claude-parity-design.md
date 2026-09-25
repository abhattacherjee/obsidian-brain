# Obsidian Brain: Claude Code and Codex parity

- Date: 2026-09-25
- Status: Draft under full review; project guidance and an interim Codex identity guard have shipped, full parity runtime has not
- Source baseline: `184715d` on `develop`, plugin `3.5.1`
- Tracking: [#272](https://github.com/abhattacherjee/obsidian-brain/issues/272) (implementation); [#360](https://github.com/abhattacherjee/obsidian-brain/issues/360) (closed specification tracker)
- Specification PR: [#361](https://github.com/abhattacherjee/obsidian-brain/pull/361), merged to `develop`
- Supersedes: `docs/codex-compatibility-design.md`

## 1. Outcome and agreed scope

Claude Code, Codex desktop, and Codex CLI use the same Obsidian vault. Users can switch between them or run them simultaneously without losing knowledge, mixing session identities, or requiring the other assistant to be installed.

The user selected these requirements:

1. Support Codex desktop and CLI alongside Claude Code.
2. Deliver full parity across every skill in the §9 table (19 at baseline `b7d8597`) and automatic capture/recovery. Intermediate milestones are not a parity release.
3. AI operations use the invoking assistant's backend. No silent cross-provider fallback.
4. Keep existing folders, tags, note types, filenames, and backlinks compatible. No bulk renaming.
5. Checkpoint after completed turns, update one session note, save before compaction, and finalize at session end. Apply this behavior to both assistants.

Parity means equivalent user outcomes, provenance, safeguards, and recovery. It does not require identical tool names, invocation syntax, model names, or hook payloads.

The first supported local environments are macOS desktop/CLI and Linux CLI. Windows and remote/cloud sessions without access to the local vault are outside this release. This is a filesystem plugin: no daemon, MCP server, Obsidian extension, or hosted service is introduced.

## 2. Evidence and changes from the previous proposal

| Evidence | Design consequence |
|---|---|
| `extract_user_messages`, `extract_assistant_messages`, and `extract_tool_uses` in `hooks/obsidian_utils.py` recognize Claude/flat records. Against the sampled current Codex rollout, 3 user and 4 assistant message records produced zero messages and zero tools. | Add an explicit Codex parser. Reading JSONL successfully is not a successful import. |
| `_resolve_session_id` prioritizes `CLAUDE_CODE_SESSION_ID` and can fall back to recent Claude transcripts. The inspected Codex process had different Claude and Codex IDs. | Runtime identity must be explicit. Never infer a Codex session from a Claude ID or the newest transcript. |
| `obsidian_session_log.py` restricts transcript paths to Claude storage. The reaper also discovers Claude project files. | Separate host discovery and validation from capture/recovery logic. |
| Six `claude -p` sites exist in `hooks/`: `generate_snapshot_summary`, `generate_theme_names` (also used by `consolidate_cli.py`), `generate_summary`, and `generate_summaries_batch` in `obsidian_utils.py`; `run_semantic_merge` and `_dispatch_classifier_chunk` in `check_items_cli.py`. | Move all six through the invoking host's typed AI execution interface; the host-neutral lint in §12 inventories every subprocess site. |
| All 19 skills contain `.claude` paths (205 occurrences at this baseline). `standup`, `emerge`, `recall`, `obsidian-setup`, and `retro` also name Claude-only task, agent, or question tools. | Share procedures but move path resolution and orchestration into deterministic helpers and paired host reference files. |
| `deep_cli.py` uses a single `deep-pipeline.json` cache with an age-only reuse check. Session state and several scratch paths are not host-scoped. | Scope jobs and cache keys to their actual inputs, vault, host, and session. |
| `note_writer.py` already locks append updates; session writing and summary upgrades have separate write paths. | Extend one writer transaction protocol to every producer. Atomic rename alone is insufficient. |
| Existing session filenames use a short hash. Recovery uses existence checks and an mtime watermark. | Use full identity checks and per-source revision cursors; preserve legacy names when their identity matches. |
| At `184715d` there are 96 `test_*.py` files. `setup.cfg` excludes `hooks/obsidian_utils.py` and `hooks/deep_cli.py` from the coverage threshold. `CLAUDE.md` was corrected in #361. | New runtime modules must not be added to those exclusions. |

Changes since the original `b7d8597` baseline: #361 added project guidance, `.codex/hooks.json`, and the single-source hook test; #362/#363 added the interim Codex marker guard to session resolution; #364 changed unreadable-directory and cascade behavior; #365 added a Bash-truth oracle and tightened the PR-base gate. Recheck counts and current behavior at each implementation stage instead of treating the original baseline as live code.

The July proposal landed through #270/#271 as documentation. Vault history records #272 being closed because its prerequisites were not actionable, not because Codex support shipped. Sources: `2026-07-26-obsidian-brain-f609` and `2026-09-05-obsidian-brain-55e8`.

Local `codex --version` reported `0.155.1`. Its help exposes `exec`, `--ephemeral`, `--output-schema`, and `--output-last-message`. A real rollout was inspected for record shapes only; private conversation content must not become a committed fixture. Desktop lifecycle behavior has not yet been tested end to end.

OpenAI hook documentation, checked 2026-09-25, says `SessionEnd` and `Interrupt` default to **one second** and allow at most **three seconds**; other hooks normally default to 600 seconds. `hooks/hooks.json` sets no timeout, so the currently auto-discovered Codex `SessionEnd` handler gets the one-second default. Switching conversations is not immediate session end. Recheck these claims for each supported client version in §11 step 1. The transcript format is not a stable hook interface. These are reasons for turn checkpoints and versioned parser fixtures, not reasons to substitute `Stop` for finalization. [Hooks](https://learn.chatgpt.com/docs/hooks)

## 3. Architecture

Keep one shared Python core and one authored set of skill procedures. Use thin host adapters for the parts that differ. Runtime helpers remain Python stdlib-only; this work adds no runtime package dependency.

| Boundary | Responsibility |
|---|---|
| Runtime adapter | Identify the host/client, normalize hook input/output, expose supported capabilities, discover and validate that host's transcripts. |
| Transcript adapter | Convert records into ordered messages, tool calls/results, timestamps, lineage, and source revision information. |
| Capture service | Apply filtering, checkpoints, snapshots, finalization, and replay rules without knowing host JSON layouts. |
| Writer service | Validate paths, scrub secrets, check identity/revision, lock, merge owned content, and atomically publish notes. |
| AI execution adapter | Run summary/classification work using the invoking host, validate returned data, and report failures. |
| Existing knowledge core | Keep indexing, retrieval, themes, clustering, note validation, and deterministic triage shared. |
| CLI and skills | CLI performs data operations; skills supply user interaction, synthesis, and host-appropriate orchestration. |

Proposed module boundaries are `runtime_context.py`, `runtime_adapters/`, `transcripts/`, `capture.py`, `ai_backend.py`, and `brain_cli.py` under `hooks/`. Extend `note_writer.py` as the writer entry point. Existing entry points and imports remain compatibility wrappers during extraction. Avoid moving unrelated algorithms. As each boundary moves, update `docs/architecture/architecture.json` in the same stage: `transcript-parser` and `summarizer` components, plus the `orphan-reaper`, `theme-seeding`, `recall-deferred-summarize`, `session-end-autolog`, and `precompact-snapshot` flows currently describe the Claude-only implementation. Add and validate component `hosts` when adapters first appear, then re-render the architecture page; the current smoke test only verifies file existence and will not catch stale flow text.

Rejected alternatives: two independently maintained plugins would drift; an MCP service would add deployment work while still needing host lifecycle adapters.

## 4. Identity, configuration, and shared storage

### Identity contract

Every operation receives a `RuntimeContext` with `host` (`claude` or `codex`), `client` (`claude-code`, `codex-cli`, or `codex-desktop`), native session ID when known, canonical project root, current worktree path, and selected vault.

Host and client are distinct. A Codex thread opened in desktop and CLI is the same session, not two origins. A resumed native ID keeps its session note; a fork gets a new identity and an explicit parent link. Child-agent activity is associated with its parent rather than automatically creating another top-level session note.

Hook wrappers pass the host explicitly. Skill invocations pass the host from their host reference instructions and native runtime context. The precedence is an explicit wrapper/CLI `host`, then a validated `RuntimeContext`, then unambiguous host-native evidence. Never choose a host from environment variables alone when both hosts' markers are present. After host selection, only that host's native ID source is eligible; a foreign variable cannot override it. Codex skill shells can use `CODEX_THREAD_ID`; §11 step 1 must prove that it matches the rollout thread ID. Claude skill shells use `CLAUDE_CODE_SESSION_ID` only under an explicit or unambiguous Claude context. Thus Claude launched inside Codex can use its own ID despite inherited Codex markers, and Codex launched inside Claude can use its own ID despite inherited `CLAUDE_CODE_SESSION_ID`. Ambiguous context or conflicting explicit IDs fails with a diagnostic, not a borrowed ID. Native IDs are opaque strings, not UUID-only values. Query-only operations can run without a session ID. A curated note may omit unavailable session links; it must never invent or borrow them.

#362/#363 shipped an interim guard in `obsidian_utils.py`: `_CODEX_HOST_MARKERS` checks `CODEX_THREAD_ID`, `CODEX_SANDBOX`, and `CODEX_SANDBOX_NETWORK_DISABLED`; `_resolve_session_id` returns `unknown` and `check_hook_status` short-circuits when a marker is set, while `_UNCACHEABLE_SIDS` keeps `unknown` out of the session cache. A `codex exec` shell on 0.155.1 had all three markers and an inherited Claude session ID. This guard prevents that Claude ID from being stamped by Codex, but it also makes Claude-inside-Codex resolve `unknown`. Explicit host context replaces the heuristic when the adapters ship. Until then, diagnostics must distinguish the interim skip from an ordinary missing session.

Session key: `(host, native_session_id)` within the selected vault. Operational state also keys on the canonical project root; the current worktree path remains available as provenance. Two different repositories with the same basename must not share state or recovery scans. The current `_BOOTSTRAP_PREFIX` creates `sid-<basename(cwd)>` files and collides for equal repo basenames; replace it during the state migration.

### Paths and configuration

Keep Claude's existing config path. Codex uses `$CODEX_HOME/obsidian-brain-config.json`, falling back to `~/.codex/obsidian-brain-config.json`. Both contain the same vault/folder settings when paired. No config is written into a plugin cache.

Add a new `OBSIDIAN_BRAIN_CONFIG` override. Resolve explicit CLI options first, then this override, then the invoking host's native config. Codex setup may offer an existing Claude configuration as a source, but normal Codex commands do not silently load Claude settings. Setup copies only shared vault/schema/index settings; host-specific AI models and permissions are not copied.

Add an explicit `index_path`. Paired hosts use one index for the same vault so access history, themes, and ranking do not diverge. When pairing an existing Claude installation, preserve its current DB path and contents. For a new vault, default to `~/.local/share/obsidian-brain/vaults/<vault-key>/index.sqlite3`. The vault key hashes the resolved vault path. Do not relocate existing databases automatically. `OBSIDIAN_BRAIN_DB` remains an explicit override, including for tests.

Store and verify vault/folder identity in the index. Refuse to synchronize an index against a different vault or conflicting folder set. Setup reports pairing conflicts and requires a deliberate selection instead of combining settings silently.

Claude's current scratch root is `_SECURE_DIR = ~/.claude/obsidian-brain`; Codex has no existing Obsidian Brain state root. Use `$CODEX_HOME/obsidian-brain/state` (default `~/.codex/obsidian-brain/state`) for Codex, with owner-only permissions. Evaluate Codex's hook-only `PLUGIN_DATA` as an input/resource location, but do not make skill shells depend on it. Add `OBSIDIAN_BRAIN_STATE_DIR` as the explicit override for either host and for tests. Under the selected root, use a versioned subtree keyed by vault, host, native session ID, canonical project root, and unique operation ID. Record both new overrides in `docs/architecture/architecture.json` `environment` when implemented. Shared session cursors and writer locks live in an owner-only coordination directory beside the selected index. Index rebuilds preserve coordination state; cursors are not disposable search cache data.

No global `current-session` file is authoritative. Cache reuse requires a matching input digest, vault/project identity, algorithm version, and relevant backend settings; age alone is insufficient.

## 5. Transcript contract and privacy

Normalized records carry stable source IDs or offsets, role, visible text, tool name/category, timestamps, and ordered sequence. Session metadata carries host, native ID, project/worktree, parent identity, and runtime version when available.

The Codex adapter handles observed `session_meta`, `response_item`, `event_msg`, function calls/results, custom tool calls/results, and compaction boundaries. Choose one canonical representation where the stream repeats the same activity. Do not count `event_msg` mirrors twice or count inherited fork history as new work.

Unknown tools retain their native name and an `unknown` category. Do not fabricate file edits from JavaScript orchestration text; use structured tool evidence where available. Tool mapping affects presentation, not the underlying call identity.

Ignore hidden reasoning, encrypted payloads, system/developer instructions, environment dumps, and transport metadata. Capture user-visible messages and relevant tool evidence only. Apply existing secret scrubbing before durable capture and again before AI dispatch. Metadata-only files are not sessions. Inspect enough of the source to distinguish a metadata tail from an empty conversation.

Parser results distinguish `ok`, `partial`, `unsupported`, and `unavailable`, with consumed offsets and warnings. Keep an incomplete final line for the next read. Rotation/truncation starts a new source generation without duplicating prior events. An unknown substantive record layout must produce a visible partial/unsupported result rather than a successful empty note.

Validate transcript paths against the selected adapter's allowed locations, resolved symlinks, and expected identity. Both `obsidian_session_log.py` and `obsidian_context_snapshot.py` currently restrict live transcripts to `~/.claude/projects`; remove those Claude-only assumptions from shared validation. Each wrapper normalizes the available `session_id`, `transcript_path`, `cwd`, event/trigger name, and `stop_hook_active` fields before the shared code runs. The four current hooks read a Claude-shaped `session_id` from stdin; no shared module may infer the host from that field alone. Explicit historical imports accept user-selected files through a separate validated path; they do not weaken live-hook validation.

## 6. Capture lifecycle and recovery

| Event | Shared behavior |
|---|---|
| Session start/resume | Establish identity, perform bounded pending-work recovery, and inject project context across both hosts. |
| Completed turn (`Stop`) | Capture new visible activity and update the same session note; then evaluate the existing blocking retro classification gate. |
| Before compaction | Persist new activity and an immutable snapshot linked to the parent session note. |
| Session end | Flush remaining activity and mark the observed session end. Do not call an AI backend. |
| Abrupt termination | Recover from the last committed cursor and remaining source records at the next supported recovery opportunity. |

States are `active`, `ended`, and `interrupted`; capture completeness is a separate `complete` or `partial` field. An ended session can resume and become active again. A crash-recovered note without a trustworthy end event stays interrupted, not falsely finalized.

Existing minimum-message/duration settings remain effective. Below-threshold activity can be checkpointed in private state without publishing a noisy vault note; when it qualifies, publish all retained eligible activity. Excluded/internal AI jobs never become user session notes. Diagnostics distinguish filtered activity from failed capture.

The writer provides at-least-once processing with idempotent note updates. Event identity includes the session key, source generation, and source event ID/offset. Persist the raw scrubbed checkpoint before publishing the note, publish atomically, then commit the cursor. A crash at any step replays safely. The note stores the applied revision, allowing recovery after a note write but before cursor commit.

For overlapping events, acquire the shared per-session lock, merge from the last committed revision, and serialize publication. Finalization cannot discard a newer checkpoint. A replayed compaction event creates no duplicate snapshot. Snapshot identity includes the session, source revision, and trigger; date changes do not break its parent link.

Hooks perform no full-vault scan, index rebuild, or AI call. The Codex wrapper declares an explicit `SessionEnd` timeout no greater than three seconds; without it Codex uses one second. The implementation plan sets measurable start/recovery limits (maximum sources and wall time per invocation) and an end-handler deadline with a safety margin below the configured timeout. Contract tests read the timeout from the installed manifest rather than a fixed constant. The handler records unfinished work durably and exits before the deadline. Heavy source catch-up occurs at normal turn checkpoints or recovery. Never advance the cursor beyond durable data. A remaining unread source is reported as pending; source disappearance is reported as loss of recoverable input, not success.

Recover pending work on start/resume, recall, and vault-doctor. Refactor the existing reaper to use the same identity/parser/writer services. It must not finalize active sessions, skip an incomplete note merely because it exists, or use a rounded global mtime watermark. Bounded scans keep deterministic per-source cursors so interrupted batches cannot strand files. Recovery needs no background service.

For Codex, `Stop` output must use the documented JSON contract, checked against each installed client in §11 step 1. Capture does not block completion. The existing `obsidian_retro_gate.py` is a **blocking** classification gate, not a reminder: in Codex, a blocking `Stop` decision continues the turn. Add a Codex fixture for that decision and its `stop_hook_active` loop protection. Record the decision per host/session/turn to avoid loops. A capture failure must not look like a retro-policy block. Claude's wrapper preserves its native output contract. [Codex hook contracts](https://learn.chatgpt.com/docs/hooks), documentation checked 2026-09-25; local CLI `0.155.1` and desktop bundle `0.155.0-alpha.9.2` were version-checked, not yet validated end to end.

## 7. Notes, concurrent updates, and compatibility

Continue using `claude-session`, `claude-snapshot`, existing curated note types, `claude/*` tags, configured folders, and existing dashboard queries.

New session notes add `agent_provider`, `agent_session_id`, `capture_state`, `capture_completeness`, `capture_revision`, and `summary_revision`. Preserve existing `session_id`/`source_session` fields with their native value. Curated notes record authoring host separately from source-session provenance; Codex summarizing a Claude note does not change that note's origin.

Old notes lacking a provider field are read as legacy Claude notes. Do not backfill them in bulk. When updating a known legacy note, preserve its filename and links. New session filenames retain the date/project pattern and use a host-qualified, 16-hex-character identity digest. Before writing an existing destination, verify full host/session identity; extend the digest if another identity occupies the name. Curated notes include an operation ID to avoid same-session title collisions.

New session notes separate managed capture content from summary and user-owned content. Checkpoints replace only the managed capture region and permitted metadata. Summary updates are conditional on the captured revision used to generate them; later capture marks older summaries stale without deleting them. Stale or failed AI results cannot overwrite newer content.

When a user edits a managed region, detect its content-hash mismatch, preserve the note, and retain the new capture as pending data. Surface a conflict for reconciliation instead of silently replacing either version. Legacy notes without managed regions retain their body; add a managed continuation region when new activity arrives. Do not regenerate the whole legacy document from a template.

All note mutations, including summary upgrades, capture, snapshots, imports, links, and check-item edits, use the shared lock/revision/atomic-write service. Locks have ownership tokens, bounded acquisition, and verified release. A slow live writer's lock cannot be stolen solely because a fixed age elapsed. Search index updates are retryable after note publication; a stale index cannot roll back a valid note.

Legacy readers and dashboards continue to work. A writable deployment must upgrade all installations that write the shared vault to the new protocol before simultaneous-host mode is enabled. Setup and vault-doctor report incompatible installed writer versions; old binaries cannot be made safe by a new file format alone. This release preserves data compatibility, not concurrent-write guarantees with unupgraded executables.

## 8. AI execution

The invoking host selects the backend, independently of the origin of the notes being processed. Claude invokes Claude; Codex invokes Codex. Cross-provider fallback is prohibited.

Expose bounded operations such as `summarize`, `classify`, and `name_theme`, each with validated input/output schemas. Reuse existing prompt intent and validation. The model returns structured data; trusted Python code performs writes and applies any existing review/approval policy. Model output is not a shell command or an instruction to bypass evidence checks.

Prefer the invoking session's available execution/subagent facility when the skill controls the operation. Existing batch CLI paths use the matching CLI backend: `claude -p` or `codex exec`. The Codex adapter uses ephemeral execution and schema-validated final output. Authentication stays with the host; no second-provider account or new API-key requirement is introduced.

Do not translate `haiku` into an invented Codex model name. Preserve existing Claude configuration; Codex uses its configured/default model or an explicitly configured Codex model. Never copy a Claude-only model setting into Codex config. Report the actual backend/model used when available.

CLI jobs receive scrubbed, bounded input through stdin or private job files, run with restricted filesystem access, and cannot edit the vault directly. They must not use approval, sandbox, or hook-trust bypass flags. An internal-job marker suppresses Obsidian Brain's own hooks and retro gate in nested work without disabling unrelated user policies. Verify marker propagation in each supported runtime.

Enforce timeouts, bounded output, cancellation cleanup, and output-schema checks. Authentication failure, absent executable, denied execution, malformed output, and timeout are distinct outcomes. Preserve raw notes and pending work. Do not retry forever, report a failed summary as successful, or switch providers. If a host-native execution path is unavailable, report that capability as unavailable; full parity is not certified until the supported client has a working path.

## 9. Skills and installation

Author common workflow instructions once. Keep small host-specific references for invocation, setup, permissions, and execution. Do not maintain a second independently edited `skills-codex/` tree. Where a host requires different descriptor text, generate it from the common source and test that generation is deterministic.

The §9 table is the acceptance inventory. At implementation, generate it from the capability matrix or test that its skill names equal the discovered `skills/*/SKILL.md` names in both directions. A newly added skill without a host decision fails CI.

`brain_cli.py` is the stable data-operation interface. Commands return JSON status, result, warnings, and structured errors. Supply host/context and the installed resource root explicitly; do not search for the newest cache version. Existing lower-level helpers remain usable during migration. The current skills have 72 embedded resolver sites (68 `_ob_hooks`, one `_ob_doctor`, three `_ob_repo`). They read Claude marketplace/cache paths and often fall back to cwd-relative `hooks`, which can make tests pass inside this checkout while every skill fails under a Codex-only install. Hook-only `PLUGIN_ROOT`/`PLUGIN_DATA` cannot be assumed in skill shells; `CODEX_PLUGIN_ROOT` is not present in the tested CLI. Each host reference must derive the active installation from the SKILL.md path presented by that host, pass the canonical root to `brain_cli.py`, and record it during setup for later CLI use. §11 step 1 verifies that handoff in installed clients and from an unrelated cwd without `~/.claude`. An unresolved root fails loudly; no relative `hooks` fallback remains.

| Skill | Required parity acceptance |
|---|---|
| obsidian-setup | Fresh install and pairing; vault/index access; host permissions; hook trust; actionable capability report. |
| vault-config | Read/update invoking-host settings; validate shared settings; preserve other-host config. |
| recall | Cross-host project brief, pending recovery, stale-summary upgrades through invoking backend, snapshot context. |
| vault-search | Same corpus and ranking; correct source links across both hosts. |
| vault-ask | Grounded synthesis with session/snapshot citations; host-appropriate execution. |
| compress | Create/update curated knowledge with deduplication, evidence checks, provenance, and protected writes. |
| decide | Create/supersede decisions; valid source links and existing confirmation behavior. |
| error-log | Capture cause/fix/evidence using the same privacy and provenance rules. |
| retro | Include every eligible snapshot; preserve the save preview and blocking classification gate. |
| standup | Ordinary/deep reports across both hosts; isolated pipeline inputs and caches. |
| check-items | Same evidence tiers and human decisions; invoking-host AI; no false success when unclassifiable. |
| consolidate | Cross-host clustering and curated consolidation using shared writer protections. |
| emerge | Cross-host patterns and themes; backend-neutral prompts and validation. |
| link | Resolve and update links without breaking legacy or cross-host references. |
| vault-import | Import both native transcript formats; explicit source host; replay-safe provenance and deduplication. |
| vault-doctor | Check both runtimes, parser support, identities, cursors, conflicts, index health, and permissions. |
| vault-stats | Include both hosts; preserve existing totals and offer provenance breakdowns. |
| vault-reindex | Rebuild from mixed/legacy notes without losing ingestion cursors or durable knowledge state. |
| dev-test | Install/restore either host's development package and validate it against a temporary vault. |

Keep `.claude-plugin` packaging and one marketplace source. Codex 0.155.1 currently accepts this repository through legacy compatibility, snapshots it into `~/.codex/plugins/cache/`, and auto-discovers `hooks/hooks.json`. All four Claude handlers are trusted and run under Codex with Claude semantics; they are registered **once**, not twice. The Claude `SessionEnd` handler then rejects Codex rollout paths as `SKIPPED_TRANSCRIPT_OUTSIDE_PROJECTS`, while SessionStart and Stop also run the wrong host policy. Until Codex adapters land, these handlers must detect Codex and exit cleanly with an explicit `SKIPPED_CODEX_HOST` outcome. The OpenAI package must declare its Codex hooks explicitly so the Claude `hooks/hooks.json` is never selected; an installed-package test asserts the selected hook file and executes the wrappers. The project-level `.codex/hooks.json` added in #361 registers Git policy hooks when Codex works in this repository, but it has no trust entry on the tested machine and is not the future package lifecycle manifest. Decide whether that project file remains alongside, or moves into, the explicit plugin hook selection; setup and vault-doctor report its trust state either way.

OpenAI documentation supports root `plugin.json` with `extensions.com.openai` and `.codex-plugin/plugin.json` as a compatibility fallback. Do not choose between them from file layout alone: §11 step 1 tests manifest precedence alongside `.claude-plugin/` and explicit hook selection in both installed clients. Share skill resources, and do not treat Claude alias environment variables as host evidence. [Plugin packaging](https://developers.openai.com/plugins/build/plugins)

### Installation and updates

| Host | Install and update path |
|---|---|
| Claude Code | `/plugin marketplace add <repo>` then `/plugin install obsidian-brain@<marketplace>`; a directory source loads the checkout live. Update through the marketplace. |
| Codex CLI/desktop | `codex plugin marketplace add <path-or-repo>` then `codex plugin add obsidian-brain@<marketplace>`; review handlers in `/hooks`. A local-path install is a cache snapshot. `codex plugin marketplace upgrade` refreshes Git sources, not a local-path snapshot; local development uses `codex plugin remove` then `codex plugin add`. |

Codex CLI `0.155.1` and ChatGPT desktop 26.915.31945's bundled Codex `0.155.0-alpha.9.2` share config, cache, and hook trust. Record and test both versions; vault-doctor checks the desktop bundle as well as `codex` on PATH. A local cache on this machine was 19 commits behind `develop` despite reporting version `3.5.1`; setup/doctor therefore compare package content hash or commit as well as version. Each published Codex-visible change needs a new package version. Define packaging include/ignore rules so `.git`, tests, coverage files, and local state do not enter the snapshot. Handler trust is tied to handler content: setup and vault-doctor report untrusted or changed handlers and how to review them in `/hooks`.

`dev-test` must support the Codex remove/add recopy and restore the previous cache and `config.toml` plugin entry on exit. Audit `scripts/test-dev-skill.sh` and all of `scripts/dev-test/` for Claude-only assumptions; 23 of 26 dev-test files and the installer reference `.claude`. Setup distinguishes package installation, hook trust, vault write permission, cache freshness, and backend readiness. It must not treat discovered skills as proof that hooks run. Run installed-package checks from an unrelated working directory, without Claude installed, and with paths containing spaces. Extend `scripts/bump-version.sh` and the version-sync preflight check when the new descriptor lands.

Update README, CLAUDE.md, AGENTS.md, and architecture JSON/HTML when implementation changes land. AGENTS.md already links to the current Claude-side facts in CLAUDE.md; keep shared guidance there and Codex differences in AGENTS.md. Explain native invocation syntax in each client. Core workflows must not depend on optional external skills such as context-shield or conversation-search being installed.

## 10. Acceptance and release gates

All gates must pass before claiming parity. Passing unit tests alone does not prove desktop hook behavior.

| Gate | Required evidence |
|---|---|
| Legacy behavior | Existing Claude regression suite passes; approved checkpoint behavior has explicit updated tests. Old notes, queries, links, and custom folder settings remain usable. |
| Host independence | Codex workflows pass with Claude executable/config absent; Claude passes with Codex absent. Test Codex-in-Claude and Claude-in-Codex with both hosts' environment markers present; an explicit host selects only its native ID, while ambiguous context fails without borrowing either ID. |
| Client matrix | Every skill in the §9 table plus start, stop, compact, resume, interrupt/crash recovery, and end run on Claude Code, Codex desktop, and Codex CLI. Record exact versions and permissions, including the tested CLI `0.155.1` and desktop bundle `0.155.0-alpha.9.2`; do not infer desktop behavior from the CLI. |
| Transcript correctness | Sanitized fixtures for actual runtime shapes, repeated/mirrored events, nested tools, metadata-only files, compaction, forks, partial lines, rotation, missing sources, and unknown formats. |
| Simultaneous use | Two hosts in one repo; one Codex thread in desktop/CLI; equal native IDs across hosts; different repos with equal basenames; moved/deleted worktrees. |
| Durable writes | Kill after checkpoint, note publication, and cursor commit; replay remains idempotent. Race capture/reaper, summary/capture, two summaries, and manual edits. No lost content. |
| Timing | Measure small and large transcripts, lock contention, cold startup, and slow writes. Codex end handler stays below its explicit configured timeout (no more than three seconds, one second if omitted) with a safety margin; unfinished work is durable and later recovered. |
| Privacy/security | No hidden reasoning or secret leakage; containment/symlink tests; 0600 files and 0700 private directories; hook entry points cap stdin with `sys.stdin.read(1_000_000)`; paths passed to `python3 -c` use `sys.argv`; JSON passes via stdin, not shell arguments; `scrub_secrets()` runs before vault writes; unsupported sources never trigger unsafe writes. |
| AI failures | Missing auth/CLI, policy denial, timeout, cancellation, invalid schema, and nested-hook recursion. No cross-provider invocation or success claim on failure. |
| Installation | Fresh installs, existing-vault pairing, custom CODEX_HOME, upgrades, hook trust, unrelated cwd, and paths with spaces on supported platforms. |
| Diagnostics | Explain partial/pending/conflicted states and recovery action without revealing prompt contents or secrets. |

Use synthetic fixtures and scrubbed shape-preserving samples, never committed live transcripts. Each parser/identity detector needs positive and negative controls. Failure-injection tests call the real writer/recovery functions rather than duplicate their logic.

Tests must isolate config, scratch state, coordination state, and databases in temporary directories, including subprocess paths. The current check-items tests reach the live Claude scratch directory and fail under a workspace-only sandbox; the parity work must remove that dependency. A passing suite must not require real host credentials or permission to mutate production state.

Run the existing full pytest coverage gate, DB-pollution check, security checks, skill-snippet checks, and architecture validation. Add coverage for every new runtime module. Add macOS and Linux CLI coverage; desktop lifecycle/trust/permission checks require a recorded live run. Current CI is Ubuntu-only and does not establish desktop support.

The source, matrix, lint, resolver, and contract checks in §12 run in CI on every PR and become required status checks on `develop` before parity is declared. Only the desktop live run remains a release-time check.

The release evidence records tested client versions and parser format fingerprints. Unsupported versions/formats get a capability warning or fail closed for affected writes. Do not promise compatibility solely from a version number or a schema on an unreleased upstream branch.

## 11. Implementation sequence

1. Capture sanitized runtime fixtures and establish lifecycle/output contracts for installed CLI and desktop clients. Verify `CODEX_THREAD_ID` against the rollout thread ID in a skill shell, the SKILL.md-to-resource-root handoff, nested-host markers, `SessionEnd` default and configured timeout, `Stop` output, host-native AI execution, manifest precedence, and selected hooks without vault writes. Test both CLI `0.155.1` and desktop bundle `0.155.0-alpha.9.2`; installed behavior, not filename convention, decides the OpenAI manifest location.
2. Extract identity/config/resource resolution and shared CLI boundaries while retaining legacy wrappers. Replace the interim #363 marker heuristic with explicit host precedence and the basename-keyed bootstrap file with host/session/project state. Update architecture components and `hosts` entries here.
3. Introduce the writer transaction, shared cursors, managed regions, and conflict handling. Fix overlapping cache/reaper defects in the same work.
4. Add transcript adapters and capture checkpoints, snapshots, finalization, and recovery for both hosts. Update the `session-end-autolog`, `precompact-snapshot`, and `orphan-reaper` architecture flows in this stage.
5. Port all six AI execution sites and every skill in the §9 table; remove unconditional Claude paths and tool assumptions from shared workflows. Move tool-specific orchestration in `standup`, `emerge`, `recall`, `obsidian-setup`, and `retro` into paired `references/host-claude.md` and `references/host-codex.md` files. Update the `summarizer`, `theme-seeding`, and `recall-deferred-summarize` architecture entries in this stage.
6. Complete packaging, setup/doctor/dev-test, documentation, and the full acceptance matrix. Make Codex's manifest select only its host wrappers, check handler trust and content freshness, and wire every §12 check into CI as a required status check on `develop`.

These are implementation stages within one compatibility effort, not separate claims of partial parity. Scope fixes exposed here into this change-set rather than generating a follow-up issue for each path or cache bug.

The written implementation plan follows user review of this spec. It will map these stages to files, tests, and reviewable commits. This document authorizes no deployment or merge.

## 12. Keeping hosts in parity

Parity is a per-PR invariant after implementation, not a one-time release audit. The implementation is done only when the following checks run in CI for every PR and are required on `develop`. Every check must compare discovered source items with the declared inventory in both directions, so a new item and a stale entry both fail. Use `tests/test_hooks_resolver_drift.py` as the precedent for equality checks, then replace its Claude-only resolver assumptions with the shared resolver.

| Check | Required behavior |
|---|---|
| Single source for hooks | Keep policy code once, with only thin host entry points. #361's `tests/test_host_hook_single_source.py` currently asserts no `.codex/hooks/*.py`, equality with the Bash registrations in `.claude/settings.json`, allow/deny behavior, and no `/Users/` or `/home/` paths in hook registrations. It does **not** compare hashes. When Codex lifecycle wrappers are added, test that they call shared policy rather than copy it. Test installed Codex hook selection so `hooks/hooks.json` is not auto-loaded, and report trust for any project-level `.codex/hooks.json`. |
| Capability matrix | Add `docs/parity/capabilities.json`. Inventory every supported Codex hook event (including `PostCompact`, `SubagentStart`, `SubagentStop`, `Interrupt`, and `PermissionRequest`), each registered handler and field, skill, `brain_cli.py` subcommand, config key, environment variable, and vault-doctor check. For Claude Code, Codex CLI, and Codex desktop, mark each item `supported`, `unsupported:<reason>`, or `n/a`; unused events need an explicit decision. Tests discover items from both host manifests, `skills/*/SKILL.md`, CLI subcommands, doctor checks, `_DEFAULTS`, and every `config.get`/`config[...]` read, not only `load_config`. The current reads of `reaper_enabled`, `reaper_max_runtime_seconds`, and `workspace_roots` need coverage even though they have no `_DEFAULTS` entry. Compare exact sets and validate per-handler fields; Claude-only fields such as `asyncRewake` and a SessionStart `matcher` must not be silently treated as Codex support. The matrix carries each client's supported version range and fixture provenance. |
| Host-neutral shared code | A pytest beside `tests/test_skill_snippets.py` rejects `~/.claude`, all six `claude -p` sites, Claude tool names, and direct host-specific resource lookup in shared `skills/**` and `hooks/*.py`. Only named adapters and paired `references/host-claude.md`/`references/host-codex.md` files may contain host-specific steps. The lint requires the pair or a matrix `unsupported:<reason>` decision whenever one host gets a step. Rewrite `tests/test_hooks_resolver_drift.py` around the host-neutral resolver; a skill snippet must run from an unrelated cwd without `~/.claude`. |
| One config/env resolver | `runtime_context.py` owns config and environment resolution. A `scripts/ci-checks/` lint rejects direct reads of `OBSIDIAN_BRAIN_*`, `CLAUDE_*`, `CODEX_*`, and hook environment names, plus hardcoded `.claude/` paths, outside that module and named adapters. Discover names in literal reads **and** constant tuples such as `_CODEX_HOST_MARKERS`. Move direct `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PROJECT_DIR`, and Codex-marker reads out of shared `obsidian_utils.py`. Inventory `OBSIDIAN_BRAIN_SESSIONS_FOLDER`, `OBSIDIAN_BRAIN_INSIGHTS_FOLDER`, `PLUGIN_ROOT`, and `PLUGIN_DATA`; do not rely on absent `CODEX_PLUGIN_ROOT`. Every environment variable must appear in the matrix and `docs/architecture/architecture.json` `environment`, including the new `OBSIDIAN_BRAIN_CONFIG` and `OBSIDIAN_BRAIN_STATE_DIR` overrides. |
| Both-host hook contracts | A parameterized pytest runs each host wrapper as a subprocess with sanitized golden fixtures under `tests/fixtures/hosts/<host>/<event>.json`. It asserts exit code, output schema, and wall time, reading the configured Codex `SessionEnd` timeout from the installed manifest and keeping a safety margin beneath it. Include a blocking Codex `Stop` retro-gate fixture with `stop_hook_active` loop protection. Pin client version and capture date with each fixture. The matrix requires fixtures for every supported event; a live desktop run still verifies installation and trust. |
| Both-host behavior conformance | Parameterize tests that pass through capture, the writer, `brain_cli.py`, or skill snippets over `host ∈ {claude, codex}` by default, using `RuntimeContext` and sanitized host fixtures. An opt-out requires `@host_only(<host>, reason=…)` and a matching matrix `unsupported:<reason>` decision. Feed the same logical session through both transcript adapters and compare the resulting vault note after removing only provenance fields. Shared filters, frontmatter, summaries, and skill outcomes must match. This is the required behavior check that structural inventory alone cannot provide. In CI, install the pinned Codex CLI from the checkout, list the installed plugin and its declared skills/hooks, and assert that the Codex manifest selects its wrappers rather than Claude's lifecycle hooks; verify the no-auth installation path in §11 step 1. |
| Upstream Codex drift | The matrix pins supported CLI and desktop version ranges. Vault-doctor checks `codex --version` and the desktop bundled binary, plus cache content provenance, and reports parser `partial` or `unsupported` outcomes. A weekly CI job captures a fresh sanitized fixture from a synthetic session, reruns parser and hook contracts, and rechecks `SessionEnd` availability, default, maximum, and configured timeout. If automated fresh capture is not possible, a required release-checklist step performs it before widening the supported range. |
| Instructions and review | `AGENTS.md` links to `CLAUDE.md` and contains only Codex differences. CI checks instruction-file links and referenced repository paths. Add and validate the `hosts` field as adapters are introduced in §11 step 2, not at the end. Add `.github/pull_request_template.md` with `Codex impact: none / updated / unsupported (reason)` and require a filled choice; this declaration supplements, but never substitutes for, the failing Codex conformance leg. Extend `scripts/bump-version.sh` and the version-sync preflight check to cover the tested OpenAI descriptor when it is added. |

Do not mark a capability `supported` solely because its descriptor or hook registration exists. A supported status requires passing hook contracts, both-host behavior conformance, and the appropriate installed-client validation in §10. Any later Claude-side change to a shared capability must either update Codex behavior and fixtures or record an explicit unsupported decision with a reason.
