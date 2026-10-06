---
name: obsidian-setup
description: "First-run configuration and upgrade for the Obsidian Brain plugin. Sets up vault path, creates folders, copies dashboard templates, configures hookify nudges, and writes config. Idempotent — safe to re-run to pick up new features without overwriting existing config or dashboards. Use when: (1) first time installing obsidian-brain, (2) changing vault path, (3) /obsidian-setup command, (4) upgrading to a new version."
metadata:
  version: 1.4.0
---

## Native runtime and installed resources

For fresh setup, ask for the vault path FIRST and validate that it exists. Set
`OB_VAULT` to that explicit path before any command below. For upgrades, select
the already configured vault. No temporary or fake vault is used for bootstrap.

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
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --vault "$OB_VAULT" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" context < /dev/null
```

Use the returned `config_path`, `vault_path`, `index_path`, and `state_path`.
Create the operation with this fixed literal request:

```bash
printf '{}' | python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --vault "$OB_VAULT" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'prepare'
```

Call `prepare` to create a private operation under native state. Retain its
`operation_id` and `operation_dir`. Register approved helper output names with `artifact-store`;
inputs are read through the immutable artifact manifest. Do not discover resources from the current directory or another plugin
cache. Each shell invocation supplies the same explicit values; a previous
shell's variables are not assumed to persist.

Each data operation uses the installed launcher with a JSON request on stdin:

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --vault "$OB_VAULT" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation '<fixed operation>' < "$REQUEST_PATH"
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

# Obsidian Brain Setup

Configure the obsidian-brain plugin for first use. This skill validates prerequisites, creates vault folders, installs dashboard templates, writes the config file, and verifies everything works.

**Tools needed:** native shell, trusted publication, native file reading

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Step 1 — Check for existing installation

**Argument handling:** If the user ran `/obsidian-setup --deps`, skip the existing-config detection and jump directly to Step 8.7 (Performance Dependencies). `--deps` re-triggers the dependency prompt regardless of the `optional_deps_prompted` flag in config. After Step 8.7 completes, skip to Step 10 (success message).

Check for existing config:

Request for `config` (substitute the values as data):

```json
{}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --vault "$OB_VAULT" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'config' < "$REQUEST_PATH"
```

**If the config JSON names an existing vault**, read fields from the returned JSON. Extract `VAULT_PATH` and present:

> **Existing obsidian-brain installation detected.**
> - Vault path: `<vault_path from config>`
>
> Would you like to:
> - **upgrade** — add new features (dashboards, nudges) without touching existing config or dashboards
> - **reconfigure** — start fresh (will overwrite config and dashboards)
> - **cancel** — exit setup

If **upgrade**: store `MODE=upgrade`. Store `VAULT_PATH` from the existing config's `vault_path` field. Also extract `sessions_folder` (default `claude-sessions`), `insights_folder` (default `claude-insights`), `dashboards_folder` (default `claude-dashboards`), and `check_items_folder` (default `claude-check-items`). Skip to Step 5 (Create vault folders — `mkdir -p` is already safe). In upgrade mode:
- Step 5 (Create vault folders): runs normally (`mkdir -p` is idempotent)
- Step 6 (Install dashboards): only write dashboard files that do NOT already exist (`test -f` before each write)
- Step 7 (Write config): SKIP entirely — preserve existing config
- Step 8 (Verify vault access): runs normally
- Step 8.5 (Build vault index): runs normally (idempotent ensure_index call)
- Step 8.7 (Performance Dependencies): runs normally — has its own idempotency check via `optional_deps_prompted`/`optional_deps_declined` config fields; users with declined deps are NOT re-prompted unless they explicitly run `/obsidian-setup --deps`
- Step 9 (Configure skill-kit:extract nudge): runs normally (has its own idempotency check)
- Step 10 (Print success message): show upgrade-specific message

If **reconfigure**: store `MODE=reconfigure`. Proceed to Step 2 (Ask for vault path) as normal — full setup flow.

If **cancel**: stop here.

**If no native vault was configured before this invocation**: store `MODE=fresh`. Proceed to Step 2 (Ask for vault path) — first-time setup.

### Step 1.5 — Native permissions

Follow the invoking host reference for permission and hook trust. Select the
vault before bootstrapping a fresh context. Report a permission refusal and
preserve pending work; use the host's native permission controls.

### Step 2 — Ask for vault path

Ask the user:

> What is the absolute path to your Obsidian vault? (e.g. `/Users/you/obsidian/my-vault`)

Store the response as `VAULT_PATH`. Strip any trailing slash.

### Step 3 — Validate the vault path

Run:

```bash
test -d "$VAULT_PATH" && test -w "$VAULT_PATH" && echo "OK" || echo "FAIL"
```

If FAIL, tell the user the path does not exist or is not writable and ask them to correct it. Repeat until OK.

### Step 4 — Check claude CLI availability

Run:

```bash
which claude && echo "OK" || echo "FAIL"
```

If FAIL, warn the user:

> The `claude` CLI was not found on PATH. Hook-based AI summarization will not work until it is installed. You can continue setup, but session notes will be raw (unsummarized) until `claude` is available.

Continue regardless — this is a warning, not a blocker.

### Step 5 — Create vault folders

First, test that the vault path is writable:

```bash
echo "test" > "$VAULT_PATH/.obsidian-brain-canary" 2>&1 && rm -f "$VAULT_PATH/.obsidian-brain-canary" && echo "OK" || echo "FAIL"
```

If **FAIL**, tell the user:

> **Cannot write to your vault at `$VAULT_PATH`.** Follow the invoking host reference to grant native write access, then re-run `/obsidian-setup`.

Stop here if FAIL.

If **OK**, create the folders:

```bash
mkdir -p "$VAULT_PATH/claude-sessions" "$VAULT_PATH/claude-insights" "$VAULT_PATH/claude-dashboards" "$VAULT_PATH/claude-wiki"
```

### Step 6 — Install dashboard templates

For each dashboard file below, check if it already exists before writing:

```bash
test -f "$VAULT_PATH/claude-dashboards/<filename>" && echo "EXISTS" || echo "MISSING"
```

If EXISTS and `MODE=upgrade`, skip this file — preserve user customizations.
Otherwise (MISSING, or `MODE=fresh`, or `MODE=reconfigure`), publish the approved dashboard through `note-create`; bind any existing destination before a conditional replacement through `note-apply`.

**Dashboard files to install:**

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

**File: `$VAULT_PATH/claude-dashboards/learning-velocity.md`**

```markdown
# Learning Velocity

