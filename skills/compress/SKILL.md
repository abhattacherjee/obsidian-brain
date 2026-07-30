---
name: compress
description: "Interactively saves curated insights from the current Claude Code session to the Obsidian vault. Use when: (1) /compress command to save session insights, (2) /compress <topic> to extract a specific topic, (3) user wants to capture decisions, patterns, solutions, or error fixes from the current session."
metadata:
  version: 1.1.0
---

# Compress — Save Session Insights to Obsidian

Analyze the current conversation, extract valuable insights, and save them as structured notes in the Obsidian vault. Supports both interactive multi-insight selection and targeted single-topic extraction.

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
print("VAULT=" + c["vault_path"])
print("SESS=" + c.get("sessions_folder", "claude-sessions"))
print("INS=" + c.get("insights_folder", "claude-insights"))
'
```

Parse each output line as KEY=VALUE, splitting on the first `=`.

If the output is empty or errors, tell the user:

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

### Step 3 — Determine mode

Check if the user provided a topic argument after `/compress`.

- **With argument** (e.g. `/compress rate limiting strategy`): Go to Step 3.5.
- **Without argument** (bare `/compress`): Go to Step 4B.

### Step 3.5 — Search for existing notes on this topic

Run a single Python call to search the vault index for existing notes matching the topic:

~~~bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python3 -c '
import sys, os, json
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
try:
    from vault_index import ensure_index, search_vault, compute_query_vector
    from obsidian_utils import load_config
    # Pure predicate: top rank must pass absolute-strength gate AND |top|-|#2| delta gate.
    # MIN_RANK_DELTA tuned against scripts/compress_rank_gap_corpus.json (issue #45).
    # Cosine gate: query_vec threads through when non-empty; {} (stopword/empty query only)
    # becomes None so guard runs rank-only (legacy path). Note: a fresh (0-note) index still
    # yields IDF=1.0 for every token, so a non-stopword query produces a NON-empty dict —
    # but search_vault returns [] on an empty corpus, so the cosine gate is never reached.
    from compress_guard import is_high_confidence_match, summarize_match_evidence, topic_snippet
    c = load_config()
    vp = c["vault_path"]
    folders = [c.get("sessions_folder", "claude-sessions"), c.get("insights_folder", "claude-insights")]
    db = ensure_index(vp, folders)
    query_vec = compute_query_vector(db, sys.argv[1])
    results = search_vault(db, sys.argv[1], note_type="claude-insight", limit=3, include_vectors=True)
    results += search_vault(db, sys.argv[1], note_type="claude-decision", limit=3, include_vectors=True)
    results += search_vault(db, sys.argv[1], note_type="claude-session", limit=3, include_vectors=True)
    # No dedup needed: each note has a single type, so the three note_type searches are disjoint.
    # Sort combined results by rank (most negative = best match)
    results.sort(key=lambda r: r["rank"])
    if is_high_confidence_match(results, query_vec=query_vec or None):
        top = results[0]
        ev = summarize_match_evidence(results, query_vec=query_vec or None)
        snippet = ""
        try:
            with open(top["path"], "r", encoding="utf-8") as fh:
                snippet = topic_snippet(fh.read(1_000_000))
        except (OSError, UnicodeDecodeError):
            pass  # snippet is cosmetic — a read/decode failure must not discard the match
        print(json.dumps({"match": True, "path": top["path"], "title": top["title"], "date": top["date"], "tags": top["tags"], "rank": top["rank"], "rank_note": ev["rank_note"], "runner_up_rank": ev["runner_up_rank"], "shared_terms": ev["shared_terms"], "snippet": snippet}))
    else:
        print(json.dumps({"match": False}))
except ImportError as e:
    print(f"Warning: plugin hooks are out of date or missing ({e}) — run /dev-test install", file=sys.stderr)
    print(json.dumps({"match": False}))
except Exception as e:
    print(f"Warning: could not search vault index: {e}", file=sys.stderr)
    print(json.dumps({"match": False}))
' "$TOPIC"
~~~

Parse the JSON output. If the script exits non-zero or the output cannot be parsed as JSON, treat it as `{"match": false}` and proceed silently (log a note: "Could not search vault index; creating new note.").

If `match` is `true`, store the `path` field as `MATCH_PATH` and the `title` field as `MATCH_TITLE`. Format `tags` by splitting on commas and joining with `, `. If `tags` is empty or null, display "no tags".

**If `match` is `false`:** No existing note found. Proceed silently to Step 4A (create new note).

**If `match` is `true`:** Present the match to the user. Render the block below, applying these rules:
- OMIT the "next-best:" clause (the " · next-best: <runner_up_rank>" tail, including the leading " · " separator and its surrounding spaces) when `runner_up_rank` is null — the line becomes exactly `Match rank: <rank> (<rank_note>)`.
- OMIT the "Shared terms" line entirely when `shared_terms` is empty.
- OMIT the "Snippet" line entirely when `snippet` is empty.

> Found an existing note on this topic:
> **"<title>"** (<date>)
> Tags: <tags as comma-separated list, or "no tags">
> Match rank: <rank> (<rank_note>) · next-best: <runner_up_rank>
> Shared terms with your query: `<term1>`, `<term2>`, …
> Snippet: "<snippet>"
>
> Would you like to **update** this note or **create new**?

Wait for the user's response:
- **"update"** → Go to Step 4A-update.
- **"create new"** → Go to Step 4A (create new note as before).

### Step 4A — Single-topic extraction

Analyze the current conversation for content related to the user's specified topic. Draft a note that includes:

- **Summary:** 2-4 sentence overview of the topic as discussed in this session
- **Details:** Key points, code snippets, configurations, or commands relevant to the topic
- **Context:** Why this came up, what problem it solved, any trade-offs discussed

Skip to Step 5.

### Step 4A-update — Append to existing note

This step is reached when the user chose "update" in Step 3.5. The matched note path is `$MATCH_PATH`.

#### 4A-update.1 — Read the existing note

Use the Read tool to read the full contents of `$MATCH_PATH`. Note the existing frontmatter tags and whether a `last_updated` field is already present.

#### 4A-update.2 — Draft the update section

Analyze the current conversation for content related to the topic. Draft a dated update section:

~~~markdown
## Update (YYYY-MM-DD)

<New content about this topic from today's session. Include:
- New findings, corrections, or extensions to the original insight
- Code snippets or commands if relevant
- Context on why this update was triggered>
~~~

Where `YYYY-MM-DD` is today's date.

**Important:** Do NOT rewrite or duplicate existing content. The update section captures only what is NEW from this session.

#### 4A-update.3 — Show preview and ask for edits

Present ONLY the new update section (not the full existing note):

> **Update section to append to "< existing note title>":**
>
> (show the drafted `## Update (YYYY-MM-DD)` section)
>
> Preview above. Would you like to:
> - **save** — append this update
> - **edit content** — tell me what to change
> - **cancel** — discard this update

