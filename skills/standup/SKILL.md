---
name: standup
description: "Generates daily/weekly standup summaries across all projects from the Obsidian vault. Includes a Closed This Period section listing items checked off during the window, grouped by project. Use when: (1) /standup for today's summary, (2) /standup this week for weekly summary, (3) /standup <date range> for custom range."
metadata:
  version: 1.2.0
---

# Standup — Generate Standup Summaries from Obsidian Vault

Searches the Obsidian vault for session notes and insights within a date range, upgrades any unsummarized notes with AI summaries, groups findings by project, and generates a structured standup note.

**Tools needed:** Bash, Grep, Read

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Step 1 — Load config

Run:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys, os
import glob, json, os, re, sys
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json"))).values():
            _h = os.path.join((_m or {}).get("installLocation", ""), "hooks")
            if os.path.isfile(os.path.join(_h, "obsidian_utils.py")):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")) if re.fullmatch("[0-9]+([.][0-9]+)*", _d.split("/")[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split("/")[-2].split(".")], _p), default="hooks")
sys.path.insert(0, _ob_hooks())
from obsidian_utils import load_config
c = load_config()
if not c.get("vault_path"):
    print("ERROR: vault_path not configured", file=sys.stderr)
    sys.exit(1)
print("VAULT=" + c["vault_path"])
print("SESS=" + c.get("sessions_folder", "claude-sessions"))
print("INS=" + c.get("insights_folder", "claude-insights"))
'
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

Run two Grep searches in parallel to find notes whose `date:` frontmatter field falls within the range.

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

From `MATCHED_FILES`, isolate those in `$SESSIONS_FOLDER/`. Use Grep to check each for the unsummarized frontmatter status (NOT body text — body text matches cause false positives from logged tool usage):

```
pattern: "^status: auto-logged"
path: <each session file>
output_mode: files_with_matches
```

**Defense-in-depth:** For each file matching `^status: auto-logged`, also check if it already has a real `## Summary` section (without `"AI summary unavailable"`). If so, the note was summarized by a legacy code path that never flipped the status. Skip it and fix the status:

```bash
python3 -c '
import sys, os
import glob, json, os, re, sys
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json"))).values():
            _h = os.path.join((_m or {}).get("installLocation", ""), "hooks")
            if os.path.isfile(os.path.join(_h, "obsidian_utils.py")):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")) if re.fullmatch("[0-9]+([.][0-9]+)*", _d.split("/")[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split("/")[-2].split(".")], _p), default="hooks")
sys.path.insert(0, _ob_hooks())
from obsidian_utils import flip_note_status
flip_note_status(sys.argv[1], "auto-logged", "summarized")
' "$FILE_PATH"
```

Split into:
- `UNSUMMARIZED` — session files with `status: auto-logged` AND no real `## Summary`
- `SUMMARIZED` — all other matched files (sessions + insights + auto-fixed legacy notes)

### Step 6 — Deferred summarization for unsummarized notes

If `UNSUMMARIZED` is empty, skip to Step 7.

**Always parallelize unsummarized note upgrades.** For each unsummarized note, spawn a sub-agent immediately — even for 1-2 notes. Each sub-agent should call:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys, os
import glob, json, os, re, sys
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json"))).values():
            _h = os.path.join((_m or {}).get("installLocation", ""), "hooks")
            if os.path.isfile(os.path.join(_h, "obsidian_utils.py")):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")) if re.fullmatch("[0-9]+([.][0-9]+)*", _d.split("/")[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split("/")[-2].split(".")], _p), default="hooks")
