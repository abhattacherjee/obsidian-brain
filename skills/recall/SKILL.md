---
name: recall
description: "Loads historical context from the Obsidian vault for the current project. Summarizes any unsummarized session notes, then presents a context brief with recent sessions, open items, and curated insights. Also auto-detects open items completed in the most recent loaded session and offers to check them off. Use when: (1) /recall command, (2) /recall <project-name>, (3) resuming work on a project and wanting prior context."
metadata:
  version: 1.3.0
---

# Recall — Load Project Context from Obsidian Vault

Searches the Obsidian vault for session notes and insights matching the current project, upgrades any unsummarized notes with AI summaries, and presents a concise context brief.

**Tools needed:** Bash, Grep, Read, Write

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Step 1 — Load config

Run:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys
sys.path.insert(0, "hooks")
from obsidian_utils import load_config
c = load_config()
if not c.get("vault_path"):
    print("ERROR: vault_path not configured", file=sys.stderr)
    sys.exit(1)
print(f"VAULT={c[\"vault_path\"]} SESS={c.get(\"sessions_folder\",\"claude-sessions\")} INS={c.get(\"insights_folder\",\"claude-insights\")}")
'
```

Parse the output line to extract `VAULT_PATH`, `SESSIONS_FOLDER`, and `INSIGHTS_FOLDER`.

If the output is empty or errors, tell the user:

> Config not found. Run `/obsidian-setup` first to configure your Obsidian vault.

Stop here if config is missing.

**Create the task manifest** for the full `/recall` flow:

```
TaskCreate: subject="Find unsummarized notes", activeForm="Searching for unsummarized notes"
TaskCreate: subject="Summarize unsummarized notes", activeForm="Summarizing notes"
TaskCreate: subject="Build context brief", activeForm="Building context brief"
TaskCreate: subject="Present results and detect completed items", activeForm="Presenting results"
```

Track the returned task IDs — you will update them as each step completes. Immediately set task #1 to `in_progress` via TaskUpdate.

### Step 2 — Derive project name

If the user passed a project name argument (e.g. `/recall my-project`), use that.

Otherwise, derive from the current working directory:

```bash
basename "$(pwd)"
```

Store as `PROJECT`. Normalize: lowercase, hyphens for spaces.

### Step 3 — Summarize unsummarized notes (deferred summarization, truncation-aware)

> ⚠️ **THIS STEP IS MANDATORY. DO NOT SKIP IT.**
>
> If Grep finds any file matching both "AI summary unavailable" AND `project: $PROJECT`, you **must** produce an upgraded summary for every such file before proceeding to Step 4. "Skipping to save context" or "the other session covers it" is a bug, not an optimization — the user ran `/recall` specifically to get current-session context, and stale unsummarized notes are exactly what they asked you to fix.
>
> **Visibility requirement:** Before Step 4, emit a one-line status: `Step 3: processing N unsummarized note(s) for $PROJECT` (or `Step 3: no unsummarized notes for $PROJECT` if the intersection is empty). This makes the decision auditable in the tool trace.

Search for raw/unsummarized session notes matching this project.

Use Grep to find notes containing the "AI summary unavailable" marker in the sessions folder:

```
pattern: "AI summary unavailable"
path: $VAULT_PATH/$SESSIONS_FOLDER/
output_mode: files_with_matches
```

For each file found, use Grep to confirm it matches the current project:

```
pattern: "project: $PROJECT"
path: <each matched file>
output_mode: content
```

Count the files that match BOTH conditions (unsummarized AND belongs to this project). Store as `N`.

Update task #1 to completed. Update task #2 subject to `Summarize N unsummarized note(s)` and set to `in_progress`.

#### Path A: N=0 (no unsummarized notes)

Update task #2 subject to `No unsummarized notes found` and set to `completed`. Skip to Step 4.

#### Path B: N=1 (single note — Python pipeline)

Create a sub-task for the note:

```
TaskCreate: subject="Haiku pipeline: <basename>", activeForm="Summarizing <basename> via Haiku"
```

Run the upgrade pipeline in Python:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys
sys.path.insert(0, "hooks")
from obsidian_utils import upgrade_unsummarized_note
status = upgrade_unsummarized_note(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
print(status)
' "$NOTE_PATH" "$VAULT_PATH" "$SESSIONS_FOLDER" "$PROJECT"
```

If the status starts with "Failed:", use the sub-agent fallback:

