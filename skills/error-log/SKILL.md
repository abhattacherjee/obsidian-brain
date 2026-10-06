---
name: error-log
description: "Captures non-obvious errors and their solutions as structured Obsidian notes for future reference. Use when: (1) /error-log command to capture an error from the current session, (2) /error-log <error description> to log a specific error, (3) user wants to document a tricky bug fix or error resolution."
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

# Error Log — Capture Error Solutions to Obsidian

Analyze the current conversation for error -> investigation -> fix patterns, structure them as reusable troubleshooting notes, and save to the Obsidian vault.

**Tools needed:** native shell, native file reading

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

Parse each output line as KEY=VALUE, splitting on the first `=`.

If the file does not exist or is invalid JSON, tell the user:

> Config not found. Please run `/obsidian-setup` first to configure your Obsidian vault.

Stop here if config is missing.

### Step 2 — Validate vault access

Run:

```bash
test -d "$VAULT_PATH/$INSIGHTS_FOLDER" && test -w "$VAULT_PATH/$INSIGHTS_FOLDER" && echo "OK" || echo "FAIL"
```

If FAIL, tell the user:

> The insights folder `$VAULT_PATH/$INSIGHTS_FOLDER` does not exist or is not writable. Run `/obsidian-setup` to fix this.

Stop here if FAIL.

### Step 3 — Identify the error

Check if the user provided an argument after `/error-log`.

- **With argument** (e.g. `/error-log BrokenPipeError in subprocess`): Use the description to search the current conversation for matching error context, investigation steps, and resolution.
- **Without argument** (bare `/error-log`): Scan the full conversation for error -> investigation -> fix patterns. Look for:
  - Stack traces, error messages, or exception output
  - Debugging steps taken (hypothesis, investigation, failed attempts)
  - The fix that ultimately resolved the issue
  - If multiple errors were resolved, present a numbered list and ask the user which to log

If no error pattern is found in the conversation, tell the user:

> No error -> fix pattern detected in this session. You can run `/error-log <description>` to manually describe an error to document.

Stop here if no error is found.

### Step 4 — Structure the error note

Draft the note body with these four sections. Each section should be concise but complete enough to be useful months later when encountering the same error:

- **Error:** The exact error message, symptoms, and context where it appeared (include relevant stack trace snippets or command output if available, formatted as code blocks)
- **Root Cause:** Why the error happened — the underlying reason, not just the surface symptom
- **Fix:** The specific change or command that resolved it — include code diffs, config changes, or commands as code blocks
- **Prevention:** How to avoid this error in the future — linting rules, config patterns, pre-checks, or design principles

### Step 5 — Auto-generate topic tags

Based on the error content, generate 1-3 topic tags. Tags should be lowercase, hyphenated, and specific to the technology or domain. Examples:

- `claude/topic/subprocess-pipes`
- `claude/topic/python-async`
- `claude/topic/npm-dependencies`
- `claude/topic/git-hooks`

### Step 6 — Show preview and ask for edits

Present the full note to the user including frontmatter:

```
---
type: claude-error-fix
date: YYYY-MM-DD
created_at: <ISO-8601-UTC>
source_session: <current-session-id>
source_session_note: "[[<session-note-filename>]]"
project: <project-name>
tags:
  - claude/error-fix
  - claude/project/<project-name>
  - claude/topic/<auto-generated-topic-1>
  - claude/topic/<auto-generated-topic-2>
---

# <Error Title>

## Error

<error message and symptoms>

## Root Cause

<why it happened>

## Fix

<what resolved it>

## Prevention

<how to avoid it in future>
```

Where:
- `YYYY-MM-DD` is today's date
- `<ISO-8601-UTC>` is the current UTC timestamp at second precision. Get it via:
  Request for `clock` (substitute the values as data):

  ```json
  {}
  ```

  ```bash
  python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'clock' < "$REQUEST_PATH"
  ```
  Example: `2026-04-24T18:42:11+00:00`
- `<current-session-id>` and `<session-note-filename>` are derived together. Get session context via the shared helper:

  Request for `session` (substitute the values as data):

  ```json
  {}
  ```

  ```bash
  python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'session' < "$REQUEST_PATH"
  ```

  Parse the output to get `SESSION_ID`, `HASH`, `PROJECT`, `SESSION_NOTE`, and `RESOLVED_NOTE`.

  **Important:** If `SESSION_ID` is `unknown`, use `unknown` for `source_session` and omit `source_session_note` entirely. Otherwise, use `RESOLVED_NOTE` (**not** `SESSION_NOTE`) for the `source_session_note` wikilink — `RESOLVED_NOTE` is the note name in the normal case (target absent yet, a forward reference, or present and agreeing) and `""` ONLY when the target note exists, parses, and its own `session_id` frontmatter CONTRADICTS `SESSION_ID` (#330). When `RESOLVED_NOTE` is empty, omit `source_session_note` entirely even though `SESSION_ID` is known (omit the line — do not write `source_session_note: ""`).
- `<project-name>` is the `PROJECT` value from `get_session_context()` (lowercased, hyphenated basename of cwd)
- `<Error Title>` is a short, descriptive title for the error (e.g. "BrokenPipeError when piping subprocess output to head")
- The `source_session_note` field creates an Obsidian backlink to the source session note

Ask the user:

> Preview above. Would you like to:
> - **save** as-is
> - **edit tags** — add or remove tags
> - **edit content** — tell me what to change
> - **cancel** — discard this note

Wait for the user's response. Apply any requested edits and show the updated preview. Repeat until the user says **save** or **cancel**.

If cancel, stop here.

### Step 7 — Generate filename

Construct the filename from these parts:

1. **Date:** `YYYY-MM-DD` (today)
2. **Slug:** The error title, lowercased, spaces replaced with hyphens, non-alphanumeric characters (except hyphens) removed, truncated to 50 characters
3. **Hash:** 4-character hex hash derived from the current timestamp: `date +%s | md5 | cut -c29-32` (macOS) or `date +%s | md5sum | cut -c1-4` (Linux). Do NOT use `tail -c 4` — it counts the trailing newline as a byte and returns only 3 visible characters.
4. **Suffix:** `-error`

Final filename: `YYYY-MM-DD-<slug>-<hash>-error.md`

Example: `2026-04-04-brokenpipeerror-subprocess-pipe-a3f2-error.md`

### Step 8 — Write the note

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
type: claude-error-fix
...
---

# <Error Title>
...
```

On success this prints `OK: <absolute path>` — that is the file at `$VAULT_PATH/$INSIGHTS_FOLDER/<filename>`. On failure it prints `ERROR: <reason>` to stderr and exits non-zero; surface that message to the user and stop here.

If the error is `note already exists`, the 4-hex filename hash collided with a note written in the same second. Regenerate the hash (Step 7's command), rebuild the filename, and retry the write **once**. If it fails again for any reason, surface the error and stop — do not loop.

### Step 9 — Confirm

Print:

> **Error fix logged!**
> - File: `$VAULT_PATH/$INSIGHTS_FOLDER/<filename>`
> - Tags: `claude/error-fix`, `claude/project/<name>`, `claude/topic/<topic1>`, ...
> - Open in Obsidian to view. This note will appear in the "Error Fixes" section of the Project Index dashboard.