sys.path.insert(0, _ob_hooks())
from obsidian_utils import upgrade_batch
results = upgrade_batch([sys.argv[1]], sys.argv[2], sys.argv[3], sys.argv[4])
print(results[0]["status"])
' "$NOTE_PATH" "$VAULT_PATH" "$SESSIONS_FOLDER" "$PROJECT"
```

The snippet prints a one-line status from `results[0]["status"]`. If that printed status starts with `Failed:`, note the failure and fall back to the manual procedure below for that note. Collect results from all sub-agents before proceeding.

For each file in `UNSUMMARIZED`, if `upgrade_batch()` is unavailable or the printed `results[0]["status"]` starts with `Failed:`, fall back to the manual upgrade procedure:

1. **Read the full file** using the Read tool.
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

This overwrites the note in place at its EXISTING path: pass `$SESSIONS_FOLDER` as the folder and the note's own basename (from `$NOTE_PATH`) as `<filename>` — never a new name. `write_vault_note()` (which the CLI delegates to) replaces an existing file atomically via the same temp-file + rename it uses to create one, so overwriting in place is a supported, safe case. **Two rules for the heredoc terminator, both load-bearing.** (1) It must stay **quoted** (`<<'OB_NOTE_EOF_<eof4>'`) — do not drop the quotes in a future edit. (2) It must be **unique per invocation**: substitute the same 4 random hex characters for `<eof4>` in BOTH the `<<'OB_NOTE_EOF_<eof4>'` opener and the terminator line, then confirm that **no line of the content you are about to emit is exactly that terminator** — if one is, pick different hex characters and re-check. **Never** replace this with a fixed delimiter. Quoting stops `$`/backtick expansion but does NOT stop early termination: a line equal to the terminator at column 0 ends the heredoc there, silently truncating the content AND handing everything after it to the shell as commands to execute. Notes written by this plugin routinely quote these very blocks, so a fixed terminator is a live hazard, not a theoretical one. **Self-check before you emit the block: if the terminator still contains `<` or `>`, you have not substituted it.** Stop and substitute it — the literal `<eof4>` form appears at column 0 inside these SKILL.md blocks themselves, so a note quoting one of them collides all over again, and nothing on the shell side can catch that. The `HOOKS=` line below sorts cached plugin versions **numerically** (a plain `max()` is lexicographic and picks `3.9.0` over `3.10.0`, resolving to a cache with no `note_writer.py`), and the `test -f` line turns a stale/incomplete cache into the documented `ERROR:` shape instead of a raw Python `can't open file` message. An unquoted delimiter lets the shell expand `$` variables and backtick commands embedded in the transcript content, silently corrupting the note.

**The fence, its content, and the terminator below must all sit at column 0 — never indent this block, even though it is referenced from inside a numbered list item.** The heredoc is `<<'...'`, not `<<-'...'`, so POSIX requires the terminator at the start of its line; an indented terminator never closes the heredoc, which silently swallows every following command as note content.

This is also the ONE call site that passes `--overwrite`: it upgrades an existing note in place, and the CLI refuses to replace an existing file without that flag (a filename collision anywhere else must fail loudly rather than destroy a note). Do not copy `--overwrite` to any other `note_writer.py write` block.

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
HOOKS=$(python3 -c "import glob,os,re; c=glob.glob(os.path.expanduser('~/.claude/plugins/cache/*/obsidian-brain/*/hooks')); print(max(c, key=lambda p: ([int(n) for n in re.findall('[0-9]+', p.split('/')[-2])], p), default='hooks'))")
test -f "$HOOKS/note_writer.py" || { echo "ERROR: note_writer.py not found under $HOOKS - the plugin cache is stale or incomplete. Run /plugin marketplace update (or /dev-test install for local dev), then retry." >&2; exit 1; }
python3 "$HOOKS/note_writer.py" write "$VAULT_PATH" "$SESSIONS_FOLDER" "<existing basename>" --overwrite <<'OB_NOTE_EOF_<eof4>'
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
OB_NOTE_EOF_<eof4>
```

On success this prints `OK: <absolute path>` and the file is now at mode `0o600` (no separate `chmod` needed — the old `chmod 644` step is gone; this note is user data written by the plugin, same as every other CLI-written note). On failure it prints `ERROR: <reason>` to stderr and exits non-zero; treat this the same as an `upgrade_batch()` failure for this file. Either way — success or failure — this step never modifies frontmatter (see the Important note below), so the note stays at `status: auto-logged` regardless of outcome, and a later `/standup` run will re-attempt the upgrade for any note whose status is still `auto-logged`.

**Important:** Do NOT modify frontmatter. Do NOT change the filename. Do NOT add or remove tags. The content piped into the CLI above must satisfy all three — frontmatter copied verbatim, filename the note's existing basename, tags untouched.

Move all upgraded files from `UNSUMMARIZED` into the working set alongside `SUMMARIZED`. Track the count of upgraded notes as `UPGRADED_COUNT`.

### Step 7 — Read and distill note content

> **Security:** If you need to write temp files during distillation, use `~/.claude/obsidian-brain/` (NOT `/tmp/`). This is a security requirement — predictable `/tmp` paths are vulnerable to symlink attacks.

Collect all matched files (now all summarized). Apply the /context-shield rule:

For each note, check its size using `wc -l`. Apply the context-shield rule **per note** based on size:

- **Notes under ~100 lines (~3000 tokens):** Read directly using the Read tool.
- **Notes over ~100 lines:** Spawn a `/context-shield` sub-agent to read in isolation and return a distilled summary.

When multiple notes need sub-agent reads, spawn them in parallel (one sub-agent per note).

From each note (whether read directly or via sub-agent), extract: project name (from frontmatter `project:` field), note type (`type:` field), date, title (first `# Heading`), summary (content of `## Summary` section), decisions (bullets from `## Key Decisions`), errors resolved (bullets from `## Errors Encountered`), open items (checkboxes from `## Open Questions / Next Steps`), and the filename (for wikilinks).

