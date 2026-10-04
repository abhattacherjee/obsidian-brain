from __future__ import annotations
import json
import re

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


# --- Task 3: lookup and stale ------------------------------------------------

import datetime as dt
import os
import stat as _stat


def _page(vault, name, question, sources, fps, updated="2026-10-02", extra=None):
    meta = {"type": "claude-wiki", "title": question, "question": question, "date": updated,
            "created": updated, "updated": updated, "projects": ["demo"],
            "sources": [f"[[{s}]]" for s in sources], "memory_sources": [],
            "sources_fingerprint": fps, "confidence": "high", "filed_by": "user",
            "tags": ["claude/wiki", "claude/wiki/confidence-high", "claude/project/demo"]}
    meta.update(extra or {})
    p = Path(vault["vault"]) / "claude-wiki" / "queries" / "2026" / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(wiki.render_page(meta, "Answer body.\n"), encoding="utf-8")
    vault_index.ensure_index(vault["vault"], FOLDERS, db_path=vault["db"])
    return p


def _src(vault, name):
    return Path(vault["vault"]) / "claude-insights" / f"{name}.md"


@pytest.fixture
def paged(vault):
    fps = {n: wiki.fingerprint(_src(vault, n)) for n in ("i1", "i2", "d1")}
    page = _page(vault, "10-02-zebracorn-ranking", "How does zebracorn ranking work?", ["i1", "i2", "d1"], fps)
    return vault, page


def test_lookup_finds_page_by_paraphrase(paged):
    vault, page = paged
    hits = wiki.lookup(vault["db"], "zebracorn ranking explained")
    assert hits and hits[0]["path"] == str(page)
    assert hits[0]["question"] == "How does zebracorn ranking work?"
    assert hits[0]["updated"] == "2026-10-02" and len(hits) <= 3


def test_lookup_stopwords_only_is_empty(paged):
    vault, _ = paged
    assert wiki.lookup(vault["db"], "the of and") == []
    assert wiki.lookup(vault["db"], "   ") == []


def test_fresh_page_is_not_stale(paged):
    vault, page = paged
    assert wiki.stale(vault["db"], page, vault["roots"]) == {"stale": False, "reasons": []}


def test_mtime_only_touch_is_not_stale(paged):
    vault, page = paged
    later = dt.datetime.now().timestamp() + 100
    os.utime(_src(vault, "i1"), (later, later))
    assert wiki.stale(vault["db"], page, vault["roots"])["stale"] is False


def test_content_change_is_stale(paged):
    vault, page = paged
    _src(vault, "i2").write_text(_src(vault, "i2").read_text() + "\nedited\n")
    r = wiki.stale(vault["db"], page, vault["roots"])
    assert r["stale"] and "changed: i2" in r["reasons"]


def test_deleted_source_is_missing(paged):
    vault, page = paged
    _src(vault, "d1").unlink()
    vault_index.ensure_index(vault["vault"], FOLDERS, db_path=vault["db"])
    assert "missing: d1" in wiki.stale(vault["db"], page, vault["roots"])["reasons"]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads anything")
def test_unreadable_source_is_missing(paged):
    vault, page = paged
    p = _src(vault, "i1")
    p.chmod(0)
    try:
        assert "missing: i1" in wiki.stale(vault["db"], page, vault["roots"])["reasons"]
    finally:
        p.chmod(_stat.S_IRUSR | _stat.S_IWUSR)


def test_newer_note_marks_stale_unless_already_a_source(paged):
    vault, page = paged
    _note(Path(vault["vault"]), "claude-insights", "i9", "claude-insight",
          body="zebracorn ranking work details", date="2026-10-03")
    vault_index.ensure_index(vault["vault"], FOLDERS, db_path=vault["db"])
    r = wiki.stale(vault["db"], page, vault["roots"])
    assert "newer: i9" in r["reasons"]
    assert not any(x.startswith("newer: i1") for x in r["reasons"])


