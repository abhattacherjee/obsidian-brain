---
name: vault-doctor
description: "Diagnostic and repair skill for the Obsidian vault. Runs a battery of checks against vault notes and offers to fix detected issues. Dry-run by default — requires 'fix' to write. Use when: (1) /vault-doctor command to scan for vault health issues, (2) /vault-doctor fix to apply repairs, (3) /vault-doctor --check <name> for a specific check, (4) user reports stale backlinks or wants to audit vault integrity."
metadata:
  version: 1.3.0
---

## Native runtime and installed resources

Use the absolute path of this loaded `SKILL.md` as `OB_SKILL_PATH`. Read the
reference for the invoking host when this skill has paired host references.
Set `OB_HOST`, `OB_CLIENT`, `OB_SESSION_ID`, and `OB_CWD` from that native
invocation. The current client must be explicitly supplied by the invoking runtime.
If that binding is unavailable, stop and report it. Never label a Desktop
invocation as a CLI invocation or infer the frontend from transcript creation
metadata or inherited environment markers. Use the selected host's own session ID. Keep curated note taxonomy
separate from `agent_provider` and `agent_session_id` provenance.

```bash
: "${OB_CLIENT:?Current native client binding is unavailable; stop without choosing a frontend.}"
OB_SKILL_PATH='<absolute path of this loaded SKILL.md>'
OB_RESOURCE_ROOT=$(python3 -c 'import pathlib,sys; p=pathlib.Path(sys.argv[1]); assert p.is_absolute(); p=p.resolve(); assert p.name == "SKILL.md" and p.parent.parent.name == "skills"; print(p.parents[2])' "$OB_SKILL_PATH")
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" context < /dev/null
```

Use the returned `config_path`, `vault_path`, `index_path`, and `state_path`.
Create the operation with this fixed literal request:

```bash
printf '{}' | python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'prepare'
```

Call `prepare` to create a private operation under native state. Retain its
`operation_id` and `operation_dir`. Register approved helper output names with `artifact-store`;
inputs are read through the immutable artifact manifest. Do not discover resources from the current directory or another plugin
cache. Each shell invocation supplies the same explicit values; a previous
shell's variables are not assumed to persist.

