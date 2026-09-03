---
name: retro
description: "Generates honest session retrospectives analyzing what worked, what didn't, key learnings, and actionable process improvements. Use when: (1) /retro command at end of session, (2) user wants to reflect on session quality and outcomes."
metadata:
  version: 1.4.0
---

# Retro — Generate Honest Session Retrospective

Analyze the current conversation candidly and save a structured retrospective to the Obsidian vault. The goal is honest reflection — not self-congratulation — so future sessions can improve.

**Tools needed:** Bash, Read

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Step 1 — Read config

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

**Detect a compact boundary and recover the prior session id (#184).** Before running the helper below:
1. Scan the active conversation for a continuation preamble — the phrase "This session is being continued from a previous conversation" or "conversation ... ran out of context".
2. If present, find the transcript path it cites, of the shape `~/.claude/projects/<project-dir>/<session-id>.jsonl` — the basename minus `.jsonl` IS the prior session id.
3. If no such boundary is present, pass **no** extra ids — invoke the helper below with zero trailing arguments.

When a prior id is recovered, pass it as a **shell-quoted positional argument** after the `python3 -c` script below, one argument per id — never interpolated into the Python source itself (repo security rule). The script reads them from `sys.argv[1:]`, so it works whether zero or several are recovered.

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
mkdir -p "$HOME/.claude/obsidian-brain" && chmod 700 "$HOME/.claude/obsidian-brain"
_OB_BUNDLE="$HOME/.claude/obsidian-brain/retro-bundle-$$.json"
_OB_ERR="$HOME/.claude/obsidian-brain/retro-bundle-$$.err"
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
_ID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,63}$")
_raw_ids = sys.argv[1:]
_also_ids = [a for a in _raw_ids if _ID_RE.match(a)]
_rejected = len(_raw_ids) - len(_also_ids)
if _rejected:
    # Loud, never silent: a rejected token (e.g. a leftover placeholder that
    # was not stripped) used to just vanish from _also_ids and degrade to
    # the empty post-compact bundle #184 exists to eliminate, with no
    # signal. Exit non-zero so this routes through the existing crash path
    # below into discovery_errors and the "evidence discovery partially or
    # fully failed" banner. Never the token itself in the message — this
    # is fed straight into the model context via the transcript.
    print(f"rejected {_rejected} invalid also-session-id argument(s)", file=sys.stderr)
    sys.exit(1)
from obsidian_utils import load_config, get_session_context, gather_session_evidence, resolve_source_session_note
c = load_config()
ctx = get_session_context(c["vault_path"], c.get("sessions_folder", "claude-sessions"))
bundle = gather_session_evidence(
    c["vault_path"],
    c.get("sessions_folder", "claude-sessions"),
    c.get("insights_folder", "claude-insights"),
    ctx["session_id"], ctx["project"],
    also_session_ids=_also_ids,
)
ctx["resolved_source_session_note"] = resolve_source_session_note(
    ctx["session_note_name"], ctx["session_id"],
    c["vault_path"], c.get("sessions_folder", "claude-sessions"),
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
  'session_ids': [],
  'snapshots': [],
  'insights': [],
  'decisions': [],
  'error_fixes': [],
  'retros': [],
  'discovery_errors': [f'evidence helper crashed (exit={rc}): {errmsg[:500]}'],
  '_ctx': {'session_id': 'unknown', 'hash': 'unknown', 'project': 'unknown', 'session_note_name': 'unknown', 'resolved_source_session_note': ''},
}))
"
else
  cat "$_OB_BUNDLE"
fi
rm -f "$_OB_BUNDLE" "$_OB_ERR"
```

The canonical block above ships with zero trailing arguments, so the common no-compact-boundary case is correct by copying it verbatim — do not append a literal placeholder string. When a compact boundary IS detected and a prior id recovered, append it as a shell-quoted positional argument after the closing `'`, one argument per recovered id (per the "Detect a compact boundary" step above); the script rejects any argument that is not a bare hex-and-dash id, so a stray or malformed token fails loudly (see the "Helper crash / partial failure" handling below) rather than being silently dropped.