def test_all_reasons_are_reported(paged):
    vault, page = paged
    _src(vault, "i2").write_text("changed\n" + _src(vault, "i2").read_text())
    _src(vault, "d1").unlink()
    vault_index.ensure_index(vault["vault"], FOLDERS, db_path=vault["db"])
    r = wiki.stale(vault["db"], page, vault["roots"])
    assert "changed: i2" in r["reasons"] and "missing: d1" in r["reasons"]


# --- Task 4: file ------------------------------------------------------------

D = dt.date(2026, 10, 4)


@pytest.fixture
def ctx(vault):
    return {"vault": vault["vault"], "wiki_folder": "claude-wiki", "folders": FOLDERS, "db": vault["db"]}


def _payload(**kw):
    p = {"question": "How does zebracorn ranking work?", "body": "Ranking uses bm25.\n\n### Sources\n- [[i1]]\n",
         "sources": ["i1", "i2", "d1"], "memory_sources": [], "topics": ["ranking"],
         "confidence": "high", "filed_by": "user"}
    p.update(kw)
    return p


def _wiki(ctx):
    return Path(ctx["vault"]) / "claude-wiki"


def _all_files(ctx):
    w = _wiki(ctx)
    return sorted(str(p.relative_to(w)) for p in w.rglob("*") if p.is_file()) if w.exists() else []


def test_file_happy_path(ctx):
    out = wiki.file_page(ctx, _payload(), D)
    page = Path(out["path"])
    assert out["action"] == "file" and out["count"] == 3
    assert page == _wiki(ctx) / "queries" / "2026" / "10-04-how-does-zebracorn-ranking-work.md"
    assert _stat.S_IMODE(page.stat().st_mode) == 0o600
    meta, body = wiki.read_page(page)
    assert meta["type"] == "claude-wiki" and meta["question"] == "How does zebracorn ranking work?"
    assert meta["sources"] == ["[[i1]]", "[[i2]]", "[[d1]]"] and meta["projects"] == ["demo"]
    assert set(meta["sources_fingerprint"]) == {"i1", "i2", "d1"}
    assert "claude/wiki/confidence-high" in meta["tags"] and "claude/topic/ranking" in meta["tags"]
    assert meta["date"] == meta["updated"] == meta["created"] == "2026-10-04"
    idx = (_wiki(ctx) / "index.md").read_text()
    assert "type: \"claude-wiki-index\"" in idx and "[[10-04-how-does-zebracorn-ranking-work]]" in idx
    assert "(updated 2026-10-04, high)" in idx
    log = (_wiki(ctx) / "log-2026.md").read_text()
    assert "## [2026-10-04] file | - | How does zebracorn ranking work?" in log
    conn = vault_index._connect(ctx["db"])
    try:
        rows = {Path(r["path"]).name: r["type"] for r in conn.execute("SELECT path, type FROM notes")}
    finally:
        conn.close()
    assert rows[page.name] == "claude-wiki" and "index.md" not in rows and "log-2026.md" not in rows


def test_file_golden_page(ctx):
    out = wiki.file_page(ctx, _payload(topics=[]), D)
    text = Path(out["path"]).read_text()
    fp = {n: wiki.fingerprint(Path(ctx["vault"]) / "claude-insights" / f"{n}.md") for n in ("i1", "i2", "d1")}
    expected = (
        "---\n"
        'type: "claude-wiki"\n'
        'title: "How does zebracorn ranking work?"\n'
        'question: "How does zebracorn ranking work?"\n'
        'date: "2026-10-04"\n'
        'created: "2026-10-04"\n'
        'updated: "2026-10-04"\n'
        'projects: ["demo"]\n'
        'sources: ["[[i1]]", "[[i2]]", "[[d1]]"]\n'
        "memory_sources: []\n"
        f"sources_fingerprint: {json.dumps(fp)}\n"
        'confidence: "high"\n'
        'filed_by: "user"\n'
        "tags:\n  - claude/wiki\n  - claude/wiki/confidence-high\n  - claude/project/demo\n"
        "---\n"
        "Ranking uses bm25.\n\n### Sources\n- [[i1]]\n"
    )
    assert text == expected


