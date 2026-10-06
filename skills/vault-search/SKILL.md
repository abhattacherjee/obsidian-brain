---
name: vault-search
description: "Searches the Obsidian vault by keyword, tag, or structured query across session and insight notes. Use when: (1) /vault-search command, (2) user asks to find past notes, decisions, or error fixes, (3) user wants to recall something from their vault."
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

Use only the operations documented for this skill. Their writes bind the source revisions before analysis and preserve manual edits on conflict. Content is JSON data, never shell code. Read `references/host-claude.md` or `references/host-codex.md` when present. Codex has no native memory-file API; shared vault retrieval and wiki filing continue without borrowing another host's memory.

# Vault Search

Search the entire Obsidian vault by keyword, tag, or structured field query. Returns ranked results with snippets from both `claude-sessions/` and `claude-insights/` folders.

**Tools needed:** native content search, native file reading, native shell

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

Parse the single JSON object. Read its named fields; do not split output on `=`.

If the file does not exist, tell the user:

> Config not found. Run `/obsidian-setup` first to configure your vault path.

Stop here if config is missing.

Construct the two search directories:

- `SESSIONS_DIR` = `<vault_path>/<sessions_folder>`
- `INSIGHTS_DIR` = `<vault_path>/<insights_folder>`

### Step 2 — Parse the query

The user provides a query after `/vault-search`. Determine the search mode:

**Tag mode** — query starts with `#` (e.g. `#claude/topic/auth`):
- Strip the leading `#`
- The search target is frontmatter `tags` fields
- Pattern: the tag string, used as a regex (escape `.`, `+` and other regex characters)
- Search only the frontmatter, not the body. Step 4 does this with `vault_scan.py grep --frontmatter-only`, because content search alone cannot limit matches to frontmatter and the `tags:` block can sit past line 40 (/emerge notes close their fence as deep as line 461)

**Structured mode** — query contains `key:value` pairs (e.g. `project:api-service type:decision`):
- Parse each `key:value` pair
- Each pair maps to a frontmatter field grep: pattern `^key:.*value` (case-insensitive)
- All pairs must match in the same file (intersection)

**Keyword mode** — everything else (e.g. `jwt refresh`):
- Treat the entire query as a content search
- Search for the full phrase first; if zero results, grep for each word individually and intersect

### Step 3 — Try FTS search (fast path)

Before falling back to pattern search, try the vault index:

Request for `search` (substitute the values as data):

```json
{
  "query": "<query>",
  "project": "<project or null>",
  "limit": 20
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'search' < "$REQUEST_PATH"
```

If the output is a non-empty JSON array: parse and present results (path, title, type, date, excerpt) using the format in Step 6. Skip Steps 4 and 5 below.

If the output is `[]` or the command fails: print a note that the vault index returned no results, then fall through to Step 4. If the command failed because the DB does not exist, also suggest running `/vault-reindex` to build the index.

### Step 4 — Search both folders in parallel

Use the fixed `grep` operation. It searches validated indexed folders and emits one matching path per line. Keep only returned paths under the selected sessions and insights folders for this fallback. Query strings stay JSON data, including quotes and leading hyphens; the helper passes `--pattern=` internally, so they cannot become shell commands or CLI flags.

**Tag mode:** Set `pattern` to the escaped tag regex, `ignore_case` to true, and `frontmatter_only` to true. Frontmatter is read through its closing fence even past line 40 (an /emerge fence can appear at line 461). A note without frontmatter cannot match; an unclosed fence is skipped and counted as `bad_frontmatter` in stderr.

```json
{"pattern": "<escaped tag regex>", "ignore_case": true, "frontmatter_only": true}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'grep' < "$REQUEST_PATH"
```

**Structured mode:** Run one request per `key:value` pair with `pattern: "^<key>:.*<value>"`, `ignore_case: true`, and `frontmatter_only: true`. Intersect the returned file lists: every pair must match the same file.

**Keyword mode:** Run one request for the complete query with `ignore_case: true`. If it finds no matches and the query contains several words, run one request per word and intersect the file lists.

Check each call before you use its output. It succeeded only if it exited 0 and stderr has the `vault_scan: N match(es), M file(s) scanned, K skipped (...)` summary line; then stdout is the file list, and an empty stdout means no match. Anything else is a failure, not "no match": show the `ERROR:` line (or the whole stderr if there is none) to the user and stop. If K is more than 0, add this line to what you show the user: "K note(s) were not searched (see the breakdown) — run /vault-doctor".

Request for `grep` (substitute the values as data):

