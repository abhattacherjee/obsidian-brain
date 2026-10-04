---
name: vault-search
description: "Searches the Obsidian vault by keyword, tag, or structured query across session and insight notes. Use when: (1) /vault-search command, (2) user asks to find past notes, decisions, or error fixes, (3) user wants to recall something from their vault."
metadata:
  version: 1.0.0
---

# Vault Search

Search the entire Obsidian vault by keyword, tag, or structured field query. Returns ranked results with snippets from both `claude-sessions/` and `claude-insights/` folders.

**Tools needed:** Grep, Read, Bash

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
            _s = _m.get("source") if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get("source") == "directory"):
                continue
            _i = _m.get("installLocation") if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            _h = os.path.join(_i, "hooks")
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
- Search only the frontmatter, not the body. Step 4 does this with `vault_scan.py grep --frontmatter-only`, because the Grep tool cannot limit a search to frontmatter and the `tags:` block can sit past line 40 (/emerge notes close their fence as deep as line 461)

**Structured mode** — query contains `key:value` pairs (e.g. `project:api-service type:decision`):
- Parse each `key:value` pair
- Each pair maps to a frontmatter field grep: pattern `^key:.*value` (case-insensitive)
- All pairs must match in the same file (intersection)

**Keyword mode** — everything else (e.g. `jwt refresh`):
- Treat the entire query as a content search
- Grep for the full phrase first; if zero results, grep for each word individually and intersect

### Step 3 — Try FTS search (fast path)

Before falling back to Grep, try the vault index:

```bash
python3 -c '
import sys, os, json, glob
import glob, json, os, re, sys
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json"))).values():
            _s = _m.get("source") if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get("source") == "directory"):
                continue
            _i = _m.get("installLocation") if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            _h = os.path.join(_i, "hooks")
            if os.path.isfile(os.path.join(_h, "obsidian_utils.py")):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")) if re.fullmatch("[0-9]+([.][0-9]+)*", _d.split("/")[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split("/")[-2].split(".")], _p), default="hooks")
sys.path.insert(0, _ob_hooks())
from obsidian_utils import load_config, indexed_folders
from vault_index import ensure_index, search_vault
c = load_config()
db = ensure_index(c["vault_path"], indexed_folders(c))
results = search_vault(
    db,
    sys.argv[1],
    project=sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "None" else None,
    limit=20,
)
print(json.dumps(results))
' "$QUERY" "$PROJECT"
```

If the output is a non-empty JSON array: parse and present results (path, title, type, date, excerpt) using the format in Step 6. Skip Steps 4 and 5 below.

If the output is `[]` or the command fails: print a note that the vault index returned no results, then fall through to Step 4. If the command failed because the DB does not exist, also suggest running `/vault-reindex` to build the index.

### Step 4 — Search both folders in parallel

Use the Grep tool (never Bash grep) for structured and keyword searches. Tag mode uses `vault_scan.py` instead (see below). If the Grep tool is not in your tool list, go straight to vault_scan.py grep — do not call Grep first. See the fallback below. Launch searches across both `SESSIONS_DIR` and `INSIGHTS_DIR` in parallel.

**For tag mode:**
Run one `vault_scan.py grep --frontmatter-only` call over both folders (not the Grep tool). It searches only each note's frontmatter, however long. A note with no frontmatter cannot match; a note whose frontmatter fence does not close is skipped and counted as `bad_frontmatter` in the stderr summary line. Stdout is one matching path per line. Always write `--pattern=` with the equals sign: with a space, a term that starts with `-` (such as `--no-verify`) is read as a flag and the call fails. Paste each value inside single quotes as shown. If a value itself contains a `'`, write it as `'\''`. The success check below applies to this call too.

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
HOOKS=$(python3 -c "
import glob, json, os, re
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser('~/.claude/plugins/known_marketplaces.json'))).values():
            _s = _m.get('source') if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get('source') == 'directory'):
                continue
            _i = _m.get('installLocation') if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            _h = os.path.join(_i, 'hooks')
            if os.path.isfile(os.path.join(_h, 'obsidian_utils.py')):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser('~/.claude/plugins/cache/*/obsidian-brain/*/hooks')) if re.fullmatch('[0-9]+([.][0-9]+)*', _d.split('/')[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split('/')[-2].split('.')], _p), default='hooks')
print(_ob_hooks())
")
test -f "$HOOKS/vault_scan.py" || { echo "ERROR: vault_scan.py not found under $HOOKS - resolution checks the marketplace registered install location first, then falls back to the plugin cache; neither path produced a hooks directory containing it. Verify the obsidian-brain install resolved at $HOOKS is complete (git pull for a directory-source checkout, or run /plugin marketplace update for a cache install), then retry." >&2; exit 1; }
python3 "$HOOKS/vault_scan.py" grep '<vault_path>' '<sessions_folder>' '<insights_folder>' --pattern='<tag>' --frontmatter-only
```

**For structured mode:**
For each `key:value` pair, run two parallel Grep calls (one per folder):
- `Grep(pattern="^<key>:.*<value>", path=<folder>, glob="*.md", output_mode="files_with_matches", -i=true)`

Then intersect results across all pairs — only files matching every pair are kept.

**For keyword mode:**
Run two parallel Grep calls:
- `Grep(pattern="<query>", path=SESSIONS_DIR, glob="*.md", output_mode="files_with_matches", -i=true)`
- `Grep(pattern="<query>", path=INSIGHTS_DIR, glob="*.md", output_mode="files_with_matches", -i=true)`