1. Update the sub-task subject to `Sub-agent fallback: <basename>`.
2. Spawn a single sub-agent:

   ```
   Agent({
     description: "Summarize session note <basename>",
     prompt: "Read the session note at <NOTE_PATH>. Produce a structured summary with these exact markdown sections:\n\n## Summary\n1-3 sentence overview of what was accomplished.\n\n## Key Decisions\n- Bullet list of important technical decisions. Write \"None noted.\" if none.\n\n## Changes Made\n- Bullet list of files modified/created with brief description. Write \"None noted.\" if none.\n\n## Errors Encountered\n- Bullet list of errors and how resolved. Write \"None.\" if none.\n\n## Open Questions / Next Steps\n- [ ] Checkbox list of unresolved items. Write \"None.\" if none.\n\nReturn ONLY these markdown sections. No preamble, no commentary."
   })
   ```

3. If the sub-agent returns a valid summary, write it back via heredoc:

   ```bash
   cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
   python3 -c '
   import sys
   sys.path.insert(0, "hooks")
   from obsidian_utils import upgrade_note_with_summary
   summary = sys.stdin.read()
   status = upgrade_note_with_summary(sys.argv[1], summary, sys.argv[2], sys.argv[3], sys.argv[4])
   print(status)
   ' "$NOTE_PATH" "$VAULT_PATH" "$SESSIONS_FOLDER" "$PROJECT" <<'SUMMARY_EOF'
   <paste sub-agent summary here at column 0, no leading indentation>
   SUMMARY_EOF
   ```

   **Important:** Paste the sub-agent summary into the heredoc verbatim with NO leading indentation. Start `## Summary` at column 0, and keep the closing `SUMMARY_EOF` terminator at column 0 as well.

Mark the sub-task and task #2 as completed. Report the result.

#### Path C: N>=2 (batch — sub-agent-first, three waves)

##### Wave 1 — Prep (parallel Bash calls)

Create a prep sub-task for each note:

```
TaskCreate: subject="Prep: <basename>", activeForm="Prepping <basename>"
```

For each unsummarized note, run a parallel Bash call:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys
sys.path.insert(0, "hooks")
from obsidian_utils import prepare_summary_input
result = prepare_summary_input(sys.argv[1])
print(result)
' "$NOTE_PATH"
```

Launch all N Bash calls in a single message turn so they run in parallel.

When all return, update each prep sub-task to completed (include result in subject: e.g. `Prep: ...2322.md (RAW_OK)` or `Prep: ...4653.md (JSONL_PREPPED)`).

Parse each result:
- `RAW_OK:<note_path>` → sub-agent will read the raw note path
- `JSONL_PREPPED:<temp_path>:<note_path>` → sub-agent will read the temp file path; track `<note_path>` for Wave 3
- `NO_CONTENT:<note_path>` → skip this note, report to user

##### Wave 2 — Summarize (parallel sub-agents)

Create a summarize sub-task for each note (excluding NO_CONTENT):

```
TaskCreate: subject="Summarize: <basename>", activeForm="Summarizing <basename>"
```

Spawn all sub-agents in a **single message turn** so they run in parallel:

```
Agent({
  description: "Summarize session note <basename>",
  prompt: "Read the file at <READ_PATH>. Produce a structured summary with these exact markdown sections:\n\n## Summary\n1-3 sentence overview of what was accomplished.\n\n## Key Decisions\n- Bullet list of important technical decisions. Write \"None noted.\" if none.\n\n## Changes Made\n- Bullet list of files modified/created with brief description. Write \"None noted.\" if none.\n\n## Errors Encountered\n- Bullet list of errors and how resolved. Write \"None.\" if none.\n\n## Open Questions / Next Steps\n- [ ] Checkbox list of unresolved items. Write \"None.\" if none.\n\nReturn ONLY these markdown sections. No preamble, no commentary."
})
```

Where `<READ_PATH>` is:
- For `RAW_OK` notes: the raw note path
- For `JSONL_PREPPED` notes: the temp file path (e.g. `/tmp/ob-prep-{hash}.md`)

When all sub-agents return, check each result:
- If the sub-agent returned a valid summary (contains `## Summary`), update its summarize sub-task to completed.
- If the sub-agent returned an error, empty output, or no `## Summary` section, update its summarize sub-task subject to `Failed: <basename>` and mark completed. Exclude this note from Wave 3 — it stays unsummarized for the next `/recall`. Report the failure to the user.

