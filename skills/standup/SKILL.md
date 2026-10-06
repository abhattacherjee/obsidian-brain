---
name: standup
description: "Generates daily/weekly standup summaries across all projects from the Obsidian vault. Includes a Closed This Period section listing items checked off during the window, grouped by project. Use when: (1) /standup for today's summary, (2) /standup this week for weekly summary, (3) /standup <date range> for custom range."
metadata:
  version: 1.2.0
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

# Standup — Generate Standup Summaries from Obsidian Vault

Searches the Obsidian vault for session notes and insights within a date range, upgrades any unsummarized notes with AI summaries, groups findings by project, and generates a structured standup note.

**Tools needed:** native shell, native content search, native file reading

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Step 1 — Load config

Run:

Request for `config` (substitute the values as data):

```json
{}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'config' < "$REQUEST_PATH"
```

Parse each output line as KEY=VALUE, splitting on the first `=`.

If the command exits non-zero or prints ERROR, tell the user:

> Config not found. Run `/obsidian-setup` first to configure your Obsidian vault.

Stop here if config is missing.

### Step 2 — Validate vault access

Run:

```bash
test -d "$VAULT_PATH/$SESSIONS_FOLDER" && test -d "$VAULT_PATH/$INSIGHTS_FOLDER" && echo "OK" || echo "FAIL"
```

If FAIL, tell the user:

> The vault folders do not exist or are not accessible. Run `/obsidian-setup` to fix this.

Stop here if FAIL.

### Step 3 — Parse date range from arguments

Before date parsing, check if the argument string contains the word `deep` (case-insensitive). If found, set `IS_DEEP = true` and remove `deep` from the argument string before passing to date parsing. Otherwise `IS_DEEP = false`.

Inspect the argument passed after `/standup`. Calculate `START_DATE` and `END_DATE` as `YYYY-MM-DD` strings using bash `date` commands.

**No argument (bare `/standup`):** today only.

```bash
START_DATE=$(date +%Y-%m-%d)
END_DATE=$START_DATE
```

**`yesterday`:**

```bash
# macOS
START_DATE=$(date -v-1d +%Y-%m-%d)
END_DATE=$START_DATE

# Linux fallback
START_DATE=$(date -d "yesterday" +%Y-%m-%d)
END_DATE=$START_DATE
```

**`this week`:** Monday of the current week through today.

```bash
# macOS
DOW=$(date +%u)   # 1=Mon … 7=Sun
DAYS_BACK=$((DOW - 1))
START_DATE=$(date -v-${DAYS_BACK}d +%Y-%m-%d)
END_DATE=$(date +%Y-%m-%d)

# Linux fallback
START_DATE=$(date -d "last Monday" +%Y-%m-%d 2>/dev/null || date -d "$(date +%Y-%m-%d) -$(date +%u)-1 days" +%Y-%m-%d)
END_DATE=$(date +%Y-%m-%d)
```

**`last week`:** Monday through Sunday of the previous week.

```bash
# macOS
DOW=$(date +%u)
START_DATE=$(date -v-${DOW}d -v-6d +%Y-%m-%d)
END_DATE=$(date -v-${DOW}d +%Y-%m-%d)

# Linux fallback
START_DATE=$(date -d "last week Monday" +%Y-%m-%d)
END_DATE=$(date -d "last week Sunday" +%Y-%m-%d)
```

**`YYYY-MM-DD to YYYY-MM-DD`:** use the two dates directly as `START_DATE` and `END_DATE`.

Store both dates. Also compute `IS_RANGE` = true if `START_DATE != END_DATE`, false otherwise. This controls the filename slug in Step 11.

**Validate the parsed dates:** Check that `START_DATE` and `END_DATE` are non-empty and match `YYYY-MM-DD` format. If either is empty or malformed, tell the user:

