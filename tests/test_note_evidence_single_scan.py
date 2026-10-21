"""#342: one sessions scan for note-completion evidence, no dead confidence floor."""

from __future__ import annotations

import os
import sys
from collections import Counter

import open_item_dedup as oid
from obsidian_utils import match_items_against_evidence


def _develop_second_pass(vault_path, sessions_folder, project, max_sessions):
    """Inline copy of origin/develop's collect_open_items(), so the oracle
    shares no code with the helper under test."""
    sessions_dir = os.path.join(vault_path, sessions_folder)
    all_files = sorted(os.listdir(sessions_dir), reverse=True)
    results = []
    matched = 0
    for fname in all_files:
        if not fname.endswith('.md'):
            continue
        fpath = os.path.join(sessions_dir, fname)
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except OSError:
            continue
        project_match = False
        is_session = True
        type_field_seen = False
        for line in lines[:20]:
            stripped = line.strip()
            if stripped.startswith('project:'):
                val = stripped.split(':', 1)[1].strip().strip('"').strip("'")
                if val == project:
                    project_match = True
            elif stripped.startswith('type:'):
                type_field_seen = True
                tval = stripped.split(':', 1)[1].strip().strip('"').strip("'")
                is_session = (tval == 'claude-session')
        if not project_match or (type_field_seen and not is_session):
            continue
        matched += 1
        in_section = False
        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped == '## Open Questions / Next Steps':
                in_section = True
                continue
            if in_section:
                if stripped.startswith('## '):
                    break
                if stripped.startswith('- [ ] '):
                    results.append((fpath, line_num, stripped[6:]))
        if matched >= max_sessions:
            break
    return results


def _old_two_pass(vault_path, sessions_folder, project, max_sessions=10):
    """The pre-#342 gather_note_completion_evidence(), kept as the oracle.

    It built the date map and evidence pool in one scan, then called
    collect_open_items() for a second scan of the same notes.
    """
    sessions_dir = os.path.join(vault_path, sessions_folder)
    if not os.path.isdir(sessions_dir):
        return []
    all_files = sorted(os.listdir(sessions_dir), reverse=True)
    evidence_pool = []
    date_by_path = {}
    matched = 0
    for fname in all_files:
        if not fname.endswith('.md'):
            continue
        fpath = os.path.join(sessions_dir, fname)
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except OSError:
            continue
        project_match = False
        is_session = True
        type_field_seen = False
        note_date = ''
        for line in lines[:20]:
            stripped = line.strip()
            if stripped.startswith('project:'):
                val = stripped.split(':', 1)[1].strip().strip('"').strip("'")
                if val == project:
                    project_match = True
            elif stripped.startswith('type:'):
                type_field_seen = True
                tval = stripped.split(':', 1)[1].strip().strip('"').strip("'")
                is_session = (tval == 'claude-session')
            elif stripped.startswith('date:'):
                note_date = stripped.split(':', 1)[1].strip().strip('"').strip("'")
        if not project_match or (type_field_seen and not is_session):
            continue
        matched += 1
        if note_date:
            date_by_path[os.path.abspath(fpath)] = note_date
            m = oid._SUMMARY_RE.search(''.join(lines))
            if m:
                summary_text = m.group(1).strip()
                if summary_text:
                    first_line = summary_text.split('\n')[0].strip()
                    title = first_line or f"Session: {project}"
                    evidence_pool.append((note_date, title, summary_text))
        if matched >= max_sessions:
            break

    items = _develop_second_pass(vault_path, sessions_folder, project, max_sessions)
    if not items:
        return []
    results = []
    for fpath, line_num, item_text in items:
        item_date = date_by_path.get(os.path.abspath(fpath), '')
        if not item_date:
            continue
        best = None
        for ev_date, ev_title, ev_summary in evidence_pool:
            if ev_date[:10] <= item_date[:10]:
                continue
            for c in match_items_against_evidence(ev_summary, [(fpath, line_num, item_text)]):
                if not c.get("has_completion_phrase"):
                    continue
                if best is None or c["confidence"] > best["confidence"]:
                    c["contradicted_by"] = ev_date[:10]
                    c["contradicted_by_title"] = ev_title
                    best = c
        if best is not None:
            results.append(best)
    return results


def _note(sessions, name, *, project="demo", date=None, type_="claude-session",
          summary="Worked on things.", items=(), extra=()):
    fm = ["---"]
    if type_ is not None:
        fm.append(f"type: {type_}")
    fm.append(f"project: {project}")
    if date is not None:
        fm.append(f"date: {date}")
    fm.append("---")
    body = "\n".join([f"- [ ] {i}" for i in items] + list(extra))
    (sessions / name).write_text(
        "\n".join(fm) + f"\n\n## Summary\n{summary}\n\n"
        f"## Open Questions / Next Steps\n{body}\n\n## Notes\n- [ ] not an item\n",
        encoding="utf-8",
    )