**Also extract closed items for the "Closed This Period" section:** For each session note in the date range, get the file modification time as a YYYY-MM-DD string (in the local timezone, matching how `START_DATE` and `END_DATE` were calculated):

```bash
# Get mtime as epoch, then format. Both forms work cross-platform.
MTIME_DATE=$(date -r "$file" +%Y-%m-%d 2>/dev/null || date -d @"$(stat -c %Y "$file")" +%Y-%m-%d)
```

The first form (`date -r FILE`) works on macOS. The Linux fallback uses `stat -c %Y` for the epoch then `date -d @EPOCH` to format. Both produce a YYYY-MM-DD string in the local timezone, which matches the format of `START_DATE` and `END_DATE`.

If `MTIME_DATE` is lexicographically within the range (`MTIME_DATE >= START_DATE && MTIME_DATE <= END_DATE`), Grep the file for `- \[x\]` lines under the `## Open Questions / Next Steps` section using the same line-range verification as for open items. Collect `(project, item_text)` tuples for each checked item.

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

Run the note-writer CLI, piping the full note (frontmatter + body) in on stdin. It creates `$INSIGHTS_FOLDER` if needed and writes the file atomically at mode `0o600` — no `mkdir`/`chmod` needed. **Two rules for the heredoc terminator, both load-bearing.** (1) It must stay **quoted** (`<<'OB_NOTE_EOF_<eof4>'`) — do not drop the quotes in a future edit. (2) It must be **unique per invocation**: substitute the same 4 random hex characters for `<eof4>` in BOTH the `<<'OB_NOTE_EOF_<eof4>'` opener and the terminator line, then confirm that **no line of the content you are about to emit is exactly that terminator** — if one is, pick different hex characters and re-check. **Never** replace this with a fixed delimiter. Quoting stops `$`/backtick expansion but does NOT stop early termination: a line equal to the terminator at column 0 ends the heredoc there, silently truncating the content AND handing everything after it to the shell as commands to execute. Notes written by this plugin routinely quote these very blocks, so a fixed terminator is a live hazard, not a theoretical one. **Self-check before you emit the block: if the terminator still contains `<` or `>`, you have not substituted it.** Stop and substitute it — the literal `<eof4>` form appears at column 0 inside these SKILL.md blocks themselves, so a note quoting one of them collides all over again, and nothing on the shell side can catch that. The `HOOKS=` line below sorts cached plugin versions **numerically** (a plain `max()` is lexicographic and picks `3.9.0` over `3.10.0`, resolving to a cache with no `note_writer.py`), and the `test -f` line turns a stale/incomplete cache into the documented `ERROR:` shape instead of a raw Python `can't open file` message. An unquoted delimiter lets the shell expand `$` variables and backtick commands embedded in the note body, silently corrupting it:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
HOOKS=$(python3 -c "import glob,os,re; c=glob.glob(os.path.expanduser('~/.claude/plugins/cache/*/obsidian-brain/*/hooks')); print(max(c, key=lambda p: ([int(n) for n in re.findall('[0-9]+', p.split('/')[-2])], p), default='hooks'))")
test -f "$HOOKS/note_writer.py" || { echo "ERROR: note_writer.py not found under $HOOKS - the plugin cache is stale or incomplete. Run /plugin marketplace update (or /dev-test install for local dev), then retry." >&2; exit 1; }
python3 "$HOOKS/note_writer.py" write "$VAULT_PATH" "$INSIGHTS_FOLDER" "YYYY-MM-DD-<slug>-<hash>.md" <<'OB_NOTE_EOF_<eof4>'
---
type: claude-standup
...
---

# Standup: ...
...
OB_NOTE_EOF_<eof4>
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

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
printf '%s' "$CHECKED_ITEMS_JSON" | python3 -c '
import sys, json, os
import glob, json, os, re, sys
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json"))).values():
            _h = os.path.join((_m or {}).get("installLocation", ""), "hooks")
            if os.path.isfile(os.path.join(_h, "obsidian_utils.py")):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")) if re.fullmatch("[0-9]+([.][0-9]+)*", _d.split("/")[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split("/")[-2].split(".")], _p), default="hooks")