##### Wave 3 — Write back (parallel Bash calls)

Create a write-back sub-task for each note that got a valid summary:

```
TaskCreate: subject="Write back: <basename>", activeForm="Writing <basename>"
```

For each sub-agent that returned a valid summary (contains `## Summary`), pipe it through `upgrade_note_with_summary()` via a heredoc:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys
sys.path.insert(0, "hooks")
from obsidian_utils import upgrade_note_with_summary
summary = sys.stdin.read()
status = upgrade_note_with_summary(sys.argv[1], summary, sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
print(status)
' "$NOTE_PATH" "$VAULT_PATH" "$SESSIONS_FOLDER" "$PROJECT" "sub-agent" <<'SUMMARY_EOF'
<paste sub-agent summary here at column 0>
SUMMARY_EOF
```

**Important:** The `$NOTE_PATH` here is always the **original vault note path** (not the temp file), even for JSONL_PREPPED notes.

Launch all write-back Bash calls in a single message turn.

When all return, parse each result:
- If the status does NOT start with "Failed:", update the write-back sub-task to completed.
- If the status starts with "Failed:", update the sub-task subject to `Failed: <basename>` and mark completed. Count it in the failure tally.

##### Cleanup

Clean up only the specific temp files created in Wave 1 (not a glob — avoids racing with concurrent `/recall` invocations):

```bash
rm -f /tmp/ob-prep-<session_id_1>.md /tmp/ob-prep-<session_id_2>.md ...
```

Mark task #2 as completed. Report results: how many upgraded, how many skipped (NO_CONTENT), how many failed.

If any sub-agents returned empty or invalid summaries, report those notes as still unsummarized — they will be retried on the next `/recall`.

For `NO_CONTENT` notes, inform the user: "Note `<basename>` has no session_id or conversation content. Manually edit it in Obsidian to add a summary, or delete it if it's empty."

### Step 4 — Build context brief (sub-agent)

Update task #3 to `in_progress`.

Dispatch a single sub-agent to search, read, rank, compose the context brief, and detect completed open items. The sub-agent does all the heavy file reading in its own context — the parent only receives the compact result.

```
Agent({
  description: "Build recall context brief for $PROJECT",
  prompt: "You are building a context brief for the obsidian-brain /recall skill.

VAULT_PATH: $VAULT_PATH
SESSIONS_FOLDER: $SESSIONS_FOLDER
INSIGHTS_FOLDER: $INSIGHTS_FOLDER
PROJECT: $PROJECT
UPGRADED_COUNT: <number of notes upgraded in Step 3, or 0>

## Your task

### 1. Search for project sessions and insights (parallel Grep)

Run these two searches in parallel:

Search A — Sessions:
  pattern: 'project: $PROJECT'
  path: $VAULT_PATH/$SESSIONS_FOLDER/
  output_mode: files_with_matches

Search B — Insights:
  pattern: 'project: $PROJECT'
  path: $VAULT_PATH/$INSIGHTS_FOLDER/
  output_mode: files_with_matches

### 2. Rank and select notes

From session files, sort by date (from frontmatter or filename). Select:
- Most recent session — read in full. Store its path.
- Second most recent — read first 50 lines (frontmatter + summary + open questions)
- Last 5 sessions — collect titles, dates, branches for the history table

From insight files, read ALL of them in full (they are short).

### 3. Compose context brief (~2000 tokens)

Build this structure:

## Project Context: $PROJECT

### Last Session ($DATE)
$SUMMARY_FROM_MOST_RECENT_SESSION

**Open Items / Next Steps:**
$OPEN_QUESTIONS_FROM_MOST_RECENT_SESSION

### Previous Session ($DATE)
$BRIEF_SUMMARY_FROM_SECOND_SESSION

### Curated Insights
$ALL_INSIGHTS_CONTENT (titles + key points, trim if exceeding ~500 tokens)

### Recent Session History
| Date | Title | Branch |
|------|-------|--------|
| ... last 5 sessions ... |

If the brief exceeds ~2000 tokens, trim older session summaries first, then truncate insight bodies (keep titles).

### 4. Detect completed open items

Run this Bash command:

cd \"\$(git rev-parse --show-toplevel 2>/dev/null || pwd)\"
python3 -c '
import sys, json
sys.path.insert(0, \"hooks\")
from obsidian_utils import match_items_against_evidence
from open_item_dedup import collect_open_items

vault_path, sessions_folder, project = sys.argv[1], sys.argv[2], sys.argv[3]
evidence_file = sys.argv[4]

try:
    with open(evidence_file, \"r\") as f:
        content = f.read()
    import re as _re
    evidence_parts = []
    for section in [\"Summary\", \"Key Decisions\", \"Changes Made\", \"Errors Encountered\"]:
        m = _re.search(rf\"## {section}\n(.*?)(?=\n## |\Z)\", content, _re.DOTALL)
        if m:
            evidence_parts.append(m.group(1))
    evidence = \"\n\".join(evidence_parts) if evidence_parts else content
except OSError:
    print(\"NO_CANDIDATES\")
    sys.exit(0)

items = collect_open_items(vault_path, sessions_folder, project)
if not items:
    print(\"NO_ITEMS\")
    sys.exit(0)

candidates = match_items_against_evidence(evidence, items)
if not candidates:
    print(\"NO_CANDIDATES\")
else:
    filtered = [c for c in candidates if c.get(\"confidence\", 0) >= 3]
    print(json.dumps(filtered) if filtered else \"NO_CANDIDATES\")
' \"$VAULT_PATH\" \"$SESSIONS_FOLDER\" \"$PROJECT\" \"<MOST_RECENT_SESSION_PATH>\"

Where <MOST_RECENT_SESSION_PATH> is the full path of the most recent session note you read in step 2.

### 5. Return format

Return EXACTLY this structured format with labeled sections. Do NOT add preamble or commentary outside these sections:

CONTEXT_BRIEF:
<the composed brief from step 3>

LOAD_MANIFEST:
full_session_title: <title of most recent session>
full_session_date: <date>
full_session_path: <full file path>
summary_session_title: <title of second session>
summary_session_date: <date>
insight_count: <number of insight files found>

MOST_RECENT_SESSION_PATH:
<full path of most recent session note>

OPEN_ITEM_CANDIDATES:
<output from step 4 — either NO_CANDIDATES, NO_ITEMS, or a JSON array>

## Edge cases
- No sessions found: set CONTEXT_BRIEF to 'No session history found for $PROJECT.'
- No insights found: omit Curated Insights section from brief, set insight_count to 0
- Very large vault (50+ sessions): only grep, never glob. Limit reads to 5 most recent sessions + all insights."
})
```

**Parse the sub-agent return.** Split the returned text on the section labels:

1. Extract `CONTEXT_BRIEF:` — everything between `CONTEXT_BRIEF:` and `LOAD_MANIFEST:`. This is the brief to display.
2. Extract `LOAD_MANIFEST:` — parse `full_session_title`, `full_session_date`, `full_session_path`, `summary_session_title`, `summary_session_date`, `insight_count`.
3. Extract `MOST_RECENT_SESSION_PATH:` — the full path for checkoff edits.
4. Extract `OPEN_ITEM_CANDIDATES:` — either `NO_CANDIDATES`, `NO_ITEMS`, or a JSON array.

**Fallback:** If the sub-agent returns empty, errors, or the return does not contain `CONTEXT_BRIEF:`, fall back to performing Steps 4-6 in-context (the original flow: grep sessions + insights, read files, compose brief). Log a warning: "Context builder sub-agent failed, falling back to in-context reads."

Update task #3 to `completed`.

### Step 8 — Show load manifest and offer options

After the context brief, explicitly list what was loaded into the conversation so the user knows exactly what context is available:

> **Loaded into this conversation:**
> - Full session: *"[most recent session title]"* ([date])
> - Summary only: *"[second session title]"* ([date])
> - [N] curated insight(s)
>
> Pick any session from the history table above to load it, or ready to start working?

The session history table from Step 6 serves as a menu — if the user picks a session by name or date, use the Read tool to load that specific file and present its full contents.

If the user says they're ready to work, the context is already loaded — proceed.

Update task #4 to `completed`.

## Edge Cases

- **No sessions found:** Tell the user no session history was found for this project. Suggest they start a session and it will be logged automatically.
- **No insights found:** Omit the "Curated Insights" section. Mention: "No curated insights yet for this project."
- **Very large vault (50+ sessions):** Only grep, never glob the entire folder. Limit reads to the most recent 5 sessions + all insights.
- **Config exists but vault path is invalid:** Warn the user and suggest running `/obsidian-setup` again.