@pytest.mark.parametrize("kw,msg", [
    ({"sources": ["i1", "i2"]}, "only 2 qualifying sources"),
    ({"sources": ["i1", "i2", "d1", "nope"]}, "nope"),
    ({"confidence": "certain"}, "confidence"),
    ({"filed_by": "auto"}, "caller"),
    ({"filed_by": "auto", "caller": "Bad Caller"}, "caller"),
    ({"filed_by": "user", "caller": "x"}, "caller"),
    ({"topics": ["Not OK"]}, "topic"),
    ({"body": "  "}, "body"),
    ({"question": "q" * 501}, "question"),
    ({"question": ""}, "question"),
    ({"memory_sources": ["a/b.md"]}, "#396"),
    ({"filed_by": "robot"}, "filed_by"),
])
def test_file_refusals_write_nothing(ctx, kw, msg):
    with pytest.raises(wiki.WikiRefusal, match=re.escape(msg) if msg != "nope" else "nope"):
        wiki.file_page(ctx, _payload(**kw), D)
    assert _all_files(ctx) == []


def test_update_outside_queries_or_non_wiki_refused(ctx):
    # A wiki-typed page outside queries/: only the containment check refuses
    # it. (The tmp path itself contains "queries", so match the full message.)
    outside = Path(ctx["vault"]) / "claude-insights" / "fake-wiki.md"
    outside.write_text(wiki.render_page({"type": "claude-wiki", "question": "q"}, "x\n"))
    before = outside.read_text()
    with pytest.raises(wiki.WikiRefusal, match="update must name a page under"):
        wiki.file_page(ctx, _payload(update=str(outside)), D)
    assert outside.read_text() == before
    bogus = _wiki(ctx) / "queries" / "2026" / "x.md"
    bogus.parent.mkdir(parents=True)
    bogus.write_text("---\ntype: claude-insight\n---\nx\n")
    with pytest.raises(wiki.WikiRefusal, match="not a wiki page"):
        wiki.file_page(ctx, _payload(update=str(bogus)), D)


def test_reviewed_page_needs_override(ctx):
    first = Path(wiki.file_page(ctx, _payload(), D)["path"])
    first.write_text(first.read_text().replace('filed_by: "user"\n', 'filed_by: "user"\nreviewed: yes\n'))
    before = first.read_text()
    with pytest.raises(wiki.WikiRefusal, match="is marked reviewed; refusing"):
        wiki.file_page(ctx, _payload(update=str(first)), D + dt.timedelta(days=1))
    assert first.read_text() == before
    out = wiki.file_page(ctx, _payload(update=str(first), override_reviewed=True), D + dt.timedelta(days=1))
    meta, _ = wiki.read_page(out["path"])
    assert "reviewed" not in meta and out["action"] == "update"


def test_update_keeps_created_and_name(ctx):
    first = Path(wiki.file_page(ctx, _payload(), D)["path"])
    later = D + dt.timedelta(days=3)
    out = wiki.file_page(ctx, _payload(update=str(first), body="New answer.\n"), later)
    assert Path(out["path"]) == first and out["action"] == "update"
    meta, body = wiki.read_page(first)
    assert meta["created"] == "2026-10-04" and meta["updated"] == meta["date"] == "2026-10-07"
    assert body.strip() == "New answer."
    assert "## [2026-10-07] update | - |" in (_wiki(ctx) / "log-2026.md").read_text()


def test_auto_filing_logs_caller(ctx):
    out = wiki.file_page(ctx, _payload(filed_by="auto", caller="ship.research"), D)
    meta, _ = wiki.read_page(out["path"])
    assert out["action"] == "file-auto" and meta["caller"] == "ship.research"
    assert "file-auto | ship.research |" in (_wiki(ctx) / "log-2026.md").read_text()


def test_slug_collision_gets_suffix(ctx):
    a = wiki.file_page(ctx, _payload(question="Zebracorn ranking?"), D)["path"]
    b = wiki.file_page(ctx, _payload(question="Zebracorn: ranking!"), D)["path"]
    assert a.endswith("10-04-zebracorn-ranking.md") and b.endswith("10-04-zebracorn-ranking-2.md")