sys.path.insert(0, _ob_hooks())
from open_item_dedup import batch_cascade_checkoff
items = json.load(sys.stdin)
summary = batch_cascade_checkoff(sys.argv[1], sys.argv[2], sys.argv[3], items)
print(summary)
' "$VAULT_PATH" "$SESSIONS_FOLDER" "$PROJECT"
```

Where `$CHECKED_ITEMS_JSON` is a JSON array of the confirmed item texts for that project (passed via stdin to avoid shell quoting issues with special characters in item text), and `$PROJECT` is the project name.

Run one call per project that has completed items. If multiple projects have items, run the calls in parallel.

If the command exits with a non-zero exit code, report the error to the user:

> Cascade checkoff failed for $PROJECT: [first line of stderr]. The standup note is unaffected.

Note: `batch_cascade_checkoff()` may emit warnings to stderr while still succeeding (e.g., a specific line changed). Only treat non-zero exit code as a failure.

**14c — Report cascade results.** After all cascade calls complete, report:

> Cascaded N checkoff(s) across M vault note(s) for project(s): list.

If `batch_cascade_checkoff` is unavailable (import error), warn the user:

> Could not cascade checkoffs: [error details]. The standup note is correct, but duplicate open items in other session notes were not updated. Run `/recall` to cascade manually.

### Steps 14b–19 — Deep mode (only if IS_DEEP)

Skip to Edge Cases if `IS_DEEP` is false.

> **STOP. Before ANY deep analysis work, create the task manifest.**
> The user CANNOT see your progress without tasks. Create all 5 tasks below using TaskCreate tool calls RIGHT NOW — in your NEXT tool-call message — before proceeding to Step 15.

**Step 14b — Create deep task manifest.**

Call TaskCreate 5 times (all in one message):

1. `TaskCreate: subject="Collect data and gather evidence", activeForm="Analyzing vault and git history"`
2. `TaskCreate: subject="Classify open items", activeForm="Classifying items with AI"`
3. `TaskCreate: subject="Present deep analysis", activeForm="Presenting recommendations"`
4. `TaskCreate: subject="Execute confirmed actions", activeForm="Executing actions"`
5. `TaskCreate: subject="Cascade checkoffs", activeForm="Cascading checkoffs"`

Then set task #1 to `in_progress` via TaskUpdate. **Do NOT proceed to Step 15 until all 5 tasks exist.**

**Step 15 — Collect data and gather evidence.** First check for a fresh cache (avoids re-running the full pipeline if `/standup deep` or `/emerge` was run recently with the same data):

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
printf '{"basenames": %s, "projects": %s}' "$NOTE_BASENAMES_JSON" "$PROJECTS_JSON" | python3 -c '
import glob, json, os, re, sys
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json"))).values():
            _h = os.path.join((_m or {}).get("installLocation", ""), "hooks")
            if os.path.isfile(os.path.join(_h, "obsidian_utils.py")):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")) if re.fullmatch("[0-9]+([.][0-9]+)*", _d.split("/")[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split("/")[-2].split(".")], _p), default="hooks")
sys.path.insert(0, _ob_hooks())
from deep_cli import run_pipeline; run_pipeline(sys.argv[1], sys.argv[2], sys.argv[3])
' "$VAULT_PATH" "$SESSIONS_FOLDER" "$INSIGHTS_FOLDER"
```

If the status starts with `CACHED:`, report "Using cached deep analysis (< 15 min old)" and skip to Step 16.

Where `$NOTE_BASENAMES_JSON` is a JSON array of note basenames from Step 7's NOTE_DATA, and `$PROJECTS_JSON` is the JSON string from Step 8's project list. Both are passed via stdin to avoid shell argument injection. Mark task #1 complete, task #2 in_progress.

**Step 16 — Classify open items.** Spawn a single Agent sub-agent that:
1. Reads `~/.claude/obsidian-brain/deep-pipeline.json`
2. For each open item, classifies it as `done`, `stale`, `active`, or `duplicate` based on evidence
3. Writes classifications to `~/.claude/obsidian-brain/deep-classifications.json`

Mark task #2 complete, task #3 in_progress.

**Step 17 — Present deep analysis.** Run:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
printf '%s' "$NOTE_BASENAMES_JSON" | python3 -c '
import glob, json, os, re, sys
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json"))).values():
            _h = os.path.join((_m or {}).get("installLocation", ""), "hooks")
            if os.path.isfile(os.path.join(_h, "obsidian_utils.py")):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")) if re.fullmatch("[0-9]+([.][0-9]+)*", _d.split("/")[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split("/")[-2].split(".")], _p), default="hooks")