> Could not parse the date range from your input. Supported formats:
> - `/standup` (today)
> - `/standup yesterday`
> - `/standup this week`
> - `/standup last week`
> - `/standup 2026-03-25 to 2026-03-31`

Stop here if validation fails.

Also verify that `START_DATE <= END_DATE`. If not, tell the user the start date must be before or equal to the end date.

### Step 4 — Search for notes in date range (parallel)

Run two pattern searches in parallel to find notes whose `date:` frontmatter field falls within the range.

**Search A — Sessions:**

```
pattern: "^date: "
path: $VAULT_PATH/$SESSIONS_FOLDER/
output_mode: content
glob: "*.md"
```

**Search B — Insights:**

```
pattern: "^date: "
path: $VAULT_PATH/$INSIGHTS_FOLDER/
output_mode: content
glob: "*.md"
```

For each result, parse the `date:` value and keep only files where `START_DATE <= date <= END_DATE`. Collect the matching file paths into `MATCHED_FILES`.

If `MATCHED_FILES` is empty, tell the user:

> No session or insight notes found for the range **$START_DATE to $END_DATE**.

Stop here.

### Step 5 — Identify unsummarized session notes

From `MATCHED_FILES`, isolate those in `$SESSIONS_FOLDER/`; intersect that list with the returned pattern matches. Use the fixed `grep` operation to check each for the unsummarized frontmatter status (NOT body text — body text matches cause false positives from logged tool usage):

```
{"pattern": "^status: auto-logged", "frontmatter_only": true}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'grep' < "$REQUEST_PATH"
```

**Defense-in-depth:** For each file matching `^status: auto-logged`, also check if it already has a real `## Summary` section (without `"AI summary unavailable"`). If so, the note was summarized by a legacy code path that never flipped the status. Skip it and fix the status:

Request for `status` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "path": "<existing note>",
  "expected_revision": "<note-read SHA256>",
  "status": "summarized"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'status' < "$REQUEST_PATH"
```

Split into:
- `UNSUMMARIZED` — session files with `status: auto-logged` AND no real `## Summary`
- `SUMMARIZED` — all other matched files (sessions + insights + auto-fixed legacy notes)

### Step 6 — Deferred summarization for unsummarized notes

If `UNSUMMARIZED` is empty, skip to Step 7.

**Always parallelize unsummarized note upgrades.** For each unsummarized note, spawn a sub-agent immediately — even for 1-2 notes. Each sub-agent should call:

Request for `upgrade-batch` (substitute the values as data):

```json
{
  "paths": [
    "<eligible source notes>"
  ],
  "project": "<canonical project>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'upgrade-batch' < "$REQUEST_PATH"
```

The snippet prints a one-line status from `results[0]["status"]`. If that printed status starts with `Failed:`, note the failure and fall back to the manual procedure below for that note. Collect results from all sub-agents before proceeding.

For each file in `UNSUMMARIZED`, if `upgrade_batch()` is unavailable or the printed `results[0]["status"]` starts with `Failed:`, fall back to the manual upgrade procedure:

1. **Read the full file** using native file reading.
2. **Extract frontmatter** — preserve it exactly as-is (everything between the opening `---` and closing `---`).
3. **Extract the full conversation** — read all content after frontmatter, including:
   - `## Conversation (raw)` — interleaved user and assistant messages
   - `## Tool Usage` — commands run, files edited, searches performed
   - `## Changes Made` — files touched
   - `## Errors Encountered` — errors from tool results
4. **Generate a detailed, specific summary** with these sections:
   - `## Summary` — 3-5 sentence overview: what problem was solved, what approach was taken, what was the outcome. Name specific technologies, files, and patterns.
   - `## Key Decisions` — Bulleted list with rationale. If none, write "None noted."
   - `## Changes Made` — Bulleted list with file paths and descriptions. If none, write "None noted."
   - `## Errors Encountered` — Bulleted list with error messages, root causes, and fixes. If none, write "None."
   - `## Open Questions / Next Steps` — Checkbox list of specific, actionable items. If none, write "None."