Parse the JSON output. The bundle has these fields: `session_id` (the primary id exactly as passed — may be `"unknown"`), `session_ids` (the ordered, de-duplicated ids **actually scanned** — may be non-empty even when `session_id` is `"unknown"`), `snapshots`, `insights`, `decisions`, `error_fixes`, `retros`, `discovery_errors`, and `_ctx` (the cached `get_session_context()` result reused by Step 5).

**Unresolved-session fallback.** If `bundle["session_ids"] == []` AND `bundle["discovery_errors"] == []`, print:

> Note: this session could not be identified (no Claude Code transcript resolves to the current directory), so session-scoped evidence could not be looked up — falling back to active-conversation-only retro. This is **not** evidence that the vault is empty.

Gate on `session_ids`, not on the resolver (`bundle["_ctx"]["session_id"]`): resolver failure and preamble-id recovery are independent. The continuation preamble carrying a prior session id lives in the buffer regardless of whether the transcript resolves, so `session_ids` can be non-empty even when `_ctx["session_id"]` is `"unknown"` — that case is the branch immediately below, not this one.

**Recovered-under-prior-id note.** If `bundle["_ctx"]["session_id"] == "unknown"` AND `bundle["session_ids"] != []`, print:

> Note: this session could not be identified, but evidence was recovered under prior session id `<id>` (the last entry in `bundle["session_ids"]`).

Then proceed normally — run the mandatory snapshot pre-pass in Step 3.0 and include `## Evidence Consulted` in Step 4 — exactly as if the session had resolved. Only the unresolved-session fallback above and the empty-bundle fallback below skip the pre-pass; this case does not.

**Empty-bundle fallback.** If the session id IS resolved (`bundle["_ctx"]["session_id"] != "unknown"`), `bundle["discovery_errors"] == []`, and `snapshots`, `insights`, `decisions`, `error_fixes` and `retros` are all empty, print:

> Note: no prior-session evidence found — falling back to active-conversation-only retro.