sys.path.insert(0, _ob_hooks())
from deep_cli import run_present; run_present(sys.argv[1], sys.argv[2], sys.argv[3])
' "$VAULT_PATH" "$SESSIONS_FOLDER" "$INSIGHTS_FOLDER"
```

Display the output to the user. Wait for user response — they may confirm actions, edit classifications, or type `skip`. Mark task #3 complete, task #4 in_progress.

**Step 18 — Execute confirmed actions.** Parse user response. If user typed `skip`, skip this step.

**Important:** Do NOT use the Edit tool for batch vault edits — it requires Read first for each file, which is impractical for 20+ files. Instead, use the two Python helpers below.

**Checkoffs are text-anchored (#201).** Do NOT hand-build `old_text` from a classifier's `instances[].line` — a drifted line number can check off the WRONG still-active item, and a substring `old_text` can corrupt quoted prose. Instead, build a JSON array of confirmed checkoff items and let `run_build_checkoffs` re-resolve each target by TEXT against the file's real `- [ ] ` lines, emitting verified `[filepath, old_text, new_text]` triples. Then feed those `.edits` into `run_batch_edit` (which additionally line-anchors each flip).

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
# Stage 1 — resolve targets by text. $CHECKOFFS_JSON is a JSON array of
# {"file": "<basename>", "line": <hint>, "text": "<group representative / canonical text>"}.
RESOLVED=$(printf '%s' "$CHECKOFFS_JSON" | python3 -c '
import glob, json, os, re, sys
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json"))).values():
            _h = os.path.join((_m or {}).get("installLocation", ""), "hooks")
            if os.path.isfile(os.path.join(_h, "obsidian_utils.py")):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")) if re.fullmatch("[0-9]+([.][0-9]+)*", _d.split("/")[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split("/")[-2].split(".")], _p), default="hooks")
sys.path.insert(0, _ob_hooks())
from deep_cli import run_build_checkoffs; run_build_checkoffs()
')
# Stage 2 — apply only the verified, text-anchored edits.
printf '%s' "$RESOLVED" | python3 -c '
import glob, json, os, re, sys
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json"))).values():
            _h = os.path.join((_m or {}).get("installLocation", ""), "hooks")
            if os.path.isfile(os.path.join(_h, "obsidian_utils.py")):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")) if re.fullmatch("[0-9]+([.][0-9]+)*", _d.split("/")[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split("/")[-2].split(".")], _p), default="hooks")
sys.path.insert(0, _ob_hooks())
from deep_cli import run_batch_edit
sys.stdin = __import__("io").StringIO(json.dumps(json.load(sys.stdin)["edits"]))
run_batch_edit()
'
```

`run_build_checkoffs` reports `resolved N, skipped M` on stderr; **skipped items (drifted hint, no matching checkbox line, ambiguous text match, file-not-found) are NOT checked off** — surface them to the user rather than forcing an edit. Note: when two distinct still-active checkboxes both text-match a representative, the item is REFUSED with reason `ambiguous text match (N candidates)` — never guessed; the classifier `line` is a diagnostic hint only and is not used to disambiguate.

**Also surface Stage 2 drops.** `run_batch_edit` prints `Applied N/M edits`; whenever `N < M` it follows with a `Skipped K checkoff(s) with no matching line:` block listing each dropped `old_text`. A Stage-1-resolved triple can still be dropped here if the line changed between stages — **report any `Applied N/M` where N<M and the listed skipped checkoffs to the user** so a silently-dropped checkoff is never missed.

For confirmed link additions (NOT checkoffs), pass `[filepath, old_text, new_text]` triples directly into `run_batch_edit` via `$EDITS_JSON` — non-checkbox edits keep the substring-replace path:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
printf '%s' "$EDITS_JSON" | python3 -c '
import glob, json, os, re, sys
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json"))).values():
            _h = os.path.join((_m or {}).get("installLocation", ""), "hooks")
            if os.path.isfile(os.path.join(_h, "obsidian_utils.py")):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")) if re.fullmatch("[0-9]+([.][0-9]+)*", _d.split("/")[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split("/")[-2].split(".")], _p), default="hooks")
sys.path.insert(0, _ob_hooks())
from deep_cli import run_batch_edit; run_batch_edit()
'
```

Mark task #4 complete, task #5 in_progress.

**Step 19 — Cascade checkoffs + cleanup.** For each project with newly checked items, run `batch_cascade_checkoff()` (same as Step 14b) in parallel.

**Always clean up temp files** (even if user skipped actions — prevents stale cache from giving the same recommendations on next run):

```bash
rm -f ~/.claude/obsidian-brain/deep-pipeline.json ~/.claude/obsidian-brain/deep-classifications.json
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
