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

### Step 4 — Search for project sessions and insights (parallel)

Update task #3 to `in_progress`.

Run these two searches in parallel using Grep:

**Search A — Sessions:**

```
pattern: "project: $PROJECT"
path: $VAULT_PATH/$SESSIONS_FOLDER/
output_mode: files_with_matches
```

**Search B — Insights:**

```
pattern: "project: $PROJECT"
path: $VAULT_PATH/$INSIGHTS_FOLDER/
output_mode: files_with_matches
```

Collect both result sets.

### Step 5 — Rank and select notes

From the session files found, sort by date (extract from frontmatter `date:` field or filename). Select:

- **Most recent session** — read in full (this is the primary context). Store its full path as `MOST_RECENT_SESSION_PATH` for use in Step 7.5.
- **Second most recent session** — read summary + open questions only
- **Last 5 sessions** — collect titles and dates for the session list

From the insights files found, include **all of them** — insights are curated and always relevant.

Read the selected files using the Read tool. For efficiency:
- Read the most recent session in full
- For older sessions, read only the first 50 lines (enough for frontmatter + summary + open questions)
- Read all insight files in full (they are typically short)

### Step 6 — Compose context brief

Build a context brief targeting approximately 2000 tokens. Structure it as follows:

```
## Project Context: $PROJECT

### Last Session ($DATE)
$SUMMARY_FROM_MOST_RECENT_SESSION

**Open Items / Next Steps:**
$OPEN_QUESTIONS_FROM_MOST_RECENT_SESSION

### Previous Session ($DATE)
$BRIEF_SUMMARY_FROM_SECOND_SESSION

### Curated Insights
$ALL_INSIGHTS_CONTENT

### Recent Session History
| Date | Title | Branch |
|------|-------|--------|
| ... last 5 sessions ... |
```

If the brief exceeds ~2000 tokens, trim older session summaries first, then truncate insight bodies (keep titles).

Update task #3 to `completed`.

### Step 7 — Present to user

Update task #4 to `in_progress`.

Display:

> **Here's what I found from your Obsidian vault for `$PROJECT`:**

Then output the context brief from Step 6.

If unsummarized notes were upgraded in Step 3, also mention:

> _Upgraded N session note(s) with AI summaries._

### Step 7.5 — Detect completed open items (project-scoped auto-detect)

After presenting the context brief, scan the loaded context for evidence that any open items have been completed.

1. **Match open items against evidence in Python.** Run a single Bash call that collects open items and matches them against the most recent session note:

   ```bash
   cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
   python3 -c '
   import sys, json
   sys.path.insert(0, "hooks")
   from obsidian_utils import match_items_against_evidence
   from open_item_dedup import collect_open_items

   vault_path, sessions_folder, project = sys.argv[1], sys.argv[2], sys.argv[3]
   evidence_file = sys.argv[4]

   try:
       with open(evidence_file, "r") as f:
           content = f.read()
       # Extract only evidence sections (Summary, Changes, Errors) — exclude
       # Open Questions to avoid self-matching open items as candidates
       import re as _re
       evidence_parts = []
       for section in ["Summary", "Key Decisions", "Changes Made", "Errors Encountered"]:
           m = _re.search(rf"## {section}\n(.*?)(?=\n## |\Z)", content, _re.DOTALL)
           if m:
               evidence_parts.append(m.group(1))
       evidence = "\n".join(evidence_parts) if evidence_parts else content
   except OSError as exc:
       print("NO_CANDIDATES")
       sys.exit(0)

   items = collect_open_items(vault_path, sessions_folder, project)
   if not items:
       print("NO_ITEMS")
       sys.exit(0)

   candidates = match_items_against_evidence(evidence, items)
   if not candidates:
       print("NO_CANDIDATES")
   else:
       print(json.dumps(candidates))
   ' "$VAULT_PATH" "$SESSIONS_FOLDER" "$PROJECT" "$MOST_RECENT_SESSION_PATH"
   ```

   Where `$MOST_RECENT_SESSION_PATH` is the path to the most recent session note (already read in Step 5).

2. **Skip if no candidates.** If the output is `NO_ITEMS` or `NO_CANDIDATES`, skip to Step 8 silently.

3. **Parse candidates.** The JSON array contains objects with `file`, `line`, `text`, `evidence`, `confidence`, `has_completion_phrase`. Only present items with `confidence >= 3` to the user.

4. **Present candidates to user.** Print:

```
I noticed these open items may now be done:

1. [x] <item text>
     From: <basename of source file>
     Evidence: "<short snippet from EVIDENCE_TEXT showing the match>"

2. [x] <item text>
     ...

Confirm checkoff? (e.g. `1` or `1,2` or `all` or `none`)
```

5. **Wait for user response.** Parse the response:
   - `none` or empty → skip checkoff entirely, proceed to Step 8
   - `all` → check off all candidates
   - Comma-separated numbers (e.g. `1,3`) → check off only those

6. **For each confirmed checkoff, edit the source file.** Use Read to load the full source file. Find the exact line containing `- [ ] <item text>`. Replace it with `- [x] <item text>`. Use the Edit tool with `replace_all: false` and provide enough context (the full line plus the line before and after if available) to ensure uniqueness within the file. If the line is ambiguous (multiple matches), skip that item and warn:

```
⚠️  Could not check off item "<item text>" — line is not unique in <file>. Edit manually in Obsidian.
```

7. **Confirm checkoffs to user.** Print:

```
✅ Checked off N item(s) across <list of files>.
```

8. **Cascade check-offs to duplicate items in older notes.** Run a single Bash call that collects, matches, and edits files in Python:

    ```bash
    cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
    python3 -c '
    import sys, json
    sys.path.insert(0, "hooks")
    from open_item_dedup import batch_cascade_checkoff
    items = json.loads(sys.argv[4])
    summary = batch_cascade_checkoff(sys.argv[1], sys.argv[2], sys.argv[3], items)
    print(summary)
    ' "$VAULT_PATH" "$SESSIONS_FOLDER" "$PROJECT" "$CHECKED_ITEMS_JSON"
    ```

    Before running, construct `$CHECKED_ITEMS_JSON` as a JSON array of the confirmed item texts from sub-step 5. Use a Bash heredoc or inline Python to build it:
    ```bash
    CHECKED_ITEMS_JSON=$(python3 -c "import json; print(json.dumps([\"Git-flow migration spec pending\", \"Land PR #14\"]))")
    ```
    Replace the example items with the actual confirmed item texts. Then run the cascade command above. Report the cascade summary to the user alongside the checkoff confirmation.

Then proceed to Step 8.

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
