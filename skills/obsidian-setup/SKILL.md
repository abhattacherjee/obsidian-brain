---
name: obsidian-setup
description: "First-run configuration for the Obsidian Brain plugin. Sets up vault path, creates folders, copies dashboard templates, and writes config. Use when: (1) first time installing obsidian-brain, (2) changing vault path, (3) /obsidian-setup command."
metadata:
  version: 1.0.0
---

# Obsidian Brain Setup

Configure the obsidian-brain plugin for first use. This skill validates prerequisites, creates vault folders, installs dashboard templates, writes the config file, and verifies everything works.

**Tools needed:** Bash, Write, Read

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Step 1 — Ask for vault path

Ask the user:

> What is the absolute path to your Obsidian vault? (e.g. `/Users/you/obsidian/my-vault`)

Store the response as `VAULT_PATH`. Strip any trailing slash.

### Step 2 — Validate the vault path

Run:

```bash
test -d "$VAULT_PATH" && test -w "$VAULT_PATH" && echo "OK" || echo "FAIL"
```

If FAIL, tell the user the path does not exist or is not writable and ask them to correct it. Repeat until OK.

### Step 3 — Check claude CLI availability

Run:

```bash
which claude && echo "OK" || echo "FAIL"
```

If FAIL, warn the user:

> The `claude` CLI was not found on PATH. Hook-based AI summarization will not work until it is installed. You can continue setup, but session notes will be raw (unsummarized) until `claude` is available.

Continue regardless — this is a warning, not a blocker.

### Step 4 — Create vault folders

Run:

```bash
mkdir -p "$VAULT_PATH/claude-sessions" "$VAULT_PATH/claude-insights" "$VAULT_PATH/claude-dashboards"
```

### Step 5 — Install dashboard templates

Write these three files into the vault. Use the Write tool for each.

**File: `$VAULT_PATH/claude-dashboards/sessions-overview.md`**

```markdown
# Claude Sessions Overview

## Recent Sessions
\```dataview
TABLE date, project, git_branch, duration_minutes
FROM "claude-sessions"
WHERE type = "claude-session"
SORT date DESC
LIMIT 20
\```

## Recent Insights
\```dataview
TABLE date, project, tags
FROM "claude-insights"
WHERE type = "claude-insight"
SORT date DESC
LIMIT 10
\```

## Active Decisions
\```dataview
TABLE date, project
FROM "claude-insights"
WHERE type = "claude-decision" AND status = "active"
SORT date DESC
\```
```

**File: `$VAULT_PATH/claude-dashboards/project-index.md`**

```markdown
# Project Index

## Sessions by Project
\```dataview
TABLE length(rows) AS "Sessions", min(rows.date) AS "First", max(rows.date) AS "Last"
FROM "claude-sessions"
WHERE type = "claude-session"
GROUP BY project
SORT length(rows) DESC
\```

## Error Fixes (troubleshooting library)
\```dataview
TABLE date, project
FROM "claude-insights"
WHERE type = "claude-error-fix"
SORT date DESC
\```
```

**File: `$VAULT_PATH/claude-dashboards/weekly-review.md`**

```markdown
# This Week in Claude

\```dataview
TABLE date, project, type
FROM "claude-sessions" OR "claude-insights"
WHERE date >= date(today) - dur(7 days)
SORT date DESC
\```
```

**Important:** The backslash before the triple backticks above is an escape for this document only. When writing the actual files, use plain triple backticks (no backslash).

### Step 6 — Write config file

Write `~/.claude/obsidian-brain-config.json` with this exact structure:

```json
{
  "vault_path": "<VAULT_PATH value from Step 1>",
  "sessions_folder": "claude-sessions",
  "insights_folder": "claude-insights",
  "dashboards_folder": "claude-dashboards",
  "min_messages": 3,
  "min_duration_minutes": 2,
  "summary_model": "haiku",
  "auto_log_enabled": true,
  "snapshot_on_compact": true,
  "snapshot_on_clear": true
}
```

Replace `<VAULT_PATH value from Step 1>` with the actual vault path. Ensure the file is valid JSON.

First run `mkdir -p ~/.claude` to ensure the directory exists.

### Step 7 — Verify vault access

Run:

```bash
TESTFILE="$VAULT_PATH/claude-sessions/.obsidian-brain-test-$$"
echo "test" > "$TESTFILE" && test -f "$TESTFILE" && rm "$TESTFILE" && echo "OK" || echo "FAIL"
```

If FAIL, warn that vault writes are not working and ask the user to check permissions.

### Step 8 — Print success message

Print:

> **Obsidian Brain setup complete!**
>
> - Vault path: `<VAULT_PATH>`
> - Config written to: `~/.claude/obsidian-brain-config.json`
> - Folders created: `claude-sessions/`, `claude-insights/`, `claude-dashboards/`
> - Dashboards installed: `sessions-overview.md`, `project-index.md`, `weekly-review.md`
>
> **Next step — install the Dataview plugin in Obsidian:**
> 1. Open Obsidian Settings > Community Plugins > Browse
> 2. Search for "Dataview" and install it
> 3. Enable the plugin, then go to Dataview settings and turn on:
>    - **Enable JavaScript Queries**
>    - **Enable Inline Queries**
> 4. The dashboards in `claude-dashboards/` will start rendering automatically
>
> Hooks are already registered via `hooks.json` — session logging will begin on your next Claude Code session.
