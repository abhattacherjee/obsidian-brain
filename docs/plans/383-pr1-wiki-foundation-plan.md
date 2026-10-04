# #383 PR 1: wiki foundation — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the vault index cover a third folder, `claude-wiki`, through one helper, so PR 2 can write wiki pages that search finds, while keeping the wiki's own index and log files out of search.

**Architecture:** A new `indexed_folders(config)` helper in `hooks/obsidian_utils.py` returns the folders that user-facing search indexes. Every skill and script that builds a folder list for `ensure_index`/`rebuild_index` calls it. The indexer skips notes whose `type` is in a new `vault_index._UNINDEXED_TYPES` set (`claude-wiki-index`), which PR 2's index and log files will carry. `claude-wiki` gets a rerank weight in every context.

**Tech Stack:** Python 3.9+ stdlib, SQLite FTS5, pytest (90% coverage gate on `hooks/`, `obsidian_utils.py` excluded), Markdown skills.

**Spec:** `docs/plans/383-llm-wiki-design.md` (PR 1 section). Task 6 reconciles two deliberate deviations into the spec.

## Global Constraints

- Python stdlib only; must run on Python 3.9 (CI runs a py3.9 job): no `X | None` in runtime-evaluated annotations, no `match`.
- Branch `feature/383-wiki-foundation`, base `develop`. PR body says `Refs #383` (not `Closes`).
- Run `./scripts/commit-preflight.sh` before every commit; it runs the full suite with `--cov-fail-under=90`. Do not background it.
- Commit messages: conventional (`feat(obsidian-brain):`, `test:`, `docs:`), ending with:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01A67ZCFdJv7xQPkosWQEWam
  ```
  (An implementer on another model names that model in `Co-Authored-By`.)
- Default wiki folder name: `claude-wiki`. Config key: `wiki_folder`.
- No test touches the live vault or the live DB (`~/.claude/obsidian-brain-vault.db`); every index test passes an explicit `db_path` under `tmp_path`.
- The plan's sample code is a sketch. Implement what it means, fix what is wrong and say so. You are expected to find at least one thing; if you find nothing, say what you checked.

## Deviations from the spec (reconciled in Task 6)

1. **Exclusion by type, not by filename.** The spec has `_sync` take an `exclude_root_files` argument fed by an `index_exclusions(config)` helper. This plan instead skips notes whose frontmatter `type` is `claude-wiki-index`. Reasons: no signature change to `_sync`/`ensure_index`/`rebuild_index` or their callers; no name collision (a user note named `index.md` is still indexed because it lacks the type); PR 2's index and log files need frontmatter anyway to stay out of the "malformed" count. PR 2 must write `type: claude-wiki-index` into every index and log file.
2. **Two hooks keep explicit folder lists.** `obsidian_utils.build_context_brief` (`hooks/obsidian_utils.py:5145`) and `open_item_dedup.deep_analysis_pipeline` (`hooks/open_item_dedup.py:1279-1281`) receive `sessions_folder`/`insights_folder` as parameters from their callers, not a config. They keep their lists, with a comment. This is safe: `_sync` only deletes rows under the folders it scans (`hooks/vault_index.py:778-788`), so a call without the wiki folder never removes wiki rows; it only skips refreshing them on that call. Only `rebuild_index` prunes rows outside its scanned folders, and both `rebuild_index` callers (`/vault-reindex`, `/obsidian-setup`) move to the helper. The guard test allow-lists those two hook files by name and reason.

## Review Focus

1. **`wiki_folder` set to `""`, `null`, or the same name as `sessions_folder`.** Expected: the helper drops empty/None values and duplicates, so no folder is scanned twice and an empty value disables the wiki folder. Pinned in Task 1.
2. **`wiki_folder` set to `../outside` or an absolute path.** Expected: the helper drops it with one stderr warning and still returns sessions and insights; it never makes `_sync` walk outside the vault. Pinned in Task 1.
3. **Existing users whose vault has no `claude-wiki` folder yet** (they have not re-run `/obsidian-setup`). Expected: search works exactly as before; `_sync` skips the missing folder. Pinned in Task 2.
4. **A note whose type changes to `claude-wiki-index` after it was indexed.** Expected: the next sync removes its row and FTS entry. Pinned in Task 2.
5. **`/vault-reindex` on a vault that already has wiki pages.** Expected: wiki rows survive a preserve-mode and a full rebuild, because the rebuild scans the wiki folder. Pinned in Task 2 (rebuild test) and Task 4 (skill uses the helper).

---

## File structure

| File | Change | Responsibility |
|---|---|---|
| `hooks/obsidian_utils.py` | modify | `_DEFAULTS["wiki_folder"]`, new `indexed_folders(config)` |
| `hooks/vault_index.py` | modify | `_UNINDEXED_TYPES`, skip + delete in `_sync`, `excluded` stat, `claude-wiki` weights |
| `hooks/open_item_dedup.py` | modify | comment only at `:1279` |
| `skills/vault-ask/SKILL.md`, `skills/vault-search/SKILL.md`, `skills/compress/SKILL.md` (2 sites), `skills/vault-stats/SKILL.md`, `skills/vault-reindex/SKILL.md`, `skills/obsidian-setup/SKILL.md` | modify | call the helper; setup creates the folder and writes the key |
| `skills/vault-config/SKILL.md` | modify | `wiki_folder` as an editable string setting |
| `scripts/tune_compress_rank_gap.py`, `scripts/test-phase1-manual.sh` | modify | call the helper |
| `tests/test_indexed_folders.py` | create | helper behaviour + call-site guard |
| `tests/test_vault_index_unindexed_types.py` | create | exclusion behaviour |
| `tests/test_type_scores.py` | modify | `claude-wiki` known; unindexed types need no weight |
| `CHANGELOG.md`, `README.md`, `docs/architecture/architecture.json` + `.html`, `docs/plans/383-llm-wiki-design.md` | modify | docs |

---

### Task 1: `wiki_folder` default and `indexed_folders(config)`

**Files:**
- Modify: `hooks/obsidian_utils.py:2229-2252` (`_DEFAULTS`); add the helper right after `load_config` (ends near `:2312`)
- Test: `tests/test_indexed_folders.py` (create)

**Interfaces:**
- Produces: `indexed_folders(config: dict) -> list` — returns `[sessions, insights, wiki]` in that order, defaults applied for missing keys, empty/None and duplicate values dropped, an invalid `wiki_folder` dropped with a stderr warning. `_DEFAULTS["wiki_folder"] == "claude-wiki"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_indexed_folders.py
"""indexed_folders(config): the one list of folders user-facing search indexes (#383)."""
from __future__ import annotations