Each data operation uses the installed launcher with a JSON request on stdin:

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation '<fixed operation>' < "$REQUEST_PATH"
```

Map config JSON `vault_path`, `sessions_folder`, and `insights_folder` to the
procedure variables `VAULT_PATH`/`VAULT`, `SESSIONS_FOLDER`/`SESS`, and
`INSIGHTS_FOLDER`/`INS`. Use the canonical project returned in config JSON (and native `session` when available),
not the basename of an unrelated shell working directory.

Before preparing edits or requesting a summary of an existing note, call
`note-read` and retain its exact `expected_revision`. Apply the proposed note
with `note-apply` and that revision. A conflict leaves the current note intact;
show the pending result and do not count the note as saved. New curated notes
use `note-create`; they never overwrite a collision. Native memory discovery
is unsupported for Codex because it has no equivalent native memory-file API; shared vault retrieval
and wiki filing continue without borrowing another host's memory.

Read `references/host-claude.md` or `references/host-codex.md` when present.
All note writes described below use `note-create` or revision-bound `note-apply`,
including bidirectional related links. Content is a JSON string, never shell code.
Every later save or edit follows this revision-bound publication rule.


# vault-doctor — Audit and Repair the Obsidian Vault

Audit and repair the Obsidian vault. Ships with 13 checks — 9 in the default sweep and 4 opt-in ones that must be named with `--check`. More can be added as separate modules under `scripts/vault_doctor_checks/` without changing this skill.

**Tools needed:** native shell, native file reading

## Invocation

- `/vault-doctor` — run all checks, report only (dry-run)
- `/vault-doctor fix` — run all checks, apply after per-project confirmation
- `/vault-doctor --check source-sessions` — run one specific check; notes carrying `imported: true` frontmatter or the `claude/imported` tag are silently skipped (their `source_session` refers to another vault and can never resolve locally)
- `/vault-doctor --check snapshot-integrity` — snapshot orphans, broken backlinks, stale/missing session snapshot lists, status/summary mismatches
- `/vault-doctor --check snapshot-migration` — migrate pre-spec snapshots (legacy filenames, missing status/backlink fields, missing session snapshot lists). Runs 4 ordered sub-checks; idempotent.
- `/vault-doctor --check project-name-canonicalization` — one-time backfill check that rewrites worktree-slug project names to the canonical main-repo basename in session notes and insights. Phase 1: for each session note with a `project_path:`, derives canonical via `git rev-parse --git-common-dir` (cached per path) and proposes rewriting `project:` + the observed `claude/project/*` tag lines (production tags are slugified/40-char-truncated — both forms matched; sibling tags never touched). Phase 2: for each insight with a `source_session:` UUID, looks up the Phase-1 canonical (not the stale frontmatter value) and proposes the same rewrite. WARN rows for: missing `project_path`, path no longer exists, git unavailable/timed out, git errors (dubious ownership etc. — never silently treated as non-repo), empty `project:` field, insight source_session not in index. Non-git project dirs left alone; snapshot notes skipped. `--project` matches the old name OR the derived canonical (filtered sessions still seed the Phase-2 index); `--days` is ignored (full-vault backfill). **Opt-in** — excluded from default all-checks sweep (`OPT_IN=True`); run via `--check project-name-canonicalization`. Conceptually run after `--check project-name-normalization` (underscore → hyphen) for clean input.
- `/vault-doctor --check session-coverage`: follow the selected host capability and bounded source adapter. See [Claude](references/host-claude.md) and [Codex](references/host-codex.md) for source support and reconstruction rules.
- `/vault-doctor --check audit-historic-repairs` — one-shot audit of historic source-sessions repairs: diffs doctor backups against current notes, classifies each repair (A restore / B keep / C ambiguous / D both-wrong) by date agreement, and restores category-A mtime-bug corruptions on `fix`. **Opt-in** — excluded from the default all-checks sweep; must be named via `--check`. `--days` bounds backup-run age (default 180).
- `/vault-doctor --check missing-frontmatter-fence` — repair notes whose frontmatter lost its opening `---` fence (the leading-fence-eaten failure mode: the first byte is the first frontmatter key, so the note parses as having no frontmatter at all and is invisible to tag-based Dataview queries). Only flags a note when all four preconditions hold: first line is not `---`, first line is `key:`-shaped, a closing `---` exists within the frontmatter line bound, and every line above it is frontmatter-shaped. The fix inserts `---` as a new first line and changes nothing else (line endings and file mode preserved). `--days` is ignored (the damage is historic). Re-run `/vault-reindex` afterwards so the recovered frontmatter reaches the index.
- `/vault-doctor --check memory-index`: use the selected host memory capability. Detailed Claude index rules and the explicit Codex limitation are in the paired references.
- `/vault-doctor --check wiki-pages` — check the LLM wiki's pages under `<wiki_folder>/queries/` (#396). Per page: `stale` (a source changed, a newer note matches the question, or the fingerprint is unverifiable), `broken-source` (a cited note or memory file is gone), `reviewed-stale` (a page marked `reviewed` that is stale; `/vault-ask` never refreshes these on its own, so you re-check your edits), `auto-filed` (`filed_by: auto`, listed for review), `orphan` (no vault note links `[[page]]` except the page itself and the wiki's own `index.md`, `index-<project>.md` and `log-<year>.md` at the wiki root; only pages whose `updated` date is inside `--days`) and `page-unreadable` (always shown, even under `--project`, because its project is unknown). Once for the wiki: `index-drift` when the index files differ from a fresh rebuild (a hand edit, a missing `index.md`, or a leftover `index-*.md` typed `claude-wiki-index`). **Only `index-drift` is fixed by `fix`:** it backs up the current index files under the backup root, then rebuilds them under the wiki lock (skipped with a reason if another process holds it). Every other row is report-only (unresolved, confidence 0.0), so `--min-confidence` above 0.0 hides every row except `index-drift`. The wiki folder comes from the config file, read at run time; a wiki that is turned off or not created yet reports nothing, and so does an empty wiki (no pages and no index files yet). A corrupt config, an invalid `wiki_folder`, or a `queries/` folder that cannot be read crashes the check (exit 2) instead of looking clean. The check lists the pages, then syncs the vault index, because index lines come from it. The index is shared and belongs to the configured `vault_path`, so a `--vault` (or `OBSIDIAN_BRAIN_VAULT`) that names another folder skips this check with one stderr line. In the default sweep; `--days` defaults to all time here.
- `/vault-doctor --days 14` — override default window (default: 7 days)
- `/vault-doctor --project obsidian-brain` — limit to one project
- `/vault-doctor fix --check source-sessions --days 7` — combine flags
- `/vault-doctor --min-confidence 0.9` — dry-run showing only issues with confidence >= 0.9; report header notes the active filter and dropped count
- `/vault-doctor fix --min-confidence 0.9` — apply only the high-confidence subset (conf >= 0.9); preview matches apply scope exactly

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Step 1 — Parse arguments and locate the dispatcher

Parse the user's invocation into flags:

- No args → dry-run mode, all checks
- `fix` → apply mode, all checks
- `--check <name>` → specific check only
- `--days <N>` → window override
- `--project <name>` → project filter
- `--strict` → set STRICT=1 (session-coverage only: FAIL instead of WARN on referenced gaps)
- `--reconstruct` → set RECONSTRUCT=1 (session-coverage only: mark gaps resolvable for apply)
- `--min-confidence <FLOAT>` → set MIN_CONFIDENCE (0.0–1.0 inclusive; default 0.0 keeps all; applies to both dry-run report and --apply); note: unresolved/WARN rows (confidence=0.0) are hidden at any threshold > 0 — drop the flag to audit them

Use the absolute loaded SKILL.md to select the installed resource root.

Request for `doctor` (substitute the values as data):

```json
{
  "argv": [
    "--json",
    "<selected doctor flags>"
  ]
}
```

Request for `doctor`:

```json
{
  "argv": [
    "--json",
    "<selected doctor flags>"
  ]
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'doctor' < "$REQUEST_PATH"
```

If the dispatcher cannot be located, tell the user:

> Could not find `scripts/vault_doctor.py`. Make sure the obsidian-brain plugin is installed via `/dev-test install` (for local dev) or the marketplace.

Stop here if the dispatcher is missing.

### Step 2 — Run the dispatcher in JSON report mode

Always run with `--json` first so you can parse the output deterministically. Pass through only the flags the user provided:

Request for `doctor` (substitute the values as data):

```json
{
  "argv": [
    "--json",
    "<selected doctor flags>"
  ]
}
```

Request for `doctor`:

```json
{
  "argv": [
    "--json",
    "<selected doctor flags>"
  ]
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'doctor' < "$REQUEST_PATH"
```

Capture stdout as the JSON report. Exit codes:

- `0` — clean vault, nothing to do
- `1` — issues found (expected for a dry-run that finds things)
- `2` — apply errors OR one or more checks crashed (results incomplete; see `crashed_checks` in JSON)
- `3` — usage error (bad args, missing config)

If exit code is `3`, surface the stderr message directly to the user and stop.

### Step 3 — Present the report to the user

Parse the JSON and present a grouped-by-project table.

For each issue, after the `proposed:` line (when present), render a
`signal: <capture_signal> (conf <capture_confidence>)` line. The values
come from the top-level `capture_signal` and `capture_confidence` fields
in the JSON payload (not from `extra.*`). `capture_confidence` reports
how reliable the capture-time *signal* is (created_at=1.0, date=0.9,
filename=0.85, mtime=0.5); the issue's top-level `confidence` field
reports the *rewrite-proposal* confidence per the strict 3-band taxonomy:
0.99 = uuid-basename-stale (auto-applyable basename-only repair);
0.5 = date-window-hint (operator must content-grep before applying);
0.0 = unresolved / uuid-day-mismatch / missing-session-note (never auto-apply).
The two fields are distinct — render `capture_confidence` here so
heuristic-fall cases are visible (e.g., `signal=mtime conf=0.5` indicates
no immutable signal was available — the operator should sample a few flagged
notes before running `fix`). For unresolved issues with no `proposed:` line,
render `signal:` after `reason:`.
Render `signal_class` (from the top-level signal_class field) as a prefix tag so operators
can distinguish: [uuid-basename-stale], [uuid-day-mismatch],
[missing-session-note], [date-window-hint], [unresolved]. The
convergence_warning/convergence_count fields are deprecated as of #106
(UUID-first matching obsoleted the convergence guard) — they remain in the
JSON payload as hard-coded defaults for output schema stability but should
not drive rendering.

The `crashed_checks` key is conditional — it is only present when one or
more checks crashed during the scan or apply phase (exit code 2 on a
dry-run). If the payload contains `crashed_checks`, tell the user which
checks crashed and that the report is **INCOMPLETE** — do not present it
as a complete scan. Example: "Warning: checks [source-sessions] crashed
during this scan — results are incomplete. Re-run after the crash is
resolved to get a full report."

Example:

```
vault_doctor report — 3 issue(s) across 1 check(s)

## source-sessions

### Project: obsidian-brain (2 issues)
[FAIL] 2026-04-10-recall-profiling.md
  current:  [[2026-04-09-obsidian-brain-abcd]]
  proposed: [[2026-04-10-obsidian-brain-ef01]]
  signal:   date (conf 0.9)
  reason:   note calendar day 2026-04-10 (signal=date, conf=0.9) overlaps session ef010000 window most, not current source abcd0000

### Project: tiny-vacation-agent (1 issue)
[FAIL] 2026-04-11-enrichment-scope.md
  current:  [[2026-04-10-tiny-vacation-agent-aaaa]]
  proposed: [[2026-04-11-tiny-vacation-agent-bbbb]]
  signal:   created_at (conf 1.0)
  reason:   note capture_time 2026-04-11T09:15:00+00:00 (signal=created_at, conf=1.0) matches session bbbb0000 window, not current source aaaa0000
```

Use `[FAIL]` for actionable issues (those with a proposed fix) and `[WARN]` for unresolved ones (those the check could not auto-repair). Always include a one-line summary at the top with the total count.

If the report is empty (exit code 0), tell the user:

> Vault is clean. No issues found.

Stop here.

### Step 4 — Ask whether to apply (only if `fix` was requested)

If the user did NOT pass `fix`:

> Dry-run complete. Found **N** stale backlink(s) across **K** project(s).
> Run `/vault-doctor fix` to apply repairs. Backups will be written to `<reported native doctor backup root>/<timestamp>/`.

Stop here.

If the user DID pass `fix`:

> Found **N** repairable issue(s) across **K** project(s). I'll apply per project with confirmation.

Re-run the dispatcher with `--apply` (do NOT pass `--yes` — let the dispatcher prompt per project interactively):

Request for `doctor` (substitute the values as data):

```json
{
  "argv": [
    "--json",
    "<selected doctor flags>"
  ]
}
```

Request for `doctor`:

```json
{
  "argv": [
    "--json",
    "<selected doctor flags>"
  ]
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'doctor' < "$REQUEST_PATH"
```

The dispatcher will prompt `Apply N fix(es) for project 'X' in check 'Y'? [y/N]` on stderr for each project. Relay each prompt to the user and pipe their response to the dispatcher's stdin.

### Step 5 — Report the outcome

Parse the final stderr output from the dispatcher and summarize:

```
vault_doctor apply complete
  obsidian-brain: 3 applied, 0 unresolved, 0 errors
  tiny-vacation-agent: 1 applied, 0 unresolved, 0 errors

Backups saved to: <reported native doctor backup root>/2026-04-11T17-04-22+00-00/
```

If exit code is 2, distinguish the source:

- **Apply errors (fixes failed):** Surface the failed-fix lines from stderr prominently and recommend the user diff one of the backup files under the backup root to understand what went wrong.
- **"CHECK CRASHED" or "APPLY CRASHED" on stderr:** Report which check(s) crashed by name. For an apply crash, warn that fixes for that check may be **partially applied** (backups exist under the backup root for anything that ran before the crash). For a scan crash, note that nothing was applied for that check and **no backups exist** for it.

### Step 6 — Offer next steps

After a successful fix run:

> Repairs applied. You can diff any fixed note against its backup under the backup root.
> Re-run `/vault-doctor` to confirm the vault is clean.

## Notes for the model

- All detection and repair logic lives in `scripts/vault_doctor.py` and `scripts/vault_doctor_checks/*.py`. **Do not re-implement any of it in this skill.** The skill is pure orchestration and presentation.
- The dispatcher is dry-run by default. Pass `--apply` only when the user explicitly requests `fix`.
- Unresolved issues are never automatically repaired. Surface them in the report but do not try to guess a replacement.
- Backups are written automatically by the dispatcher to `<reported native doctor backup root>/<ISO-timestamp>/<project>/<basename>`. Always mention the backup path in your summary so the user knows where to look.