5. **Preserve the Session Metadata section** at the bottom if it exists.
6. **Write the upgraded note** using the note-writer CLI — see Step 6.6 below for the exact structure and command. Do NOT reproduce the heredoc inline inside this list item; the fenced block below must stay outdented to column 0.

#### Step 6.6 — Write the upgraded note

Run the note-writer CLI, piping the full rewritten file in on stdin — structured as:

- Original frontmatter (unchanged)
- `# <title from original note>`
- The five summary sections
- Session Metadata section (if it existed)

Send the complete note or update as a JSON string to the fixed operation.
The launcher writes atomically at mode `0o600`. Existing notes require their
pre-analysis source revision. Content never becomes shell code.

**The fence, its content, and the terminator below must all sit at column 0 — never indent this block, even though it is referenced from inside a numbered list item.** The heredoc is `<<'...'`, not `<<-'...'`, so POSIX requires the terminator at the start of its line; an indented terminator never closes the heredoc, which silently swallows every following command as note content.

Read the existing note with `note-read` before analysis. Use its retained
revision for `summary-apply`; this preserves raw capture and manual sections.
A conflict leaves the note pending. New notes refuse filename collisions.

Request for `summary-apply` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "path": "<vault note>",
  "expected_revision": "<source-read SHA256>",
  "summary": "<complete generated summary>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'summary-apply' < "$REQUEST_PATH"
```

Preserve this note content as the JSON `content` string:

```markdown
---
<original frontmatter, unchanged>
---

# <title from original note>

## Summary
...

## Key Decisions
...

## Changes Made
...

## Errors Encountered
...

## Open Questions / Next Steps
...

## Session Metadata
...
```

On success this prints `OK: <absolute path>` and the file is now at mode `0o600` (no separate `chmod` needed — the old `chmod 644` step is gone; this note is user data written by the plugin, same as every other CLI-written note). On failure it prints `ERROR: <reason>` to stderr and exits non-zero; treat this the same as an `upgrade_batch()` failure for this file. Either way — success or failure — this step never modifies frontmatter (see the Important note below), so the note stays at `status: auto-logged` regardless of outcome, and a later `/standup` run will re-attempt the upgrade for any note whose status is still `auto-logged`.

**Important:** Do NOT modify frontmatter. Do NOT change the filename. Do NOT add or remove tags. The content piped into the CLI above must satisfy all three — frontmatter copied verbatim, filename the note's existing basename, tags untouched.

Move all upgraded files from `UNSUMMARIZED` into the working set alongside `SUMMARIZED`. Track the count of upgraded notes as `UPGRADED_COUNT`.

Before analyzing open items, call `cascade-collect` with the prepared
`operation_id` and selected project. Its immutable source revisions govern
all later `cascade` calls, whose payload includes `checked_texts` and the same
operation ID. Preserve missing/stale sources as pending.

### Step 7 — Read and distill note content

> **Security:** If you need to write temp files during distillation, use `<prepared operation_dir>/` (NOT `/tmp/`). This is a security requirement — predictable `/tmp` paths are vulnerable to symlink attacks.

Collect all matched files (now all summarized). Apply the /context:shield rule (was `context-shield`):

For each note, check its size using `wc -l`. Apply the context:shield rule **per note** based on size:

- **Notes under ~100 lines (~3000 tokens):** Read directly using native file reading.
- **Notes over ~100 lines:** Spawn a `/context:shield` sub-agent to read in isolation and return a distilled summary.

When multiple notes need sub-agent reads, spawn them in parallel (one sub-agent per note).

From each note (whether read directly or via sub-agent), extract: project name (from frontmatter `project:` field), note type (`type:` field), date, title (first `# Heading`), summary (content of `## Summary` section), decisions (bullets from `## Key Decisions`), errors resolved (bullets from `## Errors Encountered`), open items (checkboxes from `## Open Questions / Next Steps`), and the filename (for wikilinks).