Wait for the user's response. Apply edits and re-show if requested. Repeat until the user says **save** or **cancel**.

If **cancel**, stop here.

#### 4A-update.4 — Append the update section and update frontmatter

Run the note-writer CLI's `append-update` command, piping the drafted `## Update (YYYY-MM-DD)` section (from 4A-update.2) in on stdin. **This single call replaces all three of the old Edit-tool steps** — it finds the correct insertion point (scanning **top-down** for `_(Summary source: ...)_`, `## Tool Usage`, `## Conversation (raw)`, `## Session Metadata`, `## Files Touched` — ignoring any inside a fenced code block, and **stopping at the first `## Update (` heading**, since everything past that is a previously appended update rather than the note's audit trail — and inserting immediately before the first marker it finds, or at end-of-file), bumps `last_updated`, and merges new tags — all in one atomic write. Do NOT use the Edit tool for any of this.

Generate 1-3 new topic tags from the update content (same logic as Step 5) and pass them via `--add-tags`. `--last-updated` is opt-in — it must be passed explicitly with today's date, or the note's `last_updated` field will NOT be bumped (that used to happen automatically; it no longer does without this flag).

**Both flags are validated — a present flag with a broken value is an error, not a silent skip.** `--last-updated` must be a real `YYYY-MM-DD` value (an empty `"$TODAY"` from an unset variable is rejected). Each `--add-tags` item must match `[A-Za-z0-9][A-Za-z0-9/_.-]*`: start with a letter or digit, then letters, digits, `/`, `_`, `.` or `-` only — **no empty items** (so no trailing comma, and no `--add-tags ""`) and **no leftover `<placeholder>` text** (the `claude/topic/<new-tag-1>` below is a template; substitute a real tag or the `<`/`>` will be rejected). If there are no new tags, **omit the `--add-tags` line entirely** — that is the supported no-op. The drafted update section must also be non-empty; piping an empty heredoc body is rejected and the note is left untouched.

