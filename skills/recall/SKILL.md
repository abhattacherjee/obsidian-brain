---
name: recall
description: "Loads historical context from the Obsidian vault for the current project. Summarizes any unsummarized session notes, then presents a context brief with recent sessions, open items, and curated insights. Also auto-detects open items completed in the most recent loaded session and offers to check them off. Use when: (1) /recall command, (2) /recall <project-name>, (3) resuming work on a project and wanting prior context."
metadata:
  version: 1.2.0
---

# Recall — Load Project Context from Obsidian Vault

Searches the Obsidian vault for session notes and insights matching the current project, upgrades any unsummarized notes with AI summaries, and presents a concise context brief.

**Tools needed:** Bash, Grep, Read, Write

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Step 1 — Load config

Read `~/.claude/obsidian-brain-config.json`:

```bash
cat ~/.claude/obsidian-brain-config.json
```

If the file does not exist or is not valid JSON, tell the user:

> Config not found. Run `/obsidian-setup` first to configure your Obsidian vault.

Stop here if config is missing. Otherwise, extract `vault_path`, `sessions_folder` (default `claude-sessions`), and `insights_folder` (default `claude-insights`).

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
> **Large-note handling:** If the `Read` tool errors because the raw note exceeds the 10k token limit, use `offset` + `limit` to read it in chunks (e.g. `limit: 80` repeatedly) and concatenate mentally. Do NOT use that error as a reason to skip the upgrade.
>
> **Missing-JSONL fallback:** If `find_transcript_jsonl` returns null, you still produce a summary — from the raw note itself, with the fallback disclaimer specified in sub-step 4. Null JSONL is not a skip signal.
>
> **Visibility requirement:** Before Step 4, emit a one-line status: `Step 3: processing N unsummarized note(s) for $PROJECT` (or `Step 3: no unsummarized notes for $PROJECT` if the intersection is empty). This makes the decision auditable in the tool trace.

This is the critical upgrade step. Search for raw/unsummarized session notes matching this project, and prefer the original Claude Code transcript JSONL over the truncated raw note when the JSONL has more data.

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

For each file that matches BOTH conditions (unsummarized AND belongs to this project):

1. **Run the upgrade pipeline in Python** — a single Bash call that handles JSONL lookup, transcript parsing, source decision, AI summarization, dedup, and atomic write:

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

   Where `$NOTE_PATH` is the full path of the unsummarized note. The function returns a one-line status like:
   - `"Upgraded 2026-04-07-obsidian-brain-aeb5.md (source: JSONL transcript), deduped 2 item(s)"`
   - `"Failed: AI summarization returned empty for 2026-04-07-obsidian-brain-aeb5.md"`

   Report the status to the user. If it starts with "Failed:", note the failure but continue to the next unsummarized note (do not stop the entire `/recall` flow).

If no unsummarized notes are found for this project, skip to Step 4.

### Step 4 — Search for project sessions and insights (parallel)

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

- **Most recent session** — read in full (this is the primary context)
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

### Step 7 — Present to user

Display:

> **Here's what I found from your Obsidian vault for `$PROJECT`:**

Then output the context brief from Step 6.

If unsummarized notes were upgraded in Step 3, also mention:

> _Upgraded N session note(s) with AI summaries._

### Step 7.5 — Detect completed open items (project-scoped auto-detect)

After presenting the context brief, scan the loaded context for evidence that any open items have been completed.

1. **Collect open items for the current project.** **Re-use the project-scoped file list from Step 4** (the result of Search A — sessions matching `project: $PROJECT`). For each file in that list, run a per-file Grep:

```
pattern: ^- \[ \] 
path: <each session file from Step 4>
output_mode: content
-n: true
```

This avoids an O(vault size) Grep across the entire sessions folder — we already have the project-scoped file list and reuse it directly.

For each match, extract `(file_path, line_number, item_text)` tuples for items appearing under a `## Open Questions / Next Steps` section. To verify the section context, read 30 lines before each match and confirm the most recent `## ` heading is `## Open Questions / Next Steps`.

2. **Skip if zero open items.** If no items found, skip to Step 8 silently.

