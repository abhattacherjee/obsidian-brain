---
name: emerge
description: "Surface unnamed patterns across vault notes. Use when: (1) /emerge for last 30 days, (2) /emerge 14d for custom window, (3) /emerge this week."
metadata:
  version: 1.0.0
---
# Emerge — Discover Patterns Across Your Obsidian Vault

**Tools needed:** Bash, Agent, Write, Read

## Procedure

### Step 0 — Create task manifest

```
TaskCreate: subject="Collect and upgrade vault corpus", activeForm="Collecting vault corpus"
TaskCreate: subject="Analyze patterns across notes", activeForm="Analyzing patterns"
TaskCreate: subject="Build emerge report", activeForm="Building report"
TaskCreate: subject="Write vault note", activeForm="Writing vault note"
TaskCreate: subject="Present results", activeForm="Presenting results"
```
Track task IDs. Set task #1 to `in_progress`.

### Step 1 — Parse args + upgrade + collect corpus
Parse DAYS: no arg=30, `Nd`/`N days`=N, `this week`=days since Monday.

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys, os
import glob; sys.path.insert(0, max(glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")), default="hooks"))
from obsidian_utils import load_config, upgrade_and_collect_corpus
c = load_config()
if not c.get("vault_path"):
    print("ERROR: vault_path not configured"); sys.exit(1)
out = os.path.expanduser("~/.claude/obsidian-brain/emerge-corpus.json")
status = upgrade_and_collect_corpus(c["vault_path"], c.get("sessions_folder", "claude-sessions"), c.get("insights_folder", "claude-insights"), int(sys.argv[1]), out)
print("VAULT=" + c["vault_path"])
print("INS=" + c.get("insights_folder", "claude-insights"))
print("STATUS=" + status)
' "$DAYS"
```

Parse STATUS (`OK:<total>:<upgraded>:<failed>` or `EMPTY:0:0:0`). EMPTY -> tell user to widen window, stop. Report upgrades. If `failed > 0` -> Step 1f, else Step 2. Mark task #1 `completed`.
### Step 1f — Sub-agent fallback (skip if no failures)
Spawn parallel sub-agents for failed notes using /recall Path C Wave 2-3 pattern (read note, write summary to temp, write back via `upgrade_note_with_summary`). Re-collect:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys, os, json, tempfile
import glob; sys.path.insert(0, max(glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")), default="hooks"))
from obsidian_utils import load_config, collect_vault_corpus
c = load_config()
corpus_json = collect_vault_corpus(c["vault_path"], c.get("sessions_folder", "claude-sessions"), c.get("insights_folder", "claude-insights"), int(sys.argv[1]))
out = os.path.expanduser("~/.claude/obsidian-brain/emerge-corpus.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(out), suffix=".tmp")
with os.fdopen(fd, "w") as f:
    f.write(corpus_json)
os.replace(tmp, out)
print("REFRESHED:" + str(json.loads(corpus_json).get("note_count", 0)))
' "$DAYS"
```

### Step 2 — Pattern synthesis
Set task #2 to `in_progress`. Spawn one Agent:
```
Agent({
  description: "Analyze vault corpus for cross-cutting patterns",
  prompt: "Read ~/.claude/obsidian-brain/emerge-corpus.json. Analyze and write to ~/.claude/obsidian-brain/emerge-analysis.md with these 5 categories:\n\n## Recurring Technical Patterns\nPatterns in approaches, tools, or architecture across 2+ sessions.\n\n## Error Clusters\nRepeated error types, common root causes, or failure modes.\n\n## Decision Trends\nDirectional shifts in technical decisions over time.\n\n## Cross-Project Connections\nShared themes between projects. Skip if only 1 project.\n\n## Emergent Practices\nImplicit conventions that formed organically but aren't documented.\n\nFor each pattern: descriptive name, 2-3 examples with note references, confidence (strong/moderate/tentative).\n\nWrite using the Write tool. Return ONLY: WRITTEN:~/.claude/obsidian-brain/emerge-analysis.md"
})
```

If no `WRITTEN:` response, report failure and stop. Mark task #2 `completed`.

### Step 3 — Build output + write note
```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys, os, json, datetime, hashlib
import glob; sys.path.insert(0, max(glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")), default="hooks"))
from obsidian_utils import load_config, write_vault_note
c = load_config()
vault, ins = c["vault_path"], c.get("insights_folder", "claude-insights")
with open(os.path.expanduser("~/.claude/obsidian-brain/emerge-corpus.json")) as f:
    corpus = json.load(f)
with open(os.path.expanduser("~/.claude/obsidian-brain/emerge-analysis.md")) as f:
    analysis = f.read()
today = datetime.date.today().isoformat()
projects = sorted(set(n.get("project", "") for n in corpus.get("notes", []) if n.get("project")))
src = ["[[" + os.path.splitext(n["file"])[0] + "]]" for n in corpus.get("notes", [])]
tags = ["claude/emerge"] + ["claude/project/" + p for p in projects]
fm = "---\ntype: claude-emerge\ndate: " + today + "\ndate_range: \"" + corpus.get("date_range", "") + "\"\nprojects:\n" + "\n".join("  - " + p for p in projects) + "\nsource_notes:\n" + "\n".join("  - \"" + s + "\"" for s in src) + "\nnote_count: " + str(corpus.get("note_count", 0)) + "\ntags:\n" + "\n".join("  - " + t for t in tags) + "\n---"
title = "# Emerge: Pattern Discovery (" + corpus.get("date_range", "") + ")"
header = "**Projects:** " + ", ".join(projects) + "\n**Notes analyzed:** " + str(corpus.get("note_count", 0))
body = fm + "\n\n" + title + "\n\n" + header + "\n\n" + analysis
h = hashlib.md5(today.encode()).hexdigest()[-4:]
filename = today + "-emerge-patterns-" + h + ".md"
if write_vault_note(vault, ins, filename, body):
    print("SAVED:" + os.path.join(vault, ins, filename))
    print("---REPORT---")
    print(analysis)
else:
    print("ERROR: write failed", file=sys.stderr); sys.exit(1)
os.remove(os.path.expanduser("~/.claude/obsidian-brain/emerge-corpus.json"))
os.remove(os.path.expanduser("~/.claude/obsidian-brain/emerge-analysis.md"))
' 2>&1
```

Parse `SAVED:<path>` and everything after `---REPORT---`. Mark tasks #3-#4 `completed`.

### Step 4 — Present to user
Display report prefixed with **Pattern Discovery Results:**. Confirm saved path. Mark task #5 `completed`.

## Edge Cases
- **No notes in window:** Suggest widening (e.g., `/emerge 60d`).
- **Only 1 project:** Sub-agent skips Cross-Project Connections.
- **Very large corpus (200+):** Python distills to ~25k tokens JSON, fits one sub-agent.
- **Config not found:** Tell user to run `/obsidian-setup` first.
