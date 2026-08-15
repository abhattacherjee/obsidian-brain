---
name: vault-doctor
description: "Diagnostic and repair skill for the Obsidian vault. Runs a battery of checks against vault notes and offers to fix detected issues. Dry-run by default — requires 'fix' to write. Use when: (1) /vault-doctor command to scan for vault health issues, (2) /vault-doctor fix to apply repairs, (3) /vault-doctor --check <name> for a specific check, (4) user reports stale backlinks or wants to audit vault integrity."
metadata:
  version: 1.3.0
---

# vault-doctor — Audit and Repair the Obsidian Vault

Audit and repair the Obsidian vault. Ships with 11 checks — 7 in the default sweep and 4 opt-in ones that must be named with `--check`. More can be added as separate modules under `scripts/vault_doctor_checks/` without changing this skill.

**Tools needed:** Bash, Read

## Invocation

- `/vault-doctor` — run all checks, report only (dry-run)
- `/vault-doctor fix` — run all checks, apply after per-project confirmation
- `/vault-doctor --check source-sessions` — run one specific check; notes carrying `imported: true` frontmatter or the `claude/imported` tag are silently skipped (their `source_session` refers to another vault and can never resolve locally)
- `/vault-doctor --check snapshot-integrity` — snapshot orphans, broken backlinks, stale/missing session snapshot lists, status/summary mismatches
- `/vault-doctor --check snapshot-migration` — migrate pre-spec snapshots (legacy filenames, missing status/backlink fields, missing session snapshot lists). Runs 4 ordered sub-checks; idempotent.
- `/vault-doctor --check project-name-canonicalization` — one-time backfill check that rewrites worktree-slug project names to the canonical main-repo basename in session notes and insights. Phase 1: for each session note with a `project_path:`, derives canonical via `git rev-parse --git-common-dir` (cached per path) and proposes rewriting `project:` + the observed `claude/project/*` tag lines (production tags are slugified/40-char-truncated — both forms matched; sibling tags never touched). Phase 2: for each insight with a `source_session:` UUID, looks up the Phase-1 canonical (not the stale frontmatter value) and proposes the same rewrite. WARN rows for: missing `project_path`, path no longer exists, git unavailable/timed out, git errors (dubious ownership etc. — never silently treated as non-repo), empty `project:` field, insight source_session not in index. Non-git project dirs left alone; snapshot notes skipped. `--project` matches the old name OR the derived canonical (filtered sessions still seed the Phase-2 index); `--days` is ignored (full-vault backfill). **Opt-in** — excluded from default all-checks sweep (`OPT_IN=True`); run via `--check project-name-canonicalization`. Conceptually run after `--check project-name-normalization` (underscore → hyphen) for clean input.
- `/vault-doctor --check session-coverage` — detect SessionEnd-hook coverage gaps: JSONLs in `~/.claude/projects/` with no corresponding session note. **Opt-in** — excluded from the default all-checks sweep (heavy all-projects JSONL walk); must be named via `--check`. Sessions below the configured `min_messages`/`min_duration_minutes` thresholds are excluded (the hook would also skip them; only text-bearing user messages count). Add `--strict` to emit `FAIL:` (not `WARN:`) when any note references the orphaned session via `source_session` (changes the reason prefix only, not the exit code). Add `--reconstruct` to enable `--apply` to reconstruct the missing note by re-running the SessionEnd hook via `replay-sessionend.py` (never automatic; always requires `--apply`). `--days` bounds JSONL mtime age (default 30). Note: the per-gap project name is derived from the JSONL's `cwd` basename, so `--project` expects the cwd-basename slug — worktree sessions may display a non-canonical expected note path (detection itself is session_id/hash-based and unaffected).
- `/vault-doctor --check audit-historic-repairs` — one-shot audit of historic source-sessions repairs: diffs doctor backups against current notes, classifies each repair (A restore / B keep / C ambiguous / D both-wrong) by date agreement, and restores category-A mtime-bug corruptions on `fix`. **Opt-in** — excluded from the default all-checks sweep; must be named via `--check`. `--days` bounds backup-run age (default 180).
- `/vault-doctor --check missing-frontmatter-fence` — repair notes whose frontmatter lost its opening `---` fence (the leading-fence-eaten failure mode: the first byte is the first frontmatter key, so the note parses as having no frontmatter at all and is invisible to tag-based Dataview queries). Only flags a note when all four preconditions hold: first line is not `---`, first line is `key:`-shaped, a closing `---` exists within the frontmatter line bound, and every line above it is frontmatter-shaped. The fix inserts `---` as a new first line and changes nothing else (line endings and file mode preserved). `--days` is ignored (the damage is historic). Re-run `/vault-reindex` afterwards so the recovered frontmatter reaches the index.
- `/vault-doctor --check memory-index` — detect memory-index drift: entries under `~/.claude/projects/<project>/memory/` that are unreachable from that store's `MEMORY.md`, dangling index pointers, stores with entries but no `MEMORY.md`, unreadable entries, and `MEMORY.md` size against the ~17 KB compaction threshold / ~24 KB read budget. Orphans are transitive (BFS from `MEMORY.md`): `orphan-isolated` when nothing links the entry at all, `orphan-unreachable` when only another orphan does. Link matching is deliberately asymmetric — reachability counts markdown links, `[[wikilinks]]` and bare `name.md` mentions, while dangling counts markdown links **only**, and only ones naming a direct child of the store (`MEMORY.md` legitimately wikilinks Obsidian vault notes, prose cites `CLAUDE.md`, and a `reference` memory line may link a URL ending in `.md`). **Report-only:** every row is unresolved at confidence 0.0, so `fix` never writes to a memory store — and `--min-confidence` above 0.0 hides every row. The index is decoded strictly: a `MEMORY.md` that is not valid UTF-8 makes that store unreadable, because replacing bad bytes would report every entry in it as an orphan. An unreadable store is dropped from the report and gets one `store-unreadable` row; only when **every** selected store fails does the check raise and surface as exit 2 / `crashed_checks`. Entries are decoded leniently but an undecodable one gets an `entry-undecodable` row, and any unreadable or undecodable entry downgrades that store's `orphan-isolated` rows to `orphan-unreachable` (its links could not be read, so the stronger claim is unprovable). A pointer whose target differs only in case is `index-case-mismatch`, not dangling: it resolves on a case-insensitive filesystem and breaks on a case-sensitive one. If `MEMORY.md` has text in it but names no file at all while the store holds entries, the check adds an `index-names-nothing` summary row **on top of** the per-entry orphan rows — the index read is strict, so every entry really is unreachable and the per-entry rows are what tell you which files need a pointer line. Row counts, sizes, both spellings of a case mismatch and the underlying read error are all in the `--json` payload, not just in the prose `reason`. `--days` is ignored (the drift is undated); `--project` is a case-insensitive **substring** match against the project directory name (e.g. `--project obsidian-brain`). **Opt-in** — excluded from the default all-checks sweep (a full run on the author's machine produced 92 rows across 7 projects, measured 2026-08-12); must be named via `--check`.
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

Locate the Python dispatcher by resolving the obsidian-brain install: prefer the local checkout registered in `known_marketplaces.json` (this also covers local dev sessions, deterministically rather than via `$PWD`), falling back to the newest allowlisted version directory in the plugin cache:

```bash
DISPATCHER="$(python3 -c "
import glob, json, os, re
def _ob_doctor():
    try:
        for _m in json.load(open(os.path.expanduser('~/.claude/plugins/known_marketplaces.json'))).values():
            _s = _m.get('source') if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get('source') == 'directory'):
                continue
            _i = _m.get('installLocation') if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            _h = os.path.join(_i, 'hooks')
            if os.path.isfile(os.path.join(_h, 'obsidian_utils.py')):
                _v = os.path.join(os.path.dirname(_h), 'scripts', 'vault_doctor.py')
                if os.path.isfile(_v):
                    return _v
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser('~/.claude/plugins/cache/*/obsidian-brain/*/scripts/vault_doctor.py')) if re.fullmatch('[0-9]+([.][0-9]+)*', _d.split('/')[-3])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split('/')[-3].split('.')], _p), default='')
print(_ob_doctor())
")"
if [[ -z "$DISPATCHER" || ! -f "$DISPATCHER" ]]; then
    echo "ERROR: could not find scripts/vault_doctor.py" >&2
    exit 1
fi
```

If the dispatcher cannot be located, tell the user:

> Could not find `scripts/vault_doctor.py`. Make sure the obsidian-brain plugin is installed via `/dev-test install` (for local dev) or the marketplace.

Stop here if the dispatcher is missing.

### Step 2 — Run the dispatcher in JSON report mode

Always run with `--json` first so you can parse the output deterministically. Pass through only the flags the user provided:

```bash
ARGS=()
[[ -n "${CHECK:-}" ]] && ARGS+=(--check "$CHECK")
[[ -n "${DAYS:-}" ]] && ARGS+=(--days "$DAYS")
[[ -n "${PROJECT:-}" ]] && ARGS+=(--project "$PROJECT")
[[ -n "${STRICT:-}" ]] && ARGS+=(--strict)
[[ -n "${RECONSTRUCT:-}" ]] && ARGS+=(--reconstruct)
[[ -n "${MIN_CONFIDENCE:-}" ]] && ARGS+=(--min-confidence "$MIN_CONFIDENCE")
ARGS+=(--json)
python3 "$DISPATCHER" "${ARGS[@]}"
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
> Run `/vault-doctor fix` to apply repairs. Backups will be written to `~/.claude/obsidian-brain-doctor-backup/<timestamp>/`.

Stop here.

If the user DID pass `fix`:

> Found **N** repairable issue(s) across **K** project(s). I'll apply per project with confirmation.

Re-run the dispatcher with `--apply` (do NOT pass `--yes` — let the dispatcher prompt per project interactively):

```bash
ARGS=()
[[ -n "${CHECK:-}" ]] && ARGS+=(--check "$CHECK")
[[ -n "${DAYS:-}" ]] && ARGS+=(--days "$DAYS")
[[ -n "${PROJECT:-}" ]] && ARGS+=(--project "$PROJECT")
[[ -n "${STRICT:-}" ]] && ARGS+=(--strict)
[[ -n "${RECONSTRUCT:-}" ]] && ARGS+=(--reconstruct)
[[ -n "${MIN_CONFIDENCE:-}" ]] && ARGS+=(--min-confidence "$MIN_CONFIDENCE")
ARGS+=(--apply)
python3 "$DISPATCHER" "${ARGS[@]}"
```

The dispatcher will prompt `Apply N fix(es) for project 'X' in check 'Y'? [y/N]` on stderr for each project. Relay each prompt to the user and pipe their response to the dispatcher's stdin.

### Step 5 — Report the outcome

Parse the final stderr output from the dispatcher and summarize:

```
vault_doctor apply complete
  obsidian-brain: 3 applied, 0 unresolved, 0 errors
  tiny-vacation-agent: 1 applied, 0 unresolved, 0 errors

Backups saved to: ~/.claude/obsidian-brain-doctor-backup/2026-04-11T17-04-22+00-00/
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
- Backups are written automatically by the dispatcher to `~/.claude/obsidian-brain-doctor-backup/<ISO-timestamp>/<project>/<basename>`. Always mention the backup path in your summary so the user knows where to look.