## Topics by Frequency
\```dataviewjs
let pages = dv.pages('"claude-insights"')
    .where(p => p.tags);

let topics = {};
for (let p of pages) {
    let tags = p.tags || [];
    if (!Array.isArray(tags)) tags = [tags];
    for (let tag of tags) {
        if (typeof tag === 'string' && tag.startsWith("claude/topic/")) {
            let topic = tag.replace("claude/topic/", "");
            topics[topic] = (topics[topic] || 0) + 1;
        }
    }
}

dv.table(
    ["Topic", "Notes"],
    Object.entries(topics)
        .sort((a, b) => b[1] - a[1])
        .map(([topic, count]) => [topic, count])
);
\```

## Recent Retrospectives
\```dataview
TABLE date, project
FROM "claude-insights"
WHERE type = "claude-retro"
SORT date DESC
LIMIT 10
\```

## Error Patterns (Most Common)
\```dataviewjs
let pages = dv.pages('"claude-insights"')
    .where(p => p.type === "claude-error-fix");

let topics = {};
for (let p of pages) {
    let tags = p.tags || [];
    if (!Array.isArray(tags)) tags = [tags];
    for (let tag of tags) {
        if (typeof tag === 'string' && tag.startsWith("claude/topic/")) {
            let topic = tag.replace("claude/topic/", "");
            topics[topic] = (topics[topic] || 0) + 1;
        }
    }
}

dv.table(
    ["Error Topic", "Occurrences"],
    Object.entries(topics)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 15)
        .map(([topic, count]) => [topic, count])
);
\```
```

