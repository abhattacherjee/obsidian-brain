---
name: vault-reindex
description: Rebuild the SQLite FTS5 vault index. Non-destructive by default — preserves Friston activation data (access_log, themes, theme_members); only regenerates derivable tables. Opt into `--full` for a complete wipe.
metadata:
  version: 2.0.0
---

## Native runtime and installed resources

Use the absolute path of this loaded `SKILL.md` as `OB_SKILL_PATH`. Read the
reference for the invoking host when this skill has paired host references.
Set `OB_HOST`, `OB_CLIENT`, `OB_SESSION_ID`, and `OB_CWD` from that native
invocation. Use the selected host's own session ID. Keep curated note taxonomy
separate from `agent_provider` and `agent_session_id` provenance.

```bash
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
is unsupported for Codex until its adapter is verified; shared vault retrieval
and wiki filing continue without borrowing another host's memory.

Read `references/host-claude.md` or `references/host-codex.md` when present.
All note writes described below use `note-create` or revision-bound `note-apply`,
including bidirectional related links. Content is a JSON string, never shell code.
Keep this rule when a later step uses the word Write or Edit.

# Vault Reindex — Rebuild FTS5 Index

Regenerates the `notes` and `notes_fts` tables from on-disk state. Use when the index is stale, corrupt, after bulk edits in Obsidian, or to purge leftover rows from pytest fixtures.

**Tools needed:** Bash

## Modes

- **Default (`/vault-reindex`)** — non-destructive. Reconciles the note index with current vault contents via an mtime-incremental sync: newly added notes get indexed, notes deleted from disk are removed, rows with paths outside the current scanned folders (pytest pollution, stale mounts) are cleaned up. **Preserves** `access_log` (ACT-R activation history), `themes`, and `theme_members` (cluster centroids + surprise scores). Orphaned rows whose note paths are no longer in scope are pruned. **Does not** re-tokenise unchanged notes or rebuild FTS/term_df for them — if an existing row is internally corrupted but its on-disk mtime hasn't changed, only `--full` will fix it.
- **`/vault-reindex --full`** — destructive. Deletes the entire `<native index_path>` file and rebuilds from an empty schema. Every Friston field is lost. Required when the schema is corrupt/incompatible or when derivable tables need a clean-slate rebuild.

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Step 1 — Parse arguments and load config

If the user passes `--full`, set `FULL_MODE=true`; otherwise `FULL_MODE=false`.

Run:

Request for `config` (substitute the values as data):

```json
{}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'config' < "$REQUEST_PATH"
```

Parse each output line as KEY=VALUE, splitting on the first `=`.

If config is missing or the command fails, tell the user:

> Config not found. Run `/obsidian-setup` first to configure your vault path.

Stop here if config is missing.

### Step 2 — Confirm full mode (only if `--full`)

If `FULL_MODE=true`, warn the user before proceeding:

> ⚠️ **Full rebuild requested.** This will **delete** `access_log`, `themes`, and `theme_members` (activation history, cluster centroids, surprise scores). These tables do not regenerate automatically — activation signal accumulates over time as you run `/recall`, `/vault-search`, and `/vault-ask`. Themes will be empty until `/consolidate` ships.
>
> Continue? Reply `yes` to confirm, anything else to cancel.

Wait for confirmation. Abort if the user does not reply `yes`. If cancelled, tell them they can run the default `/vault-reindex` (without `--full`) for a non-destructive rebuild.

### Step 3 — Rebuild

Run, passing `VAULT_PATH` and `FULL_MODE` as command-line arguments. The folders come from a fresh read of the config, not the session cache. The snippet refuses (exit 1) when that read's `vault_path` does not match `VAULT_PATH` (an unreadable config falls back to defaults) or when `wiki_folder` is invalid:

Request for `reindex` (substitute the values as data):

```json
{
  "full": false
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'reindex' < "$REQUEST_PATH"
```

If the command fails (non-zero exit or exception in output), tell the user:

> Index rebuild failed. Check that the vault path is accessible and that the plugin is installed. Error: `<stderr>`

Stop here on failure.

### Step 4 — Report

Parse the JSON output from Step 3. Extract:

- `inserted` — total notes indexed
- `skipped` — sum of `unchanged` + `malformed` (kept for backward compatibility; do not report this alone — it conflates a healthy outcome with a real one)
- `unchanged` — notes whose mtime matched the index; nothing to do, the healthy common case
- `malformed` — notes whose frontmatter failed to parse (true total, not capped): dropped from the index if never indexed before, or, if already indexed, left at its last-good indexed content
- `malformed_files` — list of `{"file": <sanitized basename>, "reason": <classifier>}`, capped (currently 20 entries) even when `malformed` is larger
- `foreign_deleted` — rows dropped because their path is outside every scanned folder (non-destructive mode only)
- `folders` — list of `{"name", "exists"}` for each folder scanned
- `excluded` — notes skipped on purpose because their type is never indexed (the wiki's own `index.md` and `log-*.md` files, typed `claude-wiki-index`, #383); not a problem
- `elapsed` — time in seconds
- `by_type` — dict mapping note type to count
- `mode` — `"preserve"` or `"full"`
- `preserved` — dict with `access_log`, `themes`, `theme_members` counts (non-destructive mode only)
- `pruned_orphans` — dict with `access_log`, `theme_members` pruned counts (non-destructive mode only)

Present this report:

> **Rebuilt vault index** (`<mode>` mode): `<inserted>` notes indexed in `<elapsed>`s
>
> | Type | Count |
> |------|-------|
> | claude-session | `<count>` |
> | claude-insight | `<count>` |
> | ... | ... |
>
> Unchanged: `<unchanged>` file(s) already indexed (nothing to do). Malformed: `<malformed>` file(s) with frontmatter that failed to parse.

Add a line naming the scanned folders from `folders`, marking each one whose `exists` is false as "(missing — nothing indexed)". A missing `wiki_folder` usually means a typo in the config or that `/obsidian-setup` has not been re-run. If `excluded` is greater than 0, add: "Skipped `<excluded>` wiki index/log file(s) (not knowledge, never indexed)." If `foreign_deleted` is greater than 0, add: "Dropped `<foreign_deleted>` row(s) for notes outside the scanned folders (a folder was renamed or removed from the config, or old test data)."

Only include rows in the table for types that appear in `by_type` (omit zero-count types). Sort rows by count descending.

**If `malformed` is greater than 0**, list the named files so the user can act:

> **Malformed files:**
> - `<file>` — `<reason>`
> - ...

List every entry in `malformed_files`. If `malformed` exceeds the length of `malformed_files` (the report is capped), append, using the actual number of entries you just listed rather than a hardcoded number:

> Showing the first `<len(malformed_files)>` of `<malformed>` malformed file(s); re-run after fixing these to surface the rest.

**Additional section — non-destructive mode only:**

> **Friston data preserved:**
> - `access_log`: `<access_log>` activation event(s)
> - `themes`: `<themes>` cluster(s)
> - `theme_members`: `<theme_members>` assignment(s)
>
> Pruned `<pruned_access_log>` orphaned access-log row(s) and `<pruned_theme_members>` orphaned theme-member row(s) referencing notes no longer in the current index scope (either deleted from disk or outside the scanned folders).

If both pruned counts are 0, replace the pruning line with `No orphan rows to prune.`.

**Additional section — full mode only:**

> ⚠️ **Friston data cleared:** `access_log`, `themes`, and `theme_members` are now empty. Activation signal will rebuild as you use the vault.

If `inserted` is 0 and `skipped` is 0, also tell the user:

> No notes found. Verify that the folders listed in `folders` (under `<VAULT>`) exist and contain markdown files.