If zero results and query has multiple words, retry by grepping each word separately and intersecting the file lists.

**If the Grep tool is not available in this session** (structured and keyword mode), run each search above with `vault_scan.py grep` instead (#375): one call per pattern, both folder names as arguments, `--pattern='<pattern>'`, `--ignore-case` for `-i=true`. Use stdout as the file list and intersect exactly as above. Always write `--pattern=` with the equals sign: with a space, a term that starts with `-` (such as `--no-verify`) is read as a flag and the call fails. Paste each value inside single quotes as shown. If a value itself contains a `'`, write it as `'\''`.

Check each call before you use its output. It succeeded only if it exited 0 and stderr has the `vault_scan: N match(es), M file(s) scanned, K skipped (...)` summary line; then stdout is the file list, and an empty stdout means no match. Anything else is a failure, not "no match": show the `ERROR:` line (or the whole stderr if there is none) to the user and stop. If K is more than 0, add this line to what you show the user: "K note(s) were not searched (see the breakdown) — run /vault-doctor".

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
HOOKS=$(python3 -c "
import glob, json, os, re
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser('~/.claude/plugins/known_marketplaces.json'))).values():
            _s = _m.get('source') if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get('source') == 'directory'):
                continue
            _i = _m.get('installLocation') if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            _h = os.path.join(_i, 'hooks')
            if os.path.isfile(os.path.join(_h, 'obsidian_utils.py')):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser('~/.claude/plugins/cache/*/obsidian-brain/*/hooks')) if re.fullmatch('[0-9]+([.][0-9]+)*', _d.split('/')[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split('/')[-2].split('.')], _p), default='hooks')
print(_ob_hooks())
")
test -f "$HOOKS/vault_scan.py" || { echo "ERROR: vault_scan.py not found under $HOOKS - resolution checks the marketplace registered install location first, then falls back to the plugin cache; neither path produced a hooks directory containing it. Verify the obsidian-brain install resolved at $HOOKS is complete (git pull for a directory-source checkout, or run /plugin marketplace update for a cache install), then retry." >&2; exit 1; }
python3 "$HOOKS/vault_scan.py" grep '<vault_path>' '<sessions_folder>' '<insights_folder>' --pattern='<pattern>' --ignore-case
```

### Step 5 — Extract metadata from matches

If there are more than 20 matched files, sort by filename (which contains the date in YYYY-MM-DD format) descending and keep only the 20 most recent.

Read the metadata of all kept files with one `vault_scan.py meta` call (one quoted path per file). Do not use a fixed-line `Read`: frontmatter can run past line 40 (/emerge notes close their fence as deep as line 461), so a fixed line limit silently drops fields. Paste each value inside single quotes as shown. If a value itself contains a `'`, write it as `'\''`.

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
HOOKS=$(python3 -c "
import glob, json, os, re
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser('~/.claude/plugins/known_marketplaces.json'))).values():
            _s = _m.get('source') if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get('source') == 'directory'):
                continue
            _i = _m.get('installLocation') if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            _h = os.path.join(_i, 'hooks')
            if os.path.isfile(os.path.join(_h, 'obsidian_utils.py')):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser('~/.claude/plugins/cache/*/obsidian-brain/*/hooks')) if re.fullmatch('[0-9]+([.][0-9]+)*', _d.split('/')[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split('/')[-2].split('.')], _p), default='hooks')
print(_ob_hooks())
")
test -f "$HOOKS/vault_scan.py" || { echo "ERROR: vault_scan.py not found under $HOOKS - resolution checks the marketplace registered install location first, then falls back to the plugin cache; neither path produced a hooks directory containing it. Verify the obsidian-brain install resolved at $HOOKS is complete (git pull for a directory-source checkout, or run /plugin marketplace update for a cache install), then retry." >&2; exit 1; }
python3 "$HOOKS/vault_scan.py" meta '<vault_path>' '<file_1>' '<file_2>'
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

```bash
python3 -c '
import sys, os, json, glob
import glob, json, os, re, sys
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json"))).values():
            _s = _m.get("source") if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get("source") == "directory"):
                continue
            _i = _m.get("installLocation") if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            _h = os.path.join(_i, "hooks")
            if os.path.isfile(os.path.join(_h, "obsidian_utils.py")):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")) if re.fullmatch("[0-9]+([.][0-9]+)*", _d.split("/")[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split("/")[-2].split(".")], _p), default="hooks")
sys.path.insert(0, _ob_hooks())
from pathlib import Path
from obsidian_utils import fetch_snapshot_summaries
snaps = fetch_snapshot_summaries(Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4])
print(json.dumps([{"hhmmss": s["hhmmss"], "trigger": s["trigger"]} for s in snaps]))
' "$SESSIONS_DIR" "$SESSION_ID" "$DATE" "$PROJECT"
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

If the user picks a number, read the full content of that file using the Read tool and present it in the conversation.

**Session-depth loading applies to snapshot picks too.** If the user picks a snapshot result, load the parent session body AND all its snapshot summaries (re-use `fetch_snapshot_summaries()`), not just the snapshot file alone — so the answer reflects the full session arc, not the mid-session fragment. Resolve the parent via the `source_session_note` stem captured in Step 5.

If the user provides a new query, go back to Step 2.