**File: `$VAULT_PATH/claude-dashboards/decision-timeline.md`**

```markdown
# Decision Timeline

## Active Decisions
\```dataview
TABLE date, project
FROM "claude-insights"
WHERE type = "claude-decision" AND status = "active"
SORT date DESC
\```

## All Decisions (Chronological)
\```dataview
TABLE date, project, status
FROM "claude-insights"
WHERE type = "claude-decision"
SORT date DESC
\```

## Superseded Decisions
\```dataview
TABLE date, project
FROM "claude-insights"
WHERE type = "claude-decision" AND status = "superseded"
SORT date DESC
\```

## Decisions by Project
\```dataview
TABLE length(rows) AS "Decisions", min(rows.date) AS "First", max(rows.date) AS "Last"
FROM "claude-insights"
WHERE type = "claude-decision"
GROUP BY project
SORT length(rows) DESC
\```
```

**File: `$VAULT_PATH/claude-dashboards/open-items.md`**

```markdown
# Open Items — All Projects

Cross-project view of all unchecked `- [ ]` items from session notes' `## Open Questions / Next Steps` sections, scoped to the last 90 days. Items older than 90 days fall off this view — use `/check-items` (unbounded) or `/vault-search` to find them.

\```dataviewjs
// Single-pass: scan all sessions in the last 90 days exactly once,
// build a master list, then render each section from in-memory data.

const cutoff90 = dv.date("today").minus(dv.duration("90 days"));
const cutoff30 = dv.date("today").minus(dv.duration("30 days"));
const cutoff7  = dv.date("today").minus(dv.duration("7 days"));

const pages = dv.pages('"claude-sessions"')
    .where(p => p.type === "claude-session" && p.date && p.date >= cutoff90);

const allItems = [];
for (const p of pages) {
    const content = await dv.io.load(p.file.path);
    if (!content) continue;
    // CRLF-tolerant: \r?\n in lookahead, split on /\r?\n/
    const match = content.match(/## Open Questions[^\r\n]*\r?\n([\s\S]*?)(?=\r?\n## |\r?\n# |$)/);
    if (!match) continue;
    const lines = match[1].split(/\r?\n/);
    for (const line of lines) {
        const m = line.match(/^- \[ \] (.+?)\s*$/);
        if (m) {
            allItems.push({
                project: p.project || "unknown",
                item: m[1],
                date: p.date,
                file: p.file.link
            });
        }
    }
}

// ----- By Project -----
dv.header(2, "By Project");

if (allItems.length === 0) {
    dv.paragraph("No open items in the last 90 days.");
} else {
    const byProject = {};
    for (const i of allItems) {
        if (!byProject[i.project]) byProject[i.project] = [];
        byProject[i.project].push(i);
    }
    const projectNames = Object.keys(byProject).sort();
    for (const project of projectNames) {
        const items = byProject[project];
        dv.header(3, project + " (" + items.length + ")");
        // Render as table to preserve clickable file link
        dv.table(
            ["Item", "Source"],
            items.map(i => [i.item, i.file])
        );
    }
}

// ----- Recent (last 7 days) -----
dv.header(2, "Recent (last 7 days)");

const recent = allItems.filter(i => i.date >= cutoff7);
if (recent.length === 0) {
    dv.paragraph("No open items from sessions in the last 7 days.");
} else {
    dv.table(
        ["Project", "Item", "Source"],
        recent.map(i => [i.project, i.item, i.file])
    );
}

// ----- Items from sessions 30-90 days ago -----
dv.header(2, "Items from sessions 30-90 days ago");
dv.paragraph("These are unchecked items captured in session notes that are 30-90 days old. The same item may also appear in a more recent session — in that case it will also show in the \"Recent\" section above. Filter is by source session date, not by item-tracking duration.");

const stale = allItems
    .filter(i => i.date < cutoff30)
    .sort((a, b) => a.date - b.date);
if (stale.length === 0) {
    dv.paragraph("No items from sessions 30-90 days ago.");
} else {
    dv.table(
        ["Project", "Item", "From", "Source"],
        stale.map(i => [i.project, i.item, i.date.toFormat("yyyy-MM-dd"), i.file])
    );
}

// ----- Stats -----
dv.header(2, "Stats");

const statsByProject = {};
let oldestDate = null;
for (const i of allItems) {
    statsByProject[i.project] = (statsByProject[i.project] || 0) + 1;
    if (!oldestDate || i.date < oldestDate) oldestDate = i.date;
}

dv.paragraph("**Total open items (last 90 days):** " + allItems.length);
dv.paragraph("**Projects with open items:** " + Object.keys(statsByProject).length);
if (oldestDate) {
    dv.paragraph("**Oldest open item from:** " + oldestDate.toFormat("yyyy-MM-dd"));
}

if (Object.keys(statsByProject).length > 0) {
    dv.table(
        ["Project", "Open Items"],
        Object.entries(statsByProject).sort((a, b) => b[1] - a[1])
    );
}
\```
```

