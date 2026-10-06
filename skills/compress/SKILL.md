---
name: compress
description: "Interactively saves curated insights from the current Claude Code session to the Obsidian vault. Use when: (1) /compress command to save session insights, (2) /compress <topic> to extract a specific topic, (3) user wants to capture decisions, patterns, solutions, or error fixes from the current session."
metadata:
  version: 1.1.0
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

Before drafting an edit or summary, call `note-read` and retain its exact `expected_revision`. Use `note-apply` with that revision for the reviewed edit. A conflict preserves the current note; show the pending result and do not count it as saved. Create new curated notes with `note-create`. A collision preserves the existing note. Use only the operations documented for this skill. Their writes bind the source revisions before analysis and preserve manual edits on conflict. Content is JSON data, never shell code. Read `references/host-claude.md` or `references/host-codex.md` when present. Codex has no native memory-file API; shared vault retrieval and wiki filing continue without borrowing another host's memory.

# Compress — Save Session Insights to Obsidian

Analyze the current conversation, extract valuable insights, and save them as structured notes in the Obsidian vault. Supports both interactive multi-insight selection and targeted single-topic extraction.

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

Parse the single JSON object. Read its named fields; do not split output on `=`.

If the output is empty or errors, tell the user:

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

### Step 3 — Determine mode

Check if the user provided a topic argument after `/compress`.

- **With argument** (e.g. `/compress rate limiting strategy`): Go to Step 3.5.
- **Without argument** (bare `/compress`): Go to Step 4B.

### Step 3.5 — Search for existing notes on this topic

Run a single Python call to search the vault index for existing notes matching the topic:

Request for `match-candidates`:

```json
{
  "query": "<topic>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'match-candidates' < "$REQUEST_PATH"
```

Parse the JSON output. If the script exits non-zero or the output cannot be parsed as JSON, treat it as `{"match": false}` and proceed silently (log a note: "Could not search vault index; creating new note.").

If `match` is `true`, store the `path` field as `MATCH_PATH` and the `title` field as `MATCH_TITLE`. Format `tags` by splitting on commas and joining with `, `. If `tags` is empty or null, display "no tags".

**If `match` is `false`:** No existing note found. Proceed silently to Step 4A (create new note).

**If `match` is `true`:** Present the match to the user. Render the block below, applying these rules:
- OMIT the "next-best:" clause (the " · next-best: <runner_up_rank>" tail, including the leading " · " separator and its surrounding spaces) when `runner_up_rank` is null — the line becomes exactly `Match rank: <rank> (<rank_note>)`.
- OMIT the "Shared terms" line entirely when `shared_terms` is empty.
- OMIT the "Snippet" line entirely when `snippet` is empty.

> Found an existing note on this topic:
> **"<title>"** (<date>)
> Tags: <tags as comma-separated list, or "no tags">
> Match rank: <rank> (<rank_note>) · next-best: <runner_up_rank>
> Shared terms with your query: `<term1>`, `<term2>`, …
> Snippet: "<snippet>"
>
> Would you like to **update** this note or **create new**?

Wait for the user's response:
- **"update"** → Go to Step 4A-update.
- **"create new"** → Go to Step 4A (create new note as before).

### Step 4A — Single-topic extraction

Analyze the current conversation for content related to the user's specified topic. Draft a note that includes:

- **Summary:** 2-4 sentence overview of the topic as discussed in this session
- **Details:** Key points, code snippets, configurations, or commands relevant to the topic
- **Context:** Why this came up, what problem it solved, any trade-offs discussed

Skip to Step 5.

### Step 4A-update — Append to existing note

This step is reached when the user chose "update" in Step 3.5. The matched note path is `$MATCH_PATH`.

#### 4A-update.1 — Read the existing note

Read the full contents of `$MATCH_PATH`. Note the existing frontmatter tags and whether a `last_updated` field is already present.

#### 4A-update.2 — Draft the update section

Analyze the current conversation for content related to the topic. Draft a dated update section:

~~~markdown
## Update (YYYY-MM-DD)