If the note's frontmatter has no recognizable `tags:` block, the command now **fails** rather than silently dropping the tags — surface the error and add them manually, or omit `--add-tags` and re-run.

**Two rules for the heredoc terminator, both load-bearing.** (1) It must stay **quoted** (`<<'OB_UPDATE_EOF_<eof4>'`) — do not drop the quotes in a future edit. (2) It must be **unique per invocation**: substitute the same 4 random hex characters for `<eof4>` in BOTH the `<<'OB_UPDATE_EOF_<eof4>'` opener and the terminator line, then confirm that **no line of the content you are about to emit is exactly that terminator** — if one is, pick different hex characters and re-check. **Never** replace this with a fixed delimiter. Quoting stops `$`/backtick expansion but does NOT stop early termination: a line equal to the terminator at column 0 ends the heredoc there, silently truncating the content AND handing everything after it to the shell as commands to execute. Notes written by this plugin routinely quote these very blocks, so a fixed terminator is a live hazard, not a theoretical one. **Self-check before you emit the block: if the terminator still contains `<` or `>`, you have not substituted it.** Stop and substitute it — the literal `<eof4>` form appears at column 0 inside these SKILL.md blocks themselves, so a note quoting one of them collides all over again, and nothing on the shell side can catch that. The `HOOKS=` line below sorts cached plugin versions **numerically** (a plain `max()` is lexicographic and picks `3.9.0` over `3.10.0`, resolving to a cache with no `note_writer.py`), and the `test -f` line turns a stale/incomplete cache into the documented `ERROR:` shape instead of a raw Python `can't open file` message. Update sections routinely contain `$` variables, backtick commands, and fenced code blocks from the session; an unquoted delimiter lets the shell expand/corrupt them before they ever reach the file.

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
HOOKS=$(python3 -c "
import glob, json, os, re
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser('~/.claude/plugins/known_marketplaces.json'))).values():
            _h = os.path.join((_m or {}).get('installLocation', ''), 'hooks')
            if os.path.isfile(os.path.join(_h, 'obsidian_utils.py')):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser('~/.claude/plugins/cache/*/obsidian-brain/*/hooks')) if re.fullmatch('[0-9]+([.][0-9]+)*', _d.split('/')[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split('/')[-2].split('.')], _p), default='hooks')
print(_ob_hooks())
")
test -f "$HOOKS/note_writer.py" || { echo "ERROR: note_writer.py not found under $HOOKS - the plugin cache is stale or incomplete. Run /plugin marketplace update (or /dev-test install for local dev), then retry." >&2; exit 1; }
TODAY=$(date +%Y-%m-%d)
python3 "$HOOKS/note_writer.py" append-update "$VAULT_PATH" "$MATCH_PATH" \
  --last-updated "$TODAY" \
  --add-tags "claude/topic/<new-tag-1>,claude/topic/<new-tag-2>" <<'OB_UPDATE_EOF_<eof4>'