import obsidian_utils
from obsidian_utils import indexed_folders


def test_default_has_wiki_folder():
    assert obsidian_utils._DEFAULTS["wiki_folder"] == "claude-wiki"


def test_order_and_defaults_from_empty_config():
    assert indexed_folders({}) == ["claude-sessions", "claude-insights", "claude-wiki"]


def test_uses_configured_names():
    cfg = {"sessions_folder": "s", "insights_folder": "i", "wiki_folder": "w"}
    assert indexed_folders(cfg) == ["s", "i", "w"]


def test_empty_or_none_wiki_folder_disables_it():
    assert indexed_folders({"wiki_folder": ""}) == ["claude-sessions", "claude-insights"]
    assert indexed_folders({"wiki_folder": None}) == ["claude-sessions", "claude-insights"]


def test_duplicate_names_are_scanned_once():
    cfg = {"sessions_folder": "notes", "insights_folder": "notes", "wiki_folder": "notes"}
    assert indexed_folders(cfg) == ["notes"]


def test_traversal_wiki_folder_is_dropped_with_warning(capsys):
    assert indexed_folders({"wiki_folder": "../outside"}) == ["claude-sessions", "claude-insights"]
    assert "wiki_folder" in capsys.readouterr().err


def test_absolute_wiki_folder_is_dropped(capsys):
    assert indexed_folders({"wiki_folder": "/tmp/x"}) == ["claude-sessions", "claude-insights"]
    assert "wiki_folder" in capsys.readouterr().err


def test_non_string_wiki_folder_is_dropped(capsys):
    assert indexed_folders({"wiki_folder": 7}) == ["claude-sessions", "claude-insights"]
    assert "wiki_folder" in capsys.readouterr().err


def test_nested_wiki_folder_is_allowed():
    assert indexed_folders({"wiki_folder": "kb/claude-wiki"})[-1] == "kb/claude-wiki"


