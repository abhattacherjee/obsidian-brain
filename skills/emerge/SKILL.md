---
name: emerge
description: "Surface unnamed patterns across vault themes. Use when: (1) /emerge for last 30 days, (2) /emerge 14d for custom window, (3) /emerge this week."
metadata:
  version: 2.0.0
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

Use only the operations documented for this skill. Their writes bind the source revisions before analysis and preserve manual edits on conflict. Content is JSON data, never shell code. Read `references/host-claude.md` or `references/host-codex.md` when present. Codex has no native memory-file API; shared vault retrieval and wiki filing continue without borrowing another host's memory.

# Emerge — Discover Patterns Across Your Obsidian Vault Themes

Operates on **themes** (clustered by `/consolidate`), not raw notes, so it scales
to large vaults within one sub-agent's context budget. Reads themes updated in a
date window, ranks them by activation, and synthesizes cross-cutting patterns.

**Tools needed:** native shell, native analysis delegation, trusted publication, native file reading

## Procedure

### Step 0 — Create task manifest

```
Native progress task: subject="Collect themes in window", activeForm="Collecting themes"
Native progress task: subject="Analyze patterns across themes", activeForm="Analyzing patterns"
Native progress task: subject="Build emerge report", activeForm="Building report"
Native progress task: subject="Write vault note", activeForm="Writing vault note"
Native progress task: subject="Present results", activeForm="Presenting results"
```
Track task IDs. Set task #1 to `in_progress`.

### Step 1 — Parse args + collect themes

Parse the arg as DAYS: no arg = 30, `Nd`/`N days` = N, `this week` = days since Monday. Bind it to `$DAYS`.

Request for `themes` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "days": 30
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'themes' < "$REQUEST_PATH"
```

Parse the `STATUS=` line:
- `STATUS=SPARSE:<n>` → tell the user verbatim: *"Only <n> theme(s) were updated in this window. Run `/consolidate` to seed themes first, or widen the window: `/emerge 90d`."* — then STOP (do not run Steps 2-4).
- `STATUS=OK:<themes>:<unassigned>` → proceed to Step 2.

If the command errors (config missing / non-zero exit), tell the user to run `/obsidian-setup` first and stop. Mark task #1 `completed`.

### Step 2 — Pattern synthesis
Set task #2 to `in_progress`. Use one native analysis helper:
```
Native analysis helper({
  description: "Analyze vault themes for cross-cutting patterns",
  prompt: "Read <registered THEMES_PATH>. It contains `themes` (each with name, summary, note_count, activation, project, and a `members` array of {title, excerpt, similarity, surprise, project}) and `unassigned_candidates` ({title, excerpt, project, date}). Return analysis text with EXACTLY these sections:\n\n## Growing Themes\nThemes with high activation / recent member growth — momentum.\n\n## Decaying Themes\nThemes with low activation / stale members — fading from focus.\n\n## Cross-Project Connections\nThemes whose members span multiple projects, or shared themes between projects. SKIP this section entirely if there is only 1 project.\n\n## Contradictions\nTensions or reversals across themes. Highlight members with high `surprise` values (they diverged from their theme centroid).\n\n## New Candidates\nProto-themes hinted by the `unassigned_candidates` — clusters of related unassigned notes not yet consolidated.\n\nFor each item: a descriptive name, 2-3 references (theme names or note titles), and a confidence (strong/moderate/tentative).\n\nIMPORTANT: Output ONLY the `##` section content as the note body — do NOT add YAML frontmatter, a top-level `#` title, or any `---` delimiter line. The note's frontmatter and title are added separately by run_build_note; any frontmatter you add would be embedded into the body and produce a malformed double-frontmatter note.\n\nReturn the complete analysis text; the parent stores it with `artifact-store` as `emerge-analysis.md`."
})
```

If the returned sections are missing or invalid, report failure and stop. Store complete text through `artifact-store` using the prepared operation ID and name `emerge-analysis.md`; use the returned registered path as ANALYSIS_PATH. Mark task #2 `completed`.

### Step 3 — Build output + write note
Request for `build-note` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "themes_path": "<registered themes.json>",
  "analysis_path": "<registered emerge-analysis.md>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'build-note' < "$REQUEST_PATH"
```

Parse `SAVED:<path>` and everything after `---REPORT---`. Mark tasks #3-#4 `completed`.

### Step 4 — Present to user
Display report prefixed with **Pattern Discovery Results:**. Confirm saved path. Mark task #5 `completed`.

## Edge Cases
- **Sparse window (< 2 themes updated):** nudge the user to run `/consolidate` first or widen the window (`/emerge 90d`) — see Step 1.
- **Only 1 project:** Sub-agent skips Cross-Project Connections.
- **Config not found:** Tell user to run `/obsidian-setup` first.

## Fixed request shapes

Pass these objects through the installed launcher for the named operation. Keep
one operation ID across source reads, analysis and reviewed publication.

Request for `artifact-store`:

```json
{
  "operation_id": "<prepared id>",
  "name": "emerge-analysis.md",
  "content": "<reviewed helper output as data>"
}
```
