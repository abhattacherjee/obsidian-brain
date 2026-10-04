---
name: vault-ask
description: "Asks questions and gets synthesized answers grounded in vault history with source citations. Use when: (1) /vault-ask <question> to reason over vault knowledge, (2) user wants to know what their notes say about a topic, (3) user wants cross-project pattern analysis."
metadata:
  version: 1.0.0
---

# Vault Ask

Synthesizes a reasoned answer to the user's question by searching session, insight and wiki notes in the Obsidian vault, plus this host's memory files, and citing sources. Returns a grounded answer, not a list of matches. Answers that draw on 3 or more qualifying notes can be saved as wiki pages (#383), which later asks find first.

**Tools needed:** Grep, Read, Bash, Write, AskUserQuestion

**Arguments:** `/vault-ask <question>` or `/vault-ask --caller <name> <question>`. Workflows and sub-agents that run vault-ask mid-task pass `--caller` with their own name. `<name>` must match `^[a-z0-9][a-z0-9_.-]{0,63}$`; anything else stays part of the question, and the run counts as user-typed. Store `CALLER` (empty when user-typed).

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
from obsidian_utils import load_config, indexed_folders
c = load_config()
if not c.get("vault_path"):
    print("ERROR: vault_path not configured", file=sys.stderr)
    sys.exit(1)
project = os.path.basename(os.getcwd()).lower().replace(" ", "-")
# WIKI comes from the validated folder list, never the raw config key.
try:
    folders = indexed_folders(c, strict=True)
    norm = os.path.normpath(c["wiki_folder"]) if c.get("wiki_folder") else ""
    wiki = norm if norm in folders else ""
except Exception as exc:
    print("WARNING: wiki turned off: " + str(exc), file=sys.stderr)
    wiki = ""
print("VAULT=" + c["vault_path"])
print("SESS=" + c.get("sessions_folder", "claude-sessions"))
print("INS=" + c.get("insights_folder", "claude-insights"))
print("PROJECT=" + project)
print("WIKI=" + wiki)
print("HOOKS=" + _ob_hooks())
'
```

Parse each output line as KEY=VALUE, splitting on the first `=`. An empty `WIKI` means the wiki is turned off: skip Step 2b and the filing gate in Step 8. `WIKI` comes from `indexed_folders(config, strict=True)`; when that raises (an invalid `wiki_folder`), WIKI= is printed empty with a `WARNING: wiki turned off` line on stderr, and the wiki steps are skipped. `HOOKS` is the plugin's hooks directory; paste it literally where later commands say `<hooks_dir>`.

Check that `wiki.py` is installed (the memory search in Step 4 needs it even when `WIKI` is empty):

```bash
HOOKS='<hooks_dir>'
test -f "$HOOKS/wiki.py" && echo "WIKI_OK" || echo "WIKI_MISSING"
```

On `WIKI_MISSING`, treat `WIKI` as empty: skip Step 2b, the memory search in Step 4 and the filing gate in Step 8.

If the command exits non-zero or prints ERROR, tell the user:

> Config not found. Run `/obsidian-setup` first to configure your Obsidian vault.

Stop here if config is missing.

Construct the search directories (`WIKI_DIR` only when `WIKI` is not empty):

- `SESSIONS_DIR` = `<vault_path>/<sessions_folder>`
- `INSIGHTS_DIR` = `<vault_path>/<insights_folder>`
- `WIKI_DIR` = `<vault_path>/<wiki_folder>` (it may not exist yet; that is fine)

Validate vault access:

```bash
test -d "$SESSIONS_DIR" && test -d "$INSIGHTS_DIR" && echo "OK" || echo "FAIL"
```

If FAIL, tell the user:

> The vault folders do not exist or are not accessible. Run `/obsidian-setup` to fix this.

Stop here if FAIL.

### Step 2 — Parse the question

The user provides a question after `/vault-ask`. Extract 3–6 key search terms from it:

- **Technology names** — e.g. `Redis`, `GraphQL`, `JWT`, `Postgres`
- **Concept keywords** — e.g. `error handling`, `rate limiting`, `caching`
- **Action words indicating note types** — e.g. `decided` → look for decision notes, `fixed` → look for error-fix notes

Store the extracted terms as `SEARCH_TERMS`. Keep the original question for use in Steps 2b and 7.

### Step 2b — Check the wiki first

Skip this step when `WIKI` is empty.

**How to call `wiki.py`.** Every call takes a JSON payload. First make sure the private directory exists: run `mkdir -m 700 -p ~/.claude/obsidian-brain`. Then write the payload with the Write tool to `~/.claude/obsidian-brain/wiki-payload-<8 random hex>.json`, run the command with the file on stdin, then delete the file. Never build the JSON in a shell string: questions and answers contain quotes.

```bash
mkdir -m 700 -p ~/.claude/obsidian-brain
python3 '<hooks_dir>/wiki.py' lookup < ~/.claude/obsidian-brain/wiki-payload-<hex>.json; rm -f ~/.claude/obsidian-brain/wiki-payload-<hex>.json
```

`wiki.py` prints one JSON object. Exit 0 is success. Exit 1 means it refused: show its `ERROR:` line. Exit 2 is a usage error or a crash: show stderr. The commands below are written as `python3 "<hooks_dir>/wiki.py" <command>`, always with a payload file on stdin.

1. Run `python3 "<hooks_dir>/wiki.py" lookup` with `{"question": "<original question>"}`. It searches wiki pages with `search_vault` (reranked; the hits are logged as accesses) and returns up to 3 `candidates` (`path`, `question`, `updated`, `rank`).
2. Decide whether a candidate asks the **same question** as the user (same intent, not just shared words). If none does, continue with Step 3.
3. If one does, run `python3 "<hooks_dir>/wiki.py" stale` with `{"page": "<its path>"}`. It returns `stale`, `reasons` and `memory_paths` (`{name: path}` for each memory file the page cites that this host has). Each reason is `changed: <note>` (a source's content changed), `missing: <note>` (a source is gone or unreadable), `newer: <note>` (a newer note matches the question) or `unverifiable: <page>` (the page's fingerprint is missing or bad, so it counts as stale). Memory files use the same forms with a `memory:` prefix: `changed: memory:<project-dir>/<file>.md` (the file changed), `missing: memory:<project-dir>/<file>.md` (this host listed its memory files and that one is gone) and `unverifiable: memory:<project-dir>/<file>.md` (no fingerprint, a host with no memory files, or the file or its folder could not be read).
   - **`stale` itself fails** (exit 1 or 2, or no JSON): treat the page as not fresh. Do not answer from it. Continue with Step 3 as if no candidate matched, and in Step 8 do not update that page.
   - **Fresh:** read the page and present its answer. Cite it as `[[<page file name>]]` and say "From the wiki (updated `<updated>`)". Skip Steps 3–7. In Step 8, save nothing.
   - **Stale, and the page is not marked reviewed:** continue with Steps 3–7. Add the page's `sources` to `CANDIDATE_FILES`. Add each path in `memory_paths` to `CANDIDATE_FILES` as type `claude-memory`, and keep its name for citing. Step 8 refreshes the page without asking and tells the user why, using `reasons`.
   - **Stale, and the page is marked reviewed** (Read the page; its frontmatter has `reviewed:` set to anything other than empty, `false`, `no`, `off` or `0`): answer from the page and warn that its sources changed, listing `reasons`.
     - User-typed run: ask with AskUserQuestion. Options: "Keep my page" (save nothing), "Refresh and overwrite my edits" (run Steps 3–7, then in Step 8 file with `update` and `"override_reviewed": true`), "Save the fresh answer as a new page" (run Steps 3–7, then in Step 8 file a new page).
     - `--caller` run: add one line that the page is reviewed and stale, and save nothing.

### Step 3 — FTS pre-filter (fast path)

Before spawning search agents, try the vault index for instant results. Join `SEARCH_TERMS` into a single space-separated string (`SEARCH_TERMS_JOINED`). Then run:

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
    limit=15,
)
print(json.dumps(results))
' "$SEARCH_TERMS_JOINED" "$PROJECT"
```

If the output is a non-empty JSON array with 5+ results: extract the `path` field from each result and use those file paths as `CANDIDATE_FILES`. Skip the Grep searches in Step 4, but still run its memory search, then go to Step 5.

In `CANDIDATE_FILES` (here and after Step 4), keep at most 3 notes of type `claude-wiki`, and drop any note of type `claude-wiki-index` (the wiki's own index and log files). Wiki pages are syntheses; the cap keeps raw notes in the top 10.

If fewer than 5 results or the command fails (non-zero exit, invalid JSON, import error): fall through to Step 4 to cast a wider net with parallel Grep agents.

### Step 4 — Search vault (3 parallel agents)

When `WIKI_DIR` exists, every search below also covers it: run each Grep call once more with `path=WIKI_DIR`, and pass `<wiki_folder>` as a third folder to `vault_scan.py grep`.

Launch three Grep searches in parallel — one per agent below. Use the Grep tool (never Bash grep). If the Grep tool is not in your tool list, go straight to vault_scan.py grep — do not call Grep first. See the fallback below.

**Agent 1 — Session content:**
For each term in `SEARCH_TERMS`, run:
```
Grep(pattern="<term>", path=SESSIONS_DIR, glob="*.md", output_mode="files_with_matches", -i=true)
```
Collect the union of all file paths returned.

**Agent 2 — Insight content:**
For each term in `SEARCH_TERMS`, run:
```
Grep(pattern="<term>", path=INSIGHTS_DIR, glob="*.md", output_mode="files_with_matches", -i=true)
```
Collect the union of all file paths returned.

**Agent 3 — Tag search (both folders):**
For each term in `SEARCH_TERMS`, run two Grep calls:
```
Grep(pattern="claude/topic/.*<term>", path=SESSIONS_DIR, glob="*.md", output_mode="files_with_matches", -i=true)
Grep(pattern="claude/topic/.*<term>", path=INSIGHTS_DIR, glob="*.md", output_mode="files_with_matches", -i=true)
```
Collect all file paths returned.

**If the Grep tool is not available in this session**, run the same searches with `vault_scan.py grep` instead (#375). It walks the folders recursively, matches one line at a time like the Grep tool, and prints one matching path per line. Run one call per pattern, passing both folder names (`<sessions_folder>` and `<insights_folder>` from Step 1). Use `--pattern='<term>'` for Agents 1 and 2 and `--pattern='claude/topic/.*<term>'` for Agent 3; `--ignore-case` stands for `-i=true`. Always write `--pattern=` with the equals sign: with a space, a term that starts with `-` (such as `--no-verify`) is read as a flag and the call fails. Paste each value inside single quotes as shown. If a value itself contains a `'`, write it as `'\''`.

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
python3 "$HOOKS/vault_scan.py" grep '<vault_path>' '<sessions_folder>' '<insights_folder>' '<wiki_folder>' --pattern='<term>' --ignore-case
```

**Memory search.** The memory search runs on every ask that reaches Step 3 (a fresh wiki answer from Step 2b stops before it, by design), even when Step 3 skipped the Grep searches. Skip it only on `WIKI_MISSING`. For each term in `SEARCH_TERMS`, write `{"pattern": "<term>"}` to a payload file (as in Step 2b) and run `python3 "<hooks_dir>/wiki.py" memgrep`. It prints `{"host": ..., "matches": [{"name": ..., "path": ...}], "skipped": [{"path": ..., "error": ...}]}`: `matches` are this host's memory files that contain the term (case-insensitive, a fixed string, not a regex), and `skipped` are files or folders that could not be read. Add each `path` in `matches` to `CANDIDATE_FILES` as type `claude-memory`, and keep its `name` for citing. When `skipped` is non-empty, show one line: "N memory file(s) could not be read" with the first `path` (count each path once across all terms). When `host` is not `claude-code`, say once that this host has no memory files. On exit 1 or 2, show the `ERROR:` line and continue without memory files.

Combine results from all three agents and the memory search. Deduplicate by file path. Store as `CANDIDATE_FILES`.

If `CANDIDATE_FILES` is empty, tell the user:

> No vault notes found matching your question. Try `/vault-search` with individual keywords to explore what's available.

Stop here.

### Step 5 — Rank results

Score each file in `CANDIDATE_FILES` using these rules:

| Condition | Points |
|-----------|--------|
| Each search term found in content (Agent 1 or 2 match, or a memory search match) | +2 per term |
| Note type is `claude-insight`, `claude-decision`, `claude-error-fix` or `claude-wiki` | +3 |
| Note type is `claude-memory` (a memory file from the memory search) | +3 |
| Note type is `claude-session` | +1 |
| File date is within the last 30 days (relative to today) | +1 |
| Matching tag found (Agent 3 match) | +2 |

To get each note's `type` and `date` without reading the full file, run one `vault_scan.py meta` call over all of `CANDIDATE_FILES` (one quoted path per file):

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

It prints one JSON object per file, one per line, with `path`, `type`, `date`, `project`, `session_id`, `source_session_note`, `tags`, `title`, `snippet` and `error`. Read `type` and `date` from it. The call succeeded only if it exited 0 and printed one JSON row per file you passed. Otherwise show the `ERROR:` line (or the whole stderr) to the user and stop. A `vault_scan: obsidian_utils unavailable: ...` line on stderr is a warning, not a failure. Do not use a fixed-line `Read` for this: frontmatter can run past line 40 (/emerge notes close their fence as deep as line 461), so a fixed line limit silently drops fields. `meta` parses the whole frontmatter block. If a row has a non-null `error`, keep the file but give it no type or recency points. Do not pass memory files to `vault_scan.py meta`: they are outside the vault. They already have type `claude-memory` and get no recency points. Paste each value inside single quotes as shown. If a value itself contains a `'`, write it as `'\''`.

Sort `CANDIDATE_FILES` by score descending. Take the top 10. Store as `RANKED_FILES`.

### Step 6 — Read top notes (context shield)

Read the top 5–10 files from `RANKED_FILES`. Apply the following size-based strategy:

- **Files under ~100 lines:** Read directly with the Read tool.
- **Files over ~100 lines:** Use the `/context:shield` skill (parallel, one sub-agent per file). Each sub-agent reads the file in isolation and returns a distilled summary relevant to the question.

For each file, extract:
- Note type (session, insight, decision, error-fix, snapshot, memory)
- Date
- Key content relevant to the question — decisions made, patterns observed, errors and fixes
- Exact filename (without path) for use as a wikilink citation

**Snapshot-aware reading.** If a ranked file has `type: claude-snapshot`, also resolve its parent session via the `source_session_note` frontmatter wikilink and include the parent session body in the synthesis pool — the snapshot alone only captures a mid-session fragment. If a ranked file has `type: claude-session` and has associated snapshots, fetch those snapshot summaries via the shared helper and include them alongside the session body:

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
print(json.dumps([{"hhmmss": s["hhmmss"], "trigger": s["trigger"], "summary": s["summary"]} for s in snaps]))
' "$SESSIONS_DIR" "$SESSION_ID" "$DATE" "$PROJECT"
```

The goal is that an answer synthesized from a session hit reflects the full session arc (pre-compact + post-compact), not only the tail transcript.

Store all extracted content as `NOTE_SUMMARIES`.

### Step 7 — Synthesize answer

Using `NOTE_SUMMARIES`, synthesize a comprehensive answer to the user's question. Follow these rules strictly:

1. **Lead with the answer, not the methodology.** Do not begin with "I searched your vault..." — begin with the actual answer.

2. **Cite sources inline using wikilinks.** Reference the note filename (without extension) as a wikilink:
   > "You chose Redis over Memcached ([[2026-04-04-use-redis-a3f2-decision]]) because TTL-per-key was required for token expiry."

3. **Distinguish certainty levels** based on the evidence:
   - `"You explicitly decided..."` — use when a decision note clearly records the choice
   - `"Based on your sessions, it appears..."` — use when the pattern is inferred from session content
   - `"Limited context available..."` — use when only one or two notes weakly touch the topic

4. **End with a Sources section:**
   ```markdown
   ### Sources
   - [[note-filename-1]] — <what this note contributed to the answer>
   - [[note-filename-2]] — <what this note contributed to the answer>
   ```

5. **Cite snapshot parents.** When a snapshot note contributes to the answer, cite BOTH the snapshot and its parent session so the user can navigate up:
   > "Mid-session you sketched the API shape ([[2026-04-18-demo-aa-snapshot-140000]]; parent: [[2026-04-18-demo-aa]])."

6. **Cite memory files as plain text.** Write `memory: <project-dir>/<file>.md`, using the `name` from the memory search, never as a wikilink: memory files are not vault notes, so a wikilink would point nowhere. List them under Sources the same way: `- memory: <project-dir>/<file>.md — <what this file contributed>`.

If the notes contain contradictory information (e.g. a decision was changed later), surface that explicitly:
> "You initially chose X ([[older-note]]), but later switched to Y ([[newer-note]])."

### Step 8 — Present answer, then the filing gate

Display the synthesized answer from Step 7 in the conversation first. A failed save never loses the answer.

Then decide whether to save it as a wiki page. Skip all of this when `WIKI` is empty, or when Step 2b answered from a fresh page.

1. **Count.** Run `python3 "<hooks_dir>/wiki.py" count` with `{"sources": [<every note cited in Sources, by file name>], "memory_sources": [<every memory file cited in Sources, by its memgrep name>]}`. It returns `count`, `qualifying`, `other` and `rejected`. Only 3 or more qualifying notes can be filed: insights, error-fixes, decisions, retros, sessions, migrated memory notes and memory files count; a snapshot counts as its parent session.
   - **Rejected names:** `file` refuses a payload that lists any name from `rejected` (unresolved or ambiguous). Drop every rejected name from the `sources` or `memory_sources` list before filing, and tell the user which names were dropped and why. Never file with a rejected name in `sources` or `memory_sources`.
   - **Below 3:** save nothing and say nothing about the wiki. Exception: on a stale refresh from Step 2b, tell the user the page could not be refreshed because the fresh answer has fewer than 3 qualifying sources (give the count); the old page stays as is.
2. **Write the page body.** Run `python3 "<hooks_dir>/wiki.py" rule` and rewrite the answer under that rule. Keep the `### Sources` section, every `[[wikilink]]` and every `memory:` line exactly. The chat answer keeps its normal style; only the page uses the rule.
3. **Choose the action.** Before you choose an update path for a candidate page, Read the candidate page and check `reviewed:` in its frontmatter. It is reviewed unless the value is absent, empty, `false`, `no`, `off` or `0`.
   - **Stale refresh** (from Step 2b, page not reviewed, or the user chose "Refresh and overwrite my edits"): file with `"update": "<page path>"` (plus `"override_reviewed": true` only when the user chose to overwrite). Do not ask. Tell the user the page was refreshed and why.
   - **Reviewed page, user chose "Save the fresh answer as a new page":** file a new page, without `update` and without `override_reviewed`. The reviewed page stays untouched.
   - **`--caller` run:** run `lookup` with the question. If a candidate asks the same question and is not reviewed, file with `update`; if that candidate is reviewed, save nothing and name the page. Otherwise file a new page with `"filed_by": "auto"` and `"caller": "<CALLER>"`. Do not ask. Print one line naming the page saved or updated.
   - **User-typed run:** ask with AskUserQuestion. Options: "Save as a new wiki page", "Update existing page [[…]]" (only when `lookup` found a same-question candidate that is not reviewed), "Skip". Skip saves nothing.
4. **File.** Run `python3 "<hooks_dir>/wiki.py" file` with:

   ```json
   {"question": "<original question>", "body": "<page body from step 2>",
    "sources": ["<note>", "..."], "memory_sources": ["<project-dir>/<file>.md", "..."], "topics": ["<topic>", "..."],
    "confidence": "high|medium|low", "filed_by": "user|auto", "caller": "<only when auto>",
    "update": "<page path, only when updating>"}
   ```

   `confidence` follows your certainty wording from Step 7: "You explicitly decided" → `high`, "it appears" → `medium`, "Limited context" → `low`. `topics` are up to 8 short lowercase slugs (`[a-z0-9-]`) for the main subjects. Projects come from the cited sources; there is no `projects` field. A new page whose file name is taken gets `-2`, `-3` and so on.

   With `update`, leave the page being refreshed out of `sources`. A page cannot cite itself, and `file` refuses it.

   Report the outcome by exit code:
   - **Exit 0, no `warning` key:** name the page (`path`).
   - **Exit 0 with a `warning` key:** tell the user the page was saved (name `path`) and quote the warning. The index or log update failed; the next write repairs it.
   - **Exit 1:** quote the `ERROR:` line and say nothing was saved.
   - **Exit 2:** report the failure and quote stderr. Do not say the page was saved.

If the user asks a follow-up question, return to Step 2 with the new question.

## Key distinction

`/vault-search` returns a ranked list of matching notes.
`/vault-ask` returns a reasoned, cited answer synthesized from note content.

Use `/vault-ask` when the user wants to know _what_ their notes say, not _which_ notes match.

`/vault-ask` can also write: it saves an answer as a wiki page in `<wiki_folder>/queries/` when the answer draws on 3 or more qualifying notes, after asking (or automatically with `--caller`). It never saves an answer drawn from fewer than 3 qualifying notes.

## Edge Cases

- **Single result:** Synthesize from that one source. Be explicit about limited coverage: "Only one relevant note was found..."
- **All results are old (>90 days):** Mention this: "Your most recent notes on this topic are from `<date>`."
- **Question is ambiguous (multiple interpretations):** Answer each interpretation with a subheading, or ask the user to clarify before proceeding.
- **No matching tags, only content matches:** That is fine — tag matches are bonus scoring, not required.
- **Config exists but vault path is invalid:** Warn the user and suggest running `/obsidian-setup` again.