3. **Use loaded context as evidence pool.** The most recent session was already read in full during Step 5. Concatenate the text of its `## Summary`, `## Changes Made`, and `## Errors Encountered` sections — store as `EVIDENCE_TEXT`.

4. **Match items to evidence.** For each open item:
   - **Tokenize** the item text into words, lowercase, drop common stopwords (`the`, `a`, `an`, `to`, `for`, `in`, `on`, `of`, `and`, `or`, `but`, `is`, `are`, `was`, `were`, `be`).
   - **Substring match:** Count how many tokens (3+ characters) appear as substrings in `EVIDENCE_TEXT` (also lowercased). If count >= 3, mark as candidate.
   - **Distinctive token match:** If the item contains any of these distinctive tokens and they appear in evidence, mark as candidate even if substring count < 3:
     - File paths (contains `/` or `.py`/`.md`/`.json`/`.ts`/`.js`/`.tsx`/`.jsx`)
     - PR/issue references (matches `#\d+` or `PR \d+` or `issue \d+`)
     - Branch names (contains `feature/` or `release/` or `hotfix/`)
     - Version numbers (matches `v?\d+\.\d+\.\d+`)
   - **Completion phrase boost:** If a completion phrase (`merged`, `shipped`, `fixed`, `released`, `closed`, `removed`, `implemented`, `deleted`, `done`, `completed`) appears within 200 characters of any matched token in evidence, increase confidence.

5. **Skip if no candidates.** Fast path: if zero items match, skip to Step 8.

6. **Present candidates to user.** Print:

```
I noticed these open items may now be done:

1. [x] <item text>
     From: <basename of source file>
     Evidence: "<short snippet from EVIDENCE_TEXT showing the match>"

2. [x] <item text>
     ...

Confirm checkoff? (e.g. `1` or `1,2` or `all` or `none`)
```

7. **Wait for user response.** Parse the response:
   - `none` or empty → skip checkoff entirely, proceed to Step 8
   - `all` → check off all candidates
   - Comma-separated numbers (e.g. `1,3`) → check off only those

8. **For each confirmed checkoff, edit the source file.** Use Read to load the full source file. Find the exact line containing `- [ ] <item text>`. Replace it with `- [x] <item text>`. Use the Edit tool with `replace_all: false` and provide enough context (the full line plus the line before and after if available) to ensure uniqueness within the file. If the line is ambiguous (multiple matches), skip that item and warn:

```
⚠️  Could not check off item "<item text>" — line is not unique in <file>. Edit manually in Obsidian.
```

9. **Confirm checkoffs to user.** Print:

```
✅ Checked off N item(s) across <list of files>.
```

10. **Cascade check-offs to duplicate items in older notes.** Run a single Bash call that collects, matches, and edits files in Python:

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

    Before running, construct `$CHECKED_ITEMS_JSON` as a JSON array of the confirmed item texts from sub-step 7. Use a Bash heredoc or inline Python to build it:
    ```bash
    CHECKED_ITEMS_JSON=$(python3 -c "import json; print(json.dumps([\"Git-flow migration spec pending\", \"Land PR #14\"]))")
    ```
    Replace the example items with the actual confirmed item texts. Then run the cascade command above. Report the cascade summary to the user alongside the checkoff confirmation.

Then proceed to Step 8.

### Step 8 — Offer options

Ask:

> Want me to load this context? Or focus on a specific session/insight?

If the user says yes or wants to load it, the context brief is already in the conversation — it is loaded. Confirm:

> Context loaded. Ready to continue where you left off.

If the user asks about a specific session or insight, use the Read tool to load that specific file and present its full contents.

## Edge Cases

- **No sessions found:** Tell the user no session history was found for this project. Suggest they start a session and it will be logged automatically.
- **No insights found:** Omit the "Curated Insights" section. Mention: "No curated insights yet for this project."
- **Very large vault (50+ sessions):** Only grep, never glob the entire folder. Limit reads to the most recent 5 sessions + all insights.
- **Config exists but vault path is invalid:** Warn the user and suggest running `/obsidian-setup` again.