<New content about this topic from today's session. Include:
- New findings, corrections, or extensions to the original insight
- Code snippets or commands if relevant
- Context on why this update was triggered>
~~~

Where `YYYY-MM-DD` is today's date.

**Important:** Do NOT rewrite or duplicate existing content. The update section captures only what is NEW from this session.

#### 4A-update.3 — Show preview and ask for edits

Present ONLY the new update section (not the full existing note):

> **Update section to append to "< existing note title>":**
>
> (show the drafted `## Update (YYYY-MM-DD)` section)
>
> Preview above. Would you like to:
> - **save** — append this update
> - **edit content** — tell me what to change
> - **cancel** — discard this update

Wait for the user's response. Apply edits and re-show if requested. Repeat until the user says **save** or **cancel**.

If **cancel**, stop here.

#### 4A-update.4 — Append the update section and update frontmatter

Run the note-writer CLI's `append-update` command, piping the drafted `## Update (YYYY-MM-DD)` section (from 4A-update.2) in on stdin. **This single call replaces all three of the old Edit-tool steps** — it finds the correct insertion point (scanning **top-down** for `_(Summary source: ...)_`, `## Tool Usage`, `## Conversation (raw)`, `## Session Metadata`, `## Files Touched` — ignoring any inside a fenced code block, and **stopping at the first `## Update (` heading**, since everything past that is a previously appended update rather than the note's audit trail — and inserting immediately before the first marker it finds, or at end-of-file), bumps `last_updated`, and merges new tags — all in one atomic write. Keep this update inside the trusted conditional publication boundary.

Generate 1-3 new topic tags from the update content (same logic as Step 5) and pass them via `--add-tags`. `--last-updated` is opt-in — it must be passed explicitly with today's date, or the note's `last_updated` field will NOT be bumped (that used to happen automatically; it no longer does without this flag).

**Both flags are validated — a present flag with a broken value is an error, not a silent skip.** `--last-updated` must be a real `YYYY-MM-DD` value (an empty `"$TODAY"` from an unset variable is rejected). Each `--add-tags` item must match `[A-Za-z0-9][A-Za-z0-9/_.-]*`: start with a letter or digit, then letters, digits, `/`, `_`, `.` or `-` only — **no empty items** (so no trailing comma, and no `--add-tags ""`) and **no leftover `<placeholder>` text** (the `claude/topic/<new-tag-1>` below is a template; substitute a real tag or the `<`/`>` will be rejected). If there are no new tags, **omit the `--add-tags` line entirely** — that is the supported no-op. The drafted update section must also be non-empty; piping an empty heredoc body is rejected and the note is left untouched.

If the note's frontmatter has no recognizable `tags:` block, the command now **fails** rather than silently dropping the tags — surface the error and add them manually, or omit `--add-tags` and re-run.

**Send update content as JSON data.** The content is never a shell heredoc.


Request for `note-append` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "path": "<MATCH_PATH>",
  "expected_revision": "<pre-analysis note-read SHA256>",
  "update_text": "<complete Update section>",
  "last_updated": "<YYYY-MM-DD>",
  "add_tags_csv": "claude/topic/<real-tag>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'note-append' < "$REQUEST_PATH"
```

On success this prints `OK: <resolved path>`. On failure it prints `ERROR: <reason>` to stderr and exits non-zero — the file is left byte-identical (no partial write happens). Surface the error to the user: "Failed to append update section — `<error message>`. Please edit manually at `$MATCH_PATH`." and stop here.

The write is atomic and a non-zero exit means nothing was written, so a Read purely to confirm the bytes landed adds nothing — do NOT add one for that purpose. The CLI also refuses to write if the note changed on disk after it was read (`ERROR: note changed on disk...`), which is what a second session running `/compress` on the same note looks like; on that error, re-run the update so it applies on top of the other change rather than discarding it.

**Do NOT change:** `date`, `source_session`, `source_session_note`, or `type` fields. These record the original creation context — the CLI never touches them.

**Note on repeated runs:** running `/compress` update again later (even later the same day) appends another `## Update (YYYY-MM-DD)` section rather than merging into an existing one for that date. This is expected and lossless — do not describe or imply that same-date updates merge.

