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


# --- Task 2: source resolution and count -------------------------------------

import vault_index
from pathlib import Path

FOLDERS = ["claude-sessions", "claude-insights", "claude-wiki"]


def _note(vault, folder, name, ntype, extra="", body="zebracorn body", date="2026-10-01"):
    p = Path(vault) / folder / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: {ntype}\ndate: {date}\nproject: demo\n{extra}---\n# {name}\n\n{body}\n",
                 encoding="utf-8")
    return p


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "v"
    _note(v, "claude-insights", "i1", "claude-insight")
    _note(v, "claude-insights", "i2", "claude-insight")
    _note(v, "claude-insights", "d1", "claude-decision")
    _note(v, "claude-sessions", "s1", "claude-session")
    _note(v, "claude-sessions", "s1-snap", "claude-snapshot", extra='source_session_note: "[[s1]]"\n')
    _note(v, "claude-insights", "st1", "claude-standup")
    _note(v, "claude-sessions", "dup", "claude-session")
    _note(v, "claude-insights", "dup", "claude-insight")
    db = str(tmp_path / "i.db")
    vault_index.ensure_index(str(v), FOLDERS, db_path=db)
    roots = [str(v / f) for f in FOLDERS]
    return {"vault": str(v), "db": db, "roots": roots}


def _count(vault, names, mem=None):
    return wiki.count_sources(vault["db"], names, mem or [], vault["roots"])


def test_count_boundary_two_and_three(vault):
    assert _count(vault, ["i1", "i2"])["count"] == 2
    assert _count(vault, ["i1", "i2", "d1"])["count"] == 3


def test_snapshot_counts_as_its_parent(vault):
    assert _count(vault, ["s1", "s1-snap"])["count"] == 1
    r = _count(vault, ["s1-snap"])
    assert r["count"] == 1 and r["qualifying"] == ["s1"]


def test_name_forms_collapse(vault):
    r = _count(vault, ["[[i1]]", "i1.md", "i1", "[[i1|alias]]"])
    assert r["count"] == 1 and r["rejected"] == []


def test_non_counting_type_is_other(vault):
    r = _count(vault, ["st1", "i1"])
    assert r["count"] == 1 and r["other"] == ["st1"]


def test_unresolved_and_ambiguous_are_rejected(vault):
    r = _count(vault, ["nope", "dup"])
    reasons = {x["name"]: x["reason"] for x in r["rejected"]}
    assert reasons["nope"] == "unresolved"
    assert reasons["dup"].startswith("ambiguous:")
    assert "claude-sessions/dup.md" in reasons["dup"] and "claude-insights/dup.md" in reasons["dup"]


def test_memory_sources_refused_until_396(vault):
    r = _count(vault, ["i1"], mem=["x/y.md"])
    assert any("#396" in x["reason"] for x in r["rejected"])


def test_note_outside_roots_is_unresolved(vault, tmp_path):
    other = Path(vault["vault"]) / "elsewhere"
    _note(Path(vault["vault"]), "elsewhere", "far", "claude-insight")
    vault_index.ensure_index(vault["vault"], FOLDERS + ["elsewhere"], db_path=vault["db"])
    assert _count(vault, ["far"])["rejected"][0]["reason"] == "unresolved"
