---
name: retro
description: "Generates honest session retrospectives analyzing what worked, what didn't, key learnings, and actionable process improvements. Use when: (1) /retro command at end of session, (2) user wants to reflect on session quality and outcomes."
metadata:
  version: 1.3.0
---

# Retro — Generate Honest Session Retrospective

Analyze the current conversation candidly and save a structured retrospective to the Obsidian vault. The goal is honest reflection — not self-congratulation — so future sessions can improve.

**Tools needed:** Bash, Write, Read

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Step 1 — Read config

Run:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys, os
import glob; sys.path.insert(0, max(glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")), default="hooks"))
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

If the file does not exist or is invalid JSON, tell the user:

> Config not found. Please run `/obsidian-setup` first to configure your Obsidian vault.

Stop here if config is missing.

### Step 2 — Validate vault access

Run:

```bash
test -d "$VAULT_PATH/$INSIGHTS_FOLDER" && test -w "$VAULT_PATH/$INSIGHTS_FOLDER" && echo "OK" || echo "FAIL"
```

If FAIL, tell the user:

> The insights folder `$VAULT_PATH/$INSIGHTS_FOLDER` does not exist or is not writable. Run `/obsidian-setup` to fix this.

Stop here if FAIL.

### Step 3a — Discover full session evidence

The active conversation buffer only covers the post-compact half of long sessions. Before drafting the analysis, gather every artifact the active session has already written to the vault.

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
mkdir -p "$HOME/.claude/obsidian-brain" && chmod 700 "$HOME/.claude/obsidian-brain"
_OB_BUNDLE="$HOME/.claude/obsidian-brain/retro-bundle-$$.json"
_OB_ERR="$HOME/.claude/obsidian-brain/retro-bundle-$$.err"
python3 -c '
import sys, os, json, glob
sys.path.insert(0, max(glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")), default="hooks"))
from obsidian_utils import load_config, get_session_context, gather_session_evidence
c = load_config()
ctx = get_session_context(c["vault_path"], c.get("sessions_folder", "claude-sessions"))
bundle = gather_session_evidence(
    c["vault_path"],
    c.get("sessions_folder", "claude-sessions"),
    c.get("insights_folder", "claude-insights"),
    ctx["session_id"], ctx["project"],
)
bundle["_ctx"] = ctx
print(json.dumps(bundle))
' >"$_OB_BUNDLE" 2>"$_OB_ERR"
_OB_RC=$?
if [ $_OB_RC -ne 0 ]; then
  _OB_ERRMSG="$([ -f "$_OB_ERR" ] && head -c 500 "$_OB_ERR" || echo "")"
  _OB_RC="$_OB_RC" _OB_ERRMSG="$_OB_ERRMSG" python3 -c "
import os, json
rc = os.environ.get('_OB_RC', '?')
errmsg = os.environ.get('_OB_ERRMSG', '')
print(json.dumps({
  'session_id': 'unknown',
  'snapshots': [],
  'insights': [],
  'decisions': [],
  'error_fixes': [],
  'discovery_errors': [f'evidence helper crashed (exit={rc}): {errmsg[:500]}'],
  '_ctx': {'session_id': 'unknown', 'hash': 'unknown', 'project': 'unknown', 'session_note_name': 'unknown'},
}))
"
else
  cat "$_OB_BUNDLE"
fi
rm -f "$_OB_BUNDLE" "$_OB_ERR"
```

Parse the JSON output. The bundle has these fields: `session_id`, `snapshots`, `insights`, `decisions`, `error_fixes`, `discovery_errors`, and `_ctx` (the cached `get_session_context()` result reused by Step 5).

**Empty-bundle fallback.** If `bundle["_ctx"]["session_id"] == "unknown"` AND `bundle["discovery_errors"] == []`, print:

> Note: no prior-session evidence found — falling back to active-conversation-only retro.

…and proceed with Step 3 using only the active conversation buffer. Do not include the `## Evidence Consulted` section in Step 4 in that case.

**Helper crash / partial failure.** If `bundle["discovery_errors"]` is non-empty, do **not** silently fall back to "no prior-session evidence found." Instead emit:

> ⚠️ Evidence discovery partially or fully failed. Some vault artifacts may be missing from this retro. See the discovery-errors warning surfaced in Step 6 for details.

Then proceed with whatever evidence was collected (possibly none).

**Discovery errors.** If `bundle["discovery_errors"]` is non-empty, remember the list — it will be surfaced after the preview in Step 6.

### Step 3 — Analyze the session honestly

Be **candid**, not defensive or self-congratulatory. This retro draws on two sources: the current session's pre-compact snapshots (the Step 3a bundle) and the live post-compact conversation. Work them in that order — mine the snapshots first (3.0 below), then review the live conversation.

**When the bundle from Step 3a is non-empty, mine the snapshots BEFORE analyzing the live conversation.** The pre-compact snapshot arcs almost always hold more decision points and dead ends than the vivid post-compact buffer, and are the evidence most easily skimmed. The pre-pass below is **mandatory** — running it first is what gives snapshots equal-or-higher priority than the live buffer.

#### 3.0 — Mine each snapshot FIRST (MANDATORY)

For **each** entry in `bundle["snapshots"]`, emit a short **visible** digest so the pass leaves an auditable trace — do not silently "consider" snapshots; print the digest:

```
Snapshot [<hhmmss>] <stem> — verdict: RELEVANT | EARLIER-ARC/UNRELATED
  (if RELEVANT)
    - decision points: ...
    - abandoned approaches / dead ends: ...
    - user corrections / redirects: ...
    - anything the post-compact buffer alone would not reveal: ...
  (if EARLIER-ARC/UNRELATED)
    - reason (e.g. "distinct earlier /ship arc for #NN, not part of this retro")
```

**Verdict rules:**
- **Default to `RELEVANT`.** Only mark a snapshot `EARLIER-ARC/UNRELATED` when it is *unmistakably* about a different, self-contained earlier task that is not part of the work this retrospective covers. This bias keeps the exclusion path from becoming a new way to drop content.
- Judge relevance by topical continuity with what this retro is about (the live buffer's focus + the most recent arc). A long session can span several `/ship` arcs that each already got their own retro — earlier arcs' snapshots are `EARLIER-ARC/UNRELATED`.
- If `bundle["snapshots"]` is empty, skip this pre-pass; emit `No current-session snapshots — skipping the snapshot pre-pass.` only if Step 3a did not already print its no-evidence fallback notice (do not print a duplicate).

Then weight the analysis by the two halves' decision density: if the pre-compact half ran 6 hours and the post-compact half ran 90 minutes, "What Didn't Work" should reflect that. Treat every `RELEVANT` snapshot body and the insight/decision/error-fix bodies as **first-class evidence**, not background context — every `RELEVANT` snapshot's findings must surface in the sections below.

The **"What Didn't Work"** section is the MOST valuable part of this retrospective — invest the most analysis there.

Evaluate the session across these five dimensions:

1. **What approaches worked?** — Successful strategies, good tool choices, efficient workflows, moments where the approach was clearly right.
2. **What didn't work?** — Be specific: wrong assumptions that led to dead ends, approaches that were abandoned partway through, time wasted on the wrong path, tools that failed or were misused, misunderstandings of the user's intent, overcomplicated solutions when a simple one existed, factual errors or hallucinations Claude produced.
3. **What did the user correct or redirect?** — Any moment the user said "no, that's wrong" or steered the conversation back — these are especially valuable signals.
4. **Key learnings** — Non-obvious insights that would be genuinely useful in future sessions. Not generic advice; specific to what happened here.
5. **Process improvements** — Concrete and actionable changes. Not vague ("be more careful") but specific ("check existing tests before writing new ones", "ask for the schema before generating SQL").

### Step 4 — Structure the retrospective

Draft the note body using this exact structure:

```markdown
## Evidence Consulted
- Active conversation: <N> messages (post-compact buffer)
- Snapshots (RELEVANT only): <K> file(s)
  - [[<stem-1>]] (<hhmmss>, <trigger>)
  - [[<stem-2>]] (<hhmmss>, <trigger>)
- Insights: <K> file(s)
  - [[<stem-1>]] — <title>
- Decisions: <K> file(s)
  - [[<stem-1>]] — <title>
- Error-fixes: <K> file(s)
  - [[<stem-1>]] — <title>

## What Went Well
- <specific thing that worked, with enough context to be meaningful>

## What Didn't Work
- <dead end: what was tried, why it failed, time impact>
- <wrong assumption: what was assumed, what was actually true>
- <user correction: what Claude did wrong, what user redirected to>

## Key Learnings
- <non-obvious insight with enough context to be useful later>

## Process Improvements
- [ ] <specific actionable change for future sessions>
```

**Rules for `## Evidence Consulted`:**
- List **only snapshots marked `RELEVANT`** in the Step 3.0 pre-pass. `EARLIER-ARC/UNRELATED` snapshots are excluded here — their exclusion rationale already lives in the 3.0 digest.
- **Coverage:** every `RELEVANT` snapshot must be represented in the analysis. If a `RELEVANT` snapshot genuinely yielded no distinct dead-ends beyond the live buffer, say so explicitly in "What Didn't Work" (e.g. `- [[stem]]: no distinct dead-ends beyond the live buffer`) rather than silently omitting it.
- Omit any list line whose count is 0 (no `... : 0 file(s)` zero-count noise).
- Omit the entire `## Evidence Consulted` section if the empty-bundle fallback fired in Step 3a.
- Wikilinks (`[[stem]]`) preserve Obsidian backlinks and round-trip through the FTS index.
- Active-conversation message count is approximate — the count visible to the model when /retro fires; an order-of-magnitude figure is fine.

**Important:** "What Didn't Work" should have MORE items than "What Went Well." If the session went smoothly with no obvious failures, still find at least one improvement opportunity — there is always something.

### Step 5 — Derive session ID and backlinks

The Step 3a bundle already carries the cached session context as `bundle["_ctx"]`. Read these fields directly:

- `SESSION_ID` = `bundle["_ctx"]["session_id"]`
- `HASH` = `bundle["_ctx"]["hash"]`
- `PROJECT` = `bundle["_ctx"]["project"]`
- `SESSION_NOTE` = `bundle["_ctx"]["session_note_name"]`

**Important:** If `SESSION_ID` is `unknown`, use `unknown` for `source_session` and omit `source_session_note` entirely.

### Step 6 — Show preview and ask for edits

Present the full note to the user including frontmatter:

```
---
type: claude-retro
date: YYYY-MM-DD
created_at: <ISO-8601-UTC>
source_session: <current-session-id>
source_session_note: "[[<session-note-filename>]]"
project: <project-name>
tags:
  - claude/retro
  - claude/project/<project-name>
---

# Session Retrospective: <project-name> (<date>)

## Evidence Consulted
...

## What Went Well
...

## What Didn't Work
...

## Key Learnings
...

## Process Improvements
...
```

Where:
- `YYYY-MM-DD` is today's date
- `<ISO-8601-UTC>` is the current UTC timestamp at second precision. Get it via:
  ```bash
  python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat(timespec="seconds"))'
  ```
  Example: `2026-04-24T18:42:11+00:00`
- `<current-session-id>` and `<session-note-filename>` are derived from Step 5
- `<project-name>` is derived from the current working directory name (basename of the git repo root or cwd)
- The `source_session_note` field creates an Obsidian backlink from the retro to its source session

Ask the user:

> Preview above. Would you like to:
> - **save** as-is
> - **edit content** — tell me what to change
> - **cancel** — discard this note

Wait for the user's response. Apply any requested edits and show the updated preview. Repeat until the user says **save** or **cancel**.

**Discovery errors.** If `bundle["discovery_errors"]` is non-empty, after the preview but before the save/edit/cancel prompt, emit:

> ⚠️  <N> file(s) could not be read during evidence discovery:
>   - `<basename>`: <reason>
>
> The retro proceeds with the readable evidence.

Each `bundle["discovery_errors"]` entry has the form `"<filename>: <exception>"`. Split on the first `: ` to separate `<basename>` from `<reason>` for the bullet rendering. This is informational only and does not block save.

If cancel, stop here.

### Step 7 — Generate filename and write

Construct the filename:

1. **Date:** `YYYY-MM-DD` (today)
2. **Slug:** `retro` (fixed — no title slug needed for retrospectives)
3. **Hash:** 4-character hex hash from current timestamp:
   - macOS: `date +%s | md5 | cut -c1-4`
   - Linux: `date +%s | md5sum | cut -c1-4`

Final filename: `YYYY-MM-DD-retro-<hash>.md`

Example: `2026-04-05-retro-a3f2.md`

Run:

```bash
mkdir -p "$VAULT_PATH/$INSIGHTS_FOLDER"
```

Then use the **Write** tool to write the full note (frontmatter + body) to:

```
$VAULT_PATH/$INSIGHTS_FOLDER/YYYY-MM-DD-retro-<hash>.md
```

Then set permissions:

```bash
chmod 644 "$VAULT_PATH/$INSIGHTS_FOLDER/YYYY-MM-DD-retro-<hash>.md"
```

**Arm the classification gate.** Immediately after writing the note, mark classification as pending. This arms the `obsidian_retro_gate.py` **Stop** hook, which blocks the turn from ending until Step 7.5 clears the gate — so classification can no longer be silently skipped:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys, os, glob
sys.path.insert(0, max(glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")), default="hooks"))
from obsidian_utils import mark_retro_classification_pending
print(mark_retro_classification_pending(sys.argv[1], sys.argv[2]))
' "<current-session-id>" "$VAULT_PATH/$INSIGHTS_FOLDER/<filename>"
```

(`<current-session-id>` is the value derived in Step 5. The gate is keyed on it and fails open: if the id can't be resolved the gate stays inactive — never blocking the session.)

### Step 7.5 — Classify and file process improvements (DO THIS BEFORE Step 8)

The retro is **not done** when the file is written. The literal next action after the `chmod 644` is to act on what the retro surfaced — **do not print the "saved" confirmation until this step is complete.** Step 7 armed a Stop-hook gate that blocks the turn from ending until you clear it here, so this is enforced, not merely advised.

1. **Extract** every item from the just-written **Process Improvements** and **Key Learnings** sections.
2. **Classify** each item into exactly one bucket:
   - **Concrete deliverable** — a code change, doc edit, new flag, or SKILL.md section with a definable "done" state. If this project tracks work in an issue tracker, file it there (with `gh issue create`, sub-classified by target repo); otherwise record it wherever the project tracks TODOs.
   - **Behavioral discipline** — a do/don't-next-time rule ("verify before claiming X", "grep before citing Y"). Trackers don't enforce behavior; durable notes do. If a persistent memory index is available (e.g. Claude Code's `MEMORY.md`), add or extend an entry there — prefer an `## Update (date)` section on an existing entry over a duplicate; otherwise capture it as a vault insight with `/compress`.
   - **Skip / already covered** — informational learnings that aren't actionable, plus anything already tracked elsewhere (cite the artifact). Most **Key Learnings** belong here unless genuinely actionable; do not manufacture issues from informational insights.
3. **Confirm via an in-turn tool, then file.** Surface the proposed classification (each item → bucket → target) and get the user's go-ahead **using `AskUserQuestion`** — do *not* end your turn to ask, because the Step 7 gate will block a turn-end before classification is done. In a non-interactive / auto-run context, file the clear-cut items directly and surface only judgment calls (which repo, which priority) for a one-line confirm; never skip tracking. After filing **2 or more** issues, run `/github-issue-triage` (when available) for labels/priority.
4. **Clear the gate.** Once every item is filed — or the user declined, or there were no actionable items — clear the gate so the turn can end:

   ```bash
   cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
   python3 -c '
   import sys, os, glob
   sys.path.insert(0, max(glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/obsidian-brain/*/hooks")), default="hooks"))
   from obsidian_utils import clear_retro_classification_pending
   print("cleared" if clear_retro_classification_pending(sys.argv[1]) else "no-gate")
   ' "<current-session-id>"
   ```
5. Only after the gate is cleared **and** the filed actions are reflected in the user-facing summary do you proceed to Step 8.

**Hard-fail signal:** if you are about to print "Retrospective saved!" with the gate still armed, you have skipped this step. The Stop hook will catch it — but classify proactively rather than relying on the block.

### Step 8 — Confirm

Print:

> **Retrospective saved!**
> - File: `$VAULT_PATH/$INSIGHTS_FOLDER/<filename>`
> - Tags: `claude/retro`, `claude/project/<name>`
> - Filed from Process Improvements / Key Learnings: `<N>` GH issue(s), `<M>` memory entr(ies) (or "none — no actionable items")
> - Open in Obsidian to review and track process improvements over time.
