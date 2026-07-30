---
name: consolidate
description: "Batch-cluster vault notes into named themes. Use when: (1) /consolidate to seed themes from unassigned notes, (2) /consolidate stats, (3) /consolidate split <id>, (4) /consolidate merge <a> <b>, (5) /consolidate --full to wipe and recluster everything."
metadata:
  version: 1.0.0
---

# Consolidate — Batch Theme Clustering

Clusters notes into themes (3+ notes per theme), names them via Haiku, and
populates `themes` / `theme_members`. The default run is a NON-destructive
seeder over unassigned notes; `--full` wipes and reclusters everything.

**Tools needed:** Bash

## Procedure

### Step 1 — Parse arguments

- no args / `--full`  → Step 2 (consolidate)
- `stats`             → Step 3
- `split <id>`        → Step 4; bind `THEME_ID=<id>` from the user's numeric argument
- `merge <a> <b>`     → Step 5; bind `A=<a>` and `B=<b>` from the two numeric arguments

### Step 2 — Consolidate (seed or full)

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
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
from consolidate_cli import run_consolidate; run_consolidate(full=(len(sys.argv) > 1 and sys.argv[1] == "--full"))
' "$@"
```

Report the `UNASSIGNED=` (or `SCANNED=` for `--full`), `CREATED=`, and
`THEMES_TOTAL=` lines. If `CREATED=0`, tell the user no clusters of 3+ similar
notes were found (themes need at least 3 notes above the similarity threshold).

### Step 3 — Stats

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
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
from consolidate_cli import run_stats; run_stats()
'
```

Present `THEMES=`, `MEMBERS=`, `UNASSIGNED=`, the `LARGEST` rows, and any `NUDGE`.

### Step 4 — Split

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
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
from consolidate_cli import run_split; run_split(int(sys.argv[1]))
' "$THEME_ID"
```

Report `SPLIT` or `NO_SPLIT`.

### Step 5 — Merge

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
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
from consolidate_cli import run_merge; run_merge(int(sys.argv[1]), int(sys.argv[2]))
' "$A" "$B"
```

Report `MERGED` or the error.