## Update (YYYY-MM-DD)

<New content about this topic from today's session>
OB_UPDATE_EOF_<eof4>
```

On success this prints `OK: <resolved path>`. On failure it prints `ERROR: <reason>` to stderr and exits non-zero — the file is left byte-identical (no partial write happens). Surface the error to the user: "Failed to append update section — `<error message>`. Please edit manually at `$MATCH_PATH`." and stop here.

The write is atomic and a non-zero exit means nothing was written, so a Read purely to confirm the bytes landed adds nothing — do NOT add one for that purpose. The CLI also refuses to write if the note changed on disk after it was read (`ERROR: note changed on disk...`), which is what a second session running `/compress` on the same note looks like; on that error, re-run the update so it applies on top of the other change rather than discarding it.

**Do NOT change:** `date`, `source_session`, `source_session_note`, or `type` fields. These record the original creation context — the CLI never touches them.

**Note on repeated runs:** running `/compress` update again later (even later the same day) appends another `## Update (YYYY-MM-DD)` section rather than merging into an existing one for that date. This is expected and lossless — do not describe or imply that same-date updates merge.

#### 4A-update.5 — Re-sync vault index

Run:

~~~bash
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
from vault_index import ensure_index
from obsidian_utils import load_config
c = load_config()
vp = c["vault_path"]
folders = [c.get("sessions_folder", "claude-sessions"), c.get("insights_folder", "claude-insights")]
try:
    ensure_index(vp, folders)
    print("OK")
except Exception as e:
    print(f"WARN: re-sync failed (non-fatal): {e}")
'
~~~

#### 4A-update.6 — Confirm

Print:

> **Note updated!**
> - File: `$MATCH_PATH`
> - Added section: "Update (YYYY-MM-DD)"
> - New tags: `<list of newly added tags>` (or "none")

Skip to Step 10 (offer follow-up). Do NOT proceed through Steps 5-9 (those are the create-new flow).

### Step 4B — Multi-insight suggestion

**First, check for claudeception output** using layered detection:

**Layer 1 — High-confidence structured markers** (check first):

Scan the current conversation for these patterns. If found, extract the skill/knowledge name and a one-line summary:

- The `MANDATORY SKILL EVALUATION REQUIRED` banner (from the claudeception activator hook)
- `Result: PASS` or `Result: FAIL` (from the claudeception skill validator)
- Skill file paths matching `~/.claude/skills/*/SKILL.md` or `.claude/skills/*/SKILL.md`

If any Layer 1 markers are found, create a candidate for each and label it `[from claudeception]`.

**Layer 2 — Broad phrase scanning** (fallback, only if Layer 1 found nothing):

Scan the conversation for these phrases:
- "created skill", "new skill at", "skill file written"
- "extracted knowledge", "pattern identified", "reusable insight"
- Output from a `/claudeception` invocation

If any Layer 2 phrases are found, create a candidate for each and label it `[possibly from claudeception]`.

**Then, perform standard insight discovery:**

Analyze the full conversation and identify 3-5 additional candidate insights (beyond any claudeception candidates). Each candidate should be one of these types:

- **Decision** — an architectural or design choice made during the session
- **Pattern** — a reusable approach, technique, or workflow discovered
- **Solution** — a specific problem solved with a clear fix
- **Error Fix** — a bug or error diagnosed and resolved
- **Discovery** — a new finding about a tool, API, library, or system behavior

**Present all candidates** as a numbered list, with claudeception candidates first:

> **Insights found in this session:**
>
> 1. [from claudeception] [Discovery] Rate limiter pattern — extracted as reusable skill
> 2. [possibly from claudeception] [Pattern] Retry with exponential backoff — identified across 3 sessions
> 3. [Decision] Chose Redis for session store — trade-off analysis
> 4. [Solution] Fixed CORS issue with Safari — root cause in preflight handling
>
> Which would you like to save? (e.g. `1,3` or `all`)