def test_secrets_scrubbed_from_body_and_question(ctx):
    tok = "ghp_" + "A" * 36
    out = wiki.file_page(ctx, _payload(question=f"Why {tok} zebracorn?", body=f"key {tok}\n"), D)
    text = Path(out["path"]).read_text()
    assert tok not in text and tok not in (_wiki(ctx) / "log-2026.md").read_text()
    assert tok not in (_wiki(ctx) / "index.md").read_text()


def test_held_lock_refuses_and_writes_nothing(ctx):
    _wiki(ctx).mkdir(parents=True)
    lock = _wiki(ctx) / "..wiki.ob-lock"
    lock.write_text("held")
    with pytest.raises(wiki.WikiRefusal, match="another process is updating"):
        wiki.file_page(ctx, _payload(), D)
    assert _all_files(ctx) == ["..wiki.ob-lock"]


def test_stale_refresh_below_threshold_leaves_page(ctx):
    first = Path(wiki.file_page(ctx, _payload(), D)["path"])
    before = first.read_bytes()
    (Path(ctx["vault"]) / "claude-insights" / "d1.md").unlink()
    (Path(ctx["vault"]) / "claude-insights" / "i2.md").unlink()
    with pytest.raises(wiki.WikiRefusal):
        wiki.file_page(ctx, _payload(update=str(first)), D + dt.timedelta(days=1))
    assert first.read_bytes() == before


def test_index_splits_by_project_and_merges_back(ctx, monkeypatch):
    v = Path(ctx["vault"])
    for n in ("p1", "p2", "p3"):
        p = v / "claude-insights" / f"{n}.md"
        p.write_text(f"---\ntype: claude-insight\ndate: 2026-10-01\nproject: other\n---\n# {n}\nzebracorn\n")
    monkeypatch.setattr(wiki, "INDEX_SPLIT", 2)
    wiki.file_page(ctx, _payload(question="alpha zebracorn?"), D)
    wiki.file_page(ctx, _payload(question="beta zebracorn?"), D)
    wiki.file_page(ctx, _payload(question="gamma zebracorn?", sources=["p1", "p2", "p3"]), D)
    idx = (_wiki(ctx) / "index.md").read_text()
    assert "[[index-demo]]" in idx and "[[index-other]]" in idx
    assert (_wiki(ctx) / "index-demo.md").exists() and (_wiki(ctx) / "index-other.md").exists()
    monkeypatch.setattr(wiki, "INDEX_SPLIT", 500)
    wiki.file_page(ctx, _payload(question="delta zebracorn?"), D)
    assert not list(_wiki(ctx).glob("index-*.md"))
    assert "[[10-04-delta-zebracorn]]" in (_wiki(ctx) / "index.md").read_text()


def test_log_rolls_over_at_year_boundary(ctx):
    wiki.file_page(ctx, _payload(question="old year zebracorn?"), dt.date(2026, 12, 31))
    wiki.file_page(ctx, _payload(question="new year zebracorn?"), dt.date(2027, 1, 1))
    for y in ("2026", "2027"):
        text = (_wiki(ctx) / f"log-{y}.md").read_text()
        assert text.startswith("---\ntype: \"claude-wiki-index\"\n---\n")
    assert "new year" not in (_wiki(ctx) / "log-2026.md").read_text()


def test_hostile_question_keeps_one_frontmatter_block(ctx):
    q = 'He said "hi"\n---\n[[x]] zebracorn'
    out = wiki.file_page(ctx, _payload(question=q), D)
    text = Path(out["path"]).read_text()
    assert text.split("\n---\n", 1)[0].count("\n---") == 0
    stats = vault_index.rebuild_index(ctx["vault"], FOLDERS, db_path=ctx["db"], full=True)
    assert stats["malformed"] == 0
    meta, _ = wiki.read_page(out["path"])
    assert meta["question"] == " ".join(q.split())


