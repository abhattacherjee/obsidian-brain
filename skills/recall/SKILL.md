---
name: recall
description: "Pure read-only context resume — summarizes unsummarized notes and surfaces last-session context. Use `/check-items` to triage open items. Use when: (1) /recall command, (2) /recall <project-name>, (3) resuming work on a project and wanting prior context."
metadata:
  version: 1.7.0
---

# Recall — Load Project Context from Obsidian Vault

Searches the Obsidian vault for session notes and insights matching the current project, upgrades any unsummarized notes with AI summaries, and presents a concise context brief.

**Tools needed:** Bash, Grep, Read, Write

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Step 1 — Load config and derive project

Run a single call that loads config and derives the project name (saves one parent round):

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys, os
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
from obsidian_utils import load_config
c = load_config()
if not c.get("vault_path"):
    print("ERROR: vault_path not configured", file=sys.stderr)
    sys.exit(1)
project = os.path.basename(os.getcwd()).lower().replace(" ", "-")
print("VAULT=" + c["vault_path"])
print("SESS=" + c.get("sessions_folder", "claude-sessions"))
print("INS=" + c.get("insights_folder", "claude-insights"))
print("PROJECT=" + project)
print("PIPELINE=" + c.get("summary_pipeline", "auto"))
'
```

Parse each output line as KEY=VALUE, splitting on the first `=`. Also capture PIPELINE (defaults to "auto").

If the user passed a project name argument (e.g. `/recall my-project`), override `PROJECT` with that value.

If the output is empty or errors, tell the user:

> Config not found. Run `/obsidian-setup` first to configure your Obsidian vault.

Stop here if config is missing.

**Create the task manifest** for the full `/recall` flow:

```
TaskCreate: subject="Find unsummarized notes", activeForm="Searching for unsummarized notes"
TaskCreate: subject="Summarize unsummarized notes", activeForm="Summarizing notes"
TaskCreate: subject="Present read-only context brief", activeForm="Building and presenting context brief"
```

Track the returned task IDs — you will update them as each step completes. Immediately set task #1 to `in_progress` via TaskUpdate.

### Step 2 — Summarize unsummarized notes (deferred summarization, truncation-aware)

> ⚠️ **THIS STEP IS MANDATORY. DO NOT SKIP IT.**
>
> If Grep finds any file matching both `status: auto-logged` AND `project: $PROJECT`, you **must** produce an upgraded summary for every such file before proceeding to Step 3. "Skipping to save context" or "the other session covers it" is a bug, not an optimization — the user ran `/recall` specifically to get current-session context, and stale unsummarized notes are exactly what they asked you to fix.
>
> **Visibility requirement:** Before Step 3, emit a one-line status: `Step 2: processing N unsummarized note(s) for $PROJECT` (or `Step 2: no unsummarized notes for $PROJECT` if the intersection is empty). This makes the decision auditable in the tool trace.

Find unsummarized notes for this project in a single Python call (replaces multiple Grep rounds):

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys, os
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
from obsidian_utils import find_unsummarized_notes
print(find_unsummarized_notes(sys.argv[1], sys.argv[2], sys.argv[3]))
' "$VAULT_PATH" "$SESSIONS_FOLDER" "$PROJECT"
```