If no claudeception output was detected, present only the standard candidates (same as before — no labels).

When the user says `all`, all candidates (including claudeception ones) are saved. When the user picks specific numbers, only those are saved — standard selection behavior.

Wait for the user to pick. For each selected insight, draft the note content and continue to Step 5. Process selected insights one at a time.

### Step 5 — Auto-generate topic tags

Based on the note content, generate 1-3 topic tags. Tags should be lowercase, hyphenated, and specific. Examples:

- `claude/topic/rate-limiting`
- `claude/topic/react-hooks`
- `claude/topic/git-workflow`
- `claude/topic/api-design`

### Step 6 — Show preview and ask for edits

Present the full note to the user including frontmatter:

```
---
type: claude-insight
date: YYYY-MM-DD
created_at: <ISO-8601-UTC>
source_session: <current-session-id>
source_session_note: "[[<session-note-filename>]]"
project: <project-name>
tags:
  - claude/insight
  - claude/project/<project-name>
  - claude/topic/<auto-generated-topic-1>
  - claude/topic/<auto-generated-topic-2>
---

# <Title>

<Note body>
```

Where:
- `YYYY-MM-DD` is today's date
- `<ISO-8601-UTC>` is the current UTC timestamp at second precision. Get it via:
  ```bash
  python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat(timespec="seconds"))'
  ```
  Example: `2026-04-24T18:42:11+00:00`
