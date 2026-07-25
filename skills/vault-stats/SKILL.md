---
name: vault-stats
description: "Vault health diagnostics and usage analytics — signal coverage, access patterns, importance distribution, top accessed notes. Saves report to vault for trend tracking. Use when: (1) /vault-stats command, (2) user wants to check vault health, (3) user wants to see access patterns or signal effectiveness."
metadata:
  version: 1.0.0
---

# Vault Stats — Health Diagnostics & Usage Analytics

Shows vault-wide health metrics and current project usage analytics, then saves the report as a vault note for trend tracking.

**Tools needed:** Bash

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Step 1 — Load config, derive project, compute stats

Run a single call that loads config, derives the project name, and computes all stats:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys, os, json
import glob; sys.path.insert(0, max(glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")), default="hooks"))
from obsidian_utils import load_config
from vault_index import ensure_index
from vault_stats import compute_stats
c = load_config()
if not c.get("vault_path"):
    print("ERROR=vault_path not configured. Run /obsidian-setup first.")
    sys.exit(0)
vp = c["vault_path"]
folders = [c.get("sessions_folder", "claude-sessions"), c.get("insights_folder", "claude-insights")]
db = ensure_index(vp, folders)
project = os.path.basename(os.getcwd()).lower().replace(" ", "-")
result = compute_stats(db, project)
print("VAULT=" + vp)
print("INS=" + c.get("insights_folder", "claude-insights"))
print("PROJECT=" + project)
print("STATS_JSON=" + result)
'
```

Parse each output line as KEY=VALUE, splitting on the first `=`.

If an `ERROR` key is present, display its value and stop.

If `STATS_JSON` contains `"error"`, display the error message and stop.

Parse `STATS_JSON` as JSON into a variable `STATS`.

### Step 2 — Check for empty/missing data

If `STATS.vault_wide.total_notes == 0`:

> No notes indexed. Run `/vault-reindex` first.

Stop here.

If `STATS.vault_wide.access_log_entries == 0`, note this for later — display the stats tables normally but append a note at the end.

### Step 3 — Format and display

Format the JSON into markdown tables and display to the user. Use this structure:

**Vault-wide section:**

```
## Vault Health

| Metric | Value |
|---|---|
| Total notes | <total_notes> |
| DB size | <db_size_bytes formatted: >= 1048576 → "X.X MB", >= 1024 → "X.X KB", else "N bytes"> |
| access_log entries | <access_log_entries with commas> |
| Oldest access | <oldest_access or "None yet"> |

## Signal Coverage

| Signal | Coverage | Notes |
|---|---|---|
| Activation (access history) | <pct>% (<has_activation>/<total_notes>) | <total_notes - has_activation> notes never accessed |
| Importance (non-default) | <pct>% (<has_importance>/<total_notes>) | <total_notes - has_importance> notes at default 5 |
| Both signals active | <pct>% (<has_both>/<total_notes>) | Full 7-signal scoring |
| Neither signal | <pct>% (<has_neither>/<total_notes>) | Using 5-signal fallback |

## Access Patterns (last 30 days)

| Context | Count | % |
|---|---|---|
| <for each entry in access_by_context, sorted by count desc> |

## Top 10 Most Accessed Notes

| # | Note | Accesses | Activation | Importance |
|---|---|---|---|---|
| <for each entry in top_accessed, numbered 1-N> |

## Importance Distribution

| Score | Count |
|---|---|
| 1-3 (trivial) | <trivial> |
| 4-6 (standard) | <standard> |
| 7-8 (significant) | <significant> |
| 9-10 (critical) | <critical> |
```

**Project section:**

```
---

## Project: <project.name>

| Metric | Value |
|---|---|
| Notes | <total_notes> |
| Access events | <access_events> |
| Avg accesses/note | <avg_accesses> |
| Notes with activation | <notes_with_activation> (<pct>%) |
| Notes with importance != 5 | <notes_with_importance> (<pct>%) |

## Recent Activity (last 7 days)

| Context | Count |
|---|---|
| <for each entry in recent_activity, sorted by count desc> |

## Top 5 Most Accessed (this project)

| # | Note | Accesses | Activation | Importance |
|---|---|---|---|---|
| <for each entry in project.top_accessed, numbered 1-N> |
```

Compute percentages: `round(count / denominator * 100)` — show as integer with `%`. For any percentage, if the denominator is 0, show `0%`. This applies to all tables (signal coverage uses total_notes, access patterns uses sum of counts).

Format large numbers with commas (e.g. `1,832`).

If `access_log_entries == 0`, append after the tables:

> Access tracking is active. Run `/vault-search` and `/recall` to start building history.

### Snapshots section

If the JSON payload has a `vault_wide.snapshots` object with
`total_snapshots > 0`, render a `## Snapshots` section after the
Importance Distribution table:

```
## Snapshots
Total: {total_snapshots} (compact: {by_trigger.compact}, clear: {by_trigger.clear}, auto: {by_trigger.auto})
Sessions with snapshots: {sessions_with_snapshots} (max {max_snapshots_per_session} per session)
Summarization: {summarized_fraction formatted as integer %}
Integrity: {orphaned_snapshots} orphan(s), {broken_backlinks} broken backlink(s)
```

If `read_errors > 0`, append on a new line before the auto-fix suggestion:

```
⚠ {read_errors} snapshot file(s) unreadable — check stderr for paths.
```

If `orphaned_snapshots > 0` or `broken_backlinks > 0`, append on a new line:

```
Run `/vault-doctor` to auto-fix.
```

If `total_snapshots == 0`, omit the section entirely.

### Step 4 — Save vault note

Generate filename:
1. Date: today's date `YYYY-MM-DD`
2. Hash: 4-character hex from `date +%s | md5 | cut -c29-32` (macOS) or `date +%s | md5sum | cut -c1-4` (Linux). Do NOT use `tail -c 4`.
3. Filename: `YYYY-MM-DD-vault-stats-<hash>.md`

Compose the full note: frontmatter + the markdown output from Step 3.

Frontmatter:

```yaml
---
type: claude-stats
date: YYYY-MM-DD
project: <PROJECT>
tags:
  - claude/stats
  - claude/project/<PROJECT>
---
```

Run the note-writer CLI, piping the full note (frontmatter + body) in on stdin. It creates `$INSIGHTS_FOLDER` if needed and writes the file atomically at mode `0o600` — no `mkdir`/`chmod` needed (this skill already used `0o600`, so nothing changes there — only the write mechanism does). **Two rules for the heredoc terminator, both load-bearing.** (1) It must stay **quoted** (`<<'OB_NOTE_EOF_<eof4>'`) — do not drop the quotes in a future edit. (2) It must be **unique per invocation**: substitute the same 4 random hex characters for `<eof4>` in BOTH the `<<'OB_NOTE_EOF_<eof4>'` opener and the terminator line, then confirm that **no line of the content you are about to emit is exactly that terminator** — if one is, pick different hex characters and re-check. **Never** replace this with a fixed delimiter. Quoting stops `$`/backtick expansion but does NOT stop early termination: a line equal to the terminator at column 0 ends the heredoc there, silently truncating the content AND handing everything after it to the shell as commands to execute. Notes written by this plugin routinely quote these very blocks, so a fixed terminator is a live hazard, not a theoretical one. The `HOOKS=` line below sorts cached plugin versions **numerically** (a plain `max()` is lexicographic and picks `3.9.0` over `3.10.0`, resolving to a cache with no `note_writer.py`), and the `test -f` line turns a stale/incomplete cache into the documented `ERROR:` shape instead of a raw Python `can't open file` message. An unquoted delimiter lets the shell expand `$` variables and backtick commands embedded in the note body, silently corrupting it:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
HOOKS=$(python3 -c "import glob,os,re; c=glob.glob(os.path.expanduser('~/.claude/plugins/cache/*/obsidian-brain/*/hooks')); print(max(c, key=lambda p: ([int(n) for n in re.findall('[0-9]+', p.split('/')[-2])], p), default='hooks'))")
test -f "$HOOKS/note_writer.py" || { echo "ERROR: note_writer.py not found under $HOOKS - the plugin cache is stale or incomplete. Run /plugin marketplace update (or /dev-test install for local dev), then retry." >&2; exit 1; }
python3 "$HOOKS/note_writer.py" write "$VAULT_PATH" "$INSIGHTS_FOLDER" "YYYY-MM-DD-vault-stats-<hash>.md" <<'OB_NOTE_EOF_<eof4>'
---
type: claude-stats
...
---

## Vault Health
...
OB_NOTE_EOF_<eof4>
```

On success this prints `OK: <absolute path>` — that is the file at `$VAULT_PATH/$INSIGHTS_FOLDER/<filename>`. On failure it prints `ERROR: <reason>` to stderr and exits non-zero; surface that message to the user and stop here.

### Step 5 — Confirm

Print:

> Stats saved to `<full path>`. View in Obsidian to track trends over time.