def test_does_not_mutate_input():
    cfg = {"wiki_folder": "w"}
    indexed_folders(cfg)
    assert cfg == {"wiki_folder": "w"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_indexed_folders.py -v`
Expected: FAIL with `ImportError: cannot import name 'indexed_folders'`.

- [ ] **Step 3: Implement**

In `_DEFAULTS`, after `"check_items_folder": "claude-check-items",` add:

```python
    "wiki_folder": "claude-wiki",  # #383: /vault-ask answers filed as wiki pages; indexed via indexed_folders()
```

After `load_config`, add:

```python
def indexed_folders(config: dict) -> list:
    """Vault folders that user-facing search indexes, in a fixed order (#383).

    Returns ``[sessions_folder, insights_folder, wiki_folder]`` with defaults
    for missing keys. Empty or None values and duplicates are dropped, so an
    empty ``wiki_folder`` turns the wiki folder off. An invalid
    ``wiki_folder`` (absolute, ``~``, a ``..`` or dot-prefixed segment) is
    dropped with a stderr warning, so ``_sync`` never walks outside the
    vault. Sessions and insights pass through unchanged, as before.

    Every skill and script that passes a folder list to ``ensure_index`` or
    ``rebuild_index`` must call this. ``tests/test_indexed_folders.py``
    enforces it.
    """
    from note_writer import _validate_folder

    names = [
        config.get("sessions_folder") or _DEFAULTS["sessions_folder"],
        config.get("insights_folder") or _DEFAULTS["insights_folder"],
    ]
    wiki = config.get("wiki_folder", _DEFAULTS["wiki_folder"])
    if wiki and not isinstance(wiki, str):
        print(f"[obsidian-brain] ignoring non-string wiki_folder {wiki!r}", file=sys.stderr)
        wiki = None
    if wiki:
        err = _validate_folder(wiki)
        if err:
            print(f"[obsidian-brain] ignoring invalid wiki_folder {wiki!r}: {err}", file=sys.stderr)
        else:
            names.append(wiki)
    out = []
    for n in names:
        if n not in out:
            out.append(n)
    return out
```

`note_writer._validate_folder` (`hooks/note_writer.py:79`) returns an error string or None (checked 2026-10-03). `note_writer` imports `obsidian_utils` at module level, so the import must stay inside the function. Confirm there is no import cycle with `python3 -c "import sys; sys.path.insert(0,'hooks'); import obsidian_utils"`.

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_indexed_folders.py tests/test_obsidian_utils.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add hooks/obsidian_utils.py tests/test_indexed_folders.py
./scripts/commit-preflight.sh
git commit -m "feat(obsidian-brain): add wiki_folder config and indexed_folders helper (#383)"
```

---

### Task 2: indexer skips `claude-wiki-index` notes

**Files:**
- Modify: `hooks/vault_index.py` — new module constant; `_sync` (`:729-860`)
- Test: `tests/test_vault_index_unindexed_types.py` (create)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `vault_index._UNINDEXED_TYPES: frozenset = frozenset({"claude-wiki-index"})`. `_sync` stats gain `"excluded": int`. PR 2 writes index and log files with `type: claude-wiki-index`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_vault_index_unindexed_types.py
"""Notes typed claude-wiki-index stay out of the index (#383)."""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import vault_index

NOTE = "---\ntype: {t}\ndate: 2026-10-03\nproject: demo\n---\n# {title}\n\nzebracorn body text\n"


def _write(p: Path, t: str, title: str = "T") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(NOTE.format(t=t, title=title), encoding="utf-8")


def _paths(db: str) -> set:
    conn = sqlite3.connect(db)
    try:
        return {Path(r[0]).name for r in conn.execute("SELECT path FROM notes")}
    finally:
        conn.close()


def test_constant():
    assert vault_index._UNINDEXED_TYPES == frozenset({"claude-wiki-index"})


def test_wiki_index_file_not_indexed_but_pages_are(tmp_path):
    vault = tmp_path / "v"
    _write(vault / "claude-wiki" / "index.md", "claude-wiki-index")
    _write(vault / "claude-wiki" / "log-2026.md", "claude-wiki-index")
    _write(vault / "claude-wiki" / "queries" / "2026" / "10-03-q.md", "claude-wiki")
    db = vault_index.ensure_index(str(vault), ["claude-wiki"], db_path=str(tmp_path / "i.db"))
    assert _paths(db) == {"10-03-q.md"}


def test_user_note_named_index_md_is_still_indexed(tmp_path):
    vault = tmp_path / "v"
    _write(vault / "claude-insights" / "index.md", "claude-insight")
    db = vault_index.ensure_index(str(vault), ["claude-insights"], db_path=str(tmp_path / "i.db"))
    assert _paths(db) == {"index.md"}


def test_excluded_count_in_stats(tmp_path):
    vault = tmp_path / "v"
    _write(vault / "claude-wiki" / "index.md", "claude-wiki-index")
    stats = vault_index.rebuild_index(str(vault), ["claude-wiki"], db_path=str(tmp_path / "i.db"), full=True)
    assert stats.get("excluded") == 1
    assert stats.get("malformed") == 0


def test_type_change_to_unindexed_removes_row(tmp_path):
    vault = tmp_path / "v"
    p = vault / "claude-wiki" / "index.md"
    _write(p, "claude-insight")
    db = str(tmp_path / "i.db")
    vault_index.ensure_index(str(vault), ["claude-wiki"], db_path=db)
    assert _paths(db) == {"index.md"}
    _write(p, "claude-wiki-index")
    later = time.time() + 5
    os.utime(p, (later, later))
    vault_index.ensure_index(str(vault), ["claude-wiki"], db_path=db)
    assert _paths(db) == set()
    assert vault_index.search_vault(db, "zebracorn", limit=5) == []


def test_missing_wiki_folder_is_skipped(tmp_path):
    vault = tmp_path / "v"
    _write(vault / "claude-sessions" / "s.md", "claude-session")
    db = vault_index.ensure_index(
        str(vault), ["claude-sessions", "claude-insights", "claude-wiki"], db_path=str(tmp_path / "i.db")
    )
    assert _paths(db) == {"s.md"}


def test_rebuild_with_wiki_folder_keeps_wiki_rows(tmp_path):
    vault = tmp_path / "v"
    _write(vault / "claude-sessions" / "s.md", "claude-session")
    _write(vault / "claude-wiki" / "queries" / "2026" / "10-03-q.md", "claude-wiki")
    db = str(tmp_path / "i.db")
    folders = ["claude-sessions", "claude-insights", "claude-wiki"]
    vault_index.ensure_index(str(vault), folders, db_path=db)
    vault_index.rebuild_index(str(vault), folders, db_path=db)             # preserve mode
    assert _paths(db) == {"s.md", "10-03-q.md"}
    vault_index.rebuild_index(str(vault), folders, db_path=db, full=True)  # full
    assert _paths(db) == {"s.md", "10-03-q.md"}
```

Before running, check `search_vault`'s real signature (`grep -n "def search_vault" hooks/vault_index.py`) and whether the `no-default-db` CI check (`scripts/ci-checks/no-default-db.py`) constrains these calls; adapt and say so.

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_vault_index_unindexed_types.py -v`
Expected: `test_constant` fails with `AttributeError`; the index-file test fails because `index.md`/`log-2026.md` are indexed; `test_excluded_count_in_stats` fails (`None != 1`); the type-change test fails. `test_user_note_named_index_md_is_still_indexed`, `test_missing_wiki_folder_is_skipped` and the rebuild test may already pass — they are regression pins, not RED tests.

- [ ] **Step 3: Implement**

Add near the top-level constants of `hooks/vault_index.py`:

```python
# Note types written into indexed folders that are not knowledge: the LLM
# wiki's own index and log files (#383). _sync skips them, and removes a row
# whose note changed to one of these types.
_UNINDEXED_TYPES = frozenset({"claude-wiki-index"})
```

In `_sync`, add `"excluded": 0,` to the `stats` dict. Right after the `if parsed is None: ... continue` block and before `_upsert_note(...)`, add:

```python
            if parsed.get("type") in _UNINDEXED_TYPES:
                # Never inserted, so the mtime short-circuit never applies and
                # the file is re-parsed each sync: a handful of small files.
                stats["excluded"] += 1
                if abs_path_str in indexed:
                    _delete_note(conn, abs_path_str)
                    stats["deleted"] += 1
                continue
```

Update the `_sync` docstring's return-shape line to mention `excluded`. Check whether `rebuild_index`'s preserve and full paths return `_sync`'s stats unchanged (`:1204`, `:1270`); if either builds its own dict, carry `excluded` through and say so.

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_vault_index_unindexed_types.py tests/test_vault_index*.py -v`
Expected: all PASS.

- [ ] **Step 5: Mutation check (do not commit the mutation)**

With `PYTHONDONTWRITEBYTECODE=1`, after committing or in a scratchpad copy (never `git checkout --` an uncommitted fix): (a) delete the whole `if parsed.get("type") in _UNINDEXED_TYPES` block → the index-file test and the stats test must fail; (b) keep the block but delete only the inner `_delete_note` lines → `test_type_change_to_unindexed_removes_row` must fail. Record both results in the task report.

- [ ] **Step 6: Commit**

```bash
git add hooks/vault_index.py tests/test_vault_index_unindexed_types.py
./scripts/commit-preflight.sh
git commit -m "feat(obsidian-brain): keep claude-wiki-index notes out of the vault index (#383)"
```

---

### Task 3: `claude-wiki` rerank weight

**Files:**
- Modify: `hooks/vault_index.py:1495-1527` (`_TYPE_SCORES_BY_CONTEXT`)
- Modify: `tests/test_type_scores.py`

**Interfaces:**
- Consumes: `_UNINDEXED_TYPES` (Task 2).
- Produces: `get_type_scores(ctx)["claude-wiki"] == get_type_scores(ctx)["claude-insight"]` in all 5 contexts.

- [ ] **Step 1: Write the failing tests**

In `tests/test_type_scores.py`:

1. Change `_EXTERNAL_TYPES = {"claude-memory"}` to:

```python
# Present in live vaults but written outside this file set: migrated memory
# notes, and claude-wiki, whose writer (hooks/wiki.py) lands in #383 PR 2.
# PR 2 removes "claude-wiki" from this set once the scan finds the writer.
_EXTERNAL_TYPES = {"claude-memory", "claude-wiki"}
```

2. In `test_scan_finds_exactly_the_known_types`, add `"claude-wiki"` to the expected set.

3. Add:

```python
def test_wiki_pages_weigh_like_insights():
    for ctx in vault_index._TYPE_SCORES_BY_CONTEXT:
        scores = vault_index.get_type_scores(ctx)
        assert scores["claude-wiki"] == scores["claude-insight"], ctx


def test_unindexed_types_need_no_weight():
    # claude-wiki-index notes never reach the index, so they never get a score.
    for ctx in vault_index._TYPE_SCORES_BY_CONTEXT:
        assert not (vault_index._UNINDEXED_TYPES & set(vault_index.get_type_scores(ctx))), ctx
```

4. In `test_every_written_type_has_a_weight_in_every_context`, compute the required set as `collect_written_types() - vault_index._UNINDEXED_TYPES`, so PR 2's writer of `claude-wiki-index` does not demand a weight.

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_type_scores.py -v`
Expected: `test_every_written_type_has_a_weight_in_every_context` and `test_wiki_pages_weigh_like_insights` FAIL with a missing `claude-wiki` key.

- [ ] **Step 3: Implement**

Add `"claude-wiki": <same value as claude-insight in that context>` to each of the 5 context dicts: debugging 0.6, standup 0.7, search 1.0, emerge 1.0, general 1.0. Add one line to the comment block above the table: `claude-wiki (#383) weighs the same as claude-insight; the wiki-first step in /vault-ask, not the weight, is what puts wiki pages first.`

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_type_scores.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add hooks/vault_index.py tests/test_type_scores.py
./scripts/commit-preflight.sh
git commit -m "feat(obsidian-brain): weight claude-wiki like claude-insight in rerank (#383)"
```

---

### Task 4: route every search-side folder list through the helper, plus the guard

**Files:**
- Modify: `skills/vault-ask/SKILL.md:117-120`, `skills/vault-search/SKILL.md:~113-115`, `skills/compress/SKILL.md:~111-123` and `:~307-313`, `skills/vault-stats/SKILL.md:~45-53`, `skills/vault-reindex/SKILL.md:~104-121`, `skills/obsidian-setup/SKILL.md` (Step 5 `:176-180`, Step 7 `:496-508`, Step 8.5 `:~545-566`), `skills/vault-config/SKILL.md` (Steps 2 and 4)
- Modify: `scripts/tune_compress_rank_gap.py:91-96`, `scripts/test-phase1-manual.sh:~142-145`
- Modify: `hooks/obsidian_utils.py:5145` and `hooks/open_item_dedup.py:1279` (comments only)
- Test: `tests/test_indexed_folders.py` (add the guard)

**Interfaces:**
- Consumes: `indexed_folders(config)` (Task 1).
- Produces: no new API. After this task, every `ensure_index`/`rebuild_index` call outside the allow-listed hook files and `scripts/dev-test/` takes its folders from `indexed_folders(...)`.

- [ ] **Step 1: Write the failing guard test**

Append to `tests/test_indexed_folders.py`:

```python
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Files that legitimately pass explicit folder lists, with the reason.
_ALLOWED = {
    # Receive sessions/insights as parameters from callers, not a config.
    # Safe: _sync deletes only under scanned folders, so wiki rows survive.
    "hooks/obsidian_utils.py": "build_context_brief takes folder params",
    "hooks/open_item_dedup.py": "deep_analysis_pipeline takes folder params",
    # Defines the functions; rebuild_index recurses with its own argument.
    "hooks/vault_index.py": "definition site",
}

_CALL_RE = re.compile(r"\b(ensure_index|rebuild_index)\(")


def _scanned_files(root: Path) -> list:
    files = list((root / "skills").glob("*/SKILL.md"))
    files += list((root / "hooks").glob("*.py"))
    files += list((root / "scripts").glob("*.py"))
    files += list((root / "scripts").glob("*.sh"))
    return sorted(files)


def _second_arg(text: str, open_paren: int) -> str:
    """Source text of the call's second positional argument ('' if none)."""
    depth, args, cur = 0, [], []
    for ch in text[open_paren:]:
        if ch in "([{":
            depth += 1
            if depth > 1:
                cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                args.append("".join(cur).strip())
                break
            cur.append(ch)
        elif ch == "," and depth == 1:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    return args[1] if len(args) > 1 else ""


def _violations(path: Path, root: Path) -> list:
    text = path.read_text(encoding="utf-8", errors="replace")
    out = []
    for m in _CALL_RE.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        before = text[line_start:m.start()]
        if before.lstrip().startswith(("def ", "#")) or "``" in before:
            continue
        arg = _second_arg(text, m.end() - 1)
        if not arg or arg.startswith("indexed_folders("):
            continue
        if re.fullmatch(r"[A-Za-z_]\w*", arg) and re.search(rf"\b{arg}\s*=\s*indexed_folders\(", text):
            continue
        line = text.count("\n", 0, m.start()) + 1
        out.append(f"{path.relative_to(root)}:{line}: {arg}")
    return out


def test_every_index_call_uses_indexed_folders():
    bad = []
    for p in _scanned_files(REPO):
        if str(p.relative_to(REPO)) in _ALLOWED:
            continue
        bad += _violations(p, REPO)
    assert not bad, "pass folders via indexed_folders(config):\n" + "\n".join(bad)


def test_guard_catches_a_literal_list(tmp_path):
    # Positive control: the guard must flag today's shape.
    p = tmp_path / "x.py"
    p.write_text('db = ensure_index(vp, [c.get("sessions_folder"), c.get("insights_folder")])\n')
    assert _violations(p, tmp_path)


def test_guard_catches_a_named_list(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("folders = [a, b]\ndb = ensure_index(vp, folders)\n")
    assert _violations(p, tmp_path)


def test_guard_accepts_the_helper(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("folders = indexed_folders(c)\ndb = ensure_index(vp, folders)\n"
                 "rebuild_index(v, indexed_folders(c), full=True)\n")
    assert not _violations(p, tmp_path)
```

Note: the argument scanner does not understand string literals containing brackets or commas. None of today's call sites have one; if a converted site does, say so and tighten the scanner.

- [ ] **Step 2: Run to verify the guard fails**

Run: `python3 -m pytest tests/test_indexed_folders.py -v`
Expected: `test_every_index_call_uses_indexed_folders` FAILS and lists exactly these 9 sites: `skills/vault-ask/SKILL.md:120`, `skills/vault-search/SKILL.md:115`, `skills/compress/SKILL.md:123`, `skills/compress/SKILL.md:313`, `skills/vault-stats/SKILL.md:53`, `skills/vault-reindex/SKILL.md:107`, `skills/obsidian-setup/SKILL.md:564`, `scripts/tune_compress_rank_gap.py:96`, `scripts/test-phase1-manual.sh:145`. If the list differs, the guard or this plan is wrong: find out which before going on. The three positive-control tests PASS.

- [ ] **Step 3: Convert each site**

- `skills/vault-ask/SKILL.md:117-120` and `skills/vault-search/SKILL.md:~113-115`:
  ```python
  from obsidian_utils import load_config, indexed_folders
  from vault_index import ensure_index, search_vault
  c = load_config()
  db = ensure_index(c["vault_path"], indexed_folders(c))
  ```
- `skills/compress/SKILL.md` (both sites) and `skills/vault-stats/SKILL.md`: replace the `folders = [c.get(...), c.get(...)]` line with `folders = indexed_folders(c)` and add `indexed_folders` to that snippet's `from obsidian_utils import ...` line (match the snippet's structure, including any `try`).
- `skills/vault-reindex/SKILL.md`:
  ```python
  from obsidian_utils import load_config, indexed_folders
  from vault_index import rebuild_index
  t0 = time.time()
  full = sys.argv[2].lower() == "true"
  stats = rebuild_index(sys.argv[1], indexed_folders(load_config()), full=full)
  ```
  with the argument line `' "$VAULT_PATH" "$FULL_MODE"`. Update prose in that skill that names "sessions and insights" as the indexed set to include the wiki folder. If its report step lists stats keys, add `excluded`.
- `skills/obsidian-setup/SKILL.md`:
  - Step 5: `mkdir -p "$VAULT_PATH/claude-sessions" "$VAULT_PATH/claude-insights" "$VAULT_PATH/claude-dashboards" "$VAULT_PATH/claude-wiki"`.
  - Step 7 JSON: add `"wiki_folder": "claude-wiki",` after `"check_items_folder"`.
  - Step 8.5: `from obsidian_utils import load_config, indexed_folders` and `counts = rebuild_index(sys.argv[1], indexed_folders(load_config()))`, argument line `' "$VAULT_PATH"`. Step 7 runs before Step 8.5 in fresh/reconfigure mode and the config already exists in upgrade mode; confirm by reading the step order.
- `skills/vault-config/SKILL.md`: add row `4. wiki_folder <value>` to the Step 2 table and renumber the rows below (5-9); add `wiki_folder` to the Step 4 string-settings list; add its default `claude-wiki` to the defaults sentence.
- `scripts/tune_compress_rank_gap.py:91-96`: import `indexed_folders` from `obsidian_utils` (match the script's import style) and set `folders = indexed_folders(config)`.
- `scripts/test-phase1-manual.sh:~142-145`: `from obsidian_utils import load_config, indexed_folders` and `ensure_index(c['vault_path'], indexed_folders(c))`.

- [ ] **Step 4: Comment the two allow-listed hook sites**

Above `hooks/obsidian_utils.py:5145`:

```python
            # Explicit list (not indexed_folders): this function receives the
            # folders as parameters. Safe for the wiki folder (#383): _sync
            # deletes only under scanned folders, so wiki rows are untouched.
```

Above `hooks/open_item_dedup.py:1279`:

```python
    # Explicit list (not indexed_folders): folders arrive as parameters, and
    # the scans below look for open `- [ ]` items, which wiki pages never
    # carry. Safe for wiki rows (#383): _sync deletes only under scanned folders.
```

- [ ] **Step 5: Run to verify**

Run: `python3 -m pytest tests/test_indexed_folders.py tests/test_skill_snippets.py -v`
Expected: all PASS (the snippet test compiles every edited `python3 -c` block).

Then exercise the helper and the exclusion together against a scratch vault (never the live one):

```bash
SP=$(mktemp -d)
mkdir -p "$SP/v/claude-sessions" "$SP/v/claude-wiki"
printf -- '---\ntype: claude-session\ndate: 2026-10-03\nproject: demo\n---\n# s\nzebracorn\n' > "$SP/v/claude-sessions/s.md"
printf -- '---\ntype: claude-wiki-index\n---\n# index\nzebracorn\n' > "$SP/v/claude-wiki/index.md"
python3 - "$SP" <<'PY'
import sys; sys.path.insert(0, "hooks")
from obsidian_utils import indexed_folders
from vault_index import ensure_index, search_vault
sp = sys.argv[1]
db = ensure_index(sp + "/v", indexed_folders({}), db_path=sp + "/i.db")
print(sorted(r["path"].rsplit("/", 1)[1] for r in search_vault(db, "zebracorn", limit=5)))
PY
```

Expected output: `['s.md']`.

- [ ] **Step 6: Commit**

```bash
git add skills/ scripts/tune_compress_rank_gap.py scripts/test-phase1-manual.sh hooks/obsidian_utils.py hooks/open_item_dedup.py tests/test_indexed_folders.py
git diff --cached --name-only
./scripts/commit-preflight.sh
git commit -m "feat(obsidian-brain): index the wiki folder from every search-side caller (#383)"
```

---

### Task 5: docs, CHANGELOG, architecture page

**Files:**
- Modify: `CHANGELOG.md` (`[Unreleased]`), `README.md` (config section near `:272`), `docs/architecture/architecture.json`, `docs/architecture/architecture.html`

**Interfaces:** none.

- [ ] **Step 1: CHANGELOG**

Under `## [Unreleased]` (create it above the latest release heading if missing), inside its `### Added` block:

```markdown
- `wiki_folder` config key (default `claude-wiki`) and `indexed_folders(config)` helper. Every search-side index call (`/vault-ask`, `/vault-search`, `/compress`, `/vault-stats`, `/vault-reindex`, `/obsidian-setup`) now indexes the wiki folder. Notes typed `claude-wiki-index` stay out of the index. Groundwork for the LLM wiki (#383).
```

Insert inside the existing block, before its closing blank line, then re-read the five surrounding lines.

- [ ] **Step 2: README**

In the config section, add `wiki_folder` (default `claude-wiki`) next to `insights_folder`, matching the existing format. Extend the existing section; do not add a new one.

- [ ] **Step 3: architecture.json**

Edit `docs/architecture/architecture.json`:
- the component that owns config in `hooks/obsidian_utils.py`: mention `indexed_folders(config)` and the `wiki_folder` key;
- the vault-index component: mention `_UNINDEXED_TYPES` and the `claude-wiki` weight;
- the vault datastore / folder description: add `claude-wiki/`;
- every flow that names "sessions and insights" as the indexed set (at least `/vault-reindex`): name the wiki folder.

Keep `version` equal to `plugin.json`'s version and set `lastUpdated` to the commit date. Then:

```bash
~/.claude/skills/architecture-page/scripts/render-html.sh --json docs/architecture/architecture.json --output docs/architecture/architecture.html
~/.claude/skills/architecture-page/scripts/smoke-test.sh --json docs/architecture/architecture.json
```

Expected: both `[main]` and `[sparse]` PASS.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md README.md docs/architecture/architecture.json docs/architecture/architecture.html
./scripts/commit-preflight.sh
git commit -m "docs: wiki_folder, indexed_folders and the claude-wiki-index exclusion (#383)"
```

---

### Task 6: reconcile the spec

**Files:**
- Modify: `docs/plans/383-llm-wiki-design.md`

- [ ] **Step 1: Edit the PR 1 section**

- Replace the "Indexer exclusion" paragraph with: `_sync` skips any note whose frontmatter `type` is in `vault_index._UNINDEXED_TYPES` (`{"claude-wiki-index"}`) and deletes its row if it was indexed before. PR 2 writes `type: claude-wiki-index` into every index and log file. A user note named `index.md` is indexed as usual because it lacks the type.
- In the `indexed_folders` paragraph, replace "Every site listed above calls it" with: every skill and script call site uses it; `build_context_brief` (`hooks/obsidian_utils.py:5145`) and `deep_analysis_pipeline` (`hooks/open_item_dedup.py:1279`) keep explicit lists because they receive folders as parameters, and this is safe because `_sync` deletes only under scanned folders. Drop the sentence about the `open_item_dedup` cache key.
- In the data model's "Index files" and "Log files" subsections, add: each file starts with a frontmatter block carrying `type: claude-wiki-index`.
- In the "Guard test" paragraph, describe the call-argument check and the allow-list.

- [ ] **Step 2: Commit**

```bash
git add docs/plans/383-llm-wiki-design.md
./scripts/commit-preflight.sh --docs-only
git commit -m "docs(plans): reconcile #383 spec with PR 1 (type-based exclusion, allow-listed hooks)"
```

---

## Done when

- `python3 -m pytest tests/ -q` passes with coverage ≥ 90%.
- The guard test lists no violations, and its three positive controls pass.
- Both Task 2 mutations were run and each failed the named test (results in the report).
- The scratch-vault check in Task 4 Step 5 printed `['s.md']`.
- The architecture smoke test passes.