**Important:** The backslash before the triple backticks above is an escape for this document only. When writing the actual files, use plain triple backticks (no backslash).

### Step 7 — Write config file

**If `MODE=upgrade`:** Skip this step — preserve existing config.

**If `MODE=fresh` or `MODE=reconfigure`:**

Call `config-read` to retain the exact configuration revision. Submit the
following settings through `configure` with that revision:

```json
{
  "vault_path": "<VAULT_PATH value from Step 2>",
  "sessions_folder": "claude-sessions",
  "insights_folder": "claude-insights",
  "dashboards_folder": "claude-dashboards",
  "check_items_folder": "claude-check-items",
  "wiki_folder": "claude-wiki",
  "min_messages": 3,
  "min_duration_minutes": 2,
  "summary_model": "haiku",
  "auto_log_enabled": true,
  "snapshot_on_compact": true,
  "snapshot_on_clear": true
}
```

Replace `<VAULT_PATH value from Step 2>` with the actual vault path. Ensure the file is valid JSON.

The trusted configuration operation creates its native private directory.

After writing the config file, restrict permissions so only the current user can read it:

The trusted configuration operation publishes at mode `0o600`.

After writing the config, inspect the final `snapshot_on_clear` and `snapshot_on_compact` values. If either is `False` (for instance, when migrating from an older config), warn the user:

> ⚠ `snapshot_on_clear` is False — /clear will not preserve pre-clear context.
> Recommended: True. (This is on by default; only change if you have a specific reason.)

Both flags ship `true` by default, so the warning only fires when a prior config, manual edit, or migration set them `false`. Ask the user whether to flip them back to `true` via `/vault-config` before continuing.

### Step 8 — Verify vault access

Run:

```bash
TESTFILE="$VAULT_PATH/claude-sessions/.obsidian-brain-test-$$"
echo "test" > "$TESTFILE" && test -f "$TESTFILE" && rm "$TESTFILE" && echo "OK" || echo "FAIL"
```

If FAIL, warn that vault writes are not working and ask the user to check permissions.

### Step 8.5 — Build vault index

Build (or rebuild) the SQLite FTS5 index for fast vault search and context-driven insight loading:

Request for `reindex` (substitute the values as data):

```json
{
  "full": false
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --vault "$OB_VAULT" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'reindex' < "$REQUEST_PATH"
```

Parse the JSON output. If successful, store `N = counts["inserted"]` for the success message.

If the command fails (non-zero exit), warn but do not block setup:

> ⚠️ Could not build vault index. Run `/vault-reindex` manually after setup.

### Step 8.7 — Performance Dependencies (optional)