```json
{
  "pattern": "<pattern>",
  "ignore_case": true
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'grep' < "$REQUEST_PATH"
```

### Step 5 — Extract metadata from matches

If there are more than 20 matched files, sort by filename (which contains the date in YYYY-MM-DD format) descending and keep only the 20 most recent.

Read all kept files through one fixed `metadata` request with an explicit `paths` array. Do not use a fixed-line reader: frontmatter can run past line 40 (/emerge notes close their fence as deep as line 461), so a fixed line limit silently drops fields. Paste each value inside single quotes as shown. If a value itself contains a `'`, write it as `'\''`.

Request for `metadata` (substitute the values as data):

```json
{
  "paths": [
    "<relative vault note>"
  ]
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'metadata' < "$REQUEST_PATH"
```

It prints one JSON object per file, one per line. The call succeeded only if it exited 0 and printed one JSON row per file you passed. Otherwise show the `ERROR:` line (or the whole stderr) to the user and stop. A `vault_scan: obsidian_utils unavailable: ...` line on stderr is a warning, not a failure. Take these fields from it:

- **date** — the `date:` field
- **type** — the `type:` field (e.g. `claude-session`, `claude-insight`, `claude-decision`, `claude-error-fix`, `claude-snapshot`)
- **project** — the `project:` field
- **session_id** — the `session_id:` field (used below to attach snapshots to session hits)
- **source_session_note** — the `source_session_note:` wikilink on snapshots (the parent session stem, enclosed in `[[...]]`)
- **title** — the first `# ` heading, or the filename without extension
- **snippet** — the first 200 characters of the body after the frontmatter, with whitespace collapsed

A missing `date`, `type`, `project`, `session_id` or `source_session_note` is `null`; missing or empty `tags` is `[]`; `title` falls back to the filename. If a row has a non-null `error` (for example `unparsable frontmatter: no_closing_fence`), still list the file, using its filename as the title and `note` as its type.

### Step 5b — Augment session hits with snapshot data

For each result whose `type` is `claude-session`, query its snapshots once via the shared Python helper `fetch_snapshot_summaries()`:

Request for `snapshots` (substitute the values as data):

```json
{
  "source_session_id": "<full source native ID>",
  "date": "<date>",
  "project": "<project>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'snapshots' < "$REQUEST_PATH"
```

If the returned JSON array is non-empty, remember the snapshot count `N` and each snapshot's `hhmmss` + `trigger` for that result. If batching many sessions, run these queries in parallel.

For results whose `type` is `claude-snapshot`, remember the `source_session_note` wikilink stem (strip `[[...]]`) as the parent pointer.

### Step 6 — Sort and present results

Sort results by date descending (most recent first). Present in this format:

```
Found <N> notes matching "<query>":

1. <icon> <title> (<type-label>, <date>)
   "<snippet>..."

2. <icon> <title> (<type-label>, <date>)
   "<snippet>..."
```

Use these icons for type labels:
- `claude-session` → session
- `claude-insight` → insight
- `claude-decision` → decision
- `claude-error-fix` → error-fix
- `claude-snapshot` → snapshot
- anything else → note

Truncate snippets at 200 characters, ending with `...` if truncated.

**Snapshot markers on session hits:** If Step 5b found `N >= 1` snapshots for a session result, append `· 📸 N` to the type-label block — e.g. `(session · 📸 2, 2026-04-18)`. Under the snippet, list each snapshot as a nested bullet:

```
   ↳ 📸 <hhmmss> (<trigger>)
```

**Parent pointer on snapshot hits:** For a `claude-snapshot` result, append `→ [[<parent-stem>]]` after the date — e.g. `(snapshot, 2026-04-18 → [[2026-04-18-demo-aa]])`. Only include the marker when `source_session_note` is set.

After the list, tell the user:

> Pick a number to load the full note, or refine your search.

### Step 7 — Handle user selection

If the user picks a number, read the full content of that file using native file reading and present it in the conversation.

**Session-depth loading applies to snapshot picks too.** If the user picks a snapshot result, load the parent session body AND all its snapshot summaries (re-use `fetch_snapshot_summaries()`), not just the snapshot file alone — so the answer reflects the full session arc, not the mid-session fragment. Resolve the parent via the `source_session_note` stem captured in Step 5.

If the user provides a new query, go back to Step 2.


A native session summary is stale when `capture_revision` differs from
`summary_revision`, or no `summary_revision` exists. Label it stale explicitly.
Use unchanged raw capture facts as evidence; do not present its old summary as fresh.
Failed or cancelled AI leaves the operation pending and preserves the note.