**Optional flags (#168 aged-note deferral):**
- If the user passed `--include-aged`, call with `include_aged=True` to include aged-out deferred notes:
  ```python
  find_unsummarized_notes(vault, sessions_folder, project, include_aged=True)
  ```
- If the user passed `--max-age-days N`, call with `aged_threshold_days=N` to override the config threshold:
  ```python
  find_unsummarized_notes(vault, sessions_folder, project, aged_threshold_days=N)
  ```

Parse the JSON output: `{"unsummarized": ["/path/to/note1.md", ...], "auto_fixed": N, "skipped_aged": [...]}`.

The function handles project filtering, defense-in-depth (skips notes with real `## Summary` but stale `auto-logged` status, auto-fixes them), and returns only genuinely unsummarized note paths.

If `auto_fixed > 0`, report: `Auto-fixed N note(s) with stale status.`

If `skipped_aged` is non-empty, report: `Skipped <len> aged-out unreferenced note(s) (>90d, no inbound links, not pinned). Run \`/recall --include-aged\` to summarize them anyway.` (Use the actual configured threshold from `aged_summarize_threshold_days`, default 90d.) This is the #168 deferral.

Store the length of `unsummarized` as `N`.

Update task #1 to completed. Update task #2 subject to `Summarize N unsummarized note(s)` and set to `in_progress`.

#### Path A: N=0 (no unsummarized notes)

Update task #2 subject to `No unsummarized notes found` and set to `completed`. Skip to Step 3.

#### Path B: N>=1 (parallel Haiku pipelines with sub-agent fallback)

> **Config escape hatch (#84):** If `PIPELINE=subagent`, SKIP Phase 1 (the `upgrade_batch` Haiku `claude -p` pipeline) entirely and treat ALL N notes as the Phase 2 fallback list — route every note directly to the sub-agent path in Phase 2. This is for machines where `claude -p` cold-start latency exceeds the timeout budget (the Haiku pipeline would waste ~2-4 min/note on doomed timeouts). When `PIPELINE=auto` (default), proceed with Phase 1 as written below.

**Task management threshold:** If N <= 5, create a sub-task per note. If N > 5, skip per-note sub-tasks — use a single progress update on task #2 instead. This saves ~15-20s of parent round-trip overhead at large N.

##### Phase 1 — Parallel Haiku upgrades (single batch call)

If N <= 5, create a sub-task for each note (subject `"Upgrade: <basename>"`, activeForm `"Upgrading <basename> via Haiku"`).

**Single Bash tool call** — `upgrade_batch()` fans out N Haiku invocations in parallel inside one Python process via `concurrent.futures.ThreadPoolExecutor`. This sidesteps the Claude Code harness's serialization of parallel Bash tool calls for subprocess-blocking work:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
printf '%s' "$UNSUMMARIZED_PATHS_JSON" | python3 -c '
import sys, os, json
from collections import Counter
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
from obsidian_utils import upgrade_batch
import time
paths = json.loads(sys.stdin.read())
_t0 = time.monotonic()
try:
    results = upgrade_batch(paths, sys.argv[1], sys.argv[2], sys.argv[3])
    # results is list[dict] with keys: path, status, elapsed_s, model_used, fallback_reason
    wall_s = round(time.monotonic() - _t0, 1)
    model_counts = Counter()
    for r in results:
        tag = r["model_used"] or "fallback"
        model_counts[tag] += 1
    DASH = chr(45)
    breakdown = " / ".join(f"{n} {m.split(DASH)[0]}" for m, n in model_counts.most_common())
    print(json.dumps(results))
    print(f"[obsidian-brain] Step 2: upgraded {len(results)} note(s) in {wall_s}s wall ({breakdown})", file=sys.stderr)
except Exception as exc:
    print(json.dumps({"error": f"{type(exc).__name__}: {exc}", "results": []}))
    print(f"[obsidian-brain] upgrade_batch failed: {exc}", file=sys.stderr)
    sys.exit(1)
' "$VAULT_PATH" "$SESSIONS_FOLDER" "$PROJECT"
```

Parse the returned JSON. If the top-level object contains an `"error"` key, `upgrade_batch` itself failed — treat all notes as failed and fall back to the Phase 2 sub-agent path for each. Otherwise, parse the array normally. Each result dict has: `path`, `status`, `elapsed_s`, `model_used` (`haiku-4.5` on success, `None` on failure; `sonnet-4.6` / `opus-*` reserved for Phase 3 #165), `fallback_reason` (`haiku_timeout` | `empty_output` | `haiku_subprocess_error` | `None`). Note: `upgrade_batch` now groups session notes into batches (default 3 per spawn, config key `summary_batch_size`; set to 1 to disable) and summarizes each group in a single `claude -p` spawn to amortize CLI startup cost (#166). Per-note parse failures (`missing_section`) and whole-spawn failures fall through to the per-note solo path automatically before any result reaches Phase 2. For each entry:
- `status` starts with `Upgraded ` → mark as succeeded
- anything else (including `Failed: ...`, empty, or unexpected prefix) → add to the Phase 2 fallback list

The stderr line emits a per-model breakdown visible in the tool trace (e.g. `Step 2: upgraded 7 note(s) in 2.8s wall (5 haiku / 2 fallback)`).

If N <= 5: update each sub-task accordingly (succeeded or `Failed: <basename>`).
If N > 5: update task #2 subject to `Upgrade N notes: M succeeded, F pending fallback`.

> **Why a single Bash call, not N parallel calls?** The Claude Code harness serializes parallel Bash tool calls through a limited shell pool when each subprocess blocks on I/O (e.g., `claude -p --model haiku` taking 5-30s). Dispatching 10 Bash calls in one message still executes them one at a time — wall time ≈ Σ per-call. Pushing fan-out into a single Python process with `ThreadPoolExecutor` gives true concurrency (the GIL releases during subprocess waits), so wall time ≈ max per-call. See `claude-insights/2026-04-21-recall-parallel-bash-dispatch-runs-sequentially-fbee-error.md` and GH #69.

##### Phase 2 — Sub-agent fallback (only for failed notes)

If no failures, skip this phase entirely.

For each failed note, spawn a sub-agent. If multiple notes failed, spawn all sub-agents in a **single message turn**:

```
Agent({
  description: "Summarize session note <basename>",
  prompt: "Read the session note at <NOTE_PATH>. Produce a structured summary with these exact markdown sections:\n\n## Summary\n1-3 sentence overview of what was accomplished.\n\n## Key Decisions\n- Bullet list of important technical decisions. Write \"None noted.\" if none.\n\n## Changes Made\n- Bullet list of files modified/created with brief description. Write \"None noted.\" if none.\n\n## Errors Encountered\n- Bullet list of errors and how resolved. Write \"None.\" if none.\n\n## Open Questions / Next Steps\n- [ ] Checkbox list of unresolved items. Write \"None.\" if none.\n\nWrite the summary to ~/.claude/obsidian-brain/summary-<basename>.md using the Write tool. After the summary sections, add a final line:\nIMPORTANCE: N\nwhere N is 1-10. 1-3: trivial (config, interrupted). 4-6: standard work. 7-8: key decisions or error resolutions. 9-10: major releases or security audits.\n\nReturn ONLY the single line: WRITTEN:~/.claude/obsidian-brain/summary-<basename>.md"
})
```

When sub-agents return, for each:

1. If the sub-agent returned `WRITTEN:<path>`, extract the path after `WRITTEN:` and replace the leading `~` with `$HOME` to get an absolute path. Store this as `SUMMARY_TEMP_PATH`. Verify the file exists: `test -f "$SUMMARY_TEMP_PATH" && echo "EXISTS" || echo "MISSING"`.
2. If EXISTS, apply it via Python:

   ```bash
   cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
   python3 -c '
   import sys, os
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
   from obsidian_utils import upgrade_note_with_summary
   with open(os.path.expanduser(sys.argv[6]), "r") as f:
       summary = f.read()
   status = upgrade_note_with_summary(sys.argv[1], summary, sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
   print(status)
   ' "$NOTE_PATH" "$VAULT_PATH" "$SESSIONS_FOLDER" "$PROJECT" "sub-agent" "$SUMMARY_TEMP_PATH"
   ```

   If the write-back status starts with `Failed:`, count this note as permanently failed — do NOT count it as upgraded. If N <= 5, update the per-note sub-task to `Permanently failed: <basename>`.

   If the write-back succeeds, and N <= 5, update the per-note sub-task to `Fallback succeeded: <basename>`.

3. If MISSING or sub-agent didn't return `WRITTEN:` → note stays unsummarized for next `/recall`. If N <= 5, update the per-note sub-task to `Permanently failed: <basename>`.

**Always** clean up temp files from Phase 2 after all write-backs complete, regardless of outcome. Use the actual `SUMMARY_TEMP_PATH` values collected from each sub-agent's `WRITTEN:` response (not placeholder names):

```bash
rm -f "$SUMMARY_TEMP_PATH_1" "$SUMMARY_TEMP_PATH_2" ...
```

If N > 5: update task #2 subject to reflect final Phase 2 results (e.g. `Upgrade N notes: M Haiku + F fallback succeeded, K failed`).

##### Completion

Mark task #2 as completed. Report results:
- How many upgraded via Haiku pipeline (Phase 1 successes)
- How many upgraded via sub-agent fallback (Phase 2 write-back successes)
- How many permanently failed (notes where both Phase 1 Haiku AND Phase 2 sub-agent fallback failed or were skipped — these stay unsummarized for next `/recall`)

For failed notes: "Note `<basename>` could not be summarized. It will be retried on the next `/recall`."

### Step 3 — Build context brief (Python)

Update task #3 to `in_progress`.

Run a single Python call that reads all session and insight files and composes the brief — no sub-agent needed:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys, os
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
from obsidian_utils import build_context_brief, check_hook_status
hs = check_hook_status()
status_line = ("[OK] " if hs["ok"] else "[WARN] ") + hs["message"]
print(build_context_brief(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], hook_status_line=status_line))
' "$VAULT_PATH" "$SESSIONS_FOLDER" "$INSIGHTS_FOLDER" "$PROJECT"
```

The first line of the emitted `CONTEXT_BRIEF` is always the hook-status line. If it starts with `[OK]`, omit it from the displayed output — the user doesn't need to see "session logging active" every time. If it starts with `[WARN]`, display it verbatim so the user knows to take action (e.g., run `/obsidian-setup`).

If the command fails (non-zero exit code), print the error and stop — do not fall back to in-context reads.

**Parse the output.** Split on section labels:

1. Extract `<<<OB_CONTEXT_BRIEF>>>` — everything between this delimiter and `<<<OB_LOAD_MANIFEST>>>`. This is the brief to display.
2. Extract `<<<OB_LOAD_MANIFEST>>>` — parse `full_session_title`, `full_session_date`, `full_session_path`, `summary_session_title`, `summary_session_date`, `insight_count`, `snapshot_count` (optional), and all `snapshot:` lines (there may be zero or more, each followed by optional 2-space-indented `key_context` bullets).
3. Extract `<<<OB_OPEN_ITEM_CANDIDATES>>>` — either `NO_CANDIDATES`, `NO_ITEMS`, or a JSON array. Count the number of `- [ ]` items across all scanned session notes. Store as `open_items_total`. When the payload is a JSON array, each element may carry two optional fields — `contradicted_by` (a `YYYY-MM-DD` date) and `contradicted_by_title` (that session's title) — meaning a STRICTLY NEWER session's own summary reports that item done. Collect every element that has a non-empty `contradicted_by` into a list of flagged items (`text`, `contradicted_by`) for Step 4. Elements without `contradicted_by` are not flagged — ignore them (do not surface, do not count as done).

**Present the brief immediately** (same turn — saves one parent round):

> **Here's what I found from your Obsidian vault for `$PROJECT`:**

Then output the `CONTEXT_BRIEF` section. For the session history table, paraphrase each session's Title column into a concise one-line summary (under ~80 characters) that captures the key accomplishment. Keep all other columns (date, duration, branch) verbatim.

Snapshots appear in the brief as nested indented rows beneath their parent session (rows starting with `↳ HH:MM:SS`). Render them verbatim — do not paraphrase snapshot titles (they're already one-line summaries). Display the `snapshot:` lines from LOAD_MANIFEST as bullet points under the most-recent session in the "Loaded into this conversation" output.

If unsummarized notes were upgraded in Step 2, also mention:

> _Upgraded N session note(s) with AI summaries._

### Step 3b — Recurring Themes (read-only)

Surface the project's top recurring themes (ranked by stored activation, kept fresh by `/consolidate` and `/emerge`). This is a fast, read-only DB read — `$PROJECT` is the value already derived in Step 1.

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys, os
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
from obsidian_utils import recurring_themes_section
from vault_index import _default_db_path
print(recurring_themes_section(_default_db_path(), sys.argv[1] if len(sys.argv) > 1 else None))
' "$PROJECT"
```

If the output is non-empty, append it verbatim to the brief (between the context brief and the open-items footer). If it is empty, print nothing — there are no themes yet.

**Graceful degradation:** the helper swallows its own exceptions (`ImportError`, empty/missing DB, no themes) and returns `""`, so a missing index or an un-consolidated vault simply prints nothing and `/recall` continues normally. Do not treat an empty result as an error.

### Step 4 — Show read-only context brief footer

For each flagged item collected in Step 3 (those carrying a non-empty `contradicted_by`), render one line, in the order returned:

> ⚠ "<item's text field, verbatim>" looks done per session <contradicted_by> — run `/check-items` to confirm

If `contradicted_by_title` is present and non-empty, you may append it in parentheses after the date for extra context. Do not paraphrase the item text.

Then append to the brief:

> _N open items in this project — run `/check-items` to triage._

Where N is the count of `- [ ]` items found while scanning sessions in Step 3 (the `open_items_total` value already computed by the Python block in Step 3; if not present, count by re-scanning the same notes) MINUS the number of flagged items already rendered above, so a flagged item is never double-counted in the plain footer.

This step remains strictly read-only: never check anything off, and never prompt the user to action an individual item beyond the single flagged-line nudge above. Do NOT independently compute candidate matches or cite session evidence of your own — the flagged lines are rendered only from the `contradicted_by` field Python already computed in Step 3.

If `N == 0` and there are no flagged items either, omit the footer/warning block entirely. If there are flagged items but `N == 0`, still render the flagged lines (omit only the plain `_N open items...` line).

Mark task #3 (the renamed final task) as `completed` and end.

## Edge Cases

- **No sessions found:** Tell the user no session history was found for this project. Suggest they start a session and it will be logged automatically.
- **No insights found:** Omit the "Curated Insights" section. Mention: "No curated insights yet for this project."
- **Very large vault (50+ sessions):** Only grep, never glob the entire folder. Limit reads to the most recent 5 sessions + all insights.
- **Config exists but vault path is invalid:** Warn the user and suggest running `/obsidian-setup` again.
- **Open items exist:** Do not attempt to check them off. Append the footer nudge pointing to `/check-items` instead.