**Also extract closed items for the "Closed This Period" section:** For each session note in the date range, get the file modification time as a YYYY-MM-DD string (in the local timezone, matching how `START_DATE` and `END_DATE` were calculated):

```bash
# Get mtime as epoch, then format. Both forms work cross-platform.
MTIME_DATE=$(date -r "$file" +%Y-%m-%d 2>/dev/null || date -d @"$(stat -c %Y "$file")" +%Y-%m-%d)
```

The first form (`date -r FILE`) works on macOS. The Linux fallback uses `stat -c %Y` for the epoch then `date -d @EPOCH` to format. Both produce a YYYY-MM-DD string in the local timezone, which matches the format of `START_DATE` and `END_DATE`.

If `MTIME_DATE` is lexicographically within the range (`MTIME_DATE >= START_DATE && MTIME_DATE <= END_DATE`), Search the file for `- \[x\]` lines under the `## Open Questions / Next Steps` section using the same line-range verification as for open items. Collect `(project, item_text)` tuples for each checked item.

Collect all distilled records as `NOTE_DATA`.

### Step 8 — Group by project

Group `NOTE_DATA` by `project` field. Sort projects alphabetically. Within each project, sort notes by `date` ascending (oldest first within the range). Separate sessions from insights within each project group.

If any notes have a missing or empty `project` field, group them under `(unknown project)`.

### Step 9 — Generate standup note body

Build the standup note body using the grouped data. For each project, emit a section:

```markdown
## $PROJECT_NAME

### Sessions
- [[filename-without-extension]] — $TITLE ($DATE)

### Insights
- [[filename-without-extension]] — $TITLE ($DATE)

### Decisions
- $DECISION_1
- $DECISION_2

### Errors Resolved
- $ERROR_1

### Open Items
- [ ] $OPEN_ITEM_1
- [ ] $OPEN_ITEM_2
```

Rules:
- Omit any subsection that has no content (e.g., if no decisions, skip `### Decisions` entirely).
- Omit the `### Insights` subsection if no insight notes exist for that project in the range.
- Wikilinks must use the bare filename without `.md` extension: `[[2026-04-05-my-note-a3f2]]`.
- Decisions and errors should be deduplicated across sessions in the same project.
- Open items should be listed as checkboxes (`- [ ]`).

Precede all project sections with a header block that includes a highlights summary and consolidated open items:

```markdown
# Standup: $START_DATE to $END_DATE

**Range:** $START_DATE → $END_DATE
**Projects covered:** $PROJECT_COUNT
**Sessions:** $SESSION_COUNT | **Insights:** $INSIGHT_COUNT

### Highlights
- **$PROJECT_A** — 1-2 sentences summarizing what was accomplished this period
- **$PROJECT_B** — 1-2 sentences summarizing what was accomplished this period

### Key Open Items
- [ ] $PROJECT_A: $MOST_IMPORTANT_OPEN_ITEM
- [ ] $PROJECT_B: $MOST_IMPORTANT_OPEN_ITEM
```

### Closed This Period

For each project that had at least one item closed within the standup window, render:

- **<project name>** (<N> closed)
  - <item text 1>
  - <item text 2>
  - ...

After the list, append this footnote on its own line in italics:

> _Detected via file modification time — may include items checked off earlier if a session note was edited during this window for unrelated reasons._

If zero items were closed across all projects, **omit this entire section** — do not render an empty header or the footnote.

Order projects alphabetically. Within each project, preserve the order items were extracted (file mtime descending — newest checkoffs first).

Rules for the header sections:
- **Highlights:** Include only projects with substantive work (skip vault-import-only or config-tweak sessions). Write 1-2 sentences per project summarizing the outcome, not the process. Order by impact/significance, not alphabetically.
- **Key Open Items:** Consolidate the most important open items across all projects (max ~5-7 items). Prefix each with the project name. These are the items that should drive next week's work. Skip low-priority or already-in-progress items.
- Both sections are written in the saved note AND presented in the conversation output.

