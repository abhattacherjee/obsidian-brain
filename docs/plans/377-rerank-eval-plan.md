# Rerank Eval (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure whether a decision model (Jev or laya) ranks `/vault-ask` candidates better than today, without changing any live ranking weights.

**Architecture:** Two small hook modules log what vault-ask saw and cited (`ask_log.py`) and move its Step 5 rule table into code (`ask_rank.py`). `vault_index.py` gains opt-in parameters: no access logging, a fixed task context, and an extra rerank signal. A script package under `scripts/rerank_eval/` builds an Opus-labelled seed set, scores four backends in `replace` and `blend` modes, and applies the go gate.

**Tech Stack:** Python 3.9+ stdlib only (plugin and harness), pytest, `claude -p` (Opus labels, Haiku backend), HTTP via `urllib` (Jev), a separate Python ≥ 3.10 venv running `laya==0.3.21`.

**Spec:** `docs/plans/377-rerank-eval-design.md` (approved 2026-09-28, commit 862bb96).

## Global Constraints

- Plugin code (`hooks/`, `scripts/`) must import on Python 3.9: `from __future__ import annotations` in every new module, no runtime `X | Y` unions, no `match`. `tests/test_py39_compat.py` scans `hooks/`, `scripts/` and `.claude/hooks/`, so `laya_score.py` must pass it too.
- laya is never imported by plugin code. Only `scripts/rerank_eval/laya_score.py` imports it, and only inside the laya venv.
- Stdlib only. No `httpx`, no `requests`.
- Coverage: `pytest tests/ --cov=hooks --cov-fail-under=90` must stay green (`./scripts/commit-preflight.sh`).
- New files with user data: `0o600`; new directories: `0o700`. Writes to JSON state files use `tempfile.mkstemp` + `os.replace`.
- Hook entry points read stdin with `sys.stdin.read(1_000_000)`.
- Paths go to `python3 -c` via `sys.argv`, never interpolated.
- The citation log, the seed set and eval reports live under `~/.claude/` and are never committed.
- Jev runs only with `OB_JEV=1` and `TYPESAFE_API_KEY` set; it sends scrubbed title, summary and snippet text only, never whole notes.
- Gate (seed set only, 95% paired bootstrap CI): nDCG@5 ≥ +0.05 over `current` with lower CI bound > 0; recall@5 difference lower CI bound ≥ −0.02; p95 latency ≤ 1.5 s per 20 candidates. `haiku` is a reference and never chosen. Among passers, prefer `laya` over `jev`.
- The eval must not write `access_log` rows.
- Commits: conventional format, run `./scripts/commit-preflight.sh` first, end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A vault-ask run that falls back to Grep (Step 4).** The pending file must still exist with `fts_rank`/`rerank_score` null, and `ask_rank.py` must sort these without a `TypeError` on `None`. Test: Task 3 `test_rank_fallback_candidates_with_null_scores`.
2. **An answer whose Sources cite `[[note|alias]]`, `[[note#heading]]` or a snapshot plus its parent on one line.** Each must match the candidate by filename stem. Test: Task 2 `test_parse_cited_alias_heading_and_multiple_per_line`.
3. **A second `/vault-ask` in the same session before `record` runs** (the model skipped the last step). The new candidates overwrite the stale pending file, and `record` never merges two questions. Test: Task 2 `test_write_candidates_overwrites_stale_pending`.
4. **A known-item question whose source note is not in the top 20.** It must count as a miss for every backend, not be dropped from the denominator. Tests: Task 5 `test_recall_counts_source_outside_pool_as_miss`, Task 8 `test_evaluate_source_outside_pool_counts_in_recall`.
5. **The harness run on a `fix/*` branch.** Results must be identical to a `develop` run, because the harness passes `task_context`. Test: Task 1 `test_search_vault_task_context_overrides_branch`.

---

## File map

| File | Responsibility |
|---|---|
| `hooks/vault_index.py` (modify) | `search_vault(log_access, task_context)`; `rerank_results(extra_signal, extra_weight)`; #376 type weights; `QTYPES`, `QTYPE_TO_CONTEXT` |
| `hooks/ask_log.py` (create) | pending candidate file, Sources parsing, citation-log append + rotation |
| `hooks/ask_rank.py` (create) | vault-ask Step 5 rule table in code; writes `rule_score`/`read` back to the pending file |
| `skills/vault-ask/SKILL.md` (modify) | Step 3/4 write candidates, Step 5 calls `ask_rank.py`, new Step 9 calls `ask_log.py record` |
| `scripts/rerank_eval/__init__.py` (create) | package marker |
| `scripts/rerank_eval/common.py` (create) | paths, `question_terms`, candidate text, `claude -p` wrapper, private writes |
| `scripts/rerank_eval/metrics.py` (create) | nDCG@k, recall@k, paired bootstrap, p95 |
| `scripts/rerank_eval/seed.py` (create) | history questions, known-item questions, candidate pools, Opus labels, spot-check |
| `scripts/rerank_eval/backends.py` (create) | `current`, `haiku`, `jev`, `laya` backends + question-type classifiers |
| `scripts/rerank_eval/laya_score.py` (create) | runs inside the laya venv; JSON lines over stdin/stdout |
| `scripts/rerank_eval/harness.py` (create) | runs backends in replace/blend modes, gate, question-type test, report |
| `tests/test_vault_index_eval_params.py` (create) | Task 1 tests |
| `tests/test_ask_log.py`, `tests/test_ask_rank.py` (create) | Tasks 2–3 tests |
| `tests/test_vault_ask_skill_wiring.py` (create) | Task 4 test |
| `tests/test_rerank_eval_*.py` (create) | Tasks 5–8 tests |
| `docs/architecture/architecture.json` + `.html` (modify) | new components and flow |
| `CHANGELOG.md` (modify) | `[Unreleased]` entry |

---

### Task 0: Live checks (run first, before any code)

These decide whether later tasks run as planned. Record results in `docs/plans/377-live-checks.md` (counts and yes/no only, no vault text) and commit it.

**Files:**
- Create: `docs/plans/377-live-checks.md`

- [ ] **Step 1: Count historical vault-ask questions properly**

Write this to the session scratchpad as `count_history.py` (`$SCRATCH` below is the scratchpad path) and run it with the vault's sessions folder as the argument:

