---
name: emerge
description: "Surface unnamed patterns across vault themes. Use when: (1) /emerge for last 30 days, (2) /emerge 14d for custom window, (3) /emerge this week."
metadata:
  version: 2.0.0
---
# Emerge — Discover Patterns Across Your Obsidian Vault Themes

Operates on **themes** (clustered by `/consolidate`), not raw notes, so it scales
to large vaults within one sub-agent's context budget. Reads themes updated in a
date window, ranks them by activation, and synthesizes cross-cutting patterns.

**Tools needed:** Bash, Agent, Write, Read

## Procedure

### Step 0 — Create task manifest

```
TaskCreate: subject="Collect themes in window", activeForm="Collecting themes"
TaskCreate: subject="Analyze patterns across themes", activeForm="Analyzing patterns"
TaskCreate: subject="Build emerge report", activeForm="Building report"
TaskCreate: subject="Write vault note", activeForm="Writing vault note"
TaskCreate: subject="Present results", activeForm="Presenting results"
```
Track task IDs. Set task #1 to `in_progress`.

### Step 1 — Parse args + collect themes

Parse the arg as DAYS: no arg = 30, `Nd`/`N days` = N, `this week` = days since Monday. Bind it to `$DAYS`.

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import glob, json, os, re, sys
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json"))).values():
            _h = os.path.join((_m or {}).get("installLocation", ""), "hooks")
            if os.path.isfile(os.path.join(_h, "obsidian_utils.py")):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")) if re.fullmatch("[0-9]+([.][0-9]+)*", _d.split("/")[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split("/")[-2].split(".")], _p), default="hooks")
sys.path.insert(0, _ob_hooks())
from emerge_cli import run_emerge_themes; run_emerge_themes(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
' "$DAYS"
```

Parse the `STATUS=` line:
- `STATUS=SPARSE:<n>` → tell the user verbatim: *"Only <n> theme(s) were updated in this window. Run `/consolidate` to seed themes first, or widen the window: `/emerge 90d`."* — then STOP (do not run Steps 2-4).
- `STATUS=OK:<themes>:<unassigned>` → proceed to Step 2.

If the command errors (config missing / non-zero exit), tell the user to run `/obsidian-setup` first and stop. Mark task #1 `completed`.

### Step 2 — Pattern synthesis
Set task #2 to `in_progress`. Spawn one Agent:
```
Agent({
  description: "Analyze vault themes for cross-cutting patterns",
  prompt: "Read ~/.claude/obsidian-brain/emerge-themes.json. It contains `themes` (each with name, summary, note_count, activation, project, and a `members` array of {title, excerpt, similarity, surprise, project}) and `unassigned_candidates` ({title, excerpt, project, date}). Analyze and write to ~/.claude/obsidian-brain/emerge-analysis.md with EXACTLY these sections:\n\n## Growing Themes\nThemes with high activation / recent member growth — momentum.\n\n## Decaying Themes\nThemes with low activation / stale members — fading from focus.\n\n## Cross-Project Connections\nThemes whose members span multiple projects, or shared themes between projects. SKIP this section entirely if there is only 1 project.\n\n## Contradictions\nTensions or reversals across themes. Highlight members with high `surprise` values (they diverged from their theme centroid).\n\n## New Candidates\nProto-themes hinted by the `unassigned_candidates` — clusters of related unassigned notes not yet consolidated.\n\nFor each item: a descriptive name, 2-3 references (theme names or note titles), and a confidence (strong/moderate/tentative).\n\nIMPORTANT: Output ONLY the `##` section content as the note body — do NOT add YAML frontmatter, a top-level `#` title, or any `---` delimiter line. The note's frontmatter and title are added separately by run_build_note; any frontmatter you add would be embedded into the body and produce a malformed double-frontmatter note.\n\nWrite using the Write tool. Return ONLY: WRITTEN:~/.claude/obsidian-brain/emerge-analysis.md"
})
```

If no `WRITTEN:` response, report failure and stop. Mark task #2 `completed`.

### Step 3 — Build output + write note
```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import glob, json, os, re, sys
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json"))).values():
            _h = os.path.join((_m or {}).get("installLocation", ""), "hooks")
            if os.path.isfile(os.path.join(_h, "obsidian_utils.py")):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")) if re.fullmatch("[0-9]+([.][0-9]+)*", _d.split("/")[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split("/")[-2].split(".")], _p), default="hooks")
sys.path.insert(0, _ob_hooks())
from emerge_cli import run_build_note; run_build_note()
' 2>&1
```

Parse `SAVED:<path>` and everything after `---REPORT---`. Mark tasks #3-#4 `completed`.

### Step 4 — Present to user
Display report prefixed with **Pattern Discovery Results:**. Confirm saved path. Mark task #5 `completed`.

## Edge Cases
- **Sparse window (< 2 themes updated):** nudge the user to run `/consolidate` first or widen the window (`/emerge 90d`) — see Step 1.
- **Only 1 project:** Sub-agent skips Cross-Project Connections.
- **Config not found:** Tell user to run `/obsidian-setup` first.
