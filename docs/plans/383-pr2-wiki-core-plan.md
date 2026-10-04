# #383 PR 2 (#395): wiki core — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/vault-ask` keeps expensive answers as wiki pages, finds them first on later asks, and refreshes them when their sources change.

**Architecture:** A new stdlib module `hooks/wiki.py` holds all wiki logic as library functions plus a JSON-in/JSON-out CLI (`rule`, `lookup`, `stale`, `count`, `file`). It reads and writes only through existing helpers: `vault_index` (`ensure_index`, `search_vault`, the `notes` table), `obsidian_utils` (`write_vault_note`, `scrub_secrets`, `load_config`, `indexed_folders`) and `note_writer` (`_acquire_lock`, `_release_lock`). `skills/vault-ask/SKILL.md` gains a wiki-first step (2b), a candidate cap, and a filing gate (Step 8) that calls the CLI.

**Tech Stack:** Python 3.9+ stdlib, SQLite FTS5, pytest (90% coverage gate covers `hooks/wiki.py`), Markdown skills.

**Spec:** `docs/plans/383-llm-wiki-design.md` (PR 2 section, Data model, Reviewed pages, Writing rule, Error handling, Scale). Acceptance criteria: issue #395.

## Global Constraints

- Python stdlib only; runs on Python 3.9 (no `X | None` at runtime, no `match`). `from __future__ import annotations` at the top of new modules.
- Branch `feature/395-wiki-core`, base `develop`. PR body: `Closes #395` and `Refs #383`.
- `./scripts/commit-preflight.sh` before every commit, in the foreground (full suite, `--cov-fail-under=90`).
- Commit trailer:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01A67ZCFdJv7xQPkosWQEWam
  ```
- No test touches the live vault, live config or live DB. Library tests pass explicit `vault_path`, `wiki_folder` and `db_path`. CLI tests run in a subprocess with `HOME` set to a temp dir (so `load_config` reads a temp config) and `OBSIDIAN_BRAIN_DB` set to a temp DB.
- Every vault write: atomic (`write_vault_note`: temp file, `0o600`, rename), contained under `<vault>/<wiki_folder>`, and done while holding the wiki lock.
- `scrub_secrets` runs on the page body and on the question before anything is written.
- Frontmatter values written by `wiki.py` are single-line JSON (valid YAML flow scalars), except `tags`, which uses the block list the indexer parses (`tags:` then `  - x` lines).
- The plan's code is a sketch. Implement what it means; fix what is wrong and say so. You are expected to find at least one thing.

## Deviations from the spec (reconciled in Task 7)

1. **Index lines omit `reviewed`.** The spec's index line ends `(updated <date>, <confidence>[, reviewed])`. The index is rebuilt from the `notes` table, which has no `reviewed` column, and reading every page file to find it is the per-page cost the Scale section rules out. `confidence` is carried as a tag (`claude/wiki/confidence-<level>`) so it is in the table. Index line: `- [[<slug>]] — <question> (updated <date>, <confidence>)`.
2. **`date:` is written as the `updated` date.** The indexer stores `date`, not `updated`; the index sort, `lookup`'s `updated` field and the stale check's "newer note" comparison read it from the table.
3. **Memory sources are refused in PR 2.** `count` and `file` reject a non-empty `memory_sources` with "memory sources arrive in #396". PR 3 removes the refusal.
4. **Spec Delivery section** is corrected: each PR closes its own child issue (#394, #395, #396); the epic #383 closes when all three are closed.

## Review Focus

1. **A question containing quotes, newlines, `---`, `[[`, or a fake secret.** Expected: the page frontmatter stays one valid block (JSON-encoded scalars), the question is scrubbed, and the indexer still parses the page (`malformed` 0). Pinned in Task 4.
2. **Two `file` calls at once (two auto-filing sub-agents).** Expected: one wins the lock, the other exits 1 with an `ERROR:` naming the lock; no partial page, no interleaved log lines. Pinned in Task 4.
3. **A stale page whose sources were deleted down to fewer than 3.** Expected: `file` with `update` refuses (count below 3) and the page is unchanged. Pinned in Task 4.
4. **A cited source basename that matches two notes, or none.** Expected: none → refused as unresolved; two → refused as ambiguous, naming both paths (no silent first match). Pinned in Task 2.
5. **The year boundary.** Expected: an entry on 2026-12-31 goes to `log-2026.md` and one on 2027-01-01 to `log-2027.md`, each file starting with the `claude-wiki-index` frontmatter. Pinned in Task 4.

---

## File structure

| File | Change | Responsibility |
|---|---|---|
| `hooks/wiki.py` | create | constants, page format, source resolution, count, lookup, stale, file, index and log writers, CLI |
| `tests/test_wiki.py` | create | library tests |
| `tests/test_wiki_cli.py` | create | subprocess CLI tests (exit codes, stdin cap, JSON shape) |
| `skills/vault-ask/SKILL.md` | modify | `--caller`, Step 2b, candidate cap, wiki folder in the Grep fallback, filing gate |
| `tests/test_vault_ask_wiki_skill_text.py` | create | pins the skill's wiki instructions |
| `tests/test_type_scores.py` | modify | `claude-wiki` is now written by `hooks/wiki.py` |
| `CHANGELOG.md`, `docs/architecture/architecture.json` + `.html`, `docs/plans/383-llm-wiki-design.md` | modify | docs |

---

### Task 1: `wiki.py` skeleton — constants, page format helpers, `rule`, CLI plumbing

**Files:** Create `hooks/wiki.py`, `tests/test_wiki.py`, `tests/test_wiki_cli.py`.

**Interfaces — Produces:**
- `WRITING_RULE: str`; `COUNTING_TYPES: frozenset`; `CALLER_RE`, `TOPIC_RE` (compiled); `THRESHOLD = 3`; `INDEX_SPLIT = 500`.
- `class WikiRefusal(Exception)` — a refusal (CLI exit 1).
- `slugify(question: str) -> str` — lowercase, runs of non-`[a-z0-9]` → `-`, trimmed of `-`, cut to ≤60 chars at the last `-` before 60 (hard cut if none); empty → `"untitled"`.
- `is_reviewed(value) -> bool` — lenient: missing/`None`, `False`, `""`, `false`, `no`, `off`, `0` (after strip, strip quotes, lower) → False; everything else → True.
- `render_page(meta: dict, body: str) -> str` and `read_page(path) -> (meta: dict, body: str)` — meta values JSON per line; `tags` block list. `read_page` decodes JSON values and falls back to the raw string when a value is not JSON (a hand-edited page).
- `main(argv=None) -> int` — dispatch; reads stdin (capped at 1,000,000 chars) for subcommands that take input; prints JSON; `WikiRefusal` → stderr `ERROR: <msg>`, return 1; unknown subcommand or any other exception → stderr `ERROR: ...`, return 2.

- [ ] **Step 1: failing tests** (`tests/test_wiki.py`)

```python
from __future__ import annotations
import pytest
import wiki


def test_rule_is_80_percent_ste():
    r = wiki.WRITING_RULE
    for phrase in ("ASD-STE100", "80%", "20 words", "25", "active voice",
                   "One topic per sentence", "wikilinks"):
        assert phrase in r, phrase


@pytest.mark.parametrize("q,slug", [
    ("Why did we pick SQLite FTS5?", "why-did-we-pick-sqlite-fts5"),
    ("  ***  ", "untitled"),
    ("a" * 80, "a" * 60),
    ("word " * 20, "word-word-word-word-word-word-word-word-word-word-word-word"),
])
def test_slugify(q, slug):
    assert wiki.slugify(q) == slug
    assert len(wiki.slugify(q)) <= 60


@pytest.mark.parametrize("v,exp", [
    (None, False), (False, False), ("", False), ("false", False), ("No", False), ("off", False), ("0", False),
    ("true", True), (" yes ", True), ("On", True), ("1", True), ('"true "', True), ("maybe", True), (True, True),
])
def test_is_reviewed_is_lenient(v, exp):
    assert wiki.is_reviewed(v) is exp


def test_render_and_read_round_trip(tmp_path):
    meta = {"type": "claude-wiki", "title": 'He said "hi"\n---', "question": 'He said "hi"\n---',
            "sources": ["[[a]]", "[[b]]"], "sources_fingerprint": {"a": "0" * 16},
            "tags": ["claude/wiki", "claude/project/demo"]}
    text = wiki.render_page(meta, "Body.\n")
    assert text.count("\n---\n") == 1  # one closing fence only
    p = tmp_path / "p.md"
    p.write_text(text)
    m2, body = wiki.read_page(p)
    assert m2["question"] == meta["question"] and m2["sources"] == meta["sources"]
    assert m2["tags"] == meta["tags"] and body.strip() == "Body."
```

`tests/test_wiki_cli.py`:

```python
from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def run(args, stdin="", home=None, db=None):
    env = dict(os.environ)
    if home:
        env["HOME"] = str(home)
    if db:
        env["OBSIDIAN_BRAIN_DB"] = str(db)
    env["CLAUDE_CODE_SESSION_ID"] = "wiki-cli-test"
    return subprocess.run([sys.executable, str(REPO / "hooks" / "wiki.py"), *args],
                          input=stdin, capture_output=True, text=True, env=env, timeout=60)


def test_rule_prints_json():
    r = run(["rule"])
    assert r.returncode == 0 and "ASD-STE100" in json.loads(r.stdout)["rule"]


def test_unknown_subcommand_exits_2():
    assert run(["nope"]).returncode == 2


def test_oversized_stdin_is_refused(tmp_path):
    r = run(["count"], stdin="x" * 1_000_001, home=tmp_path)
    assert r.returncode == 1 and "ERROR:" in r.stderr and "too large" in r.stderr


def test_bad_json_is_refused(tmp_path):
    r = run(["count"], stdin="{not json", home=tmp_path)
    assert r.returncode == 1 and "ERROR:" in r.stderr
```

- [ ] **Step 2: run** `python3 -m pytest tests/test_wiki.py tests/test_wiki_cli.py -q` → FAIL (`No module named wiki` / file missing).

- [ ] **Step 3: implement** `hooks/wiki.py` (skeleton):

```python
"""LLM wiki for /vault-ask answers (#383, #395).

Library + JSON CLI. Every write goes through write_vault_note (atomic,
0o600, contained) under one wiki lock. See docs/plans/383-llm-wiki-design.md.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STDIN_CAP_CHARS = 1_000_000
THRESHOLD = 3
INDEX_SPLIT = 500
COUNTING_TYPES = frozenset({"claude-insight", "claude-error-fix", "claude-decision",
                            "claude-retro", "claude-session", "claude-memory"})
CALLER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
CONFIDENCE = ("high", "medium", "low")

WRITING_RULE = """Write this wiki page "80% of the way to ASD-STE100" (Simplified Technical English):
- Short sentences: at most 20 words in procedures, 25 in descriptions.
- One topic per sentence. Use the active voice.
- One meaning per word; use the same term for the same thing throughout. No filler words.
- Keep technical terms, names, paths, flags, numbers and quoted text exact. Never swap a precise term for a simpler word if that loses information.
- Never reword citations or [[wikilinks]].
- A Mermaid diagram is allowed when the answer needs one; no other formats."""


class WikiRefusal(Exception):
    """A refused operation: the CLI prints ERROR: <msg> and exits 1."""


def slugify(question: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")
    if len(s) > 60:
        cut = s.rfind("-", 0, 61)
        s = s[:cut] if cut > 0 else s[:60]
    return s.strip("-") or "untitled"


_FALSE = {"", "false", "no", "off", "0"}


def is_reviewed(value) -> bool:
    """Lenient on purpose: a miss would overwrite the user's edits."""
    if value is None or value is False:
        return False
    if value is True:
        return True
    return str(value).strip().strip("\"'").strip().lower() not in _FALSE


def render_page(meta: dict, body: str) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if k == "tags":
            lines.append("tags:")
            lines += [f"  - {t}" for t in v]
        else:
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body.rstrip("\n") + "\n"


def read_page(path) -> tuple:
    from frontmatter import split_frontmatter, split_lines_lf_crlf
    text = Path(path).read_text(encoding="utf-8")
    _o, fm, _c, body_lines, err = split_frontmatter(split_lines_lf_crlf(text))
    if err:
        raise WikiRefusal(f"{path}: {err}")
    meta, tags, in_tags = {}, [], False
    for raw in fm:
        s = raw.strip()
        if in_tags and s.startswith("- "):
            tags.append(s[2:].strip())
            continue
        in_tags = False
        key, sep, val = s.partition(":")
        if not sep:
            continue
        key, val = key.strip(), val.strip()
        if key == "tags" and not val:
            in_tags = True
            continue
        try:
            meta[key] = json.loads(val)
        except ValueError:
            meta[key] = val
    if tags:
        meta["tags"] = tags
    return meta, "".join(body_lines)
```

CLI tail:

```python
def _read_stdin() -> dict:
    data = sys.stdin.read(STDIN_CAP_CHARS + 1)
    if len(data) > STDIN_CAP_CHARS:
        raise WikiRefusal(f"input too large (over {STDIN_CAP_CHARS} characters)")
    try:
        obj = json.loads(data or "{}")
    except ValueError as exc:
        raise WikiRefusal(f"input is not JSON: {exc}")
    if not isinstance(obj, dict):
        raise WikiRefusal("input must be a JSON object")
    return obj


def _context() -> dict:
    """vault, wiki folder, folders and DB from a fresh config read."""
    from obsidian_utils import load_config, indexed_folders
    import vault_index
    cfg = load_config(fresh=True)
    vault = cfg.get("vault_path") or ""
    if not vault:
        raise WikiRefusal("vault_path not configured; run /obsidian-setup")
    try:
        folders = indexed_folders(cfg, strict=True)
    except ValueError as exc:
        raise WikiRefusal(str(exc))
    wiki_folder = cfg.get("wiki_folder") or ""
    if not wiki_folder:
        raise WikiRefusal("wiki_folder is empty (the wiki is turned off)")
    return {"vault": vault, "wiki_folder": os.path.normpath(wiki_folder),
            "folders": folders, "db": vault_index._default_db_path()}


_COMMANDS: dict = {}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmds = {"rule": lambda: {"rule": WRITING_RULE}}
    cmds.update(_COMMANDS)
    if not argv or argv[0] not in cmds:
        print(f"ERROR: usage: wiki.py {{{'|'.join(sorted(cmds))}}}", file=sys.stderr)
        return 2
    try:
        out = cmds[argv[0]]()
    except WikiRefusal as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # a crash, not a refusal
        print(f"ERROR: internal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Later tasks add `lookup`, `stale`, `count` and `file` to `_COMMANDS`; each entry reads `_read_stdin()` and `_context()` and calls the library function. In this task register `count` as `_read_stdin()` only, so the stdin tests can run; Task 2 replaces it.

- [ ] **Step 4: run** the two test files → PASS.
- [ ] **Step 5: commit** `feat(obsidian-brain): add wiki.py skeleton, writing rule and CLI plumbing (#395)`.

---

### Task 2: source resolution and `count`

**Interfaces — Produces:**
- `resolve_sources(db_path: str, names: list, roots: list) -> dict` returns `{"resolved": [{"name", "path", "type", "project", "key"}], "rejected": [{"name", "reason"}]}`. A name may be `x`, `x.md`, `[[x]]` or `[[x|alias]]`; it is normalised to the basename without `.md`. Lookup is in the `notes` table: rows whose path ends in `/<name>.md` and lies under one of `roots` (absolute folder paths). 0 rows → rejected `"unresolved"`; 2+ rows → rejected `"ambiguous: <path1>, <path2>"`. `key` is the parent session basename for a `claude-snapshot` (from `notes.source_note`), else the note's own basename.
- `count_sources(db_path, names, memory_sources, roots) -> dict` returns `{"count": N, "qualifying": [keys], "other": [names], "rejected": [...]}`. `count` = number of distinct `key`s whose type (a snapshot counts as `claude-session`) is in `COUNTING_TYPES`. Non-empty `memory_sources` → each entry rejected with `"memory sources arrive in #396"`.
- CLI `count`: input `{"sources": [...], "memory_sources": [...]}`; runs `ensure_index(vault, folders, db_path=db)` first, then prints the dict. It never refuses for a low count (the gate is the caller's call); it refuses only on bad input.

- [ ] **Step 1: failing tests** — a fixture builds a temp vault with 2 insights, 1 decision, 1 session `s1`, 1 snapshot with `source_session_note: "[[s1]]"`, 1 standup, and two notes named `dup.md` (one in sessions, one in insights), then `ensure_index(vault, ["claude-sessions","claude-insights","claude-wiki"], db_path=tmp_db)`. Tests:
  - exactly 2 qualifying → `count == 2`; exactly 3 → `count == 3` (the boundary for the callers' `>= 3` gate);
  - snapshot + its parent session → counted once; snapshot alone → counted (key = parent);
  - `[[i1]]`, `i1.md`, `i1` and `[[i1|alias]]` resolve to one key and count once;
  - standup → in `other`, not counted;
  - `nope` → rejected `unresolved`; `dup` → rejected, reason starts `ambiguous:` and names both paths;
  - `memory_sources: ["x/y.md"]` → rejected with `#396`.
- [ ] **Step 2: run** → FAIL. **Step 3: implement**:

```python
def _norm_name(n) -> str:
    n = str(n).strip()
    if n.startswith("[[") and n.endswith("]]"):
        n = n[2:-2].split("|", 1)[0]
    return n[:-3] if n.endswith(".md") else n


def resolve_sources(db_path, names, roots):
    import vault_index
    conn = vault_index._connect(db_path)
    try:
        resolved, rejected, seen = [], [], set()
        for raw in names:
            name = _norm_name(raw)
            if not name or name in seen:
                continue
            seen.add(name)
            suffix = "/" + name + ".md"
            rows = [r for r in conn.execute(
                "SELECT path, type, project, source_note FROM notes WHERE substr(path, -?) = ?",
                (len(suffix), suffix)).fetchall()
                if any(vault_index._is_under(Path(r["path"]), Path(root)) for root in roots)]
            if not rows:
                rejected.append({"name": name, "reason": "unresolved"})
                continue
            if len(rows) > 1:
                rejected.append({"name": name, "reason": "ambiguous: " + ", ".join(r["path"] for r in rows)})
                continue
            r = rows[0]
            key = (r["source_note"] or name) if r["type"] == "claude-snapshot" else name
            resolved.append({"name": name, "path": r["path"], "type": r["type"],
                             "project": r["project"] or "", "key": key})
        return {"resolved": resolved, "rejected": rejected}
    finally:
        conn.close()
```

Check that `vault_index._connect` returns rows indexable by name (`sqlite3.Row`); if not, index by position and say so.

- [ ] **Step 4: run** → PASS. **Step 5: commit** `feat(obsidian-brain): resolve and count wiki sources (#395)`.

---

### Task 3: `lookup` and `stale`

**Interfaces — Produces:**
- `lookup(db_path, question, limit=3) -> list` — `search_vault(db_path, question, note_type="claude-wiki", limit=limit)`; each hit → `{"path", "question": title, "updated": date, "rank"}`. Empty or stopword-only question → `[]`.
- `fingerprint(path) -> str` — `sha256(file bytes).hexdigest()[:16]`.
- `stale(db_path, page_path, roots) -> dict` → `{"stale": bool, "reasons": [str]}`. Reads the page with `read_page`. For each `sources_fingerprint` entry (`name → hash`): resolve it (Task 2); unresolved, ambiguous or unreadable → `missing: <name>`; hash differs → `changed: <name>`. Then `search_vault(db, question, limit=5)`: a hit whose type is in `COUNTING_TYPES`, whose `date` is later than the page's `updated`, and whose basename is not among the page's sources → `newer: <basename>`. Every reason is returned, not only the first.
- CLI `lookup`: `{"question"}` → `{"candidates": [...]}`. CLI `stale`: `{"page"}`; the page must resolve under `<vault>/<wiki_folder>`, else refused.

- [ ] **Step 1: failing tests** (fixture: three insights and one wiki page written directly with `render_page`, then `ensure_index`):
  - `lookup` finds the page from a paraphrase sharing title words, returns ≤3, returns `[]` for `"the of and"`;
  - fresh page → `{"stale": False, "reasons": []}`;
  - content change in one source → `changed:`; `os.utime` only (same bytes, new mtime) → still fresh;
  - source deleted → `missing:`; source `chmod 000` (skip when running as root) → `missing:`;
  - a new insight dated after `updated` that shares the question's words → `newer:`; a newer note that is already a source → no `newer:`;
  - two triggers at once → both reasons.
- [ ] **Steps 2–4:** RED, implement, GREEN.
- [ ] **Step 5: commit** `feat(obsidian-brain): wiki lookup and staleness check (#395)`.

---

### Task 4: `file` — validation, page write, lock, index and log

**Interfaces — Consumes:** Tasks 1–3; `write_vault_note`, `scrub_secrets` (obsidian_utils); `_acquire_lock`, `_release_lock` (note_writer); `ensure_index` (vault_index).

**Produces:** `file_page(ctx: dict, payload: dict, today) -> dict` → `{"path", "action": "file"|"update"|"file-auto", "count"}`; `rebuild_index(ctx) -> list` (paths written); `append_log(ctx, op, caller, question, basename, today)`. CLI `file`: stdin payload; `today` = UTC date.

Payload: `question` (str, 1–500 chars after strip), `body` (non-empty str), `sources` (list), `memory_sources` (list; must be empty in PR 2), `topics` (≤8 `TOPIC_RE` strings), `confidence` (`high|medium|low`), `filed_by` (`user|auto`), `caller` (required and `CALLER_RE`-valid when `filed_by == "auto"`; absent or empty otherwise), `update` (optional page path), `override_reviewed` (optional bool).

Order inside `file_page`:
1. Validate fields → a `WikiRefusal` with a specific message each.
2. `ensure_index(vault, folders, db_path=db)`; `count_sources(...)`; any `rejected` → refuse naming them; `count < THRESHOLD` → refuse `"only N qualifying sources (need 3)"`.
3. With `update`: resolve it; require `is_relative_to(<vault>/<wiki_folder>/queries)`, that it exists, and `read_page` type `claude-wiki`; `is_reviewed(meta.get("reviewed"))` without `override_reviewed` → refuse `"page is marked reviewed"`; keep its `created` and file name.
   Without `update`: `<wiki_folder>/queries/<YYYY>/<MM-DD>-<slug>.md`; if taken, try `-2`, `-3`, … .
4. Meta, in this order: `type`, `title` (scrubbed question), `question` (same), `date` (today), `created`, `updated` (today), `projects` (sorted unique non-empty `project` of the resolved sources), `sources` (`[[name]]` per resolved source, input order), `memory_sources` (`[]`), `sources_fingerprint` (`{name: fingerprint(path)}`), `confidence`, `filed_by`, `caller` (auto only), `tags` (`claude/wiki`, `claude/wiki/confidence-<c>`, `claude/project/<p>` per project, `claude/topic/<t>` per topic). Body: `scrub_secrets(body)`.
5. `_acquire_lock(<vault>/<wiki_folder>/.wiki)`; an error → refuse with it. In `try/finally`, release. Inside:
   a. `write_vault_note(vault, folder, filename, render_page(meta, body))`; an error string → refuse.
   b. `ensure_index(...)` so the page is in the table.
   c. `rebuild_index(ctx)`; d. `append_log(...)`. If c or d raises → `WikiRefusal("page written at <path>, but the <index|log> update failed: <exc>; the next write repairs the index")`.
6. `action`: `update` with `update`; else `file-auto` when auto; else `file`.

`rebuild_index(ctx)`: `SELECT path, title, date, tags FROM notes WHERE type='claude-wiki' ORDER BY date DESC, path`, keeping paths under `<vault>/<wiki_folder>/queries`. Line: `- [[<basename>]] — <title> (updated <date>, <confidence from the tag, or ?>)`. Each index file is `---\ntype: "claude-wiki-index"\n---\n# Wiki index\n\n` plus lines. ≤ `INDEX_SPLIT` pages → `index.md` only; delete any `index-*.md`. Above → `index.md` lists `- [[index-<project>]] — <n> page(s)` per project (`index-unassigned` for pages with none), each `index-<project>.md` lists its pages, and `index-*.md` files not in the new set are deleted. Project names in file names go through `slugify`.

`append_log`: `log-<YYYY>.md` from `today`. If missing, start with the index frontmatter and `# Wiki log`. Append `\n## [<YYYY-MM-DD>] <op> | <caller or -> | <question>\n- <Created|Updated>: [[<basename>]]\n` and rewrite the file with `write_vault_note`.

- [ ] **Step 1: failing tests** (`ctx` fixture: temp vault with 4 counting notes, temp DB, `wiki_folder="claude-wiki"`):
  - happy path: page at `claude-wiki/queries/2026/10-04-<slug>.md`, mode `0o600`; `read_page` meta matches; the indexer parses it (sync `malformed == 0`, row type `claude-wiki`); `index.md` lists it; `log-2026.md` has a `file` entry; neither index nor log is in `notes`;
  - golden: the rendered page for a fixed payload and date equals a literal string;
  - refusals, each with its message and **no file written**: 2 sources; an unresolved source; bad confidence; `filed_by: auto` without caller; caller `"Bad Caller"`; topic `"Not OK"`; empty body; a 501-char question; non-empty `memory_sources`; `update` outside `queries/`; `update` of a non-wiki note;
  - reviewed: `update` of a page with `reviewed: "yes"` → refused; with `override_reviewed: true` → succeeds and the new page has no `reviewed` key;
  - update keeps `created`, moves `updated`/`date`, keeps the file name, logs `update`; auto filing logs `file-auto | <caller>`;
  - collision: two questions with the same slug on one day → `-2`;
  - secrets: a fake `ghp_` token in the body and in the question is redacted on disk;
  - lock: a fresh lock file pre-created → refused naming the lock; no page, no log;
  - stale refresh with sources deleted below 3 → refused; page bytes unchanged;
  - index split: monkeypatch `INDEX_SPLIT = 2`, file 3 pages over 2 projects → `index.md` lists 2 project links and two `index-<p>.md` exist; restore the split, file one more → `index-*.md` removed;
  - year boundary: `today=date(2026,12,31)` then `date(2027,1,1)` → two log files, each opening with the `claude-wiki-index` frontmatter;
  - Review Focus 1: question `'He said "hi"\n---\n[[x]]'` → one frontmatter block; the indexer parses the page.
- [ ] **Steps 2–4:** RED, implement, GREEN. CLI tests in `tests/test_wiki_cli.py`: `file` happy path in a subprocess (temp HOME config, temp DB) → exit 0 with JSON `path`; a refusal → exit 1 with `ERROR:`; `stale` on a path outside the wiki → exit 1.
- [ ] **Step 5: mutation check** (scratch copy, `PYTHONDONTWRITEBYTECODE=1`, one at a time): threshold `<` → `<=`; drop the reviewed check; drop `scrub_secrets` on the question; drop the lock acquire; drop the `is_relative_to` check on `update`. Each must fail a named test. Record results.
- [ ] **Step 6: commit** `feat(obsidian-brain): file wiki pages with index, log and lock (#395)`.

---

### Task 5: `/vault-ask` — wiki first, candidate cap, filing gate

**Files:** Modify `skills/vault-ask/SKILL.md`; create `tests/test_vault_ask_wiki_skill_text.py`.

Changes (keep the existing `_ob_hooks` resolution, the `HOOKS=$(…)` pattern and a `test -f "$HOOKS/wiki.py"` guard like the one for `vault_scan.py`):

1. **Arguments.** Document `/vault-ask --caller <name> <question>`. `<name>` must match `^[a-z0-9][a-z0-9_.-]{0,63}$`; anything else stays part of the question and the run counts as user-typed. Store `CALLER` (empty when user-typed).
2. **Step 1** also prints `WIKI=<wiki_folder>`; empty means the wiki is off, and Steps 2b and the Step 8 filing gate are skipped.
3. **Step 2b — wiki first.** `wiki.py lookup` with `{"question": <original question>}`. If a candidate asks the same question (the model judges; no score threshold): `wiki.py stale` on it.
   - Fresh: read the page, present its answer, cite `[[<page>]]`, say "From the wiki (updated <date>)". Skip Steps 3–7; Step 8 writes nothing.
   - Stale, not reviewed: run Steps 3–7 with the page's sources added to `CANDIDATE_FILES`; Step 8 refreshes the page without asking and tells the user why (`reasons`).
   - Stale and reviewed (lenient rule; `wiki.py file` enforces it regardless): answer from the page and warn with the reasons. User-typed: AskUserQuestion "Keep my page" / "Refresh and overwrite my edits" / "Save the fresh answer as a new page". `--caller`: answer from the page, add one line that it is reviewed and stale, write nothing.
4. **Steps 3–5:** keep at most 3 `claude-wiki` notes in `CANDIDATE_FILES`; drop notes typed `claude-wiki-index`. Step 4's Grep agents and the `vault_scan.py grep` fallback also search `<vault>/<wiki_folder>`. Step 5 gives `claude-wiki` +3, like insights.
5. **Step 8 — filing gate** (replaces "Do NOT write anything to the vault — this skill is read-only."):
   - Payloads: write the JSON with the Write tool to `~/.claude/obsidian-brain/wiki-payload-<8 random hex>.json`, run `python3 "$HOOKS/wiki.py" <cmd> < <that file>`, then `rm -f` it. Never build the JSON in a shell string.
   - `wiki.py count` with the cited sources. Below 3: write nothing and say nothing about the wiki.
   - Before writing a page body, run `wiki.py rule` and rewrite the answer under that rule, keeping the `### Sources` section. The chat answer keeps its normal style.
   - Stale refresh: `file` with `update`, no prompt. `--caller`: `lookup`; a same-question match → `file` with `update` (a reviewed match: write nothing and name the page); else `file` new with `filed_by: "auto"` and `caller`; then one line naming the page. User-typed: AskUserQuestion "Save as a new wiki page" / "Update existing page [[…]]" (only with a same-question candidate) / "Skip"; Skip writes nothing.
   - `confidence`: "You explicitly decided" → `high`; "it appears" → `medium`; "Limited context" → `low`.
   - `wiki.py` exit 1: show its `ERROR:` line and never say the page was filed.
6. **Key distinction / Edge cases:** one line each: `/vault-ask` can now write wiki pages; the wiki never stores an answer drawn from fewer than 3 sources.

- [ ] **Step 1: failing skill-text test**

```python
from pathlib import Path
SKILL = (Path(__file__).resolve().parent.parent / "skills/vault-ask/SKILL.md").read_text()


def test_read_only_statement_is_gone():
    assert "this skill is read-only" not in SKILL


def test_wiki_commands_are_used():
    for cmd in ("wiki.py\" lookup", "wiki.py\" stale", "wiki.py\" count", "wiki.py\" rule", "wiki.py\" file", "--caller"):
        assert cmd in SKILL, cmd


def test_payload_goes_through_a_file_not_a_shell_string():
    assert "wiki-payload-" in SKILL and "Never build the JSON in a shell string" in SKILL


def test_candidate_cap_and_index_exclusion():
    assert "at most 3" in SKILL and "claude-wiki-index" in SKILL


def test_reviewed_choices_offered():
    for opt in ("Keep my page", "Refresh and overwrite my edits", "Save the fresh answer as a new page"):
        assert opt in SKILL, opt
```

Adjust substrings to the final prose if needed, keeping one assertion per behaviour; each must fail on today's `SKILL.md`.

- [ ] **Steps 2–4:** RED, edit, GREEN. `tests/test_skill_snippets.py` and `tests/test_vault_scan_skill_text.py` stay green (update the latter only if a pinned line moved, and say so).
- [ ] **Step 5: commit** `feat(obsidian-brain): vault-ask files and reuses wiki pages (#395)`.

---

### Task 6: type-score test and early live check

- [ ] **Step 1:** in `tests/test_type_scores.py`, remove `"claude-wiki"` from `_EXTERNAL_TYPES` (the scan now finds `hooks/wiki.py`) and add `"claude-wiki-index"` to the expected set in `test_scan_finds_exactly_the_known_types`; the weight test keeps subtracting `_UNINDEXED_TYPES`. Commit as `test:` (or with Task 5 if it lands in the same preflight).
- [ ] **Step 2: early live check** (ship Phase 4): copy the live vault's sessions and insights folders into a temp vault; temp HOME config pointing at it; `OBSIDIAN_BRAIN_DB` at a temp DB. Through the CLI, in order: `count` for 3 real insight basenames → `count >= 3`; `file` → exit 0; `lookup` with a paraphrase → finds it; touch-only a source → `stale` false; edit a source → `stale` true with `changed:`; `file` with `update` → exit 0 and a log `update`. Checksum the live DB before and after. Record outputs.

---

### Task 7: docs

- [ ] `CHANGELOG.md` `[Unreleased]` → `### Added`, short sentences: `/vault-ask` can save answers that cite 3+ notes as wiki pages (it asks; `--caller` saves without asking), finds them first next time, refreshes stale pages, and never rewrites a page marked `reviewed: true`. New `hooks/wiki.py` (#395, #383).
- [ ] `docs/architecture/architecture.json`: component `wiki` (`hooks/wiki.py`); flow `vault-ask-wiki` (lookup → stale → search → count → rule → file → index/log); update the vault-ask component and the `db-vault` text. `lastUpdated` = commit date. Re-render the HTML; smoke test `[main]` and `[sparse]` PASS.
- [ ] `docs/plans/383-llm-wiki-design.md`: reconcile deviations 1–4.
- [ ] Commit `docs: wiki core (#395)` after preflight.

---

## Done when

- `python3 -m pytest tests/ -q` passes; coverage ≥ 90% with `hooks/wiki.py` in the gate.
- Task 4's five mutations each fail a named test.
- Task 6's early live check is recorded; live DB unchanged.
- Architecture smoke test passes.