```python
import re, sys
from pathlib import Path

PATTERNS = [
    re.compile(r"<command-name>/?(?:obsidian-brain:)?vault-ask</command-name>\s*"
               r"(?:<command-message>[^<]*</command-message>\s*)?"
               r"<command-args>(.*?)</command-args>", re.S),
    re.compile(r"(?m)(?:^|[\s>*`])/(?:obsidian-brain:)?vault-ask[ \t]+([^\n`]{8,300})"),
]
qs, notes = set(), 0
for p in Path(sys.argv[1]).glob("*.md"):
    t = p.read_text(encoding="utf-8", errors="replace")
    hit = False
    for pat in PATTERNS:
        for m in pat.finditer(t):
            q = " ".join(m.group(1).split()).strip().lower()
            if len(q.split()) >= 4 and not q.startswith("<"):
                qs.add(q); hit = True
    notes += hit
print(f"notes_with_questions={notes} distinct_questions={len(qs)}")
```

Run: `python3 "$SCRATCH/count_history.py" ~/obsidian/claude-code-vault/claude-sessions`
Record both numbers. **Decision:** if `distinct_questions` < 5, the seed set is effectively known-item only (Task 6 still extracts history; it just yields few rows). The known-item target stays 50.

- [ ] **Step 2: laya installs and scores on this Mac**

```bash
python3 --version   # must be >= 3.10 (3.13.3 on 2026-09-28)
python3 -m venv ~/.cache/obsidian-brain/laya-venv
~/.cache/obsidian-brain/laya-venv/bin/pip install -q "laya==0.3.21"
~/.cache/obsidian-brain/laya-venv/bin/pip show -v laya | grep -iE "^(home-page|author|project-url|  )"
```

Confirm the package metadata points at `github.com/NandhaKishorM/laya`. If it does not, stop and tell the user (supply-chain check from the vault note).

Then time a load and 20 yes/no answers:

```bash
~/.cache/obsidian-brain/laya-venv/bin/python - <<'EOF'
import time
from laya import Router
t = time.perf_counter(); r = Router()
q = {"rel": {"type": "noul", "instructions": "Does the note answer the question?"}}
state = {"question": "why did we pick sqlite fts5", "note": "Decision: use SQLite FTS5 for the vault index because it is stdlib."}
r.predict(state, q)  # first call loads the checkpoint
print("load_s", round(time.perf_counter() - t, 1))
ts = []
for _ in range(20):
    s = time.perf_counter(); out = r.predict(state, q); ts.append(time.perf_counter() - s)
print("p_yes", out["answers"]["rel"]["noul"], "sum_20_s", round(sum(ts), 2))
print("routing", out.get("routing"))
EOF
```

Record `load_s`, `sum_20_s`, `p_yes` and the routed model. **Pass:** it prints a probability and routes to the `laya` (English) checkpoint. If `sum_20_s` > 1.5, laya fails the latency gate zero-shot; still run it (quality is worth knowing) and note it. Also check `~/.cache/obsidian-brain/laya-venv/bin/python -c "from laya import Router; help(Router.predict_batch)"` and record its signature; Task 7's `laya_score.py` uses it when it exists.

- [ ] **Step 3: Jev route**

```bash
test -n "$TYPESAFE_API_KEY" && echo KEY_SET || echo KEY_MISSING
```

If set, send one request (no vault text):

```bash
python3 - <<'EOF'
import json, os, urllib.request
url = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai/v1/systemone")
body = {"model": "jev-1.13.0",
        "state": json.dumps({"question": "why sqlite", "notes": {"n0": "We chose SQLite FTS5."}}),
        "questions": {"n0": {"type": "noul", "instructions": "Does note n0 answer the question?"}}}
req = urllib.request.Request(url, json.dumps(body).encode(), {
    "Authorization": "Bearer " + os.environ["TYPESAFE_API_KEY"], "Content-Type": "application/json"})
print(urllib.request.urlopen(req, timeout=10).read()[:500])
EOF
```

Record: key present, HTTP status, whether `answers.n0.noul` is a number, and the `model` echoed. With no key, record `jev: skipped (no key)` and carry on; Task 7 still builds the client and its tests. Signups were full on 2026-09-28, so a Vercel AI Gateway or OpenRouter route may be the only way in. If the only route is OpenAI-compatible (a different request shape), stop and ask the user before writing Task 7's Jev client.

- [ ] **Step 4: Opus labelling works headless**

```bash
printf 'Reply with exactly {"ok": true}' | claude -p --model opus --output-format text
```

**Pass:** prints `{"ok": true}`. Record the time taken.

- [ ] **Step 5: Write and commit the results**

`docs/plans/377-live-checks.md`:

```markdown
# #377 live checks (<date>)

| Check | Result |
|---|---|
| History questions | notes_with_questions=<n>, distinct_questions=<n> → <seed plan> |
| laya | version 0.3.21, source <ok/mismatch>, load <s>, 20 answers <s>, routed <model>, predict_batch <yes/no + signature> |
| Jev | <key present?>, HTTP <code>, noul supported <yes/no>, model <echo> |
| Opus headless | <ok>, <s> |
```

```bash
git add docs/plans/377-live-checks.md
./scripts/commit-preflight.sh --docs-only
git commit -m "docs(plan): record #377 live checks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1: `vault_index` parameters and #376 weights

**Files:**
- Modify: `hooks/vault_index.py` (`_TYPE_SCORES_BY_CONTEXT` l.1482–1503, `rerank_results` l.1613, `search_vault` l.1729 and l.1829–1842)
- Test: `tests/test_vault_index_eval_params.py`

**Interfaces:**
- Produces:
  - `search_vault(db_path, query, project=None, note_type=None, limit=15, caller=None, include_vectors=False, log_access: bool = True, task_context: str | None = None) -> list[dict]`
  - `rerank_results(fts_results, query_terms, limit=15, db_path=None, task_context=None, extra_signal: dict[str, float] | None = None, extra_weight: float = 0.0) -> list[dict]`
  - `QTYPES: tuple[str, ...] = ("decision", "debug", "howto", "status")`
  - `QTYPE_TO_CONTEXT: dict[str, str] = {"decision": "search", "debug": "debugging", "howto": "general", "status": "standup"}`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for #377 eval parameters on search_vault / rerank_results and #376 weights."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

import vault_index

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def indexed(tmp_vault):
    sessions = tmp_vault / "claude-sessions"
    for slug, typ in [("a", "claude-session"), ("b", "claude-insight"), ("c", "claude-error-fix")]:
        (sessions / f"2026-04-16-proj-{slug}.md").write_text(
            "---\n"
            f"type: {typ}\n"
            "date: 2026-04-16\n"
            "project: proj\n"
            f"title: Note {slug}\n"
            "tags:\n  - claude/session\n"
            "status: summarized\n"
            "---\n"
            "foo bar baz\n",
            encoding="utf-8",
        )
    db = str(tmp_vault / "test.db")
    vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db)
    return db


def _access_rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
    finally:
        conn.close()


def test_search_vault_log_access_false_writes_no_rows(indexed):
    res = vault_index.search_vault(indexed, "foo", project="proj", log_access=False)
    assert res
    assert _access_rows(indexed) == 0


def test_search_vault_default_still_logs(indexed):
    res = vault_index.search_vault(indexed, "foo", project="proj")
    assert _access_rows(indexed) == len(res) > 0


def test_search_vault_task_context_overrides_branch(indexed, monkeypatch):
    monkeypatch.setattr(vault_index, "_get_git_branch", lambda: "fix/something")
    seen = {}
    real = vault_index.rerank_results

    def spy(*a, **kw):
        seen["ctx"] = kw.get("task_context")
        return real(*a, **kw)

    monkeypatch.setattr(vault_index, "rerank_results", spy)
    vault_index.search_vault(indexed, "foo", caller="vault-ask", task_context="search", log_access=False)
    assert seen["ctx"] == "search"
    vault_index.search_vault(indexed, "foo", caller="vault-ask", log_access=False)
    assert seen["ctx"] == "debugging"  # default path unchanged: branch wins


def _rows():
    return [
        {"path": "/v/a.md", "rank": -3.0, "type": "claude-session", "date": "2026-04-16",
         "title": "a", "tags": "", "body": "foo bar", "importance": 5},
        {"path": "/v/b.md", "rank": -1.0, "type": "claude-insight", "date": "2026-04-16",
         "title": "b", "tags": "", "body": "foo", "importance": 5},
    ]


def test_rerank_extra_weight_zero_is_identical():
    base = vault_index.rerank_results(_rows(), ["foo"])
    same = vault_index.rerank_results(_rows(), ["foo"], extra_signal={"/v/b.md": 1.0}, extra_weight=0.0)
    assert [(r["path"], r["rerank_score"]) for r in base] == [(r["path"], r["rerank_score"]) for r in same]


def test_rerank_extra_signal_can_flip_order():
    base = vault_index.rerank_results(_rows(), ["foo"])
    flipped = vault_index.rerank_results(
        _rows(), ["foo"], extra_signal={base[-1]["path"]: 1.0}, extra_weight=0.9)
    assert flipped[0]["path"] == base[-1]["path"]


@pytest.mark.parametrize("w", [-0.1, 1.1])
def test_rerank_extra_weight_out_of_range(w):
    with pytest.raises(ValueError):
        vault_index.rerank_results(_rows(), ["foo"], extra_signal={}, extra_weight=w)


def _writer_types():
    found = set()
    pat = re.compile(r"type: (claude-[a-z-]+)")
    for d, glob in [("templates", "*.md"), ("hooks", "*.py"), ("skills", "*/SKILL.md")]:
        for p in (ROOT / d).glob(glob):
            found |= set(pat.findall(p.read_text(encoding="utf-8")))
    return found | {"claude-memory"}  # written by the global memory migration, not this repo


def test_every_writer_type_has_a_weight_in_every_context():
    types = _writer_types()
    assert "claude-snapshot" in types  # guard: the scan itself works
    for ctx, row in vault_index._TYPE_SCORES_BY_CONTEXT.items():
        missing = types - set(row)
        assert not missing, f"{ctx} lacks {sorted(missing)}"


def test_qtype_map_targets_real_contexts():
    assert set(vault_index.QTYPE_TO_CONTEXT) == set(vault_index.QTYPES)
    assert set(vault_index.QTYPE_TO_CONTEXT.values()) <= set(vault_index._TYPE_SCORES_BY_CONTEXT)
```

- [ ] **Step 2: Run them to see them fail**

Run: `pytest tests/test_vault_index_eval_params.py -v`
Expected: FAIL — `TypeError: search_vault() got an unexpected keyword argument 'log_access'`, and the weights test lists the missing types.

- [ ] **Step 3: Implement**

In `_TYPE_SCORES_BY_CONTEXT`, add these keys to each row (starting values; the harness's question-type test measures them):

```python
# debugging row
"claude-snapshot": 0.6, "claude-memory": 0.5, "claude-emerge": 0.2,
"claude-stats": 0.1, "claude-check-items-report": 0.1,
# standup row
"claude-snapshot": 0.7, "claude-memory": 0.3, "claude-emerge": 0.4,
"claude-stats": 0.2, "claude-check-items-report": 0.4,
# search row
"claude-snapshot": 0.4, "claude-memory": 0.8, "claude-emerge": 0.5,
"claude-stats": 0.1, "claude-check-items-report": 0.2,
# emerge row
"claude-snapshot": 0.3, "claude-memory": 0.7, "claude-emerge": 0.9,
"claude-stats": 0.2, "claude-check-items-report": 0.1,
# general row
"claude-snapshot": 0.4, "claude-memory": 0.8, "claude-emerge": 0.5,
"claude-stats": 0.1, "claude-check-items-report": 0.2,
```

After the dict, add:

```python
# Question types used by the #377 eval harness, mapped to a context row above.
QTYPES: tuple[str, ...] = ("decision", "debug", "howto", "status")
QTYPE_TO_CONTEXT: dict[str, str] = {
    "decision": "search",
    "debug": "debugging",
    "howto": "general",
    "status": "standup",
}
```

`rerank_results`: add the two parameters to the signature and document them in the docstring (`extra_signal` maps path → score in [0, 1]; the final score is `(1 - extra_weight) * base + extra_weight * extra`). Validate at the top, before the empty-input early return:

```python
    if not 0.0 <= extra_weight <= 1.0:
        raise ValueError(f"extra_weight must be in [0, 1], got {extra_weight}")
```

Directly after the existing `final = (...)` block, add:

```python
        if extra_weight:
            extra = float((extra_signal or {}).get(r.get("path", ""), 0.0))
            final = (1.0 - extra_weight) * final + extra_weight * extra
```

When `extra_weight` is 0 the branch is skipped, so scores are bit-identical.

`search_vault`: add `log_access: bool = True, task_context: str | None = None` to the signature and document both. Replace l.1829–1842 with:

```python
        if task_context is None:
            task_context = detect_task_context(caller_skill=caller) if caller else None
        results = rerank_results(
            candidates, query_terms, limit,
            db_path=db_path, task_context=task_context,
        )
        # Log access for returned results (single connection, one commit).
        # Per-row project preserves each note's project attribution for
        # per-project analytics, even when the search itself was unscoped.
        # log_access=False is for eval replays (#377): they must not inflate
        # ACT-R activation.
        if log_access:
            _batch_log_access(
                conn,
                [r["path"] for r in results],
                "search",
                project=[r.get("project") for r in results],
            )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_vault_index_eval_params.py tests/test_batch_access_logging.py tests/test_rerank_v2.py tests/test_py39_compat.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation check**

Delete the `if log_access:` guard (always log) and run `pytest tests/test_vault_index_eval_params.py::test_search_vault_log_access_false_writes_no_rows`. Expected: FAIL. Restore it. Do the same for the `if task_context is None:` line → `test_search_vault_task_context_overrides_branch` fails. Restore.

- [ ] **Step 6: Commit**

```bash
git add hooks/vault_index.py tests/test_vault_index_eval_params.py
./scripts/commit-preflight.sh
git commit -m "feat(obsidian-brain): eval parameters on search_vault and rerank_results (#377, #376)

log_access=False and task_context for eval replays; extra_signal for
blend mode; weights for every note type the writers emit.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `hooks/ask_log.py`

**Files:**
- Create: `hooks/ask_log.py`
- Test: `tests/test_ask_log.py`

**Interfaces:**
- Produces:
  - `PENDING_DIR: Path`, `LOG_PATH: Path`, `HOOK_LOG: Path`, `ROTATE_BYTES = 1024 * 1024`
  - `pending_path(session_id: str) -> Path`
  - `write_candidates(question: str, candidates: list[dict], session_id: str | None = None, project: str | None = None) -> bool` — candidates have `path` and optional `fts_rank` (or `rank`) and `rerank_score`, either may be None
  - `load_pending(session_id: str | None = None) -> dict | None` / `save_pending(data: dict, session_id: str | None = None) -> bool`
  - `parse_cited(answer: str) -> list[str]` — filename stems, order kept, no duplicates
  - `record(answer: str, session_id: str | None = None) -> bool`
  - `_log_error(msg: str) -> None` (used by `ask_rank.py`)
  - CLI: `python3 ask_log.py candidates` (stdin JSON `{"question", "project", "candidates": [...]}`), `python3 ask_log.py record` (stdin: answer text). Both always exit 0.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for hooks/ask_log.py (#377 citation log)."""
from __future__ import annotations

import io
import json
import sys

import pytest

import ask_log


@pytest.fixture(autouse=True)
def _paths(tmp_path, monkeypatch):
    monkeypatch.setattr(ask_log, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(ask_log, "LOG_PATH", tmp_path / "ask-log.jsonl")
    monkeypatch.setattr(ask_log, "HOOK_LOG", tmp_path / "hook.log")
    monkeypatch.setattr(ask_log, "_session_id", lambda: "sess-1")
    return tmp_path


def _cands():
    return [
        {"path": "/v/claude-insights/2026-01-01-a.md", "fts_rank": -2.0, "rerank_score": 0.7},
        {"path": "/v/claude-sessions/2026-01-02-b.md", "fts_rank": None, "rerank_score": None},
    ]


ANSWER = """You chose X ([[2026-01-01-a]]).

### Sources
- [[2026-01-01-a]] — the decision
- [[2026-01-02-b-snapshot-140000]]; parent: [[2026-01-02-b|the session]]
- [[2026-01-03-c#Summary]] — not a candidate
"""


def test_parse_cited_alias_heading_and_multiple_per_line():
    assert ask_log.parse_cited(ANSWER) == [
        "2026-01-01-a", "2026-01-02-b-snapshot-140000", "2026-01-02-b", "2026-01-03-c"]


def test_parse_cited_ignores_links_outside_sources():
    assert ask_log.parse_cited("See [[x]].\n\n### Sources\nNone.\n") == []


def test_parse_cited_no_sources_section():
    assert ask_log.parse_cited("Just [[x]] inline.") == []


def test_record_merges_pending_and_deletes_it():
    assert ask_log.write_candidates("why x?", _cands(), project="proj")
    pend = ask_log.load_pending()
    pend["candidates"][0].update(rule_score=5, read=True)
    ask_log.save_pending(pend)
    assert ask_log.record(ANSWER)
    line = json.loads(ask_log.LOG_PATH.read_text().splitlines()[0])
    assert line["question"] == "why x?" and line["project"] == "proj"
    assert line["session_id"] == "sess-1" and line["ranker_source"] == "current"
    assert [c["read"] for c in line["candidates"]] == [True, False]
    assert line["candidates"][0]["rule_score"] == 5
    assert line["candidates"][1]["rule_score"] is None
    assert line["cited"] == ["2026-01-01-a", "2026-01-02-b-snapshot-140000", "2026-01-02-b", "2026-01-03-c"]
    assert line["cited_unmatched"] == ["2026-01-02-b-snapshot-140000", "2026-01-03-c"]
    assert not ask_log.pending_path("sess-1").exists()


def test_record_without_pending_logs_error_and_returns_false():
    assert ask_log.record(ANSWER) is False
    assert "no pending" in ask_log.HOOK_LOG.read_text()
    assert not ask_log.LOG_PATH.exists()


def test_write_candidates_overwrites_stale_pending():
    ask_log.write_candidates("first?", _cands())
    ask_log.write_candidates("second?", _cands()[:1])
    pend = ask_log.load_pending()
    assert pend["question"] == "second?" and len(pend["candidates"]) == 1


def test_write_candidates_accepts_rank_alias_and_skips_bad_rows():
    ask_log.write_candidates("q?", [{"path": "/a.md", "rank": -1.5}, {"nopath": 1}, "junk"])
    pend = ask_log.load_pending()
    assert pend["candidates"] == [{"path": "/a.md", "fts_rank": -1.5, "rerank_score": None,
                                   "rule_score": None, "read": False}]


def test_pending_file_permissions():
    ask_log.write_candidates("q?", _cands())
    p = ask_log.pending_path("sess-1")
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert oct(p.parent.stat().st_mode & 0o777) == "0o700"


def test_pending_path_sanitises_session_id():
    p = ask_log.pending_path("../../etc/passwd")
    assert p.parent == ask_log.PENDING_DIR and "/" not in p.name and ".." not in p.name


def test_load_pending_corrupt_file_returns_none():
    ask_log.PENDING_DIR.mkdir(parents=True)
    ask_log.pending_path("sess-1").write_text("{broken")
    assert ask_log.load_pending() is None
    assert "load_pending failed" in ask_log.HOOK_LOG.read_text()


def test_rotation_boundary(monkeypatch):
    monkeypatch.setattr(ask_log, "ROTATE_BYTES", 200)
    for i in range(6):
        ask_log.write_candidates(f"question number {i}?", _cands())
        ask_log.record(ANSWER)
    rotated = ask_log.LOG_PATH.with_suffix(".jsonl.1")
    assert rotated.exists()
    assert len(ask_log.LOG_PATH.read_text().splitlines()) == 1  # rotated before this append


def test_record_returns_false_when_log_unwritable(monkeypatch, tmp_path):
    ask_log.write_candidates("q?", _cands())
    blocked = tmp_path / "a-directory"
    blocked.mkdir()
    monkeypatch.setattr(ask_log, "LOG_PATH", blocked)  # open(dir, "a") raises IsADirectoryError
    assert ask_log.record(ANSWER) is False
    assert "append failed" in ask_log.HOOK_LOG.read_text()
    assert ask_log.pending_path("sess-1").exists()  # kept for a retry


def test_cli_candidates_malformed_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    assert ask_log.main(["candidates"]) == 0
    assert "malformed" in ask_log.HOOK_LOG.read_text()


def test_cli_round_trip(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"question": "q?", "project": "p", "candidates": _cands()})))
    assert ask_log.main(["candidates"]) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO(ANSWER))
    assert ask_log.main(["record"]) == 0
    assert ask_log.LOG_PATH.exists()


def test_cli_unknown_command():
    assert ask_log.main(["bogus"]) == 0
    assert "usage" in ask_log.HOOK_LOG.read_text()
```

- [ ] **Step 2: Run them to see them fail**

Run: `pytest tests/test_ask_log.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ask_log'`.

- [ ] **Step 3: Implement `hooks/ask_log.py`**

```python
"""Citation log for /vault-ask (#377).

vault-ask writes every candidate to a per-session pending file (Step 3 or 4),
ask_rank.py adds rule scores (Step 5), and the last step pipes the answer text
to ``record``. Code, not the model, builds the log line, so no candidate can be
dropped. One JSON line per question goes to LOG_PATH, rotating at 1 MB.

Nothing here raises into the skill: every failure is one line in the hook log.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PENDING_DIR: Path = Path.home() / ".claude" / "obsidian-brain-ask-pending"
LOG_PATH: Path = Path.home() / ".claude" / "obsidian-brain-ask-log.jsonl"
HOOK_LOG: Path = Path.home() / ".claude" / "obsidian-brain-hook.log"
ROTATE_BYTES: int = 1024 * 1024
_STDIN_CAP = 1_000_000

_SOURCES_RE = re.compile(r"(?ims)^#{2,4}\s*sources\s*$(.*?)(?=^#{1,4}\s|\Z)")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_error(msg: str) -> None:
    try:
        HOOK_LOG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(HOOK_LOG, "a", encoding="utf-8") as f:
            f.write(f"{_now()} AskLog outcome=error msg={msg!r}\n")
    except Exception:  # noqa: BLE001 — logging must never raise
        pass


def _session_id() -> str:
    try:
        from obsidian_utils import _get_session_id_fast
        return _get_session_id_fast() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def pending_path(session_id: str) -> Path:
    safe = _SAFE_ID_RE.sub("_", session_id)[:128] or "unknown"
    return PENDING_DIR / f"{safe}.json"


def save_pending(data: dict, session_id: str | None = None) -> bool:
    try:
        PENDING_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(PENDING_DIR, 0o700)
        target = pending_path(session_id or _session_id())
        fd, tmp = tempfile.mkstemp(dir=str(PENDING_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True
    except Exception as exc:  # noqa: BLE001
        _log_error(f"save_pending failed: {type(exc).__name__}: {exc}")
        return False


def load_pending(session_id: str | None = None) -> dict | None:
    try:
        with open(pending_path(session_id or _session_id()), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        _log_error(f"load_pending failed: {type(exc).__name__}: {exc}")
        return None


def write_candidates(question: str, candidates: list, session_id: str | None = None,
                     project: str | None = None) -> bool:
    rows = []
    for c in candidates:
        if not isinstance(c, dict) or not c.get("path"):
            continue
        rows.append({
            "path": str(c["path"]),
            "fts_rank": c.get("fts_rank", c.get("rank")),
            "rerank_score": c.get("rerank_score"),
            "rule_score": None,
            "read": False,
        })
    data = {"ts": _now(), "question": str(question), "project": project, "candidates": rows}
    return save_pending(data, session_id)


def parse_cited(answer: str) -> list:
    m = _SOURCES_RE.search(answer or "")
    if not m:
        return []
    out: list = []
    for target in _WIKILINK_RE.findall(m.group(1)):
        stem = target.strip()
        if stem.endswith(".md"):
            stem = stem[:-3]
        if stem and stem not in out:
            out.append(stem)
    return out


def _append(line: dict) -> bool:
    try:
        LOG_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > ROTATE_BYTES:
            os.replace(LOG_PATH, LOG_PATH.with_suffix(".jsonl.1"))
        new_file = not LOG_PATH.exists()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, separators=(",", ":"), ensure_ascii=False) + "\n")
        if new_file:
            os.chmod(LOG_PATH, 0o600)
        return True
    except Exception as exc:  # noqa: BLE001
        _log_error(f"append failed: {type(exc).__name__}: {exc}")
        return False


def record(answer: str, session_id: str | None = None) -> bool:
    sid = session_id or _session_id()
    pend = load_pending(sid)
    if pend is None:
        _log_error(f"record: no pending candidate file for session {sid}")
        return False
    cited = parse_cited(answer)
    stems = {Path(c["path"]).stem for c in pend.get("candidates", [])}
    line = {
        "ts": pend.get("ts"),
        "session_id": sid,
        "project": pend.get("project"),
        "question": pend.get("question"),
        "candidates": pend.get("candidates", []),
        "cited": cited,
        "cited_unmatched": [c for c in cited if c not in stems],
        "ranker_source": "current",
    }
    ok = _append(line)
    if ok:
        try:
            pending_path(sid).unlink()
        except OSError as exc:
            _log_error(f"record: could not delete pending: {exc}")
    return ok


def main(argv: list) -> int:
    cmd = argv[0] if argv else ""
    try:
        raw = sys.stdin.read(_STDIN_CAP)
        if cmd == "candidates":
            try:
                payload = json.loads(raw)
                write_candidates(payload["question"], payload.get("candidates", []),
                                 project=payload.get("project"))
            except (ValueError, KeyError, TypeError) as exc:
                _log_error(f"candidates: malformed stdin: {exc}")
        elif cmd == "record":
            record(raw)
        else:
            _log_error("usage: ask_log.py candidates|record")
    except Exception as exc:  # noqa: BLE001
        _log_error(f"{cmd}: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_ask_log.py tests/test_py39_compat.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hooks/ask_log.py tests/test_ask_log.py
./scripts/commit-preflight.sh
git commit -m "feat(obsidian-brain): citation log for vault-ask (#377)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `hooks/ask_rank.py` (Step 5 in code)

**Files:**
- Create: `hooks/ask_rank.py`
- Test: `tests/test_ask_rank.py`

**Interfaces:**
- Consumes: `ask_log.load_pending`, `ask_log.save_pending`, `ask_log._log_error` (Task 2).
- Produces:
  - `rule_score(text: str, terms: list[str], today: date) -> int`
  - `rank_candidates(candidates: list[dict], terms: list[str], today: date | None = None, read_text=None) -> list[dict]` — new dicts sorted by `(-rule_score, -(rerank_score or 0), path)`, each with `rule_score` set
  - CLI: `python3 ask_rank.py` reads stdin JSON `{"terms": [...], "top": 10}`, ranks the pending file's candidates, sets `read=True` on the top N, saves, prints the top N paths as a JSON list. On any error it prints `[]` and exits 0; the skill then ranks by hand.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for hooks/ask_rank.py (#377): vault-ask Step 5 rules in code."""
from __future__ import annotations

import io
import json
import sys
from datetime import date

import pytest

import ask_log
import ask_rank

TODAY = date(2026, 9, 28)


def _note(typ, d, body, tags=""):
    return f"---\ntype: {typ}\ndate: {d}\ntags:\n{tags}---\n{body}\n"


@pytest.mark.parametrize("text,terms,expected", [
    (_note("claude-insight", "2026-09-20", "redis ttl"), ["redis", "ttl"], 2 + 2 + 3 + 1),
    (_note("claude-session", "2026-01-01", "Redis"), ["redis"], 2 + 1),
    (_note("claude-decision", "2026-08-29", "x"), ["redis"], 3 + 1),   # exactly 30 days: counts
    (_note("claude-decision", "2026-08-28", "x"), ["redis"], 3),       # 31 days: does not
    (_note("claude-retro", "bad-date", "x"), [], 0),
    (_note("claude-session", "2026-01-01", "x", "  - claude/topic/redis-cache\n"), ["redis"], 2 + 1 + 2),
])
def test_rule_score_table(text, terms, expected):
    assert ask_rank.rule_score(text, terms, TODAY) == expected


def test_rule_score_type_in_first_40_lines_only():
    text = "---\n" + "x: y\n" * 45 + "type: claude-insight\n---\nbody\n"
    assert ask_rank.rule_score(text, [], TODAY) == 0


def test_rank_ties_broken_by_rerank_score_then_path():
    texts = {"/a.md": _note("claude-session", "2026-01-01", "x"),
             "/b.md": _note("claude-session", "2026-01-01", "x"),
             "/c.md": _note("claude-session", "2026-01-01", "x")}
    cands = [{"path": "/c.md", "rerank_score": 0.1}, {"path": "/b.md", "rerank_score": 0.9},
             {"path": "/a.md", "rerank_score": 0.1}]
    out = ask_rank.rank_candidates(cands, [], TODAY, read_text=texts.__getitem__)
    assert [c["path"] for c in out] == ["/b.md", "/a.md", "/c.md"]


def test_rank_fallback_candidates_with_null_scores():
    texts = {"/a.md": _note("claude-session", "2026-01-01", "x"),
             "/b.md": _note("claude-insight", "2026-01-01", "x")}
    cands = [{"path": "/a.md", "rerank_score": None, "fts_rank": None},
             {"path": "/b.md", "rerank_score": None, "fts_rank": None}]
    out = ask_rank.rank_candidates(cands, [], TODAY, read_text=texts.__getitem__)
    assert [c["path"] for c in out] == ["/b.md", "/a.md"]


def test_unreadable_file_scores_zero():
    def boom(_):
        raise OSError("gone")
    out = ask_rank.rank_candidates([{"path": "/gone.md"}], ["x"], TODAY, read_text=boom)
    assert out[0]["rule_score"] == 0


@pytest.fixture
def pending_env(tmp_path, monkeypatch):
    monkeypatch.setattr(ask_log, "PENDING_DIR", tmp_path / "p")
    monkeypatch.setattr(ask_log, "HOOK_LOG", tmp_path / "h.log")
    monkeypatch.setattr(ask_log, "_session_id", lambda: "s")
    return tmp_path


def test_cli_marks_top_n_read(pending_env, monkeypatch, capsys):
    paths = []
    for i in range(3):
        p = pending_env / f"n{i}.md"
        p.write_text(_note("claude-insight" if i == 2 else "claude-session", "2026-01-01", "x"))
        paths.append(str(p))
    ask_log.write_candidates("q?", [{"path": p} for p in paths])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"terms": [], "top": 2})))
    assert ask_rank.main() == 0
    top = json.loads(capsys.readouterr().out)
    assert top[0] == paths[2] and len(top) == 2
    pend = ask_log.load_pending()
    assert sum(c["read"] for c in pend["candidates"]) == 2
    assert all(c["rule_score"] is not None for c in pend["candidates"])


def test_cli_without_pending_prints_empty(pending_env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"terms": []}'))
    assert ask_rank.main() == 0
    assert json.loads(capsys.readouterr().out) == []


def test_cli_malformed_stdin_prints_empty(pending_env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{bad"))
    assert ask_rank.main() == 0
    assert json.loads(capsys.readouterr().out) == []
    assert "ask_rank" in ask_log.HOOK_LOG.read_text()
```

- [ ] **Step 2: Run them to see them fail**

Run: `pytest tests/test_ask_rank.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ask_rank'`.

- [ ] **Step 3: Implement `hooks/ask_rank.py`**

```python
"""vault-ask Step 5 rule table, in code (#377).

Same rules the skill used to apply by hand:
  +2 per search term found in the content (case-insensitive)
  +3 if type is claude-insight / claude-decision / claude-error-fix
  +1 if type is claude-session
  +1 if the note's date is within the last 30 days
  +2 if a claude/topic/ tag contains a search term
Ties are broken by rerank_score (higher first; None counts as 0), then path.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date

import ask_log

_STDIN_CAP = 1_000_000
_HIGH_TYPES = {"claude-insight", "claude-decision", "claude-error-fix"}
_TYPE_RE = re.compile(r"^type:\s*(\S+)\s*$", re.M)
_DATE_RE = re.compile(r"^date:\s*(\d{4}-\d{2}-\d{2})", re.M)


def _head(text: str) -> str:
    return "\n".join(text.splitlines()[:40])


def rule_score(text: str, terms: list, today: date) -> int:
    low = text.lower()
    head = _head(text)
    score = 0
    for t in terms:
        if t and t.lower() in low:
            score += 2
    m = _TYPE_RE.search(head)
    typ = m.group(1) if m else ""
    if typ in _HIGH_TYPES:
        score += 3
    elif typ == "claude-session":
        score += 1
    d = _DATE_RE.search(head)
    if d:
        try:
            if 0 <= (today - date.fromisoformat(d.group(1))).days <= 30:
                score += 1
        except ValueError:
            pass
    if any(re.search(r"claude/topic/\S*" + re.escape(t.lower()), low) for t in terms if t):
        score += 2
    return score


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def rank_candidates(candidates: list, terms: list, today: date | None = None,
                    read_text=None) -> list:
    today = today or date.today()
    read_text = read_text or _read
    out = []
    for c in candidates:
        c2 = dict(c)
        try:
            c2["rule_score"] = rule_score(read_text(c2["path"]), terms, today)
        except (OSError, KeyError):
            c2["rule_score"] = 0
        out.append(c2)
    out.sort(key=lambda c: (-c["rule_score"], -(c.get("rerank_score") or 0.0), c.get("path", "")))
    return out


def main() -> int:
    try:
        req = json.loads(sys.stdin.read(_STDIN_CAP) or "{}")
        terms = [str(t) for t in req.get("terms", [])]
        top = int(req.get("top", 10))
        pend = ask_log.load_pending()
        if not pend or not pend.get("candidates"):
            print("[]")
            return 0
        ranked = rank_candidates(pend["candidates"], terms)
        for i, c in enumerate(ranked):
            c["read"] = i < top
        pend["candidates"] = ranked
        ask_log.save_pending(pend)
        print(json.dumps([c["path"] for c in ranked[:top]]))
    except Exception as exc:  # noqa: BLE001 — the skill falls back to hand ranking
        ask_log._log_error(f"ask_rank: {type(exc).__name__}: {exc}")
        print("[]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_ask_rank.py tests/test_py39_compat.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hooks/ask_rank.py tests/test_ask_rank.py
./scripts/commit-preflight.sh
git commit -m "feat(obsidian-brain): move vault-ask Step 5 rules into ask_rank.py (#377)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Wire vault-ask, architecture page, changelog

**Files:**
- Modify: `skills/vault-ask/SKILL.md` (Step 3 l.92–133, end of Step 4 l.~160, Step 5 l.169–187, new step after Step 8)
- Modify: `docs/architecture/architecture.json`, `docs/architecture/architecture.html`
- Modify: `CHANGELOG.md`
- Test: `tests/test_vault_ask_skill_wiring.py`

**Interfaces:**
- Consumes: `ask_log.py candidates|record`, `ask_rank.py` CLI (Tasks 2–3).

- [ ] **Step 1: Write the failing test**

```python
"""vault-ask must call the #377 citation-log scripts at the right steps."""
from __future__ import annotations

import re
from pathlib import Path

SKILL = (Path(__file__).resolve().parent.parent / "skills" / "vault-ask" / "SKILL.md").read_text()


def _step(n):
    m = re.search(rf"(?ms)^### Step {n} .*?(?=^### Step |\Z)", SKILL)
    assert m, f"Step {n} missing"
    return m.group(0)


def test_step3_writes_candidates():
    s = _step(3)
    assert "ask_log.py" in s and '"candidates"' in s and '"$QUESTION"' in s


def test_step4_writes_grep_union():
    assert "ask_log.py" in _step(4) and '"fts_rank": null' in _step(4)


def test_step5_calls_ask_rank_with_fallback():
    s = _step(5)
    assert "ask_rank.py" in s
    assert "If the script prints `[]`" in s  # hand-ranking fallback kept


def test_last_step_records_answer():
    last = re.findall(r"(?m)^### Step (\d+)", SKILL)[-1]
    s = _step(last)
    assert "ask_log.py" in s and "record" in s
```

- [ ] **Step 2: Run it to see it fail**

Run: `pytest tests/test_vault_ask_skill_wiring.py -v`
Expected: FAIL on all four.

- [ ] **Step 3: Edit `skills/vault-ask/SKILL.md`**

In the Step 3 Python block, replace `print(json.dumps(results))` with:

```python
print(json.dumps(results))
try:
    import subprocess
    subprocess.run(
        [sys.executable, os.path.join(_ob_hooks(), "ask_log.py"), "candidates"],
        input=json.dumps({"question": sys.argv[3], "project": sys.argv[2],
                          "candidates": [{"path": r["path"], "fts_rank": r.get("rank"),
                                          "rerank_score": r.get("rerank_score")} for r in results]}),
        text=True, timeout=10,
    )
except Exception:
    pass
```

and change the argument line to `' "$SEARCH_TERMS_JOINED" "$PROJECT" "$QUESTION"'`, where `$QUESTION` is the user's question exactly as asked (Step 2 already has it). Add one sentence under the block: "The extra call records the candidates for the citation log (#377); it never changes the output."

At the end of Step 4, after "Store as `CANDIDATE_FILES`.", add:

````markdown
Record the Grep candidates for the citation log (#377). Pass them as JSON on stdin, never as arguments:

```bash
printf '%s' "$CANDIDATES_JSON" | python3 "$OB_HOOKS/ask_log.py" candidates
```

`CANDIDATES_JSON` is `{"question": "<question as asked>", "project": "<PROJECT>", "candidates": [{"path": "<path>", "fts_rank": null, "rerank_score": null}, ...]}` with one entry per `CANDIDATE_FILES` path. `OB_HOOKS` is the directory `_ob_hooks()` returns; print it once with the Step 3 resolver if you don't have it. This call always exits 0; ignore its output.
````

Replace the body of Step 5 (keep the heading) with:

````markdown
Rank with the script. It applies the rule table below in code and records the scores for the citation log:

```bash
printf '%s' "$RANK_JSON" | python3 "$OB_HOOKS/ask_rank.py"
```

`RANK_JSON` is `{"terms": [<SEARCH_TERMS as JSON strings>], "top": 10}`. It prints a JSON list of up to 10 paths, best first. Store it as `RANKED_FILES`.

If the script prints `[]`, rank by hand with the table below: read the first 40 lines of each candidate for `type:` and `date:`, sort by score descending, and take the top 10.

| Condition | Points |
|-----------|--------|
| Each search term found in content | +2 per term |
| Note type is `claude-insight`, `claude-decision`, or `claude-error-fix` | +3 |
| Note type is `claude-session` | +1 |
| File date is within the last 30 days (relative to today) | +1 |
| Matching `claude/topic/` tag | +2 |

Ties go to the higher `rerank_score`, then the path.
````

After Step 8, add:

````markdown
### Step 9 — Record citations (#377)

Pipe the final answer text, exactly as presented, to the citation log. The script finds the Sources section itself and merges it with the candidates recorded in Steps 3–5:

```bash
printf '%s' "$ANSWER_TEXT" | python3 "$OB_HOOKS/ask_log.py" record
```

It always exits 0 and prints nothing. Do not mention it to the user.
````

- [ ] **Step 4: Run the test**

Run: `pytest tests/test_vault_ask_skill_wiring.py -v`
Expected: PASS.

- [ ] **Step 5: Architecture page and changelog**

List the layers and components first:

```bash
python3 -c "import json;d=json.load(open('docs/architecture/architecture.json'));print([(l['id'],[c['id'] for c in l['components']]) for l in d['layers']])"
```

In `docs/architecture/architecture.json`: add components `ask-log` (`files: ["hooks/ask_log.py"]`) and `ask-rank` (`files: ["hooks/ask_rank.py"]`) to the layer that holds the support modules; add a datastore for `~/.claude/obsidian-brain-ask-log.jsonl` and `~/.claude/obsidian-brain-ask-pending/`; add to the vault-ask flow the steps vault-ask → ask-log (candidates), vault-ask → ask-rank, ask-rank → ask-log (pending update), vault-ask → ask-log (record). Set `lastUpdated` to the commit date. Then:

```bash
~/.claude/skills/architecture-page/scripts/render-html.sh --json docs/architecture/architecture.json --output docs/architecture/architecture.html
~/.claude/skills/architecture-page/scripts/smoke-test.sh --json docs/architecture/architecture.json
```

Expected: `[main]` and `[sparse]` both pass.

`CHANGELOG.md` under `[Unreleased]`:

```markdown
### Added
- `/vault-ask` keeps a local citation log (`~/.claude/obsidian-brain-ask-log.jsonl`): every candidate, its scores, and which notes the answer cited (#377).
- `search_vault(log_access=False, task_context=...)` and `rerank_results(extra_signal=..., extra_weight=...)` for eval replays (#377).
- `scripts/rerank_eval/`: seed eval set, backends and harness for the rerank go gate (#377).

### Changed
- `/vault-ask` Step 5 ranking now runs as a script (`hooks/ask_rank.py`) instead of by hand. Same rules; ties now go to the higher `rerank_score` (#377).

### Fixed
- Note types `claude-snapshot`, `claude-stats`, `claude-emerge`, `claude-memory` and `claude-check-items-report` now have explicit search weights (#376).
```

- [ ] **Step 6: Manual dogfood**

The marketplace points at the local checkout, so skills load this worktree's code only if the directory marketplace's `installLocation` is this worktree. Check with `python3 -c "import json,os;print([m.get('installLocation') for m in json.load(open(os.path.expanduser('~/.claude/plugins/known_marketplaces.json'))).values()])"`. If it points at the main checkout, run the dogfood after merge to develop, or use `/dev-test install` from this worktree.

Run `/vault-ask why does search_vault log every result` in a fresh session. Then:

```bash
tail -1 ~/.claude/obsidian-brain-ask-log.jsonl | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(len(d["candidates"]), sum(c["read"] for c in d["candidates"]), d["cited"], d["cited_unmatched"])'
ls ~/.claude/obsidian-brain-ask-pending/
```

Expected: ≥ 5 candidates, 10 or fewer read, a non-empty `cited`, and an empty pending directory. Repeat with a question rare enough to fall through to Step 4 (fewer than 5 FTS hits) and confirm `fts_rank` is null on its candidates.

- [ ] **Step 7: Commit**

```bash
git add skills/vault-ask/SKILL.md tests/test_vault_ask_skill_wiring.py docs/architecture/ CHANGELOG.md
./scripts/commit-preflight.sh
git commit -m "feat(obsidian-brain): wire vault-ask to the citation log (#377)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: `scripts/rerank_eval/` common helpers and metrics

**Files:**
- Create: `scripts/rerank_eval/__init__.py`, `scripts/rerank_eval/common.py`, `scripts/rerank_eval/metrics.py`
- Test: `tests/test_rerank_eval_metrics.py`

**Interfaces:**
- Produces (`common.py`):
  - `HOME_DIR`, `SEED_PATH = HOME_DIR / "obsidian-brain-rerank-seed.jsonl"`, `SEED_STATUS_PATH = HOME_DIR / "obsidian-brain-rerank-seed.status.json"`, `REPORT_DIR = HOME_DIR / "obsidian-brain-rerank-reports"`, `LAYA_PYTHON = ~/.cache/obsidian-brain/laya-venv/bin/python`
  - `question_terms(q: str) -> list[str]`
  - `candidate_text(path: str, max_chars: int = 1200) -> str` — title, `## Summary` body, first 60 body lines, trimmed
  - `claude_json(prompt: str, model: str, timeout: int = 180) -> dict` — raises `RuntimeError` on failure
  - `write_private(path: Path, text: str) -> None` — atomic, 0o600, parent 0o700
  - Importing `common` puts `hooks/` on `sys.path`.
- Produces (`metrics.py`):
  - `ndcg_at_k(ranked_gains, all_gains, k=5) -> float | None` — `None` when ideal DCG is 0
  - `recall_at_k(ranked_relevant, n_relevant, k=5) -> float | None` — `None` when `n_relevant` is 0
  - `paired_bootstrap(a, b, n=2000, seed=377) -> tuple[float, float, float]` — mean of `a - b` and its 95% percentile CI
  - `p95(xs) -> float` — nearest rank

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for scripts/rerank_eval/metrics.py and common (#377)."""
from __future__ import annotations

import math
import os
import sys

_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import pytest

from rerank_eval import common, metrics


def test_ndcg_hand_computed():
    # ranked gains [2,0,1]: DCG = 2/1 + 0 + 1/2 = 2.5
    # ideal [2,1,0]:      IDCG = 2/1 + 1/log2(3) = 2.63093
    got = metrics.ndcg_at_k([2, 0, 1], [2, 1, 0], k=5)
    assert got == pytest.approx(2.5 / (2 + 1 / math.log2(3)), abs=1e-9)


def test_ndcg_perfect_is_one():
    assert metrics.ndcg_at_k([2, 1, 0], [0, 1, 2]) == pytest.approx(1.0)


def test_ndcg_no_relevant_is_none():
    assert metrics.ndcg_at_k([0, 0], [0, 0]) is None


def test_ndcg_only_top_k_counts():
    assert metrics.ndcg_at_k([0, 0, 0, 0, 0, 2], [2, 0, 0, 0, 0, 0], k=5) == 0.0


def test_recall_counts_source_outside_pool_as_miss():
    # one relevant in the top 5; the known-item source (relevant) is not in the pool
    assert metrics.recall_at_k([True, False, False], n_relevant=2) == 0.5


def test_recall_none_without_relevant():
    assert metrics.recall_at_k([False], n_relevant=0) is None


def test_paired_bootstrap_constant_shift():
    mean, lo, hi = metrics.paired_bootstrap([0.6] * 30, [0.5] * 30)
    assert mean == pytest.approx(0.1) and lo == pytest.approx(0.1) and hi == pytest.approx(0.1)


def test_paired_bootstrap_is_seeded_and_brackets_mean():
    a = [0.1 * (i % 7) for i in range(40)]
    b = [0.1 * (i % 5) for i in range(40)]
    assert metrics.paired_bootstrap(a, b) == metrics.paired_bootstrap(a, b)
    mean, lo, hi = metrics.paired_bootstrap(a, b)
    assert lo <= mean <= hi and lo < hi


def test_paired_bootstrap_rejects_bad_input():
    with pytest.raises(ValueError):
        metrics.paired_bootstrap([], [])
    with pytest.raises(ValueError):
        metrics.paired_bootstrap([1.0], [1.0, 2.0])


def test_p95_nearest_rank():
    assert metrics.p95([float(i) for i in range(1, 21)]) == 19.0
    assert metrics.p95([3.0]) == 3.0


@pytest.mark.parametrize("q,expected", [
    ("Why did we choose SQLite FTS5 for the vault index?", ["choose", "sqlite", "fts5", "vault", "index"]),
    ("what is the status of #377", ["377"]),
])
def test_question_terms(q, expected):
    assert common.question_terms(q) == expected


def test_candidate_text_trims(tmp_path):
    p = tmp_path / "n.md"
    p.write_text("---\ntitle: T\n---\n## Summary\n" + "word " * 1000 + "\n")
    out = common.candidate_text(str(p), max_chars=200)
    assert out.startswith("T\n") and len(out) <= 200


def test_candidate_text_without_frontmatter_uses_stem(tmp_path):
    p = tmp_path / "2026-01-01-x.md"
    p.write_text("plain body\n")
    assert common.candidate_text(str(p)).startswith("2026-01-01-x\n")


def test_claude_json_extracts_object(monkeypatch):
    class R:
        returncode, stdout, stderr = 0, 'noise {"a": 1} tail', ""
    monkeypatch.setattr(common.subprocess, "run", lambda *a, **k: R())
    assert common.claude_json("p", "opus") == {"a": 1}


@pytest.mark.parametrize("rc,out", [(1, ""), (0, "no json here"), (0, "[1, 2]")])
def test_claude_json_raises_on_failure(monkeypatch, rc, out):
    class R:
        returncode, stdout, stderr = rc, out, "boom"
    monkeypatch.setattr(common.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(RuntimeError):
        common.claude_json("p", "opus")


def test_write_private(tmp_path):
    target = tmp_path / "d" / "f.json"
    common.write_private(target, "x")
    assert target.read_text() == "x"
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert oct(target.parent.stat().st_mode & 0o777) == "0o700"
```

- [ ] **Step 2: Run them to see them fail**

Run: `pytest tests/test_rerank_eval_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rerank_eval'`.

- [ ] **Step 3: Implement**

`scripts/rerank_eval/__init__.py`:

```python
"""#377 rerank eval: seed set, backends and harness. Never imported by hooks/."""
```

`scripts/rerank_eval/common.py`:

```python
"""Shared helpers for the #377 rerank eval."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_HOOKS = Path(__file__).resolve().parent.parent.parent / "hooks"
if str(_HOOKS) not in sys.path:
    sys.path.insert(0, str(_HOOKS))

HOME_DIR = Path.home() / ".claude"
SEED_PATH = HOME_DIR / "obsidian-brain-rerank-seed.jsonl"
SEED_STATUS_PATH = HOME_DIR / "obsidian-brain-rerank-seed.status.json"
REPORT_DIR = HOME_DIR / "obsidian-brain-rerank-reports"
LAYA_PYTHON = Path.home() / ".cache" / "obsidian-brain" / "laya-venv" / "bin" / "python"

_STOP = set("""
a an and are as at be by can did do does for from had has have how i in is it its
me my of on or our so that the this to was we were what when where which who why
with you your should would could about into over than then there these those
status
""".split())
_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]*[a-z0-9]|[a-z0-9]")


def question_terms(q: str) -> list:
    out = []
    for w in _WORD.findall(q.lower()):
        w = w.strip("._-")
        if (len(w) >= 3 or w.isdigit()) and w not in _STOP and w not in out:
            out.append(w)
    return out


_FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_TITLE = re.compile(r"^title:\s*(.+)$", re.M)
_SUMMARY = re.compile(r"(?ms)^## Summary\s*\n(.*?)(?=^## |\Z)")


def candidate_text(path: str, max_chars: int = 1200) -> str:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    fm = _FM.match(text)
    title_m = _TITLE.search(fm.group(1)) if fm else None
    title = title_m.group(1).strip().strip('"') if title_m else Path(path).stem
    body = text[fm.end():] if fm else text
    summ = _SUMMARY.search(body)
    head = "\n".join(body.splitlines()[:60])
    parts = [title, summ.group(1).strip() if summ else "", head]
    return "\n".join(p for p in parts if p)[:max_chars]


def claude_json(prompt: str, model: str, timeout: int = 180) -> dict:
    r = subprocess.run(
        ["claude", "-p", "--model", model, "--output-format", "text"],
        input=prompt, capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f"claude -p failed ({r.returncode}): {r.stderr[:300]}")
    start = r.stdout.find("{")
    if start < 0:
        raise RuntimeError("no JSON object in output")
    try:
        obj, _ = json.JSONDecoder().raw_decode(r.stdout[start:])
    except ValueError as exc:
        raise RuntimeError(f"bad JSON in output: {exc}") from exc
    if not isinstance(obj, dict):
        raise RuntimeError("output JSON is not an object")
    return obj


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
```

`scripts/rerank_eval/metrics.py`:

```python
"""Ranking metrics for the #377 eval: nDCG@k, recall@k, paired bootstrap, p95."""
from __future__ import annotations

import math
import random


def _dcg(gains: list) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at_k(ranked_gains: list, all_gains: list, k: int = 5):
    ideal = _dcg(sorted(all_gains, reverse=True)[:k])
    if ideal == 0:
        return None
    return _dcg(list(ranked_gains)[:k]) / ideal


def recall_at_k(ranked_relevant: list, n_relevant: int, k: int = 5):
    if n_relevant <= 0:
        return None
    return sum(1 for r in list(ranked_relevant)[:k] if r) / n_relevant


def paired_bootstrap(a: list, b: list, n: int = 2000, seed: int = 377) -> tuple:
    if len(a) != len(b) or not a:
        raise ValueError("paired_bootstrap needs two equal, non-empty lists")
    diffs = [x - y for x, y in zip(a, b)]
    rng = random.Random(seed)
    m = len(diffs)
    means = sorted(sum(diffs[rng.randrange(m)] for _ in range(m)) / m for _ in range(n))
    return (sum(diffs) / m, means[int(0.025 * n)], means[int(0.975 * n) - 1])


def p95(xs: list) -> float:
    s = sorted(xs)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_rerank_eval_metrics.py tests/test_py39_compat.py -v`
Expected: PASS. If `test_question_terms` fails on a stop word, fix `_STOP`, not the test.

- [ ] **Step 5: Commit**

```bash
git add scripts/rerank_eval/__init__.py scripts/rerank_eval/common.py scripts/rerank_eval/metrics.py tests/test_rerank_eval_metrics.py
./scripts/commit-preflight.sh
git commit -m "feat(obsidian-brain): rerank eval metrics and helpers (#377)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Seed set builder `scripts/rerank_eval/seed.py`

**Files:**
- Create: `scripts/rerank_eval/seed.py`
- Test: `tests/test_rerank_eval_seed.py`

**Interfaces:**
- Consumes: `common.*` (Task 5); `vault_index.search_vault(..., log_access=False, task_context="search")`, `vault_index._connect`, `vault_index.QTYPES` (Task 1); `ask_rank.rank_candidates` (Task 3); `obsidian_utils.load_config`.
- Produces:
  - Seed record (one JSON line in `SEED_PATH`): `{"qid": str, "question": str, "origin": "history"|"known_item", "source_path": str|None, "source_in_pool": bool|None, "qtype": str, "candidates": [{"path", "title", "type", "date", "rank", "rerank_score", "rule_score", "label"}]}`. `candidates` is in `current` order (index 0 = best).
  - `extract_history_questions(sessions_dir: Path) -> list[str]`
  - `sample_known_items(db_path: str, n: int = 50, seed: int = 377) -> list[dict]`
  - `build_pool(db_path: str, question: str, limit: int = 20) -> list[dict]`
  - `label_question(question: str, pool: list[dict]) -> tuple[list[int], str]`
  - `make_record(qid, question, origin, source_path, db_path) -> dict`
  - `spot_check(records: list[dict], answers: list[int]) -> dict` — `{"n", "agreement", "status": "PASS"|"INCONCLUSIVE"}`
  - CLI: `seed.py build [--known 50] [--limit N]`, `seed.py spot-check [--n 20]`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for scripts/rerank_eval/seed.py (#377). All model calls are faked."""
from __future__ import annotations

import os
import sqlite3
import sys

_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import pytest

import vault_index
from rerank_eval import seed


def test_extract_history_questions(tmp_path):
    (tmp_path / "a.md").write_text(
        "<command-name>/obsidian-brain:vault-ask</command-name>\n"
        "<command-args>why does search log every result</command-args>\n"
        "later: /vault-ask what did we decide about Jev pricing\n"
        "/vault-ask short\n")
    (tmp_path / "b.md").write_text("/vault-ask why does search log every result\n")
    qs = seed.extract_history_questions(tmp_path)
    assert qs == ["what did we decide about jev pricing", "why does search log every result"]


@pytest.fixture
def db(tmp_vault):
    s = tmp_vault / "claude-sessions"
    i = tmp_vault / "claude-insights"
    for k in range(12):
        folder, typ = (i, "claude-insight") if k % 2 else (s, "claude-session")
        (folder / f"2026-0{1 + k % 9}-10-p{k % 3}-{k}.md").write_text(
            f"---\ntype: {typ}\ndate: 2026-0{1 + k % 9}-10\nproject: p{k % 3}\n"
            f"title: Note {k} about sqlite\ntags:\n  - claude/x\nstatus: summarized\n---\n"
            f"sqlite index note {k}\n")
    path = str(tmp_vault / "t.db")
    vault_index.ensure_index(str(tmp_vault), ["claude-sessions", "claude-insights"], db_path=path)
    return path


def test_sample_known_items_spreads_types_and_is_seeded(db):
    a = seed.sample_known_items(db, n=6)
    assert a == seed.sample_known_items(db, n=6)
    assert len(a) == 6 and {r["type"] for r in a} == {"claude-session", "claude-insight"}


def test_build_pool_writes_no_access_rows(db):
    pool = seed.build_pool(db, "why sqlite index", limit=5)
    assert 0 < len(pool) <= 5
    assert [p["rule_score"] for p in pool] == sorted((p["rule_score"] for p in pool), reverse=True)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0] == 0
    conn.close()


def test_label_question_parses_and_clamps(monkeypatch, tmp_path):
    notes = []
    for k in range(3):
        p = tmp_path / f"n{k}.md"
        p.write_text(f"---\ntitle: N{k}\n---\nbody {k}\n")
        notes.append({"path": str(p)})
    monkeypatch.setattr(seed.common, "claude_json",
                        lambda prompt, model, timeout=180: {"labels": {"0": 2, "1": 7, "2": "x"}, "qtype": "debug"})
    labels, qtype = seed.label_question("q?", notes)
    assert labels == [2, 0, 0] and qtype == "debug"


def test_label_question_bad_qtype_falls_back(monkeypatch, tmp_path):
    p = tmp_path / "n.md"
    p.write_text("x")
    monkeypatch.setattr(seed.common, "claude_json",
                        lambda *a, **k: {"labels": {"0": 1}, "qtype": "banana"})
    assert seed.label_question("q?", [{"path": str(p)}]) == ([1], "howto")


def test_spot_check_threshold():
    recs = [{"label": 2}] * 20
    assert seed.spot_check(recs, [2] * 17 + [0] * 3)["status"] == "PASS"          # 85%
    assert seed.spot_check(recs, [2] * 16 + [0] * 4)["status"] == "INCONCLUSIVE"  # 80%
    assert seed.spot_check([], [])["status"] == "INCONCLUSIVE"


def test_known_item_source_outside_pool_is_recorded(monkeypatch):
    monkeypatch.setattr(seed, "build_pool", lambda db_path, q, limit=20: [
        {"path": "/other.md", "title": "o", "type": "claude-session", "date": "2026-01-01",
         "rank": -1.0, "rerank_score": 0.1, "rule_score": 1}])
    monkeypatch.setattr(seed, "label_question", lambda q, pool: ([0], "decision"))
    rec = seed.make_record("k1", "q?", "known_item", "/src.md", "unused.db")
    assert rec["source_in_pool"] is False
    assert rec["candidates"][0]["label"] == 0


def test_known_item_source_in_pool_forced_relevant(monkeypatch):
    monkeypatch.setattr(seed, "build_pool", lambda db_path, q, limit=20: [
        {"path": "/src.md", "title": "s", "type": "claude-insight", "date": "2026-01-01",
         "rank": -1.0, "rerank_score": 0.1, "rule_score": 3}])
    monkeypatch.setattr(seed, "label_question", lambda q, pool: ([1], "decision"))
    rec = seed.make_record("k1", "q?", "known_item", "/src.md", "unused.db")
    assert rec["source_in_pool"] is True and rec["candidates"][0]["label"] == 2
```

- [ ] **Step 2: Run them to see them fail**

Run: `pytest tests/test_rerank_eval_seed.py -v`
Expected: FAIL — `ImportError: cannot import name 'seed'`.

- [ ] **Step 3: Implement `scripts/rerank_eval/seed.py`**

```python
"""Build the #377 seed eval set: questions, candidate pools, Opus labels, spot-check.

Usage:
  python3 scripts/rerank_eval/seed.py build [--known 50] [--limit N]
  python3 scripts/rerank_eval/seed.py spot-check [--n 20]
Writes ~/.claude/obsidian-brain-rerank-seed.jsonl (never commit it).
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rerank_eval import common  # noqa: E402  (puts hooks/ on sys.path)

import ask_rank  # noqa: E402
import vault_index  # noqa: E402

_HIST = [
    re.compile(r"<command-name>/?(?:obsidian-brain:)?vault-ask</command-name>\s*"
               r"(?:<command-message>[^<]*</command-message>\s*)?"
               r"<command-args>(.*?)</command-args>", re.S),
    re.compile(r"(?m)(?:^|[\s>*`])/(?:obsidian-brain:)?vault-ask[ \t]+([^\n`]{8,300})"),
]
_KNOWN_TYPES = ("claude-insight", "claude-decision", "claude-error-fix", "claude-session",
                "claude-retro", "claude-snapshot")


def extract_history_questions(sessions_dir: Path) -> list:
    qs = set()
    for p in sorted(Path(sessions_dir).glob("*.md")):
        text = p.read_text(encoding="utf-8", errors="replace")
        for pat in _HIST:
            for m in pat.finditer(text):
                q = " ".join(m.group(1).split()).strip().lower()
                if len(q.split()) >= 4 and not q.startswith("<"):
                    qs.add(q)
    return sorted(qs)


def sample_known_items(db_path: str, n: int = 50, seed: int = 377) -> list:
    conn = vault_index._connect(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT path, type, project, date FROM notes WHERE type IN (%s) ORDER BY path"
            % ",".join("?" * len(_KNOWN_TYPES)), _KNOWN_TYPES)]
    finally:
        conn.close()
    rng = random.Random(seed)
    by_type: dict = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    for v in by_type.values():
        rng.shuffle(v)
    out: list = []
    # Round-robin over types so every type is represented before any repeats.
    while len(out) < n and any(by_type.values()):
        for t in sorted(by_type):
            if by_type[t] and len(out) < n:
                out.append(by_type[t].pop())
    return out


def build_pool(db_path: str, question: str, limit: int = 20) -> list:
    terms = common.question_terms(question)
    hits = vault_index.search_vault(db_path, " ".join(terms), limit=limit,
                                    log_access=False, task_context="search")
    ranked = ask_rank.rank_candidates(
        [{"path": h["path"], "rerank_score": h.get("rerank_score")} for h in hits], terms)
    meta = {h["path"]: h for h in hits}
    return [{"path": r["path"], "title": meta[r["path"]].get("title"),
             "type": meta[r["path"]].get("type"), "date": meta[r["path"]].get("date"),
             "rank": meta[r["path"]].get("rank"), "rerank_score": r.get("rerank_score"),
             "rule_score": r["rule_score"]} for r in ranked]


_KNOWN_PROMPT = """You will see one note from a personal engineering knowledge base.
Write ONE question that this note answers, the way its author would ask months later
without remembering the note's exact wording. Do not copy distinctive phrases or IDs
from the title. Reply with JSON only: {{"question": "..."}}

NOTE:
{note}
"""

_LABEL_PROMPT = """You judge search results for a personal engineering knowledge base.

QUESTION: {question}

For each numbered note, give a relevance label:
0 = not relevant, 1 = related but does not answer, 2 = answers the question.
Also classify the question as one of: decision (why or what did we decide),
debug (an error or a bug), howto (how to do something, or general), status
(where something stands or what is left).

Reply with JSON only: {{"labels": {{"0": <0-2>, "1": <0-2>, ...}}, "qtype": "<type>"}}

NOTES:
{notes}
"""


def write_known_item_question(path: str) -> str:
    out = common.claude_json(_KNOWN_PROMPT.format(note=common.candidate_text(path, 3000)), "opus")
    q = str(out.get("question", "")).strip()
    if not q:
        raise RuntimeError("empty question")
    return q


def label_question(question: str, pool: list) -> tuple:
    notes = "\n\n".join(f"[{i}] {common.candidate_text(c['path'])}" for i, c in enumerate(pool))
    out = common.claude_json(_LABEL_PROMPT.format(question=question, notes=notes), "opus", timeout=300)
    raw = out.get("labels") if isinstance(out.get("labels"), dict) else {}
    labels = []
    for i in range(len(pool)):
        v = raw.get(str(i))
        labels.append(v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 2 else 0)
    qtype = out.get("qtype")
    return labels, qtype if qtype in vault_index.QTYPES else "howto"


def make_record(qid: str, question: str, origin: str, source_path, db_path: str) -> dict:
    pool = build_pool(db_path, question)
    labels, qtype = label_question(question, pool) if pool else ([], "howto")
    for c, lab in zip(pool, labels):
        c["label"] = lab
    in_pool = None
    if source_path is not None:
        in_pool = False
        for c in pool:
            if c["path"] == source_path:
                c["label"] = 2  # the known item answers its own question by construction
                in_pool = True
    return {"qid": qid, "question": question, "origin": origin, "source_path": source_path,
            "source_in_pool": in_pool, "qtype": qtype, "candidates": pool}


def spot_check(records: list, answers: list) -> dict:
    n = len(answers)
    agree = sum(1 for r, a in zip(records, answers) if r["label"] == a)
    rate = agree / n if n else 0.0
    return {"n": n, "agreement": round(rate, 3), "status": "PASS" if n and rate >= 0.85 else "INCONCLUSIVE"}


def _cmd_build(args) -> int:
    from obsidian_utils import load_config
    c = load_config()
    vault = Path(c["vault_path"])
    sessions = c.get("sessions_folder", "claude-sessions")
    db = vault_index.ensure_index(str(vault), [sessions, c.get("insights_folder", "claude-insights")])
    jobs = [(f"h{i}", q, "history", None)
            for i, q in enumerate(extract_history_questions(vault / sessions))]
    for i, row in enumerate(sample_known_items(db, n=args.known)):
        try:
            jobs.append((f"k{i}", write_known_item_question(row["path"]), "known_item", row["path"]))
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"skip known item {row['path']}: {exc}", file=sys.stderr)
    if args.limit:
        jobs = jobs[: args.limit]
    lines = []
    for qid, q, origin, src in jobs:
        try:
            lines.append(json.dumps(make_record(qid, q, origin, src, db)))
            print(f"{qid} ok", file=sys.stderr)
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"{qid} failed: {exc}", file=sys.stderr)
    common.write_private(common.SEED_PATH, "\n".join(lines) + "\n")
    print(f"wrote {len(lines)} questions to {common.SEED_PATH}")
    return 0


def _cmd_spot_check(args) -> int:
    recs = [json.loads(l) for l in common.SEED_PATH.read_text().splitlines() if l.strip()]
    pairs = [(r["question"], c) for r in recs for c in r["candidates"]]
    rng = random.Random(377)
    by_label: dict = {}
    for p in pairs:
        by_label.setdefault(p[1]["label"], []).append(p)
    picked = []
    while len(picked) < args.n and any(by_label.values()):
        for lab in sorted(by_label):
            if by_label[lab] and len(picked) < args.n:
                picked.append(by_label[lab].pop(rng.randrange(len(by_label[lab]))))
    answers = []
    for i, (q, c) in enumerate(picked, 1):
        print(f"\n[{i}/{len(picked)}] QUESTION: {q}\n---\n{common.candidate_text(c['path'], 1500)}\n---")
        a = input("0 not relevant / 1 related / 2 answers it: ").strip()
        answers.append(int(a) if a in ("0", "1", "2") else -1)
    result = spot_check([c for _, c in picked], answers)
    common.write_private(common.SEED_STATUS_PATH, json.dumps(result))
    print(json.dumps(result))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--known", type=int, default=50)
    b.add_argument("--limit", type=int, default=0)
    s = sub.add_parser("spot-check")
    s.add_argument("--n", type=int, default=20)
    args = ap.parse_args(argv)
    return _cmd_build(args) if args.cmd == "build" else _cmd_spot_check(args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_rerank_eval_seed.py tests/test_py39_compat.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/rerank_eval/seed.py tests/test_rerank_eval_seed.py
./scripts/commit-preflight.sh
git commit -m "feat(obsidian-brain): seed eval set builder for rerank eval (#377)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Backends and `laya_score.py`

**Files:**
- Create: `scripts/rerank_eval/backends.py`, `scripts/rerank_eval/laya_score.py`
- Test: `tests/test_rerank_eval_backends.py`

**Interfaces:**
- Consumes: `common.candidate_text`, `common.claude_json`, `common.LAYA_PYTHON` (Task 5); `obsidian_utils.scrub_secrets`.
- Modifies: `hooks/obsidian_utils.py` `_SECRET_PATTERNS` (l.4474). It has no pattern for bare `sk-…` keys, `github_pat_…` or Slack `xox…` tokens. The gap also affects vault notes, so it is fixed here, not filed.
- Produces:
  - `scrub_for_cloud(text: str) -> str` — `scrub_secrets`, plus home path → `~` and login name → `<user>` (the identity literals the cc-token-router phase A scrubber removes; that scrubber lives in another repo and cannot be imported)
  - `class Backend`: `name: str`; `available() -> tuple[bool, str]`; `score(question: str, candidates: list[dict]) -> list[float]`; `classify(question: str) -> str | None`; `close() -> None`
  - `CurrentBackend`, `HaikuBackend`, `JevBackend(opener=urllib.request.urlopen, timeout=5.0)`, `LayaBackend(python=common.LAYA_PYTHON, popen=subprocess.Popen)`
  - `keyword_qtype(question: str) -> str`
  - `all_backends() -> list[Backend]`, order `current, laya, jev, haiku`
  - `laya_score.py` protocol: one JSON object per stdin line. `{"op": "score", "question", "notes": [str]}` → `{"p": [float]}`; `{"op": "classify", "question"}` → `{"qtype": str}`; errors → `{"error": str}`. Prints `{"ready": true}` once loaded.

- [ ] **Step 0: Close the scrubber gap (TDD)**

Add to `tests/test_security.py`:

```python
import pytest
from obsidian_utils import scrub_secrets


@pytest.mark.parametrize("secret", [
    "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
    "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789",
    "sk-abcdefghijklmnopqrstuvwx",
    "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz0123456789",
    "xoxb-1234567890-abcdefghijkl",
])
def test_scrub_secrets_bare_provider_keys(secret):
    out = scrub_secrets(f"see {secret} here")
    assert secret not in out and "[REDACTED" in out


def test_scrub_secrets_leaves_prose_alone():
    text = "the sk-learn task and a risk-averse plan"
    assert scrub_secrets(text) == text
```

Run: `pytest tests/test_security.py -k scrub_secrets -v`. Expected: the five provider-key cases FAIL.

Append to `_SECRET_PATTERNS` in `hooks/obsidian_utils.py`:

```python
    (re.compile(r'\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}'), '[REDACTED:api-key]'),
    (re.compile(r'\bgithub_pat_[A-Za-z0-9_]{22,}'), '[REDACTED:github-token]'),
    (re.compile(r'\bxox[abprs]-[A-Za-z0-9-]{10,}'), '[REDACTED:slack-token]'),
```

Run the same tests again. Expected: PASS. `pytest tests/test_security.py tests/test_obsidian_utils.py -q` stays green. This is committed with Step 6 (same task).

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for scripts/rerank_eval/backends.py (#377). No network, no laya, no claude."""
from __future__ import annotations

import io
import json
import os
import sys

_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import pytest

from rerank_eval import backends


@pytest.fixture
def cands(tmp_path):
    out = []
    for k in range(3):
        p = tmp_path / f"n{k}.md"
        p.write_text(f"---\ntitle: N{k}\n---\nbody {k} key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789\n")
        out.append({"path": str(p)})
    return out


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_current_preserves_pool_order(cands):
    s = backends.CurrentBackend().score("q", cands)
    assert s[0] > s[1] > s[2]


def test_haiku_parses_scores(monkeypatch, cands):
    monkeypatch.setattr(backends.common, "claude_json", lambda p, m, timeout=120: {"scores": [0.1, 0.9, "x"]})
    assert backends.HaikuBackend().score("q", cands) == [0.1, 0.9, 0.0]


def test_jev_unavailable_without_opt_in(monkeypatch):
    monkeypatch.delenv("OB_JEV", raising=False)
    ok, why = backends.JevBackend().available()
    assert not ok and "OB_JEV" in why


def test_jev_unavailable_without_key(monkeypatch):
    monkeypatch.setenv("OB_JEV", "1")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    ok, why = backends.JevBackend().available()
    assert not ok and "TYPESAFE_API_KEY" in why


def test_jev_request_is_scrubbed_and_pinned(monkeypatch, cands):
    monkeypatch.setenv("OB_JEV", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    sent = {}

    def opener(req, timeout):
        sent["body"] = json.loads(req.data)
        sent["auth"] = req.get_header("Authorization")
        return Resp(json.dumps({"answers": {f"n{i}": {"noul": 0.2 * i} for i in range(3)}}).encode())

    b = backends.JevBackend(opener=opener)
    assert b.available()[0]
    assert b.score("q", cands) == [0.0, 0.2, 0.4]
    assert sent["body"]["model"] == "jev-1.13.0"
    assert "sk-ant-api03" not in sent["body"]["state"]
    assert set(sent["body"]["questions"]) == {"n0", "n1", "n2"}
    assert sent["auth"] == "Bearer k"


def test_scrub_for_cloud_removes_identity():
    home = os.path.expanduser("~")
    user = backends._login()
    out = backends.scrub_for_cloud(f"file at {home}/dev/x.py by {user}")
    assert home not in out and "~/dev/x.py" in out
    if len(user) >= 3:
        assert user not in out


def test_jev_bad_response_raises(monkeypatch, cands):
    monkeypatch.setenv("OB_JEV", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    b = backends.JevBackend(opener=lambda req, timeout: Resp(b'{"answers": {}}'))
    with pytest.raises(RuntimeError):
        b.score("q", cands)


def test_jev_classify(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    b = backends.JevBackend(opener=lambda req, timeout: Resp(
        b'{"answers": {"qtype": {"choice": "status"}}}'))
    assert b.classify("what is left") == "status"


def test_laya_unavailable_without_venv(tmp_path):
    ok, why = backends.LayaBackend(python=tmp_path / "nope").available()
    assert not ok and "python3 -m venv" in why


class FakeProc:
    def __init__(self, replies):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("\n".join(json.dumps(r) for r in replies) + "\n")
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def test_laya_protocol(tmp_path, cands):
    py = tmp_path / "python"
    py.write_text("")
    proc = FakeProc([{"ready": True}, {"p": [0.3, 0.2, 0.1]}, {"qtype": "debug"}])
    b = backends.LayaBackend(python=py, popen=lambda *a, **k: proc)
    assert b.score("q", cands) == [0.3, 0.2, 0.1]
    assert b.classify("q") == "debug"
    sent = [json.loads(l) for l in proc.stdin.getvalue().splitlines()]
    assert sent[0]["op"] == "score" and len(sent[0]["notes"]) == 3
    b.close()
    assert proc.terminated


def test_laya_error_reply_raises(tmp_path, cands):
    py = tmp_path / "python"
    py.write_text("")
    b = backends.LayaBackend(python=py, popen=lambda *a, **k: FakeProc([{"ready": True}, {"error": "oom"}]))
    with pytest.raises(RuntimeError, match="oom"):
        b.score("q", cands)


def test_laya_not_ready_raises(tmp_path, cands):
    py = tmp_path / "python"
    py.write_text("")
    b = backends.LayaBackend(python=py, popen=lambda *a, **k: FakeProc([{"error": "no checkpoint"}]))
    with pytest.raises(RuntimeError, match="did not start"):
        b.score("q", cands)


@pytest.mark.parametrize("q,t", [
    ("why did we choose sqlite", "decision"),
    ("TypeError when importing hooks", "debug"),
    ("how do I run the preflight", "howto"),
    ("what is left on #377", "status"),
])
def test_keyword_qtype(q, t):
    assert backends.keyword_qtype(q) == t


def test_all_backends_order():
    assert [b.name for b in backends.all_backends()] == ["current", "laya", "jev", "haiku"]
```

- [ ] **Step 2: Run them to see them fail**

Run: `pytest tests/test_rerank_eval_backends.py -v`
Expected: FAIL — `ImportError: cannot import name 'backends'`.

- [ ] **Step 3: Implement `scripts/rerank_eval/backends.py`**

```python
"""Scoring backends for the #377 rerank eval. Each returns p(relevant) per candidate."""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

from rerank_eval import common

from obsidian_utils import scrub_secrets  # hooks/ is on sys.path via common

JEV_MODEL = "jev-1.13.0"
JEV_DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
_QTYPE_INSTR = "What kind of question is this?"
_QTYPE_OPTIONS = {"decision": "asks why or what was decided",
                  "debug": "asks about an error or a bug",
                  "howto": "asks how to do something, or a general question",
                  "status": "asks where something stands or what is left"}


def _login() -> str:
    import getpass
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""


def scrub_for_cloud(text: str) -> str:
    """scrub_secrets plus identity literals: home path -> ~, login name -> <user>."""
    text = scrub_secrets(text).replace(str(Path.home()), "~")
    user = _login()
    if len(user) >= 3:
        text = re.sub(r"\b" + re.escape(user) + r"\b", "<user>", text)
    return text


class Backend:
    name = "base"

    def available(self) -> tuple:
        return True, ""

    def score(self, question: str, candidates: list) -> list:
        raise NotImplementedError

    def classify(self, question: str):
        return None

    def close(self) -> None:
        pass


class CurrentBackend(Backend):
    """The pool is already in current order (seed.build_pool), so score = -index."""
    name = "current"

    def score(self, question, candidates):
        return [float(-i) for i in range(len(candidates))]


class HaikuBackend(Backend):
    name = "haiku"

    def score(self, question, candidates):
        notes = "\n\n".join(f"[{i}] {common.candidate_text(c['path'])}" for i, c in enumerate(candidates))
        out = common.claude_json(
            "Give the probability (0 to 1) that each numbered note answers the question.\n"
            f"QUESTION: {question}\n\nNOTES:\n{notes}\n\n"
            'Reply with JSON only: {"scores": [p0, p1, ...]} in note order.', "haiku", timeout=120)
        raw = out.get("scores", [])
        return [float(raw[i]) if i < len(raw) and isinstance(raw[i], (int, float)) else 0.0
                for i in range(len(candidates))]


class JevBackend(Backend):
    name = "jev"

    def __init__(self, opener=urllib.request.urlopen, timeout: float = 5.0):
        self.opener = opener
        self.timeout = timeout

    def available(self):
        if os.environ.get("OB_JEV") != "1":
            return False, "jev skipped: set OB_JEV=1 to opt in (sends scrubbed snippets to TypeSafe)"
        if not os.environ.get("TYPESAFE_API_KEY"):
            return False, "jev skipped: TYPESAFE_API_KEY is not set"
        return True, ""

    def _post(self, body: dict) -> dict:
        url = os.environ.get("TYPESAFE_BASE_URL", JEV_DEFAULT_URL)
        req = urllib.request.Request(url, json.dumps(body).encode(), {
            "Authorization": "Bearer " + os.environ["TYPESAFE_API_KEY"],
            "Content-Type": "application/json"})
        with self.opener(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())

    def score(self, question, candidates):
        notes = {f"n{i}": scrub_for_cloud(common.candidate_text(c["path"])) for i, c in enumerate(candidates)}
        body = {
            "model": JEV_MODEL,
            "state": json.dumps({"question": scrub_for_cloud(question), "notes": notes}, sort_keys=True),
            "questions": {k: {"type": "noul", "instructions": f"Does note {k} answer the question?"}
                          for k in notes},
        }
        answers = self._post(body).get("answers", {})
        try:
            return [float(answers[k]["noul"]) for k in notes]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"bad jev response: {exc!r}") from exc

    def classify(self, question):
        body = {"model": JEV_MODEL, "state": json.dumps({"question": scrub_for_cloud(question)}),
                "questions": {"qtype": {"type": "choice", "instructions": _QTYPE_INSTR,
                                        "criteria": _QTYPE_OPTIONS}}}
        return self._post(body).get("answers", {}).get("qtype", {}).get("choice")


class LayaBackend(Backend):
    name = "laya"

    def __init__(self, python: Path = common.LAYA_PYTHON, popen=subprocess.Popen):
        self.python = Path(python)
        self.popen = popen
        self.proc = None

    def available(self):
        if not self.python.exists():
            return False, ("laya skipped: no venv. Create it with: python3 -m venv "
                           "~/.cache/obsidian-brain/laya-venv && "
                           "~/.cache/obsidian-brain/laya-venv/bin/pip install laya==0.3.21")
        return True, ""

    def _ensure(self):
        if self.proc is None:
            script = str(Path(__file__).resolve().parent / "laya_score.py")
            self.proc = self.popen([str(self.python), script], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True, bufsize=1)
            ready = json.loads(self.proc.stdout.readline() or "{}")
            if not ready.get("ready"):
                raise RuntimeError(f"laya did not start: {ready}")

    def _ask(self, req: dict) -> dict:
        self._ensure()
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        out = json.loads(self.proc.stdout.readline() or '{"error": "laya exited"}')
        if "error" in out:
            raise RuntimeError(f"laya: {out['error']}")
        return out

    def score(self, question, candidates):
        # 900 chars keeps question + note inside laya's 512-token window.
        notes = [common.candidate_text(c["path"], 900) for c in candidates]
        return [float(p) for p in self._ask({"op": "score", "question": question, "notes": notes})["p"]]

    def classify(self, question):
        return self._ask({"op": "classify", "question": question}).get("qtype")

    def close(self):
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait(timeout=10)
            self.proc = None


_KW = [
    ("debug", re.compile(r"\b(error|exception|traceback|fail\w*|bug|broke\w*|crash\w*|typeerror|fix)\b")),
    ("status", re.compile(r"\b(status|left|remaining|progress|where are we|open items?)\b")),
    ("decision", re.compile(r"\b(why|decid\w*|decision|chose|choose|picked|trade-?off)\b")),
]


def keyword_qtype(question: str) -> str:
    q = question.lower()
    for t, pat in _KW:
        if pat.search(q):
            return t
    return "howto"


def all_backends() -> list:
    return [CurrentBackend(), LayaBackend(), JevBackend(), HaikuBackend()]
```

`scripts/rerank_eval/laya_score.py` (runs only inside the laya venv; it must still parse on 3.9 for `test_py39_compat.py`):

```python
"""Runs inside the laya venv (Python >= 3.10). Never imported by plugin code.

Protocol: one JSON request per stdin line, one JSON reply per stdout line.
Prints {"ready": true} once the checkpoint is loaded.
"""
from __future__ import annotations

import json
import sys

REL = {"rel": {"type": "noul", "instructions": "Does the note answer the question?"}}
QTYPE = {"qtype": {"type": "choice",
                   "instructions": "What kind of question is this?",
                   "criteria": {"decision": "asks why or what was decided",
                                "debug": "asks about an error or a bug",
                                "howto": "asks how to do something, or a general question",
                                "status": "asks where something stands or what is left"}}}


def main() -> int:
    try:
        from laya import Router  # only importable inside the venv
        router = Router()
        router.predict({"question": "warm up", "note": "warm up"}, REL)  # load the checkpoint now
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), flush=True)
        return 1
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
            if req["op"] == "score":
                p = [router.predict({"question": req["question"], "note": n}, REL)["answers"]["rel"]["noul"]
                     for n in req["notes"]]
                out = {"p": p}
            elif req["op"] == "classify":
                out = {"qtype": router.predict({"question": req["question"]}, QTYPE)["answers"]["qtype"]["choice"]}
            else:
                out = {"error": f"unknown op {req.get('op')!r}"}
        except Exception as exc:  # noqa: BLE001 — report, keep serving
            out = {"error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(out), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

If Task 0 Step 2 found `Router.predict_batch`, replace the per-note list comprehension in `score` with one `predict_batch` call using the signature recorded in `docs/plans/377-live-checks.md`, and keep the per-note loop as the fallback when `predict_batch` raises `TypeError`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_rerank_eval_backends.py tests/test_py39_compat.py -v`
Expected: PASS. `test_jev_request_is_scrubbed_and_pinned` depends on Step 0's new `sk-` pattern.

- [ ] **Step 5: Live smoke of laya (uses the Task 0 venv)**

```bash
printf '%s\n' '{"op":"score","question":"why sqlite","notes":["We chose SQLite FTS5.","Lunch menu."]}' '{"op":"classify","question":"why did we pick sqlite"}' \
  | ~/.cache/obsidian-brain/laya-venv/bin/python scripts/rerank_eval/laya_score.py
```

Expected: `{"ready": true}`, then `{"p": [x, y]}` with x > y, then `{"qtype": "decision"}` (or another valid type; note it if wrong).

- [ ] **Step 6: Commit**

```bash
git add hooks/obsidian_utils.py tests/test_security.py scripts/rerank_eval/backends.py scripts/rerank_eval/laya_score.py tests/test_rerank_eval_backends.py
./scripts/commit-preflight.sh
git commit -m "feat(obsidian-brain): current, haiku, jev and laya rerank backends (#377)

Also redact bare sk-, github_pat_ and xox tokens in scrub_secrets.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Harness `scripts/rerank_eval/harness.py`

**Files:**
- Create: `scripts/rerank_eval/harness.py`
- Test: `tests/test_rerank_eval_harness.py`

**Interfaces:**
- Consumes: `metrics.*`, `common.*` (Task 5); seed records and `seed.make_record` (Task 6); `backends.*` (Task 7); `vault_index.rerank_results(extra_signal, extra_weight)`, `vault_index._connect`, `vault_index.QTYPE_TO_CONTEXT`, `vault_index.ensure_index` (Task 1).
- Produces:
  - `evaluate(records, order_fn) -> dict` — `order_fn(record) -> list[int]` (indices into `record["candidates"]`); returns `{"ndcg": [float|None], "recall": [float|None]}`
  - `blend_order(record, probs, weight, db_path, task_context="search") -> list[int]`
  - `gate(cur, cand, latencies) -> dict` — `{"ndcg": (mean, lo, hi), "recall": (mean, lo, hi), "p95", "pass", "reasons"}`
  - `choose(results: dict) -> str` — winner key such as `"laya:blend-0.35"`, or `"none"`
  - `count_access_rows(db_path) -> int`, `check_seed_status(allow_unchecked) -> bool`
  - CLI: `harness.py [--backends current,laya,jev,haiku] [--allow-unchecked]`; exit 0 ok, 2 seed not checked, 3 `access_log` changed

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for scripts/rerank_eval/harness.py (#377)."""
from __future__ import annotations

import json
import os
import sys

_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import pytest

from rerank_eval import harness


def _rec(labels, source_in_pool=None):
    return {"qid": "q", "question": "q", "source_in_pool": source_in_pool, "qtype": "decision",
            "candidates": [{"path": f"/n{i}.md", "label": l, "rank": -1.0 - i, "rerank_score": 0.0,
                            "type": "claude-insight", "date": "2026-01-01", "title": f"n{i}"}
                           for i, l in enumerate(labels)]}


def test_evaluate_identity_order():
    out = harness.evaluate([_rec([2, 0, 0])], lambda r: [0, 1, 2])
    assert out["ndcg"] == [pytest.approx(1.0)] and out["recall"] == [1.0]


def test_evaluate_reversed_order():
    out = harness.evaluate([_rec([2, 0, 0, 0, 0, 0])], lambda r: [5, 4, 3, 2, 1, 0])
    assert out["ndcg"] == [0.0] and out["recall"] == [0.0]


def test_evaluate_source_outside_pool_counts_in_recall():
    out = harness.evaluate([_rec([2, 0], source_in_pool=False)], lambda r: [0, 1])
    assert out["recall"] == [0.5]
    assert out["ndcg"][0] < 1.0  # the missing source raises the ideal DCG


def test_gate_pass_and_fail_reasons():
    cur = {"ndcg": [0.5] * 40, "recall": [0.5] * 40}
    better = {"ndcg": [0.6] * 40, "recall": [0.5] * 40}
    g = harness.gate(cur, better, [0.5] * 40)
    assert g["pass"], g["reasons"]
    slow = harness.gate(cur, better, [2.0] * 40)
    assert not slow["pass"] and any("p95" in r for r in slow["reasons"])
    small = harness.gate(cur, {"ndcg": [0.53] * 40, "recall": [0.5] * 40}, [0.5] * 40)
    assert not small["pass"] and any("nDCG" in r for r in small["reasons"])
    worse = harness.gate(cur, {"ndcg": [0.7] * 40, "recall": [0.4] * 40}, [0.5] * 40)
    assert not worse["pass"] and any("recall" in r for r in worse["reasons"])


def test_gate_boundaries():
    cur = {"ndcg": [0.5] * 40, "recall": [0.5] * 40}
    exact = harness.gate(cur, {"ndcg": [0.55] * 40, "recall": [0.48] * 40}, [1.5] * 40)
    assert exact["pass"], exact["reasons"]  # +0.05, -0.02 and 1.5 s are all allowed


def test_gate_skips_none_pairs():
    cur = {"ndcg": [None, 0.5] * 20, "recall": [None, 0.5] * 20}
    new = {"ndcg": [None, 0.6] * 20, "recall": [None, 0.5] * 20}
    assert harness.gate(cur, new, [0.1] * 40)["pass"]


def test_choose_prefers_laya_and_never_haiku():
    res = {"haiku:replace": {"pass": True, "ndcg": (0.3, 0.2, 0.4)},
           "jev:blend-0.35": {"pass": True, "ndcg": (0.2, 0.1, 0.3)},
           "laya:replace": {"pass": True, "ndcg": (0.06, 0.01, 0.1)}}
    assert harness.choose(res) == "laya:replace"
    assert harness.choose({"haiku:replace": {"pass": True, "ndcg": (0.3, 0.2, 0.4)}}) == "none"
    assert harness.choose({"jev:replace": {"pass": False}}) == "none"


def test_choose_best_mode_within_backend():
    res = {"jev:replace": {"pass": True, "ndcg": (0.06, 0.01, 0.1)},
           "jev:blend-0.5": {"pass": True, "ndcg": (0.09, 0.02, 0.2)}}
    assert harness.choose(res) == "jev:blend-0.5"


def test_blend_weight_zero_vs_heavy(monkeypatch):
    rec = _rec([0, 2, 1])
    monkeypatch.setattr(harness, "_rows_for", lambda db, r: [
        dict(c, body="foo", tags="", importance=5) for c in r["candidates"]])
    zero = harness.blend_order(rec, [0.0, 1.0, 0.5], 0.0, None)
    heavy = harness.blend_order(rec, [0.0, 1.0, 0.5], 0.9, None)
    assert sorted(zero) == [0, 1, 2]
    assert heavy[0] == 1


def test_check_seed_status(tmp_path, monkeypatch):
    monkeypatch.setattr(harness.common, "SEED_STATUS_PATH", tmp_path / "missing.json")
    assert harness.check_seed_status(allow_unchecked=False) is False
    assert harness.check_seed_status(allow_unchecked=True) is True
    (tmp_path / "ok.json").write_text(json.dumps({"status": "PASS"}))
    monkeypatch.setattr(harness.common, "SEED_STATUS_PATH", tmp_path / "ok.json")
    assert harness.check_seed_status(allow_unchecked=False) is True
```

- [ ] **Step 2: Run them to see them fail**

Run: `pytest tests/test_rerank_eval_harness.py -v`
Expected: FAIL — `ImportError: cannot import name 'harness'`.

- [ ] **Step 3: Implement `scripts/rerank_eval/harness.py`**

```python
"""#377 rerank eval harness: score backends on the seed set and apply the go gate.

Usage: python3 scripts/rerank_eval/harness.py [--backends current,laya,jev,haiku] [--allow-unchecked]
Writes a JSON report to ~/.claude/obsidian-brain-rerank-reports/ and prints a table.
Exit codes: 0 ok, 2 seed set not spot-checked, 3 access_log changed during the run.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rerank_eval import backends as be  # noqa: E402
from rerank_eval import common, metrics  # noqa: E402

import vault_index  # noqa: E402

BLEND_WEIGHTS = (0.2, 0.35, 0.5)
PREFERENCE = ("laya", "jev")  # haiku is a reference only; current is the baseline
_EPS = 1e-9  # float slack for the exact-threshold gate checks


def _gains(record):
    g = [c["label"] for c in record["candidates"]]
    return g + ([2] if record.get("source_in_pool") is False else [])


def evaluate(records, order_fn) -> dict:
    ndcg, recall = [], []
    for r in records:
        order = order_fn(r)
        labels = [r["candidates"][i]["label"] for i in order]
        all_g = _gains(r)
        ndcg.append(metrics.ndcg_at_k(labels, all_g))
        recall.append(metrics.recall_at_k([l == 2 for l in labels], sum(1 for g in all_g if g == 2)))
    return {"ndcg": ndcg, "recall": recall}


def _pairs(a, b):
    kept = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    return [x for x, _ in kept], [y for _, y in kept]


def gate(cur: dict, cand: dict, latencies: list) -> dict:
    reasons = []
    n_a, n_b = _pairs(cand["ndcg"], cur["ndcg"])
    r_a, r_b = _pairs(cand["recall"], cur["recall"])
    nd = metrics.paired_bootstrap(n_a, n_b)
    rc = metrics.paired_bootstrap(r_a, r_b)
    p = metrics.p95(latencies) if latencies else float("inf")
    if not (nd[0] >= 0.05 - _EPS and nd[1] > 0):
        reasons.append(f"nDCG@5 gain {nd[0]:+.3f} (CI {nd[1]:+.3f}..{nd[2]:+.3f}) needs >= +0.05 and lower > 0")
    if rc[1] < -0.02 - _EPS:
        reasons.append(f"recall@5 CI lower bound {rc[1]:+.3f} < -0.02")
    if p > 1.5 + _EPS:
        reasons.append(f"p95 latency {p:.2f}s > 1.5s")
    return {"ndcg": nd, "recall": rc, "p95": p, "pass": not reasons, "reasons": reasons}


def choose(results: dict) -> str:
    for name in PREFERENCE:
        passing = [(k, v) for k, v in results.items() if k.split(":")[0] == name and v.get("pass")]
        if passing:
            return max(passing, key=lambda kv: kv[1]["ndcg"][0])[0]
    return "none"


def _rows_for(db_path, record):
    conn = vault_index._connect(db_path)
    try:
        out = []
        for c in record["candidates"]:
            row = conn.execute("SELECT path, type, date, title, tags, body, importance FROM notes "
                               "WHERE path = ?", (c["path"],)).fetchone()
            d = dict(row) if row else {"path": c["path"], "body": "", "tags": "", "importance": 5}
            d["rank"] = c.get("rank") if c.get("rank") is not None else 0.0
            out.append(d)
        return out
    finally:
        conn.close()


def blend_order(record, probs, weight, db_path, task_context="search"):
    rows = _rows_for(db_path, record)
    extra = {c["path"]: float(p) for c, p in zip(record["candidates"], probs)}
    ranked = vault_index.rerank_results(rows, common.question_terms(record["question"]),
                                        limit=len(rows), db_path=db_path, task_context=task_context,
                                        extra_signal=extra, extra_weight=weight)
    index = {c["path"]: i for i, c in enumerate(record["candidates"])}
    return [index[r["path"]] for r in ranked]


def count_access_rows(db_path: str) -> int:
    conn = vault_index._connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
    finally:
        conn.close()


def check_seed_status(allow_unchecked: bool) -> bool:
    try:
        status = json.loads(common.SEED_STATUS_PATH.read_text()).get("status")
    except (OSError, ValueError):
        status = None
    if status == "PASS":
        return True
    print(f"seed spot-check status: {status or 'missing'} (run seed.py spot-check)", file=sys.stderr)
    return allow_unchecked


def _qtype_test(records, backend_objs, db_path) -> dict:
    out = {}
    truth = [r["qtype"] for r in records]
    guessers = {"keyword": be.keyword_qtype}
    for b in backend_objs:
        if b.name in ("laya", "jev") and b.available()[0]:
            guessers[b.name] = b.classify

    def ordered(ctx_for):
        return evaluate(records, lambda r: blend_order(r, [0.0] * len(r["candidates"]), 0.0,
                                                       db_path, ctx_for(r)))

    base = ordered(lambda r: "search")
    for name, fn in guessers.items():
        try:
            guesses = {r["qid"]: fn(r["question"]) for r in records}
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": str(exc)}
            continue
        acc = sum(guesses[r["qid"]] == t for r, t in zip(records, truth)) / len(truth)
        typed = ordered(lambda r: vault_index.QTYPE_TO_CONTEXT.get(guesses[r["qid"]], "search"))
        a, b = _pairs(typed["ndcg"], base["ndcg"])
        out[name] = {"accuracy": round(acc, 3),
                     "ndcg_change": metrics.paired_bootstrap(a, b) if a else None}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backends", default="current,laya,jev,haiku")
    ap.add_argument("--allow-unchecked", action="store_true")
    args = ap.parse_args(argv)
    if not check_seed_status(args.allow_unchecked):
        return 2
    from obsidian_utils import load_config
    c = load_config()
    db = vault_index.ensure_index(c["vault_path"], [c.get("sessions_folder", "claude-sessions"),
                                                    c.get("insights_folder", "claude-insights")])
    records = [json.loads(l) for l in common.SEED_PATH.read_text().splitlines() if l.strip()]
    records = [r for r in records if r["candidates"]]
    before = count_access_rows(db)

    wanted = set(args.backends.split(","))
    objs = [b for b in be.all_backends() if b.name in wanted]
    cur = evaluate(records, lambda r: list(range(len(r["candidates"]))))
    results, skipped = {}, {}
    try:
        for b in objs:
            if b.name == "current":
                continue
            ok, why = b.available()
            if not ok:
                skipped[b.name] = why
                print(why, file=sys.stderr)
                continue
            probs, lat, failures = {}, [], 0
            for r in records:
                t = time.perf_counter()
                try:
                    probs[r["qid"]] = b.score(r["question"], r["candidates"])
                except Exception as exc:  # noqa: BLE001 — a failure counts against the backend
                    print(f"{b.name} {r['qid']}: {exc}", file=sys.stderr)
                    failures += 1
                    probs[r["qid"]] = [0.0] * len(r["candidates"])
                lat.append(time.perf_counter() - t)

            def replace_order(r, _p=probs):
                p = _p[r["qid"]]
                return sorted(range(len(p)), key=lambda i: (-p[i], i))

            results[f"{b.name}:replace"] = dict(gate(cur, evaluate(records, replace_order), lat),
                                                failures=failures)
            for w in BLEND_WEIGHTS:
                ev = evaluate(records, lambda r, _p=probs, _w=w: blend_order(r, _p[r["qid"]], _w, db))
                results[f"{b.name}:blend-{w}"] = dict(gate(cur, ev, lat), failures=failures)
        qtypes = _qtype_test(records, objs, db)
    finally:
        for b in objs:
            b.close()

    after = count_access_rows(db)
    report = {"ts": datetime.now().isoformat(timespec="seconds"), "questions": len(records),
              "results": results, "skipped": skipped, "qtype": qtypes,
              "winner": choose(results), "access_log_delta": after - before}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    common.write_private(common.REPORT_DIR / f"report-{stamp}.json", json.dumps(report, indent=2))
    print("| variant | nDCG@5 gain (CI) | recall@5 diff (CI) | p95 s | fails | pass |")
    print("|---|---|---|---|---|---|")
    for k, v in results.items():
        nd, rc = v["ndcg"], v["recall"]
        print(f"| {k} | {nd[0]:+.3f} ({nd[1]:+.3f}..{nd[2]:+.3f}) | {rc[0]:+.3f} ({rc[1]:+.3f}..{rc[2]:+.3f}) "
              f"| {v['p95']:.2f} | {v['failures']} | {'yes' if v['pass'] else 'no'} |")
    print(f"\nwinner: {report['winner']}  questions: {len(records)}  skipped: {skipped or 'none'}")
    print(f"question type: {json.dumps(qtypes)}")
    if after != before:
        print(f"ERROR: access_log changed by {after - before} rows during the run", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_rerank_eval_harness.py tests/test_py39_compat.py -v`
Expected: PASS.

- [ ] **Step 5: Add an end-to-end access-log test**

Append to `tests/test_rerank_eval_harness.py`:

```python
def test_full_run_leaves_access_log_unchanged(tmp_vault, tmp_path, monkeypatch):
    import vault_index
    from rerank_eval import backends, seed
    s = tmp_vault / "claude-sessions"
    for k in range(6):
        (s / f"2026-01-0{k + 1}-p-{k}.md").write_text(
            f"---\ntype: claude-session\ndate: 2026-01-0{k + 1}\nproject: p\ntitle: sqlite {k}\n"
            f"tags:\n  - claude/x\nstatus: summarized\n---\nsqlite index {k}\n")
    db = str(tmp_path / "t.db")
    vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db)
    monkeypatch.setattr(seed, "label_question", lambda q, pool: ([2] + [0] * (len(pool) - 1), "decision"))
    rec = seed.make_record("k0", "why sqlite index", "known_item", None, db)
    assert rec["candidates"], "fixture must produce a pool"
    seed_path = tmp_path / "seed.jsonl"
    seed_path.write_text(json.dumps(rec) + "\n")
    monkeypatch.setattr(harness.common, "SEED_PATH", seed_path)
    monkeypatch.setattr(harness.common, "REPORT_DIR", tmp_path / "reports")
    import obsidian_utils
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: {
        "vault_path": str(tmp_vault), "sessions_folder": "claude-sessions",
        "insights_folder": "claude-insights"})
    monkeypatch.setattr(vault_index, "ensure_index", lambda *a, **k: db)
    monkeypatch.setattr(backends.HaikuBackend, "score",
                        lambda self, q, c: [1.0 / (i + 1) for i in range(len(c))])
    assert harness.main(["--backends", "current,haiku", "--allow-unchecked"]) == 0
    assert harness.count_access_rows(db) == 0
    assert list((tmp_path / "reports").glob("report-*.json"))
```

Run: `pytest tests/test_rerank_eval_harness.py -v`. Expected: PASS.

Mutation check: temporarily change `log_access=False` to `True` in `seed.build_pool`. `test_build_pool_writes_no_access_rows` and `test_full_run_leaves_access_log_unchanged` must both fail. Restore.

- [ ] **Step 6: Commit**

```bash
git add scripts/rerank_eval/harness.py tests/test_rerank_eval_harness.py
./scripts/commit-preflight.sh
git commit -m "feat(obsidian-brain): rerank eval harness with go gate (#377)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: First real run, review, PR

- [ ] **Step 1: Small seed set first**

Run: `python3 scripts/rerank_eval/seed.py build --known 5 --limit 6`
Expected: `wrote 6 questions`. Check with a one-liner (counts only, no note text in chat): every record has candidates with labels, and each known item has `source_in_pool` set. Fix anything broken before the full run. This overwrites the seed file; the full build in Step 2 replaces it again.

- [ ] **Step 2: Full seed build**

Run: `python3 scripts/rerank_eval/seed.py build`
Expected: about 50 known-item questions plus the history count from Task 0. Note Opus failures from stderr.

- [ ] **Step 3: Human spot-check (the user)**

Ask the user to run `! python3 scripts/rerank_eval/seed.py spot-check`. If the status is `INCONCLUSIVE`, fix `_LABEL_PROMPT` in `seed.py`, rebuild, and re-check. Do not score backends until it is `PASS`.

- [ ] **Step 4: Run the harness**

Run: `python3 scripts/rerank_eval/harness.py` (prefix `OB_JEV=1` only if the user approves sending scrubbed snippets and Task 0 found a working key).
Expected: a table, a winner or `none`, exit 0. Exit 3 means `access_log` changed: stop and debug.

- [ ] **Step 5: Record the result on the issue**

Post the table, the question-type results and the winner on #377 as a comment (no note text). If the winner is `none`, say phase 2 is dropped, as with cc-token-router#143.

- [ ] **Step 6: One whole-branch review, then one Codex review**

One whole-branch review (`/code-review high`), fix findings, then one Codex review (`/adversarial-review`), fix confirmed findings. Re-run `./scripts/commit-preflight.sh` after each round.

- [ ] **Step 7: Open the PR**

```bash
git push -u origin feature/377-rerank-eval
gh pr create --base develop --title "feat(obsidian-brain): rerank eval phase 1 (#377)" --body-file "$SCRATCH/pr-body.md"
gh pr view --json baseRefName -q .baseRefName   # must print develop
```

The PR body lists the tasks, the eval result, and `Closes #377` and `Closes #376`. The base is `develop`, not the default branch, so close both issues by hand after merge and move the board card.

---

## Self-review notes

- **Spec coverage:** citation log → Tasks 2–4; seed set (history + known items, Opus labels and question type, spot-check) → Task 6; backends → Task 7; metrics and gate → Tasks 5 and 8; Friston replace/blend and `log_access` → Tasks 1 and 8; question-type test and #376 → Tasks 1 and 8; privacy (paths under `~/.claude`, `OB_JEV` opt-in, scrubbing) → Tasks 2, 5 and 7; error-handling table → Tasks 2 (never raises), 7 (skip with reason) and 8 (failures counted). The `current+surprise` variant is out of scope (spec, Friston item 3).
- **Spec wording reconciled:** the spec's Jev row said "one `Score` question per candidate". The plan uses a yes/no (`noul`) question, because it returns p(relevant) directly and laya uses the same type. The spec is updated in the same commit as this plan.
- **Scrubber:** the spec names the cc-token-router phase A scrubber. It lives in another repo, so Task 7 uses `scrub_secrets` (with three new patterns) plus the same identity literals (home path, login name).
- **Known limitation:** `question_terms` stands in for the terms the model picks in vault-ask Step 2, so seed pools can differ from what a live vault-ask would see. The citation log, which records the model's terms, is the trend check for that.
