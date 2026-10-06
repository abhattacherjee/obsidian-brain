---
name: vault-stats
description: "Vault health diagnostics and usage analytics — signal coverage, access patterns, importance distribution, top accessed notes. Saves report to vault for trend tracking. Use when: (1) /vault-stats command, (2) user wants to check vault health, (3) user wants to see access patterns or signal effectiveness."
metadata:
  version: 1.0.0
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

# Vault Stats — Health Diagnostics & Usage Analytics

Shows vault-wide health metrics and current project usage analytics, then saves the report as a vault note for trend tracking.

**Tools needed:** native shell

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Step 1 — Load config, derive project, compute stats

Run a single call that loads config, derives the project name, and computes all stats:

Request for `stats` (substitute the values as data):

```json
{
  "project": "<canonical project>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'stats' < "$REQUEST_PATH"
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

Send the complete note or update as a JSON string to the fixed operation.
The launcher writes atomically at mode `0o600`. Existing notes require their
pre-analysis source revision. Content never becomes shell code.

Request for `note-create` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "folder": "<selected folder>",
  "filename": "<filename.md>",
  "content": "<complete frontmatter + body>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'note-create' < "$REQUEST_PATH"
```

Preserve this note content as the JSON `content` string:

```markdown
---
type: claude-stats
...
---

## Vault Health
...
```

On success this prints `OK: <absolute path>` — that is the file at `$VAULT_PATH/$INSIGHTS_FOLDER/<filename>`. On failure it prints `ERROR: <reason>` to stderr and exits non-zero; surface that message to the user and stop here.

If the error is `note already exists`, the 4-hex filename hash collided with a note written in the same second. Regenerate the hash (the filename step above), rebuild the filename, and retry the write **once**. If it fails again for any reason, surface the error and stop — do not loop.

### Step 5 — Confirm

Print:

> Stats saved to `<full path>`. View in Obsidian to track trends over time.
