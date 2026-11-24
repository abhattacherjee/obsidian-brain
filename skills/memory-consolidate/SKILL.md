---
name: memory-consolidate
description: "Moves durable facts out of a project's Claude Code memory store into the Obsidian vault, one note per fact, so MEMORY.md shrinks or loses its orphans without losing anything. Dry run first; originals go to a backup dir, never deleted. Use when: (1) /memory-consolidate to shrink a MEMORY.md near its size limit, (2) /memory-consolidate to rescue orphaned memory files the index no longer reaches, (3) vault-doctor's memory-index check reports drift or an oversized index."
metadata:
  version: 1.0.0
---

# Memory Consolidate — move memory facts into the vault

`MEMORY.md` is the only part of a project's memory that loads every session, and it has a
read budget (about 24,000 bytes; compaction is asked for near 17,000) and a 200-line limit.
This skill moves facts that do not need to be in context every session into the vault, where
`/vault-ask` and `/vault-search` find them.

Two modes, same steps:
- **Shrink:** the index is near a limit and every entry is live.
- **Rescue:** orphans exist: memory files no index line reaches.

Claude makes the judgement calls (keep or move, note type, dedupe). The CLI
`hooks/memory_consolidate.py` does the mechanics (`audit`, `apply`, `verify`, `smoke`).

**Tools needed:** Bash, Read, Write

## Rules

- **Dry run first, always.** Show the dry run to the user and get a yes before `--apply`.
- **Nothing is deleted.** `apply --apply` copies the whole store to a backup dir, then moves each
  original to `<backup>/moved/<slug>.md`.
- **Word for word.** Each new note copies the memory body exactly. Moved notes carry
  `migrated_from_memory: <slug>` and no `source_session` (retro would count them as the moving
  session's findings, #495).
- **Write the plan with the Write tool.** No heredoc, no JSON in a shell argument (#524).

## Procedure

### Step 0 — Find the CLI

Run this as a command of its own and keep the printed hooks directory; the steps below call it
`<HOOKS>`. Paste it literally, in single quotes.

If it prints the bare word `hooks`, the plugin install was not found: stop and tell the user to
check the obsidian-brain install.

```bash
python3 -c "
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
"
```

### Step 1 — Audit

```bash
python3 '<HOOKS>/memory_consolidate.py' audit
```

The first line is `store: <path>`: the store this run works on (the main worktree's store, even
from a worktree). Keep it as `<STORE>`. If the user names another project, add
`--store '<that store path>'`. The plan JSON carries the store (Step 4), so `apply` and `verify`
need no `--store`. Exit 2 means the store could not be chosen; show the error and stop.

Report to the user: index size in bytes, characters and lines (say which unit), the limits,
the mode (`shrink`, `rescue` or `ok`), and any orphans, dangling links or duplicates. Mode `ok`
with no drift: say there is nothing to do and stop unless the user asks to go on.

### Step 2 — Decide keep, move or retire

Read each candidate entry in `<STORE>` (Read tool). Apply this test:

- **Keep** a fact in memory when missing it at the moment it applies would cause damage before
  anyone thought to run `/vault-ask`: deploy and live-service rules, approval limits, hook and
  preflight mechanics, daily tool gotchas.
- **Move** one-off design lessons, general testing lessons, and anything the global `CLAUDE.md`
  already says.
- **Retire** point-in-time state that git or GitHub now holds (PR status, "shipped on <date>",
  filed-issue records). Retired entries are backed up but get no vault note.

When the user asks for a number of entries or for older ones, use `audit --json` (each entry has
`mtime` and `body_chars`) to pick, oldest first, among those that pass the move test; if fewer
pass, move only those and say so.

Pick a note type per moved entry: `claude-insight` (a reusable rule; the default),
`claude-error-fix` (a diagnosed failure and its fix) or `claude-decision` (a chosen option with
its rationale).

**Index lines that point at several entries.** When one `MEMORY.md` line links more than one
entry, plan those entries as a group: all move, or none. `apply` refuses to move one entry off a
line that still points at entries that stay, and the dry run says so.

### Step 3 — Dedupe against the vault

For each entry you plan to move, search the vault (`/vault-search <key phrase>`, or
`/vault-ask` for a fuzzy match). A hit counts only when the matching note is **about that
lesson**, not a retro or session note that mentions it in passing. On a real hit, plan the entry
as `append` to that note instead of `new`. Check `_provisional/` hits too: an earlier capture of
the same lesson often sits there.

### Step 4 — Write the plan

Write the plan JSON with the Write tool to a file in your scratchpad (never `/tmp/ob-*`):

```json
{
  "version": 1,
  "store": "<STORE>",
  "items": [
    {"slug": "feedback_x", "action": "new", "type": "claude-insight", "topic": "testing"},
    {"slug": "feedback_y", "action": "append", "target": "claude-insights/2026-11-03-foo-0d18.md"},
    {"slug": "project_z", "action": "retire", "reason": "PR merged; git holds it"}
  ]
}
```

- `slug` is the file name without `.md`. `topic` is optional kebab-case.
- `target` is the vault-relative path of the note to append to.
- `reason` is required for `retire`.

Keep the path as `<PLAN>`.

### Step 5 — Dry run, then ask

```bash
python3 '<HOOKS>/memory_consolidate.py' apply --plan '<PLAN>'
```

It prints `NEW`, `APPEND`, `RETIRE`, `REFUSE` and `WARN` lines and every index line it would
drop, and writes nothing. Exit 2 means the plan is bad: fix it and rerun.

Show the user the full list, every `RETIRE` and `REFUSE` line in particular, and ask before going
on. If the user drops or changes items, edit the plan and rerun this dry run before Step 6.
A `REFUSE` for a secret-shaped string means the entry stays in memory; tell the user which.

### Step 6 — Apply

```bash
python3 '<HOOKS>/memory_consolidate.py' apply --plan '<PLAN>' --apply
```

Keep the `backup:` line's path as `<BACKUP>`. Exit 1 means some items were refused and the rest
were done. If the run stops partway, it prints the `apply --resume '<BACKUP>'` line to finish it;
run that.

### Step 7 — Verify

```bash
python3 '<HOOKS>/memory_consolidate.py' verify --backup '<BACKUP>'
```

It prints one line per check: `index-links-resolve`, `no-duplicates`, `all-indexed`,
`removed-equals-moved`, `notes-lossless`, `backups-present`, `sizes`, then `verify: ok`.
Every line must be `PASS`; a missing line counts as a failure.
Any `FAIL` (exit 1): report it to the user with its detail line. The originals are all in
`<BACKUP>/moved/` and `<BACKUP>/full-copy/`, so nothing is lost.

### Step 8 — Reindex and search

Pick a distinctive phrase from one moved fact and the vault-relative path of the note it went to:

```bash
python3 '<HOOKS>/memory_consolidate.py' smoke --query '<phrase>' --expect '<note path>'
```

This runs the default (non-wiping) reindex, then searches. Exit 0 means the moved note was found.
Exit 1 (`MISSING`) means the move is not retrievable yet: report it, do not claim success.

### Step 9 — Report

Tell the user, in a short table: entries moved, appended, retired and refused; index size before
and after in bytes, characters and lines (from `verify`'s `sizes` line); and the backup path. Name
any `WARN` lines (for example, an aside left on a kept index line).