def test_secret_never_reaches_the_filename(ctx):
    # The slug is built from the scrubbed question (security review on c919292).
    tok = "ghp_" + "a1b2" * 9
    out = wiki.file_page(ctx, _payload(question=f"Why {tok} zebracorn?"), D)
    name = Path(out["path"]).name
    assert wiki.slugify(tok) not in name and "a1b2a1b2" not in name
    for f in ("index.md", "log-2026.md"):
        assert "a1b2a1b2" not in (_wiki(ctx) / f).read_text()


def test_type_constants_match_the_indexer():
    assert wiki.INDEX_TYPE in vault_index._UNINDEXED_TYPES
    assert wiki.PAGE_TYPE in vault_index._TYPE_SCORES_BY_CONTEXT["general"]


# --- review fix wave (#397) --------------------------------------------------


@pytest.mark.parametrize("fp_line", [
    None, "sources_fingerprint: {}", 'sources_fingerprint: "oops"', "sources_fingerprint: [1, 2]",
    "sources_fingerprint:\n  i1: abc",
])
def test_malformed_fingerprint_is_unverifiable_not_fresh(paged, fp_line):
    vault, page = paged
    lines = [l for l in page.read_text().split("\n") if not l.startswith("sources_fingerprint:")]
    if fp_line:
        lines.insert(3, fp_line)
    page.write_text("\n".join(lines))
    r = wiki.stale(vault["db"], page, vault["roots"])
    assert r["stale"] is True
    assert any(x.startswith("unverifiable:") for x in r["reasons"])


def test_fingerprint_missing_one_cited_source_is_unverifiable(paged):
    vault, page = paged
    meta, body = wiki.read_page(page)
    meta["sources_fingerprint"].pop("d1")
    page.write_text(wiki.render_page(meta, body))
    r = wiki.stale(vault["db"], page, vault["roots"])
    assert "unverifiable: d1" in r["reasons"]


def test_same_day_note_is_not_newer(paged):
    vault, page = paged
    _note(Path(vault["vault"]), "claude-insights", "i8", "claude-insight",
          body="zebracorn ranking work details", date="2026-10-02")
    vault_index.ensure_index(vault["vault"], FOLDERS, db_path=vault["db"])
    assert not any(x == "newer: i8" for x in wiki.stale(vault["db"], page, vault["roots"])["reasons"])


def test_later_note_already_cited_is_not_newer(vault):
    _note(Path(vault["vault"]), "claude-insights", "i7", "claude-insight",
          body="zebracorn ranking work details", date="2026-10-05")
    vault_index.ensure_index(vault["vault"], FOLDERS, db_path=vault["db"])
    fps = {n: wiki.fingerprint(_src(vault, n)) for n in ("i1", "i2", "i7")}
    page = _page(vault, "10-02-zebracorn-ranking", "How does zebracorn ranking work?", ["i1", "i2", "i7"], fps)
    assert not any(x.startswith("newer: i7") for x in wiki.stale(vault["db"], page, vault["roots"])["reasons"])


@pytest.mark.parametrize("bad", ["yes", 1, "false", "true"])
def test_override_reviewed_must_be_literal_true(ctx, bad):
    first = Path(wiki.file_page(ctx, _payload(), D)["path"])
    first.write_text(first.read_text().replace('filed_by: "user"\n', 'filed_by: "user"\nreviewed: true\n'))
    with pytest.raises(wiki.WikiRefusal, match="is marked reviewed; refusing"):
        wiki.file_page(ctx, _payload(update=str(first), override_reviewed=bad), D)


def test_refresh_below_threshold_with_resolving_sources_names_the_count(ctx):
    v = Path(ctx["vault"])
    first = Path(wiki.file_page(ctx, _payload(), D)["path"])
    for n in ("i2", "d1"):
        p = v / "claude-insights" / f"{n}.md"
        p.write_text(p.read_text().replace("type: claude-insight", "type: claude-standup")
                     .replace("type: claude-decision", "type: claude-standup"))
    with pytest.raises(wiki.WikiRefusal, match=r"only 1 qualifying sources \(need 3\)"):
        wiki.file_page(ctx, _payload(update=str(first)), D + dt.timedelta(days=1))