Keep the three apart. "No evidence found" asserts a fact about the **vault** ("there is nothing"); an unresolved session id is a fact about the **resolver** ("I could not identify this session"), and reporting the second as the first is what let a session-resolution failure read as a genuinely fresh session (#260 I4). The unresolved-session and empty-bundle fallbacks behave identically from here — proceed with Step 3 using only the active conversation buffer, and do not include the `## Evidence Consulted` section in Step 4 in either case. The recovered-under-prior-id note above is different: evidence exists, so it takes the full evidence-mining path instead.

**Helper crash / partial failure.** If `bundle["discovery_errors"]` is non-empty, do **not** silently fall back to "no prior-session evidence found." Instead emit:

> ⚠️ Evidence discovery partially or fully failed. Some vault artifacts may be missing from this retro. See the discovery-errors warning surfaced in Step 6 for details.

Then proceed with whatever evidence was collected (possibly none).

**Discovery errors.** If `bundle["discovery_errors"]` is non-empty, remember the list — it will be surfaced after the preview in Step 6.

**Prior-retro scoping (#285).** If `bundle["retros"]` is non-empty, take the **last** entry (ascending filename order = most recent) and scope this retro to the arc **after** it — the work this retrospective covers picks up where that prior retro left off. A `claude-retro` entry is a **boundary marker**, not evidence: it is deliberately excluded from the "every `RELEVANT` snapshot must surface in the analysis" coverage rule that governs snapshots in Step 3.0 below. Do not mine a prior retro's own body for content to fold into this one's findings — use it only to know where to stop looking backward.

### Step 3 — Analyze the session honestly

Be **candid**, not defensive or self-congratulatory. This retro draws on the current session's pre-compact evidence (the Step 3a bundle — snapshots plus insights/decisions/error-fixes) and the live post-compact conversation. Work the bundle first — mine the snapshots in 3.0 below — then review the live conversation.

**When the bundle from Step 3a is non-empty, mine the snapshots BEFORE analyzing the live conversation.** The pre-compact snapshot arcs almost always hold more decision points and dead ends than the vivid post-compact buffer, and are the evidence most easily skimmed. The pre-pass below is **mandatory** — running it first is what gives snapshots equal-or-higher priority than the live buffer.

#### 3.0 — Mine each snapshot FIRST (MANDATORY)

For **each** entry in `bundle["snapshots"]`, emit a short **visible** digest so the pass leaves an auditable trace — do not silently "consider" snapshots; print the digest:

```
Snapshot [<hhmmss>] <stem> [(pre-compact arc)] — verdict: RELEVANT | EARLIER-ARC/UNRELATED
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
- Judge relevance by topical continuity with what this retro is about. `/retro` takes no arc argument — it reflects on the current session as a whole — so infer the focus from the live buffer and the most recent arc, and treat `most recent arc` as a heuristic, not a hard boundary. Only a clearly-concluded earlier `/ship` arc that already got its own retro is `EARLIER-ARC/UNRELATED`; when in any doubt, mark `RELEVANT` (the default-to-`RELEVANT` bias above governs). Every excluded snapshot is still recorded in `## Evidence Consulted` (Step 4), so a wrong exclusion stays visible to the user in the Step 6 preview rather than being silently lost.
- If `bundle["snapshots"]` is empty, skip this pre-pass; emit `No current-session snapshots — skipping the snapshot pre-pass.` only if Step 3a did not already print its no-evidence fallback notice (do not print a duplicate).
- Print `(pre-compact arc)` after `<stem>` when that snapshot's `session_id` field differs from `bundle["_ctx"]["session_id"]` (#184) — it labels a snapshot recovered via `also_session_ids` from a prior session, purely informational, and carries no relevance verdict of its own; judge `RELEVANT` vs `EARLIER-ARC/UNRELATED` the same way regardless of which arc it came from.

Then weight the analysis by the two halves' decision density: if the pre-compact half ran 6 hours and the post-compact half ran 90 minutes, "What Didn't Work" should reflect that. Estimate this weighting only from `RELEVANT` snapshots — exclude the time span covered by any `EARLIER-ARC/UNRELATED` snapshot from both the duration estimate and the resulting weight. Treat every `RELEVANT` snapshot body and the insight/decision/error-fix bodies as **first-class evidence**, not background context — every `RELEVANT` snapshot's findings must surface in the sections below.

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
- Sessions scanned: <id-a> (current), <id-b> (pre-compact)
- Active conversation: <N> messages (post-compact buffer)
- Snapshots — RELEVANT: <K> file(s)
  - [[<stem-1>]] (<hhmmss>, <trigger>)
  - [[<stem-2>]] (<hhmmss>, <trigger>) (pre-compact arc)
- Snapshots — excluded as earlier-arc: <M> file(s)
  - [[<stem-x>]] (<hhmmss>) — <one-line exclusion reason from the 3.0 digest>
- Insights: <K> file(s)
  - [[<stem-1>]] — <title>
- Decisions: <K> file(s)
  - [[<stem-1>]] — <title>
- Error-fixes: <K> file(s)
  - [[<stem-1>]] — <title>
- Prior retros: <K> file(s)
  - [[<stem>]] — <title>
- Scope: the arc after [[<most-recent-retro-stem>]] — <what is not re-litigated>

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
- Under `Snapshots — RELEVANT`, list the snapshots marked `RELEVANT` in the Step 3.0 pre-pass. Under `Snapshots — excluded as earlier-arc`, list every `EARLIER-ARC/UNRELATED` snapshot with its one-line reason. The 3.0 digest is console-only, so persisting excluded snapshots here is what keeps an exclusion auditable in the saved note (and catchable by the user in the Step 6 preview) — never rely on the digest alone.
- **Coverage:** every `RELEVANT` snapshot must be represented in the analysis. If a `RELEVANT` snapshot genuinely yielded no distinct dead-ends beyond the live buffer, say so explicitly in "What Didn't Work" (e.g. `- [[stem]]: no distinct dead-ends beyond the live buffer`) rather than silently omitting it.
- **`Sessions scanned` (#184):** render this line only when `bundle["session_ids"]` has more than one entry — omit it entirely for a single-id scan. List each id from `bundle["session_ids"]` in order, labelling the one matching `bundle["_ctx"]["session_id"]` `(current)` and the rest `(pre-compact)`.
- **`(pre-compact arc)` marker (#184):** for each snapshot listed under `Snapshots — RELEVANT` or `Snapshots — excluded as earlier-arc`, append ` (pre-compact arc)` when that snapshot's `session_id` field differs from `bundle["_ctx"]["session_id"]`; omit the marker when it matches (the common, single-arc case).
- **`Prior retros` / `Scope` (#285):** these follow the existing zero-count omission rule below, and are **not** among the two always-rendered `Snapshots —` lines — omit both when `bundle["retros"]` is empty. When non-empty, `Prior retros` lists every entry in `bundle["retros"]`; `Scope` names only the **last** one (most recent) per the Step 3a prior-retro-scoping rule, with a short phrase for what is not being re-litigated (e.g. "the auth refactor already covered in that retro").
- Omit any list line whose count is 0 (no `... : 0 file(s)` zero-count noise) — **except** the two `Snapshots —` lines: whenever `bundle["snapshots"]` was non-empty, render BOTH lines even at count 0, so an all-excluded session is never indistinguishable from a no-snapshot one.
- Omit the entire `## Evidence Consulted` section if the empty-bundle fallback fired in Step 3a.
- Wikilinks (`[[stem]]`) preserve Obsidian backlinks and round-trip through the FTS index.
- Active-conversation message count is approximate — the count visible to the model when /retro fires; an order-of-magnitude figure is fine.

**Important:** "What Didn't Work" should have MORE items than "What Went Well." If the session went smoothly with no obvious failures, still find at least one improvement opportunity — there is always something.

### Step 5 — Derive session ID and backlinks

The Step 3a bundle already carries the cached session context as `bundle["_ctx"]`. Read these fields directly:

- `SESSION_ID` = `bundle["_ctx"]["session_id"]`
- `HASH` = `bundle["_ctx"]["hash"]`
- `PROJECT` = `bundle["_ctx"]["project"]`
- `SESSION_NOTE` = `bundle["_ctx"]["resolved_source_session_note"]` (**not** `session_note_name` — this is already guarded: it is the note name in the normal case (the target either doesn't exist yet — SessionEnd hasn't written it, this is a forward reference just like the PreCompact snapshot backlink — or exists and agrees), and `""` ONLY when the target note exists, parses, and its own `session_id` frontmatter CONTRADICTS `SESSION_ID` — that is what stops a `source_session_note` backlink from pointing at a note that disagrees with the stamped `source_session` (#330))

**Important:** If `SESSION_ID` is `unknown`, use `unknown` for `source_session` and omit `source_session_note` entirely. Also omit `source_session_note` whenever `SESSION_NOTE` is empty (`""`), even when `SESSION_ID` is known — that means the guard above found a genuine contradiction and rejected the link (omit this line entirely when the resolved note is empty; do not write `source_session_note: ""`).

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

Run the note-writer CLI, piping the full note (frontmatter + body) in on stdin. It creates `$INSIGHTS_FOLDER` if needed and writes the file atomically at mode `0o600` — no `mkdir`/`chmod` needed. **Two rules for the heredoc terminator, both load-bearing.** (1) It must stay **quoted** (`<<'OB_NOTE_EOF_<eof4>'`) — do not drop the quotes in a future edit. (2) It must be **unique per invocation**: substitute the same 4 random hex characters for `<eof4>` in BOTH the `<<'OB_NOTE_EOF_<eof4>'` opener and the terminator line, then confirm that **no line of the content you are about to emit is exactly that terminator** — if one is, pick different hex characters and re-check. **Never** replace this with a fixed delimiter. Quoting stops `$`/backtick expansion but does NOT stop early termination: a line equal to the terminator at column 0 ends the heredoc there, silently truncating the content AND handing everything after it to the shell as commands to execute. Notes written by this plugin routinely quote these very blocks, so a fixed terminator is a live hazard, not a theoretical one. **Self-check before you emit the block: if the terminator still contains `<` or `>`, you have not substituted it.** Stop and substitute it — the literal `<eof4>` form appears at column 0 inside these SKILL.md blocks themselves, so a note quoting one of them collides all over again, and nothing on the shell side can catch that. The `HOOKS=` line below checks the marketplace-registered directory-source install location FIRST (#278 — on a local checkout that is what loads, not the released cache), and only falls back to the plugin cache, where it sorts versions **numerically** (a plain `max()` is lexicographic and picks `3.9.0` over `3.10.0`, resolving to a cache with no `note_writer.py`); the `test -f` line turns a stale/incomplete cache into the documented `ERROR:` shape instead of a raw Python `can't open file` message. An unquoted delimiter lets the shell expand `$` variables and backtick commands embedded in the note body, silently corrupting it:

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
test -f "$HOOKS/note_writer.py" || { echo "ERROR: note_writer.py not found under $HOOKS - resolution checks the marketplace registered install location first, then falls back to the plugin cache; neither path produced a hooks directory containing it. Verify the obsidian-brain install resolved at $HOOKS is complete (git pull for a directory-source checkout, or run /plugin marketplace update for a cache install), then retry." >&2; exit 1; }
python3 "$HOOKS/note_writer.py" write "$VAULT_PATH" "$INSIGHTS_FOLDER" "YYYY-MM-DD-retro-<hash>.md" <<'OB_NOTE_EOF_<eof4>'
---
type: claude-retro
...
---

# Session Retrospective: ...
...
OB_NOTE_EOF_<eof4>
```

On success this prints `OK: <absolute path>` — that is the file at `$VAULT_PATH/$INSIGHTS_FOLDER/<filename>`. On failure it prints `ERROR: <reason>` to stderr and exits non-zero; surface that message to the user and stop here (do not proceed to arming the classification gate below on a failed write).

If the error is `note already exists`, the 4-hex filename hash collided with a note written in the same second. Regenerate the hash (Step 7's command), rebuild the filename, and retry the write **once**. If it fails again for any reason, surface the error and stop — do not loop.

**Arm the classification gate.** Immediately after writing the note, mark classification as pending. This arms the `obsidian_retro_gate.py` **Stop** hook, which blocks the turn from ending until Step 7.5 clears the gate — so classification can no longer be silently skipped:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys, os, glob
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
from obsidian_utils import mark_retro_classification_pending
print(mark_retro_classification_pending(sys.argv[1], sys.argv[2]))
' "<current-session-id>" "$VAULT_PATH/$INSIGHTS_FOLDER/<filename>"
```

(`<current-session-id>` is the value derived in Step 5. The gate is keyed on it and fails open on an unusable id: if `session_id` is empty or `"unknown"` the gate stays inactive — never blocking the session.)

**Check the printed output before continuing.** If it starts with `Failed:`, the gate did **NOT** arm — print that line to the user in the transcript so the refusal is visible (do not let it scroll past silently). Treat Step 7.5 below as **unenforced but still mandatory**: nothing will block the turn from ending if you skip it, so you must not skip it anyway. Do not print a "saved" confirmation that implies the classification step is enforced when it is not.

### Step 7.5 — Classify and file process improvements (DO THIS BEFORE Step 8)

The retro is **not done** when the file is written. The literal next action after arming the classification gate is to act on what the retro surfaced — **do not print the "saved" confirmation until this step is complete.** If Step 7's gate armed successfully (the printed output did not start with `Failed:`), it is a Stop-hook gate that blocks the turn from ending until you clear it here, so this is enforced, not merely advised. If Step 7's gate failed to arm, there is nothing blocking the turn — treat this step as **unenforced but still mandatory**, per Step 7's own instruction.

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