#### 4A-update.5 — Re-sync vault index

Run:

Request for `sync`:

```json
{}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'sync' < "$REQUEST_PATH"
```

#### 4A-update.6 — Confirm

Print:

> **Note updated!**
> - File: `$MATCH_PATH`
> - Added section: "Update (YYYY-MM-DD)"
> - New tags: `<list of newly added tags>` (or "none")

Skip to Step 10 (offer follow-up). Do NOT proceed through Steps 5-9 (those are the create-new flow).

### Step 4B — Multi-insight suggestion

**First, check for skill-extraction output** (`skill-kit:extract`, was `claudeception`; old transcripts still carry the old names) using layered detection:

**Layer 1 — High-confidence structured markers** (check first):

Scan the current conversation for these patterns. If found, extract the skill/knowledge name and a one-line summary:

- The `MANDATORY SKILL EVALUATION REQUIRED` banner or the `Skill(skill-kit:extract)` / `skill-kit:extract` reminder (from the activator hook, still named `claudeception-activator.sh`; older transcripts show `Skill(claudeception)`)
- `Result: PASS` or `Result: FAIL` (from the `skill-kit:extract` skill validator; older transcripts: the claudeception validator)
- Native skill source paths under the selected installation or project skill directory

If any Layer 1 markers are found, create a candidate for each and label it `[from skill-kit:extract]` (`[from claudeception]` for old transcripts).

**Layer 2 — Broad phrase scanning** (fallback, only if Layer 1 found nothing):

Scan the conversation for these phrases:
- "created skill", "new skill at", "skill file written"
- "extracted knowledge", "pattern identified", "reusable insight"
- Output from a `skill-kit:extract` invocation (or `/claudeception` in old transcripts)

If any Layer 2 phrases are found, create a candidate for each and label it `[possibly from skill-kit:extract]`.

**Then, perform standard insight discovery:**

Analyze the full conversation and identify 3-5 additional candidate insights (beyond any skill-kit:extract candidates). Each candidate should be one of these types:

- **Decision** — an architectural or design choice made during the session
- **Pattern** — a reusable approach, technique, or workflow discovered
- **Solution** — a specific problem solved with a clear fix
- **Error Fix** — a bug or error diagnosed and resolved
- **Discovery** — a new finding about a tool, API, library, or system behavior

**Present all candidates** as a numbered list, with skill-kit:extract candidates first:

> **Insights found in this session:**
>
> 1. [from skill-kit:extract] [Discovery] Rate limiter pattern — extracted as reusable skill
> 2. [possibly from skill-kit:extract] [Pattern] Retry with exponential backoff — identified across 3 sessions
> 3. [Decision] Chose Redis for session store — trade-off analysis
> 4. [Solution] Fixed CORS issue with Safari — root cause in preflight handling
>
> Which would you like to save? (e.g. `1,3` or `all`)

If no skill-kit:extract output was detected, present only the standard candidates (same as before — no labels).

When the user says `all`, all candidates (including skill-kit:extract ones) are saved. When the user picks specific numbers, only those are saved — standard selection behavior.

Wait for the user to pick. For each selected insight, draft the note content and continue to Step 5. Process selected insights one at a time.

### Step 5 — Auto-generate topic tags

Based on the note content, generate 1-3 topic tags. Tags should be lowercase, hyphenated, and specific. Examples:

- `claude/topic/rate-limiting`
- `claude/topic/react-hooks`
- `claude/topic/git-workflow`
- `claude/topic/api-design`

### Step 6 — Show preview and ask for edits

Present the full note to the user including frontmatter:

```
---
type: claude-insight
date: YYYY-MM-DD
created_at: <ISO-8601-UTC>
source_session: <current-session-id>
source_session_note: "[[<session-note-filename>]]"
project: <project-name>
tags:
  - claude/insight
  - claude/project/<project-name>
  - claude/topic/<auto-generated-topic-1>
  - claude/topic/<auto-generated-topic-2>
---

# <Title>

<Note body>
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

  Parse the output to get `SESSION_ID`, `HASH`, `PROJECT`, `SESSION_NOTE`, and `RESOLVED_NOTE`. Use these for the frontmatter fields.

  **Important:** If `SESSION_ID` is `unknown`, use `unknown` for `source_session` and omit `source_session_note` entirely. Otherwise, use `RESOLVED_NOTE` (**not** `SESSION_NOTE`) for the `source_session_note` wikilink — `RESOLVED_NOTE` is the note name in the normal case (target absent yet, a forward reference, or present and agreeing) and `""` ONLY when the target note exists, parses, and its own `session_id` frontmatter CONTRADICTS `SESSION_ID` (#330). When `RESOLVED_NOTE` is empty, omit `source_session_note` entirely even though `SESSION_ID` is known (omit the line — do not write `source_session_note: ""`).
- `<project-name>` is the `PROJECT` value from `get_session_context()` (lowercased, hyphenated basename of cwd)
- The `source_session_note` field creates an Obsidian backlink from the insight to its source session, enabling bidirectional navigation in the graph view

Ask the user:

> Preview above. Would you like to:
> - **save** as-is
> - **edit tags** — add or remove tags
> - **edit content** — tell me what to change
> - **cancel** — discard this note

Wait for the user's response. Apply any requested edits and show the updated preview. Repeat until the user says **save** or **cancel**.

If cancel, stop here (or move to the next selected insight if processing multiple from Step 4B).

### Step 7 — Generate filename

Construct the filename from these parts:

1. **Date:** `YYYY-MM-DD` (today)
2. **Slug:** The note title, lowercased, spaces replaced with hyphens, non-alphanumeric characters (except hyphens) removed, truncated to 50 characters
3. **Hash:** 4-character hex hash derived from the current timestamp: `date +%s | md5 | cut -c29-32` (macOS) or `date +%s | md5sum | cut -c1-4` (Linux). Do NOT use `tail -c 4` — it counts the trailing newline as a byte and returns only 3 visible characters.

Final filename: `YYYY-MM-DD-<slug>-<hash>.md`

Example: `2026-04-04-rate-limiting-with-redis-a3f2.md`

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
type: claude-insight
...
---

# <Title>
...
```

On success this prints `OK: <absolute path>` — that is the file at `$VAULT_PATH/$INSIGHTS_FOLDER/<filename>`. On failure it prints `ERROR: <reason>` to stderr and exits non-zero; surface that message to the user and stop here.

If the error is `note already exists`, the 4-hex filename hash collided with a note written in the same second. Regenerate the hash (Step 7's command), rebuild the filename, and retry the write **once**. If it fails again for any reason, surface the error and stop — do not loop.

### Step 9 — Confirm

Print:

> **Insight saved!**
> - File: `$VAULT_PATH/$INSIGHTS_FOLDER/<filename>`
> - Tags: `claude/insight`, `claude/project/<name>`, `claude/topic/<topic1>`, ...
> - Open in Obsidian to view and link to other notes.

If processing multiple insights from Step 4B, repeat Steps 5-9 for each remaining selected insight.

### Step 10 — Offer follow-up

After all insights are saved, ask:

> Anything else to capture from this session? You can run `/compress` again or `/compress <topic>` to extract a specific topic (will offer to update if an existing note matches).

## Fixed request shapes

Pass these objects through the installed launcher for the named operation. Keep
one operation ID across source reads, analysis and reviewed publication.

Request for `note-read`:

```json
{
  "operation_id": "<prepared id>",
  "path": "<vault-relative note.md>"
}
```

Request for `note-apply`:

```json
{
  "operation_id": "<same prepared id>",
  "path": "<same vault-relative note.md>",
  "expected_revision": "<note-read SHA256>",
  "content": "<complete reviewed note>"
}
```