If `IS_RANGE` is false (single day), use `# Standup: $DATE` and omit the "Range:" line. For single-day standups, the Highlights section may be omitted if only 1-2 sessions occurred.

### Step 10 — Build frontmatter

Construct the `source_notes` array from ALL matched filenames (sessions + insights), formatted as wikilinks:

```yaml
---
type: claude-standup
date: YYYY-MM-DD
date_range: "START_DATE to END_DATE"
projects:
  - project-a
  - project-b
source_notes:
  - "[[note-filename-1]]"
  - "[[note-filename-2]]"
tags:
  - claude/standup
  - claude/project/project-a
  - claude/project/project-b
---
```

Where:
- `date` is today's date (the date the standup was generated, not the range start)
- `date_range` is `"$START_DATE to $END_DATE"` (use the same value for single-day standups)
- `projects` lists all unique project names found, sorted alphabetically
- `source_notes` lists every contributing note as a wikilink (filename without `.md`)
- `tags` includes `claude/standup` plus a `claude/project/<name>` tag for each project covered by the standup
- If `IS_DEEP`, also append `claude/standup-deep` to the tags list

### Step 11 — Generate filename

Construct the filename:

1. **Date prefix:** `YYYY-MM-DD` (today's date, i.e., when the standup is generated)
2. **Slug:**
   - If `IS_RANGE` is false (single day): `standup-daily`
   - If `IS_RANGE` is true and the range spans exactly 7 days Mon-Sun: `standup-weekly`
   - Otherwise: `standup-range`
3. **Hash:** last 4 hex characters of the current timestamp hash:
   ```bash
   # macOS
   HASH=$(date +%s | md5 | cut -c29-32)
   # Linux fallback
   HASH=$(date +%s | md5sum | cut -c1-4)
   ```

Final filename: `YYYY-MM-DD-<slug>-<hash>.md`

Example: `2026-04-05-standup-daily-a3f2.md`

### Step 12 — Write the note

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
type: claude-standup
...
---

# Standup: ...
...
```

On success this prints `OK: <absolute path>` — that is the file at `$VAULT_PATH/$INSIGHTS_FOLDER/<filename>`. On failure it prints `ERROR: <reason>` to stderr and exits non-zero; surface that message to the user and stop here.

If the error is `note already exists`, the 4-hex filename hash collided with a note written in the same second. Regenerate the hash (Step 11's command), rebuild the filename, and retry the write **once**. If it fails again for any reason, surface the error and stop — do not loop.

### Step 13 — Present to user

Display the full standup in the conversation:

> **Standup for $START_DATE to $END_DATE:**

Then output the standup body (without frontmatter) as formatted markdown.

If `UPGRADED_COUNT > 0`, append:

> _Upgraded $UPGRADED_COUNT session note(s) with AI summaries._

Then confirm the saved file:

> **Saved:** `$VAULT_PATH/$INSIGHTS_FOLDER/<filename>`

### Step 14 — Cascade completed open items across vault

When open items are checked off in the standup note (either during generation or by the user afterwards), those same items may appear as unchecked `- [ ]` entries in other session notes across the vault. This step ensures all references are updated.

**14a — Collect confirmed completed items.** Gather all items that were marked `[x]` in the standup note's per-project `### Open Items` sections or the top-level `### Key Open Items` section. Include items from the `### Closed This Period` section as well. Extract just the item text (without the checkbox prefix or project prefix).

**14b — For each project that has completed items, cascade checkoffs across the vault.** Run:

Request for `cascade` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "argv": [
    "<arguments from this step>"
  ]
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'cascade' < "$REQUEST_PATH"
```

Where `$CHECKED_ITEMS_JSON` is a JSON array of the confirmed item texts for that project (passed via stdin to avoid shell quoting issues with special characters in item text), and `$PROJECT` is the project name.

Run one call per project that has completed items. If multiple projects have items, run the calls in parallel.

If the command exits with a non-zero exit code, report the error to the user:

> Cascade checkoff failed for $PROJECT: [first line of stderr]. The standup note is unaffected.

Note: `batch_cascade_checkoff()` may emit warnings to stderr while still succeeding (e.g., a specific line changed). Only treat non-zero exit code as a failure. Since #250, a target line that no longer text-matches the item it was recorded for (drifted onto a different active item, or has no recorded text at all) is skipped rather than flipped — that skip is named in `summary` itself (a trailing `Skipped N item(s) failing text verification: ...` line), not only on stderr, so it is visible even if stderr is not surfaced. Since #320, the checkbox-already-gone case is surfaced the same way (`Skipped N item(s) whose checkbox is gone: ...`), and a target that verified but whose write to disk failed makes the block exit non-zero with a `WRITE FAILED for N file(s); M verified flip(s) were NOT saved: ...` line in `summary` — that is the actual failure to report, not just "a specific line changed".

**14c — Report cascade results.** After all cascade calls complete, report:

> Cascaded N checkoff(s) across M vault note(s) for project(s): list.

If `summary` contains a "Skipped ..." line (failing text verification, unverifiable, or checkbox gone) for any project, include it in the report so the user knows which items were left unchecked and why. If it contains a "WRITE FAILED" line, treat that project's cascade as failed — the flips it names were computed but never saved — and say so explicitly rather than folding it into the success count.

If `batch_cascade_checkoff` is unavailable (import error), warn the user:

> Could not cascade checkoffs: [error details]. The standup note is correct, but duplicate open items in other session notes were not updated. Run `/recall` to cascade manually.

### Steps 14b–19 — Deep mode (only if IS_DEEP)

Skip to Edge Cases if `IS_DEEP` is false.

> **STOP. Before ANY deep analysis work, create the task manifest.**
> The user CANNOT see your progress without tasks. Create all 5 tasks below using native progress task calls RIGHT NOW — in your NEXT tool-call message — before proceeding to Step 15.

**Step 14b — Create deep task manifest.**

Create 5 native progress tasks (all in one message):

1. `Native progress task: subject="Collect data and gather evidence", activeForm="Analyzing vault and git history"`
2. `Native progress task: subject="Classify open items", activeForm="Classifying items with AI"`
3. `Native progress task: subject="Present deep analysis", activeForm="Presenting recommendations"`
4. `Native progress task: subject="Execute confirmed actions", activeForm="Executing actions"`
5. `Native progress task: subject="Cascade checkoffs", activeForm="Cascading checkoffs"`

Then set task #1 to `in_progress` via Native progress update. **Do NOT proceed to Step 15 until all 5 tasks exist.**

**Step 15 — Collect data and gather evidence.** First check for a fresh cache (avoids re-running the full pipeline if `/standup deep` or `/emerge` was run recently with the same data):

Request for `deep-pipeline` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'deep-pipeline' < "$REQUEST_PATH"
```

If the status starts with `CACHED:`, report "Using cached deep analysis (< 15 min old)" and skip to Step 16.

Where `$NOTE_BASENAMES_JSON` is a JSON array of note basenames from Step 7's NOTE_DATA, and `$PROJECTS_JSON` is the JSON string from Step 8's project list. Both are passed via stdin to avoid shell argument injection. Mark task #1 complete, task #2 in_progress.

**Step 16 — Classify open items.** Use one native analysis helper that:
1. Reads `<registered PIPELINE_PATH>`
2. For each open item, classifies it as `done`, `stale`, `active`, or `duplicate` based on evidence
3. Writes classifications to `<registered CLASSIFICATIONS_PATH>`

Mark task #2 complete, task #3 in_progress.

**Step 17 — Present deep analysis.** Run:

Request for `deep-present` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "pipeline_path": "<registered pipeline.json>",
  "classifications_path": "<registered classifications.json>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'deep-present' < "$REQUEST_PATH"
```

Display the output to the user. Wait for user response — they may confirm actions, edit classifications, or type `skip`. Mark task #3 complete, task #4 in_progress.

**Step 18 — Execute confirmed actions.** Parse user response. If user typed `skip`, skip this step.

**Important:** Publish batch vault edits through the shared transaction boundary — it requires Read first for each file, which is impractical for 20+ files. Instead, use the two Python helpers below.

**Checkoffs are text-anchored (#201).** Do NOT hand-build `old_text` from a classifier's `instances[].line` — a drifted line number can check off the WRONG still-active item, and a substring `old_text` can corrupt quoted prose. Instead, build a JSON array of confirmed checkoff items and let `run_build_checkoffs` re-resolve each target by TEXT against the file's real `- [ ] ` lines, emitting verified `[filepath, old_text, new_text]` triples. Then feed those `.edits` into `run_batch_edit` (which additionally line-anchors each flip).

Request for `deep-checkoffs` (substitute the values as data):

```json
{}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'deep-checkoffs' < "$REQUEST_PATH"
```

`run_build_checkoffs` reports `resolved N, skipped M` on stderr; **skipped items (drifted hint, no matching checkbox line, ambiguous text match, file-not-found) are NOT checked off** — surface them to the user rather than forcing an edit. Note: when two distinct still-active checkboxes both text-match a representative, the item is REFUSED with reason `ambiguous text match (N candidates)` — never guessed; the classifier `line` is a diagnostic hint only and is not used to disambiguate.

**Also surface Stage 2 drops.** `run_batch_edit` prints `Applied N/M edits`; whenever `N < M` it follows with a `Skipped K checkoff(s) with no matching line:` block listing each dropped `old_text`. A Stage-1-resolved triple can still be dropped here if the line changed between stages — **report any `Applied N/M` where N<M and the listed skipped checkoffs to the user** so a silently-dropped checkoff is never missed.

For confirmed link additions (NOT checkoffs), pass `[filepath, old_text, new_text]` triples directly into `run_batch_edit` via `$EDITS_JSON` — non-checkbox edits keep the substring-replace path:

Request for `deep-edit` (substitute the values as data):

```json
{
  "stdin": "<reviewed JSON edits with source_revision>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'deep-edit' < "$REQUEST_PATH"
```

Mark task #4 complete, task #5 in_progress.

**Step 19 — Cascade checkoffs + cleanup.** For each project with newly checked items, run `batch_cascade_checkoff()` (same as Step 14b) in parallel.

**Always clean up temp files** (even if user skipped actions — prevents stale cache from giving the same recommendations on next run):

```bash
echo "Private operation artifacts remain scoped to this operation."
```

This invalidates the 15-min cache so the next `/standup deep` run gets fresh data reflecting any changes made.

Mark task #5 complete.

## Edge Cases

- **No notes found for range:** Tell the user and suggest narrowing or widening the range, or checking that vault path is correct.
- **All notes are unsummarized:** Summarize all in Step 5 before proceeding — never skip summarization.
- **Single project:** Omit the per-project `## $PROJECT_NAME` heading if there is exactly one project; output the sections directly under the top-level header.
- **Config exists but vault path is invalid:** Warn the user and suggest running `/obsidian-setup` again.
- **macOS vs Linux date syntax:** Always try macOS syntax (`date -v`) first; fall back to Linux (`date -d`) if it fails.
- **Notes with missing project field:** Group under `(unknown project)` and note this to the user.

A native session summary is stale when `capture_revision` differs from
`summary_revision`, or no `summary_revision` exists. Label it stale explicitly.
Use unchanged raw capture facts as evidence; do not present its old summary as fresh.
Failed or cancelled AI leaves the operation pending and preserves the note.