@pytest.mark.parametrize("kw", [
    {"topics": ["ranking\n"]}, {"filed_by": "auto", "caller": "ship\n"},
    {"topics": [f"t{i}" for i in range(9)]}, {"update": 5},
])
def test_more_validation_refusals(ctx, kw):
    with pytest.raises(wiki.WikiRefusal):
        wiki.file_page(ctx, _payload(**kw), D)
    assert _all_files(ctx) == []


def test_question_of_exactly_500_chars_is_accepted(ctx):
    q = ("zebracorn " * 60)[:500]
    assert len(q.strip()) <= 500
    wiki.file_page(ctx, _payload(question=q.strip()), D)


def test_update_target_missing_is_a_refusal(ctx):
    missing = _wiki(ctx) / "queries" / "2026" / "gone.md"
    with pytest.raises(wiki.WikiRefusal, match="update page not found"):
        wiki.file_page(ctx, _payload(update=str(missing)), D)


def test_index_at_exactly_the_split_stays_single(ctx, monkeypatch):
    monkeypatch.setattr(wiki, "INDEX_SPLIT", 2)
    wiki.file_page(ctx, _payload(question="alpha zebracorn?"), D)
    wiki.file_page(ctx, _payload(question="beta zebracorn?"), D)
    assert not list(_wiki(ctx).glob("index-*.md"))


def test_user_index_named_file_is_not_deleted(ctx):
    _wiki(ctx).mkdir(parents=True)
    mine = _wiki(ctx) / "index-ideas.md"
    mine.write_text("---\ntype: claude-insight\n---\nmy ideas\n")
    wiki.file_page(ctx, _payload(), D)
    assert mine.exists()


def test_wiki_note_outside_queries_is_not_indexed(ctx):
    stray = _wiki(ctx) / "drafts" / "x.md"
    stray.parent.mkdir(parents=True)
    stray.write_text(wiki.render_page({"type": "claude-wiki", "title": "stray zebracorn", "date": "2026-10-01"}, "x\n"))
    wiki.file_page(ctx, _payload(), D)
    assert "[[x]]" not in (_wiki(ctx) / "index.md").read_text()


@pytest.mark.parametrize("stage", ["index", "log"])
def test_post_write_failure_returns_warning_and_releases_lock(ctx, monkeypatch, stage):
    target = "rebuild_wiki_index" if stage == "index" else "append_log"
    real = getattr(wiki, target)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(wiki, target, boom)
    out = wiki.file_page(ctx, _payload(), D)
    assert Path(out["path"]).is_file()
    assert out["warning"].startswith(f"page saved, but the {stage} update failed: disk full")
    monkeypatch.setattr(wiki, target, real)
    wiki.file_page(ctx, _payload(question="second zebracorn?"), D)  # lock was released


def test_corrupt_log_does_not_block_filing(ctx):
    _wiki(ctx).mkdir(parents=True)
    (_wiki(ctx) / "log-2026.md").write_bytes(b"---\ntype: \"claude-wiki-index\"\n---\n\xff\xfe bad\n")
    out = wiki.file_page(ctx, _payload(), D)
    assert "warning" not in out
    assert "## [2026-10-04] file" in (_wiki(ctx) / "log-2026.md").read_text(encoding="utf-8")


def test_index_and_lookup_show_unescaped_question(ctx):
    q = 'He said "hi" about zebracorn'
    wiki.file_page(ctx, _payload(question=q), D)
    assert q in (_wiki(ctx) / "index.md").read_text()
    assert wiki.lookup(ctx["db"], "zebracorn hi")[0]["question"] == q


def test_is_reviewed_strips_single_quotes():
    assert wiki.is_reviewed("'no'") is False


@pytest.mark.parametrize("kind", ["symlink-out", "dot"])
def test_wiki_root_outside_or_at_vault_is_a_refusal(ctx, tmp_path_factory, kind):
    if kind == "symlink-out":
        outside = tmp_path_factory.mktemp("elsewhere")
        (Path(ctx["vault"]) / "claude-wiki").symlink_to(outside)
    else:
        ctx = {**ctx, "wiki_folder": "."}
    with pytest.raises(wiki.WikiRefusal):
        wiki.file_page(ctx, _payload(), D)
