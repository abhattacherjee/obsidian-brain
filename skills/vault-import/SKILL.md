---
name: vault-import
description: "Backfills the Obsidian vault from explicitly selected Claude or Codex transcript history. Use when: (1) /vault-import command to import recent sessions, (2) /vault-import 30d to import last 30 days, (3) /vault-import project:api-service 30d to filter by project, (4) user wants to populate vault with past session history."
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

Create new curated notes with `note-create`. A collision preserves the existing note. Use only the operations documented for this skill. Their writes bind the source revisions before analysis and preserve manual edits on conflict. Content is JSON data, never shell code. Read `references/host-claude.md` or `references/host-codex.md` when present. Codex has no native memory-file API; shared vault retrieval and wiki filing continue without borrowing another host's memory.

# Vault Import — Backfill Historical Sessions

Discover the explicitly selected source host's history, summarize with the invoking host, and write structured session notes. Source identity stays unchanged when another host performs the import. Skip sessions already present in the vault.

**Tool use:** Read the invoking host's reference for its available shell and file operations. Use only tools that runtime exposes.

**Prerequisites:**
- Under Claude, optional `/context:search` and `/context:shield` come from `context@claude-code-skills`. Under Codex, use the fixed `import-list`/`import-read` operations documented below; do not invoke a Claude-only helper.
- Obsidian Brain must be configured (run `/obsidian-setup` if not)

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Step 1 — Read config

Run:

Request for `config` (substitute the values as data):

```json
{}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'config' < "$REQUEST_PATH"
```

Parse the single JSON object. Read its named fields; do not split output on `=`.

If the command exits non-zero or prints ERROR, tell the user:

> Config not found. Please run `/obsidian-setup` first to configure your Obsidian vault.

Stop here if config is missing.

Store the extracted values as `VAULT_PATH` and `SESSIONS_FOLDER` (default `claude-sessions`).

### Step 2 — Validate vault access

Run:

```bash
test -d "$VAULT_PATH/$SESSIONS_FOLDER" && test -w "$VAULT_PATH/$SESSIONS_FOLDER" && echo "OK" || echo "FAIL"
```

If FAIL, tell the user:

> The sessions folder `$VAULT_PATH/$SESSIONS_FOLDER` does not exist or is not writable. Run `/obsidian-setup` to fix this.

Stop here if FAIL.

### Step 3 — Parse arguments

Parse the user's invocation to extract:

- **Time range:** A duration like `7d`, `14d`, `30d`. Default is `7d` if not specified.
- **Project filter:** An optional `project:<name>` argument (e.g. `project:api-service`).

Examples:
- `/vault-import` — last 7 days, all projects
- `/vault-import 30d` — last 30 days, all projects
- `/vault-import project:api-service 14d` — last 14 days, only api-service
- `/vault-import project:api-service` — last 7 days, only api-service

Store as `TIME_RANGE` and `PROJECT_FILTER` (empty string if no filter).

### Step 4 — Discover sessions

Select `source_host` explicitly (Claude or Codex). The installed native
registry discovers only that host's historical transcript roots. It reports a
bounded discovery limit as pending; choose a narrower root/date window rather
than silently dropping records. An explicit `source_root` may narrow an existing
native history root. It cannot authorize `/` or unrelated folders.

Request for `import-list` (substitute the values as data):

```json
{
  "source_host": "<explicit source host>",
  "days": 30,
  "project": "<optional source project>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'import-list' < "$REQUEST_PATH"
```

The operation prints one JSON object per source with full `session_id`,
`session_path`, original project path, date, and source host. Unknown branch
and message counts stay unknown. Missing transcripts are skipped explicitly.

Before requesting any summary, run `import-read` with `operation_id`,
`source_host`, `source_path`, and the full `source_session_id`. It normalizes
visible native records and stores an immutable artifact keyed by full source
provider/session identity. Retain `source_host` and `source_session_id` for
the matching `note-create`; another import in this operation cannot replace them. Unknown or
partial rows leave the operation pending. Use the normalized records as AI
input. AI remains on the invoking host; origin metadata remains on the source.

Map each discovered `session_path` to `source_path` and its full `session_id`
to `source_session_id`. Keep the source host selected for that discovery.

Request for `import-read`:

```json
{
  "operation_id": "<prepared id>",
  "source_host": "<claude or codex origin>",
  "source_session_id": "<full source native ID>",
  "source_path": "<absolute discovered session_path>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'import-read' < "$REQUEST_PATH"
```

If no sessions are found, tell the user:

> No sessions found in the last `<TIME_RANGE>` matching your filters.

Stop here if no sessions found.

### Step 5 — Filter already-imported sessions

Run `import-read` for each discovered source before generating a summary.
The trusted helper queries the selected index for the full provider/native ID,
verifies the complete bounded frontmatter, and returns `skipped` only for a
proven identity match. It never scans the entire vault or adopts a hash alone.
Treat a capped, ambiguous, or timed-out lookup as pending.