- `<current-session-id>` and `<session-note-filename>` are derived together. Get session context via the shared helper:

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
  from obsidian_utils import load_config, get_session_context
  c = load_config()
  ctx = get_session_context(c["vault_path"], c.get("sessions_folder", "claude-sessions"))
  print("SID=" + ctx["session_id"] + " HASH=" + ctx["hash"] + " PROJECT=" + ctx["project"] + " SESSION_NOTE=" + ctx["session_note_name"])
  '
  ```

  Parse the output to get `SESSION_ID`, `HASH`, `PROJECT`, and `SESSION_NOTE`. Use these for the frontmatter fields.

  **Important:** If `SESSION_ID` is `unknown`, use `unknown` for `source_session` and omit `source_session_note` entirely.
- `<project-name>` is the `PROJECT` value from `get_session_context()` (lowercased, hyphenated basename of cwd)
- The `source_session_note` field creates an Obsidian backlink from the insight to its source session, enabling bidirectional navigation in the graph view

Ask the user:

> Preview above. Would you like to:
> - **save** as-is
> - **edit tags** — add or remove tags
> - **edit content** — tell me what to change
> - **cancel** — discard this note

Wait for the user's response. Apply any requested edits and show the updated preview. Repeat until the user says **save** or **cancel**.

If cancel, stop here (or move to the next selected insight if processing multiple from Step 4B).

### Step 7 — Generate filename

Construct the filename from these parts:

1. **Date:** `YYYY-MM-DD` (today)
2. **Slug:** The note title, lowercased, spaces replaced with hyphens, non-alphanumeric characters (except hyphens) removed, truncated to 50 characters
3. **Hash:** 4-character hex hash derived from the current timestamp: `date +%s | md5 | cut -c29-32` (macOS) or `date +%s | md5sum | cut -c1-4` (Linux). Do NOT use `tail -c 4` — it counts the trailing newline as a byte and returns only 3 visible characters.

Final filename: `YYYY-MM-DD-<slug>-<hash>.md`

Example: `2026-04-04-rate-limiting-with-redis-a3f2.md`

### Step 8 — Write the note

Run the note-writer CLI, piping the full note (frontmatter + body) in on stdin. It creates `$INSIGHTS_FOLDER` if needed and writes the file atomically at mode `0o600` — no `mkdir`/`chmod` needed. **Two rules for the heredoc terminator, both load-bearing.** (1) It must stay **quoted** (`<<'OB_NOTE_EOF_<eof4>'`) — do not drop the quotes in a future edit. (2) It must be **unique per invocation**: substitute the same 4 random hex characters for `<eof4>` in BOTH the `<<'OB_NOTE_EOF_<eof4>'` opener and the terminator line, then confirm that **no line of the content you are about to emit is exactly that terminator** — if one is, pick different hex characters and re-check. **Never** replace this with a fixed delimiter. Quoting stops `$`/backtick expansion but does NOT stop early termination: a line equal to the terminator at column 0 ends the heredoc there, silently truncating the content AND handing everything after it to the shell as commands to execute. Notes written by this plugin routinely quote these very blocks, so a fixed terminator is a live hazard, not a theoretical one. **Self-check before you emit the block: if the terminator still contains `<` or `>`, you have not substituted it.** Stop and substitute it — the literal `<eof4>` form appears at column 0 inside these SKILL.md blocks themselves, so a note quoting one of them collides all over again, and nothing on the shell side can catch that. The `HOOKS=` line below sorts cached plugin versions **numerically** (a plain `max()` is lexicographic and picks `3.9.0` over `3.10.0`, resolving to a cache with no `note_writer.py`), and the `test -f` line turns a stale/incomplete cache into the documented `ERROR:` shape instead of a raw Python `can't open file` message. An unquoted delimiter lets the shell expand `$` variables and backtick commands embedded in the note body, silently corrupting it:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
HOOKS=$(python3 -c "
import glob, json, os, re
def _ob_hooks():
    try:
        for _m in json.load(open(os.path.expanduser('~/.claude/plugins/known_marketplaces.json'))).values():
            _h = os.path.join((_m or {}).get('installLocation', ''), 'hooks')
            if os.path.isfile(os.path.join(_h, 'obsidian_utils.py')):
                return _h
    except Exception:
        pass
    _c = [_d for _d in glob.glob(os.path.expanduser('~/.claude/plugins/cache/*/obsidian-brain/*/hooks')) if re.fullmatch('[0-9]+([.][0-9]+)*', _d.split('/')[-2])]
    return max(_c, key=lambda _p: ([int(_n) for _n in _p.split('/')[-2].split('.')], _p), default='hooks')
print(_ob_hooks())
")
test -f "$HOOKS/note_writer.py" || { echo "ERROR: note_writer.py not found under $HOOKS - the plugin cache is stale or incomplete. Run /plugin marketplace update (or /dev-test install for local dev), then retry." >&2; exit 1; }
python3 "$HOOKS/note_writer.py" write "$VAULT_PATH" "$INSIGHTS_FOLDER" "YYYY-MM-DD-<slug>-<hash>.md" <<'OB_NOTE_EOF_<eof4>'
---
type: claude-insight
...
---

# <Title>
...
OB_NOTE_EOF_<eof4>
```

On success this prints `OK: <absolute path>` — that is the file at `$VAULT_PATH/$INSIGHTS_FOLDER/<filename>`. On failure it prints `ERROR: <reason>` to stderr and exits non-zero; surface that message to the user and stop here.

If the error is `note already exists`, the 4-hex filename hash collided with a note written in the same second. Regenerate the hash (Step 7's command), rebuild the filename, and retry the write **once**. If it fails again for any reason, surface the error and stop — do not loop.

### Step 9 — Confirm

Print:

> **Insight saved!**
> - File: `$VAULT_PATH/$INSIGHTS_FOLDER/<filename>`
> - Tags: `claude/insight`, `claude/project/<name>`, `claude/topic/<topic1>`, ...
> - Open in Obsidian to view and link to other notes.

If processing multiple insights from Step 4B, repeat Steps 5-9 for each remaining selected insight.

### Step 10 — Offer follow-up

After all insights are saved, ask:

> Anything else to capture from this session? You can run `/compress` again or `/compress <topic>` to extract a specific topic (will offer to update if an existing note matches).
