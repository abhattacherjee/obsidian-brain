---
name: vault-import
description: "Backfills the Obsidian vault with historical Claude Code sessions using conversation search and parallel sub-agents. Use when: (1) /vault-import command to import recent sessions, (2) /vault-import 30d to import last 30 days, (3) /vault-import project:api-service 30d to filter by project, (4) user wants to populate vault with past session history."
metadata:
  version: 1.0.0
---

# Vault Import — Backfill Historical Sessions

Discover historical Claude Code sessions, summarize them via parallel sub-agents, and write structured session notes to the Obsidian vault. Skips sessions already present in the vault.

**Tools needed:** Bash, Read, Write, Skill (for /context:shield sub-agents)

**Prerequisites:**
- `/context:search` skill must be installed (plugin `context@claude-code-skills`; was `conversation-search`)
- `/context:shield` skill must be installed (same plugin; was `context-shield`)
- Obsidian Brain must be configured (run `/obsidian-setup` if not)

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Repo root — run first

Run this as a command of its own:

```bash
git rev-parse --show-toplevel
```

If it fails (not a git repo), run `pwd` instead. Keep the printed path; the steps below call it `<REPO_ROOT>`. Paste that literal path wherever a block below names `'<REPO_ROOT>'`, in single quotes (write a `'` inside it as `'\''`). Do not put `git` inside `$(...)`: a worktree-isolated session refuses any command substitution whose text contains `git` (#521).

### Step 1 — Read config

Run:

```bash
cd '<REPO_ROOT>'
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

If the command exits non-zero or prints ERROR, tell the user:

> Config not found. Please run `/obsidian-setup` first to configure your Obsidian vault.

Stop here if config is missing.

Store the extracted values as `VAULT_PATH` and `SESSIONS_FOLDER` (default `claude-sessions`).

### Step 2 — Validate vault access

Run:

```bash
test -d "$VAULT_PATH/$SESSIONS_FOLDER" && test -w "$VAULT_PATH/$SESSIONS_FOLDER" && echo "OK" || echo "FAIL"
```

If FAIL, tell the user:

> The sessions folder `$VAULT_PATH/$SESSIONS_FOLDER` does not exist or is not writable. Run `/obsidian-setup` to fix this.

Stop here if FAIL.

### Step 3 — Parse arguments

Parse the user's invocation to extract:

- **Time range:** A duration like `7d`, `14d`, `30d`. Default is `7d` if not specified.
- **Project filter:** An optional `project:<name>` argument (e.g. `project:api-service`).

Examples:
- `/vault-import` — last 7 days, all projects
- `/vault-import 30d` — last 30 days, all projects
- `/vault-import project:api-service 14d` — last 14 days, only api-service
- `/vault-import project:api-service` — last 7 days, only api-service

Store as `TIME_RANGE` and `PROJECT_FILTER` (empty string if no filter).

### Step 4 — Discover sessions

Use the `/context:search` skill's underlying search script (was `conversation-search`) to find sessions matching the time range and project filter.

The script lives in the `context@claude-code-skills` plugin. Run this as one block. It finds the script from `~/.claude/plugins/installed_plugins.json`, falls back to the old `~/.claude/skills/conversation-search/` path, and stops if neither exists:

```bash
SEARCH=$(python3 - <<'PY'
import json, os
home = os.path.expanduser("~")
cands = []
try:
    with open(os.path.join(home, ".claude/plugins/installed_plugins.json")) as f:
        for e in json.load(f).get("plugins", {}).get("context@claude-code-skills", []):
            if e.get("installPath"):
                cands.append(os.path.join(e["installPath"], "skills/search/scripts/search-conversations.sh"))
except (OSError, ValueError):
    pass
cands.append(os.path.join(home, ".claude/skills/conversation-search/scripts/search-conversations.sh"))
for c in cands:
    if os.path.isfile(c):
        print(c)
        break
PY
)
if [ -z "$SEARCH" ]; then
  echo "vault-import: search-conversations.sh not found. Install the context plugin (context@claude-code-skills) and retry." >&2
else
  AFTER=$(python3 -c 'import datetime,sys; print((datetime.date.today()-datetime.timedelta(days=int(sys.argv[1]))).isoformat())' <TIME_RANGE_NUMBER>)
  bash "$SEARCH" list --after "$AFTER" --limit 100000 --json <PROJECT_ARGS> | python3 -c '
import glob, json, os, sys
for e in json.load(sys.stdin):
    sid = e["sessionId"]
    hits = glob.glob(os.path.expanduser("~/.claude/projects/*/" + sid + ".jsonl"))
    print(json.dumps({"session_id": sid, "session_path": hits[0] if hits else "",
                      "project": e.get("projectPath", ""), "date": (e.get("created") or e.get("modified") or "")[:10],
                      "git_branch": e.get("gitBranch", ""), "message_count": e.get("messageCount", 0)}))
'
fi
```

Replace `<TIME_RANGE_NUMBER>` with the number of days (`7d` becomes `7`). Replace `<PROJECT_ARGS>` with `--project <PROJECT_FILTER>` when a project filter is set, else remove it. The script can take a few minutes on a large history, so give the call a long timeout.

If the block prints the "not found" message, stop and show it to the user. Do not scan `~/.claude/projects/` by hand and do not skip this step.

The block prints one JSON object per line. Each is a session with `session_id`, `session_path` (empty if the transcript file is gone, so skip those), `project` (a path; use its last component as the project name), `date`, `git_branch` and `message_count`.

If no sessions are found, tell the user:

> No sessions found in the last `<TIME_RANGE>` matching your filters.

Stop here if no sessions found.

### Step 5 — Filter already-imported sessions

Check the vault for existing session notes that match discovered session IDs.

Run:

```bash
grep -rl "session_id:" "$VAULT_PATH/$SESSIONS_FOLDER/" 2>/dev/null | xargs grep -l "<SESSION_ID>" 2>/dev/null
```

More efficiently, build a single grep command:

```bash
for f in "$VAULT_PATH/$SESSIONS_FOLDER/"*.md; do
  awk 'NR==1 { if ($0 != "---") exit; next } $0 == "---" { exit } { print }' "$f" 2>/dev/null
done | grep "^session_id:" | awk '{print $2}'
```

The `awk` reads each note's whole frontmatter, from the opening `---` to the closing one, so `session_id` is found however deep it sits (#312). Collect all session IDs already in the vault into a set called `EXISTING_IDS`. Remove any session from the discovered list whose `session_id` is in `EXISTING_IDS`.

Store the remaining sessions as `PENDING_SESSIONS` and the count of skipped sessions as `SKIPPED_COUNT`.

If no pending sessions remain, tell the user:

> All `<TOTAL>` sessions from the last `<TIME_RANGE>` are already in the vault. Nothing to import.

Stop here if nothing to import.

Otherwise, report:

> Found `<TOTAL>` sessions, `<SKIPPED_COUNT>` already imported, `<PENDING_COUNT>` to import.

### Step 6 — Summarize sessions with parallel sub-agents

**This is the performance-critical step. Use parallel sub-agents to maximize throughput.**

For each session in `PENDING_SESSIONS`, delegate to a `/context:shield` sub-agent with this prompt:

> Read the Claude Code session transcript at `<SESSION_PATH>`. Extract and return a structured summary with these exact sections:
>
> - **Summary:** 2-3 sentence overview of what was accomplished
> - **Key Decisions:** Bulleted list of architectural or design choices made
> - **Changes Made:** Bulleted list of files created, modified, or deleted
> - **Errors Encountered:** Bulleted list of errors hit and how they were resolved (or "None")
> - **Next Steps:** Bulleted list of follow-up tasks mentioned (or "None")
> - **Git Info:** Branch name, commit hashes if any (or "None")
>
> Keep the total output under 300 tokens. Return only the structured summary, no preamble.

**Parallelism rules:**
- Launch up to 5 sub-agents in parallel (sessions are independent — no shared state)
- Wait for the batch to complete before launching the next batch
- If a sub-agent fails or times out, log the error and skip that session — do not block the entire import

Collect the distilled summaries. Store each as `SUMMARY` keyed by `session_id`.

### Step 7 — Construct and write session notes

For each successfully summarized session, construct a vault note with this format:

```markdown
---
type: claude-session
date: <YYYY-MM-DD from session date>
session_id: <session_id>
project: <project name>
git_branch: <branch from git info, or empty>
duration_minutes: <estimated from transcript length, or empty>
imported: true
imported_date: <today's date YYYY-MM-DD>
tags:
  - claude/session
  - claude/project/<project-name>
  - claude/imported
---

# <Session Title derived from summary>

<Summary section>

## Key Decisions

<Key decisions bulleted list>

## Changes Made

<Changes bulleted list>

## Errors Encountered

<Errors bulleted list>

## Next Steps

<Next steps bulleted list>

## Git Info

<Git info>
```

Generate the filename using the same convention as other session notes:

1. **Date:** `YYYY-MM-DD` (session date)
2. **Slug:** Title lowercased, spaces to hyphens, non-alphanumeric (except hyphens) removed, truncated to 50 chars
3. **Hash:** 4-character hex hash from the session_id: `echo -n "<session_id>" | md5 | cut -c1-4` (macOS) or `echo -n "<session_id>" | md5sum | cut -c1-4` (Linux). Do NOT use `tail -c 4` — it counts the trailing newline as a byte and returns only 3 visible characters.

Final filename: `YYYY-MM-DD-<slug>-<hash>.md`

Write each note by running the note-writer CLI once **per session**, passing that session's note file with `--body-file`, so no imported session text goes into the Bash command (#517). It creates `$SESSIONS_FOLDER` if needed (idempotent across repeated calls in this loop — only the first call actually creates it) and writes each file atomically at mode `0o600` — no `mkdir`/`chmod` needed. The `HOOKS=` line below checks the marketplace-registered directory-source install location FIRST (#278 — on a local checkout that is what loads, not the released cache), and only falls back to the plugin cache, where it sorts versions **numerically** (a plain `max()` is lexicographic and picks `3.9.0` over `3.10.0`, resolving to a cache with no `note_writer.py`); the `test -f` line turns a stale/incomplete cache into the documented `ERROR:` shape instead of a raw Python `can't open file` message.

Write the note with the **Write tool** to `~/.claude/obsidian-brain/<draft-id>-<filename>`, using the same filename the command below passes (expand `~` to your home directory; run `echo "$HOME"` if you do not know it). `<draft-id>` is 8 random hex characters you pick once per run, so two runs are very unlikely to share a draft file. That path is `<BODY_FILE>`. The file holds the whole note, shaped like this:

```markdown
---
type: claude-session
...
---

# <Session Title>
...
```

```bash
cd '<REPO_ROOT>'
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
if ! test -f "$HOOKS/note_writer.py"; then echo "ERROR: note_writer.py not found under $HOOKS - resolution checks the marketplace registered install location first, then falls back to the plugin cache; neither path produced a hooks directory containing it. Verify the obsidian-brain install resolved at $HOOKS is complete (git pull for a directory-source checkout, or run /plugin marketplace update for a cache install), then retry." >&2; exit 1; fi
python3 "$HOOKS/note_writer.py" write "$VAULT_PATH" "$SESSIONS_FOLDER" "<filename>" --body-file '<BODY_FILE>'
```

After an `OK:` line, run `rm -f '<BODY_FILE>'` (the literal path). If the command fails, keep `<BODY_FILE>` and tell the user its path, so the note text is not lost.

On success this prints `OK: <absolute path>`. On failure it prints `ERROR: <reason>` to stderr and exits non-zero for THAT session only — record it under `<FAILED_COUNT>`/`<Failed sessions>` in Step 8 and continue importing the remaining sessions; one failed write must not abort the whole import loop.

**One exception: an `ERROR:` containing `note already exists` counts as SKIPPED, not failed.** It means this session was imported by an earlier run, which is the normal outcome of a re-import — increment `<SKIPPED_COUNT>` and move on. Do NOT pass `--overwrite` to make it go away: that would silently replace an existing note, which is exactly what the flag exists to prevent.

### Step 8 — Report results

Print a summary report:

> **Vault import complete!**
>
> - **Imported:** `<IMPORTED_COUNT>` sessions
> - **Skipped (already in vault):** `<SKIPPED_COUNT>` sessions
> - **Failed:** `<FAILED_COUNT>` sessions (if any)
> - **Time range:** last `<TIME_RANGE>`
> - **Project filter:** `<PROJECT_FILTER>` (or "all projects")
>
> New notes written to: `$VAULT_PATH/$SESSIONS_FOLDER/`

If any sessions failed, list them:

> **Failed sessions:**
> - `<session_id>`: `<error reason>`

Offer follow-up:

> Run `/vault-import <longer range>` to go further back, or open Obsidian to browse the imported sessions.