Store the remaining sessions as `PENDING_SESSIONS` and the count of skipped sessions as `SKIPPED_COUNT`.

If no pending sessions remain, tell the user:

> All `<TOTAL>` sessions from the last `<TIME_RANGE>` are already in the vault. Nothing to import.

Stop here if nothing to import.

Otherwise, report:

> Found `<TOTAL>` sessions, `<SKIPPED_COUNT>` already imported, `<PENDING_COUNT>` to import.

### Step 6 — Summarize sessions with parallel sub-agents

Summarize each source with the invoking host's native AI. Use independent
helpers when available; retain pending failures rather than switching hosts.

For each session in `PENDING_SESSIONS`, use a native analysis helper with this prompt:

> Read the immutable normalized records from `<registered import-source.json>` for the explicitly selected source host. Extract and return a structured summary with these exact sections:
>
> - **Summary:** 2-3 sentence overview of what was accomplished
> - **Key Decisions:** Bulleted list of architectural or design choices made
> - **Changes Made:** Bulleted list of files created, modified, or deleted
> - **Errors Encountered:** Bulleted list of errors hit and how they were resolved (or "None")
> - **Next Steps:** Bulleted list of follow-up tasks mentioned (or "None")
> - **Git Info:** Branch name, commit hashes if any (or "None")
>
> Keep the total output under 300 tokens. Return only the structured summary, no preamble.

**Parallelism rules:**
- Launch up to 5 sub-agents in parallel (sessions are independent — no shared state)
- Wait for the batch to complete before launching the next batch
- If a sub-agent fails or times out, log the error and skip that session — do not block the entire import

Collect the distilled summaries. Store each as `SUMMARY` keyed by `session_id`.

### Step 7 — Construct and write session notes

For each successfully summarized session, construct a vault note with this format:

```markdown
---
type: claude-session
date: <YYYY-MM-DD from session date>
session_id: <session_id>
project: <project name>
git_branch: <branch from git info, or empty>
duration_minutes: <estimated from transcript length, or empty>
imported: true
imported_date: <today's date YYYY-MM-DD>
tags:
  - claude/session
  - claude/project/<project-name>
  - claude/imported
---

# <Session Title derived from summary>

<Summary section>

## Key Decisions

<Key decisions bulleted list>

## Changes Made

<Changes bulleted list>

## Errors Encountered

<Errors bulleted list>

## Next Steps

<Next steps bulleted list>

## Git Info

<Git info>
```

Generate the filename using the same convention as other session notes:

1. **Date:** `YYYY-MM-DD` (session date)
2. **Slug:** Title lowercased, spaces to hyphens, non-alphanumeric (except hyphens) removed, truncated to 50 chars
3. **Hash:** 4-character hex hash from the session_id: `echo -n "<session_id>" | md5 | cut -c1-4` (macOS) or `echo -n "<session_id>" | md5sum | cut -c1-4` (Linux). Do NOT use `tail -c 4` — it counts the trailing newline as a byte and returns only 3 visible characters.

Final filename: `YYYY-MM-DD-<slug>-<hash>.md`

Send the complete note or update as a JSON string to the fixed operation.
The launcher writes atomically at mode `0o600`. Existing notes require their
pre-analysis source revision. Content never becomes shell code.

Request for `note-create` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "source_host": "<claude or codex origin>",
  "source_session_id": "<full source native ID>",
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
type: claude-session
...
---

# <Session Title>
...
```

On success this prints `OK: <absolute path>`. On failure it prints `ERROR: <reason>` to stderr and exits non-zero for THAT session only — record it under `<FAILED_COUNT>`/`<Failed sessions>` in Step 8 and continue importing the remaining sessions; one failed write must not abort the whole import loop.

**One exception: an `ERROR:` containing `note already exists` counts as SKIPPED, not failed.** It means this session was imported by an earlier run, which is the normal outcome of a re-import — increment `<SKIPPED_COUNT>` and move on. Do not overwrite a collision: that would silently replace an existing note, which is exactly what the flag exists to prevent.

### Step 8 — Report results

Print a summary report:

> **Vault import complete!**
>
> - **Imported:** `<IMPORTED_COUNT>` sessions
> - **Skipped (already in vault):** `<SKIPPED_COUNT>` sessions
> - **Failed:** `<FAILED_COUNT>` sessions (if any)
> - **Time range:** last `<TIME_RANGE>`
> - **Project filter:** `<PROJECT_FILTER>` (or "all projects")
>
> New notes written to: `$VAULT_PATH/$SESSIONS_FOLDER/`

If any sessions failed, list them:

> **Failed sessions:**
> - `<session_id>`: `<error reason>`

Offer follow-up:

> Run `/vault-import <longer range>` to go further back, or open Obsidian to browse the imported sessions.

Deduplication uses full `(agent_provider, agent_session_id)` within the selected
vault. A four-character filename hash alone never proves identity. Multiple
matching identities or a bounded lookup failure remain pending. New import
notes retain original date/project, source provider/full native ID, and
`author_host` from the invoking host.
