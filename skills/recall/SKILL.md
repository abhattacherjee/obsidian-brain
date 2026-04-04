---
name: recall
description: "Loads historical context from the Obsidian vault for the current project. Summarizes any unsummarized session notes, then presents a context brief with recent sessions, open items, and curated insights. Use when: (1) /recall command, (2) /recall <project-name>, (3) resuming work on a project and wanting prior context."
metadata:
  version: 1.0.0
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

### Step 3 — Summarize unsummarized notes (deferred summarization)

This is the critical upgrade step. Search for raw/unsummarized session notes matching this project.

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

1. **Read the full file** using the Read tool.
2. **Extract frontmatter** — preserve it exactly as-is (everything between the opening `---` and closing `---`).
3. **Extract raw user messages** — look for the content after the summary sections. This is the raw session data to summarize.
4. **Generate a structured summary** from the raw content. Produce these sections:
   - `## Summary` — 2-3 sentence overview of what was accomplished
   - `## Key Decisions` — Bulleted list of architectural or design decisions made
   - `## Changes Made` — Bulleted list of files changed or features implemented
   - `## Errors Encountered` — Bulleted list of errors hit and how they were resolved (or "_None._" if clean)
   - `## Open Questions / Next Steps` — Bulleted list of unfinished items or follow-up tasks
5. **Preserve the Session Metadata section** at the bottom if it exists (commits, files touched).
6. **Write the upgraded note** using the Write tool — same file path. The structure must be:
   - Original frontmatter (unchanged)
   - `# <title from original note>`
   - The five summary sections generated above
   - The Session Metadata section (if it existed)

**Important:** Do NOT modify frontmatter. Do NOT change the filename. Do NOT add or remove tags.

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