def _vault(tmp_path):
    sessions = tmp_path / "claude-sessions"
    sessions.mkdir()
    # Newest first by filename. max_sessions=4 below stops before the last two.
    _note(sessions, "2026-03-06-a.md", date="2026-03-06",
          summary="Fixed parser_crash.py and shipped cache_layer.py today.")
    _note(sessions, "2026-03-05-b.md", project="other", date="2026-03-05",
          summary="Fixed parser_crash.py.", items=["fix parser_crash.py"])
    _note(sessions, "2026-03-05-c.md", type_="claude-snapshot", date="2026-03-05",
          items=["fix parser_crash.py"])
    _note(sessions, "2026-03-04-d.md", date="2026-03-04",
          items=["fix parser_crash.py", "tidy docs_index.md"],
          extra=["- [x] done thing", "  - [ ] nested parser_crash.py"])
    _note(sessions, "2026-03-03-e.md", type_=None, date="2026-03-03",
          items=["add cache_layer.py"])
    _note(sessions, "2026-03-02-f.md", date=None, items=["fix parser_crash.py"])
    _note(sessions, "2026-03-01-g.md", date="2026-03-01",
          items=["fix parser_crash.py"])
    _note(sessions, "2026-02-28-h.md", date="2026-02-28",
          items=["add cache_layer.py"])
    (sessions / "notes.txt").write_text("project: demo\n", encoding="utf-8")
    return tmp_path


def test_gather_matches_two_pass_output(tmp_path):
    vault = _vault(tmp_path)
    for max_sessions in (1, 3, 4, 10):
        new = oid.gather_note_completion_evidence(str(vault), "claude-sessions", "demo", max_sessions)
        old = _old_two_pass(str(vault), "claude-sessions", "demo", max_sessions)
        assert new == old, max_sessions
    # Discriminating fixture: the full window flags items from dated, legacy
    # and older notes, and skips the undated one.
    full = oid.gather_note_completion_evidence(str(vault), "claude-sessions", "demo", 10)
    flagged = {(os.path.basename(r["file"]), r["text"]) for r in full}
    assert flagged == {
        ("2026-03-04-d.md", "fix parser_crash.py"),
        ("2026-03-04-d.md", "nested parser_crash.py"),
        ("2026-03-03-e.md", "add cache_layer.py"),
        ("2026-03-01-g.md", "fix parser_crash.py"),
        ("2026-02-28-h.md", "add cache_layer.py"),
    }
    # Exact (basename, line, text), with line numbers found independently in
    # the fixture files (1-based), not taken from the code under test.
    sessions_dir = vault / "claude-sessions"

    def _line_of(name, text):
        lines = (sessions_dir / name).read_text(encoding="utf-8").split("\n")
        hits = [i + 1 for i, ln in enumerate(lines) if ln.strip() == f"- [ ] {text}"]
        assert len(hits) == 1, (name, text, hits)
        return hits[0]

    assert {(os.path.basename(r["file"]), r["line"], r["text"]) for r in full} == {
        (n, _line_of(n, t), t) for n, t in flagged
    }
    # max_sessions=4 stops after 2026-03-01-g.md, so 2026-02-28-h.md drops out.
    window = oid.gather_note_completion_evidence(str(vault), "claude-sessions", "demo", 4)
    assert "2026-02-28-h.md" not in {os.path.basename(r["file"]) for r in window}
    assert window


def test_gather_reads_each_note_once(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    opened: Counter = Counter()
    real_open = open

    def counting_open(path, *args, **kwargs):
        opened[os.path.basename(str(path))] += 1
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(oid, "open", counting_open, raising=False)
    assert oid.gather_note_completion_evidence(str(vault), "claude-sessions", "demo", 10)
    assert opened, "the counter saw no reads; the patch did not take"
    assert max(opened.values()) == 1, opened


def test_match_items_never_returns_below_threshold():
    """Pins why /recall needs no `confidence < 3` floor (#342)."""
    item = [("f.md", 1, "alpha bravo charlie")]
    # 0, 1 and 2 shared plain tokens score 0, 1 and 2: never returned.
    for evidence in ("nothing shared here", "alpha only", "alpha bravo fixed"):
        assert match_items_against_evidence(evidence, item) == [], evidence
    # 3 shared tokens score 3: returned, at or above the floor.
    hits = match_items_against_evidence("alpha bravo charlie", item)
    assert len(hits) == 1 and hits[0]["confidence"] >= 3
