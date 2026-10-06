---
name: consolidate
description: "Batch-cluster vault notes into named themes. Use when: (1) /consolidate to seed themes from unassigned notes, (2) /consolidate stats, (3) /consolidate split <id>, (4) /consolidate merge <a> <b>, (5) /consolidate --full to wipe and recluster everything."
metadata:
  version: 1.0.0
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

# Consolidate — Batch Theme Clustering

Clusters notes into themes (3+ notes per theme), names them through the bound native AI backend, and
populates `themes` / `theme_members`. The default run is a NON-destructive
seeder over unassigned notes; `--full` wipes and reclusters everything.

**Tools needed:** Bash

## Procedure

### Step 1 — Parse arguments

- no args / `--full`  → Step 2 (consolidate)
- `stats`             → Step 3
- `split <id>`        → Step 4; bind `THEME_ID=<id>` from the user's numeric argument
- `merge <a> <b>`     → Step 5; bind `A=<a>` and `B=<b>` from the two numeric arguments

### Step 2 — Consolidate (seed or full)

Request for `consolidate` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "argv": [
    "<arguments from this step>"
  ]
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'consolidate' < "$REQUEST_PATH"
```

Report the `UNASSIGNED=` (or `SCANNED=` for `--full`), `CREATED=`, and
`THEMES_TOTAL=` lines. If `CREATED=0`, tell the user no clusters of 3+ similar
notes were found (themes need at least 3 notes above the similarity threshold).

### Step 3 — Stats

Request for `theme-stats` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "argv": [
    "<arguments from this step>"
  ]
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'theme-stats' < "$REQUEST_PATH"
```

Present `THEMES=`, `MEMBERS=`, `UNASSIGNED=`, the `LARGEST` rows, and any `NUDGE`.

### Step 4 — Split

Request for `theme-split` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "argv": [
    "<arguments from this step>"
  ]
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'theme-split' < "$REQUEST_PATH"
```

Report `SPLIT` or `NO_SPLIT`.

### Step 5 — Merge

Request for `theme-merge` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "argv": [
    "<arguments from this step>"
  ]
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'theme-merge' < "$REQUEST_PATH"
```

Report `MERGED` or the error.