Phase 2 theme clustering can use `numpy` (vectorized TF-IDF) and `scipy` (agglomerative batch clustering) for ~4-5x speedups on large vaults. They are strictly optional — every feature works on pure-Python stdlib fallbacks.

**Idempotent detection.** Load the current config + installed status:

Request for `dependencies` (substitute the values as data):

```json
{}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --vault "$OB_VAULT" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'dependencies' < "$REQUEST_PATH"
```

Parse each line as `KEY=VALUE`.

**Decision tree:**

1. If `numpy` and `scipy` are both true → both installed; skip the prompt entirely. Log one line `[setup] numpy + scipy available — theme clustering will use fast paths.` and continue to Step 9.
2. If `optional_deps_prompted` is true AND every missing package appears in `optional_deps_declined` AND this run was NOT invoked with `--deps` → user already declined; skip the prompt silently. Continue to Step 9.
3. Otherwise → show the prompt below.

**Prompt the user** using native user decision tool:

> Optional performance dependencies for Phase 2 theme clustering:
>
>   numpy: [installed / not installed]
>   scipy: [installed / not installed]
>
> These accelerate batch clustering and TF-IDF math (numpy ~5x, scipy ~4x). Without them, everything still works via pure-Python stdlib fallbacks — daily use is unaffected; only bulk /consolidate runs are slower at very large vault sizes.
>
> 1. **Install missing packages** (runs `python3 -m pip install --user <missing>`)
> 2. **Skip** — remember my choice (won't ask again; re-trigger anytime with `/obsidian-setup --deps`)
> 3. **Not now** — ask again next time

**Apply the choice:**

- **Install:** run `python3 -m pip install --user` for every missing package. Report success/failure for each. After install, update config:

Request for `configure` (substitute the values as data):

```json
{
  "expected_revision": "<config-read SHA256>",
  "settings": {
    "<setting>": "<value>"
  }
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --vault "$OB_VAULT" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'configure' < "$REQUEST_PATH"
```

If pip itself is missing or the install fails (restricted environments, e.g. Homebrew-managed Python), print the failure and continue — all features still work.

- **Skip:** write both flags with every missing package added to `optional_deps_declined`:

Request for `configure` (substitute the values as data):

```json
{
  "expected_revision": "<config-read SHA256>",
  "settings": {
    "<setting>": "<value>"
  }
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --vault "$OB_VAULT" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'configure' < "$REQUEST_PATH"
```

- **Not now:** write neither flag. The next `/obsidian-setup` run will re-prompt.

### Step 9 — Native extraction nudge

Follow the invoking host reference. The Claude hookify integration remains in
its Claude reference. Codex reports this integration unsupported until a
native nudge adapter is verified; no Claude global rule is written for Codex.

### Step 10 — Print success message

**If `MODE=upgrade`:**

> **Obsidian Brain upgraded!**
>
> - Vault path: `<VAULT_PATH>` (unchanged)
> - Config: preserved (unchanged)
> - New dashboards: installed (existing dashboards preserved)
> - skill-kit:extract nudge: configured
> - Vault index: N notes indexed (run `/vault-reindex` to rebuild) — _or omit this line if Step 8.5 failed_
>
> Re-run `/obsidian-setup` anytime to pick up new features.

**If `MODE=fresh` or `MODE=reconfigure`:**

> **Obsidian Brain setup complete!**
>
> - Vault path: `<VAULT_PATH>`
> - Config written to: `<native config_path>`
> - Folders created: `claude-sessions/`, `claude-insights/`, `claude-dashboards/`, `claude-wiki/`
> - Dashboards installed: `sessions-overview.md`, `project-index.md`, `weekly-review.md`, `learning-velocity.md`, `decision-timeline.md`, `open-items.md`
> - skill-kit:extract nudge: configured (run `/compress` reminder after knowledge extraction)
> - Vault index: N notes indexed (run `/vault-reindex` to rebuild) — _or omit this line if Step 8.5 failed_
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
