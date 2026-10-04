"""Tests for the wiki-pages vault-doctor check (#396).

Each reason has a positive and a negative fixture. Pages are filed through
the real ``wiki.file_page`` into a tmp vault, with HOME and the index DB
pointed at tmp paths, so nothing touches the live vault or DB.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import vault_doctor_checks  # noqa: E402
import vault_doctor_checks.wiki_pages as wp  # noqa: E402
import vault_index  # noqa: E402
import wiki  # noqa: E402

FOLDERS = ["claude-sessions", "claude-insights", "claude-wiki"]
D = dt.date(2026, 10, 2)


def _note(vault: Path, folder: str, name: str, ntype: str, body: str = "zebracorn body",
          date: str = "2026-10-01") -> Path:
    p = vault / folder / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: {ntype}\ndate: {date}\nproject: demo\n---\n# {name}\n\n{body}\n",
                 encoding="utf-8")
    return p


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    vault = tmp_path / "v"
    for n, t in (("i1", "claude-insight"), ("i2", "claude-insight"), ("d1", "claude-decision")):
        _note(vault, "claude-insights", n, t)
    (vault / "claude-sessions").mkdir(parents=True)
    cfg = {"vault_path": str(vault), "sessions_folder": "claude-sessions",
           "insights_folder": "claude-insights", "wiki_folder": "claude-wiki"}
    (home / ".claude" / "obsidian-brain-config.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("HOME", str(home))
    db = str(tmp_path / "i.db")
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", db)
    monkeypatch.setattr(wiki, "_memory_files", lambda: [])
    monkeypatch.setattr(wiki, "_memory_listing", lambda: (wiki._memory_files(), [], "claude-code"))
    vault_index.ensure_index(str(vault), FOLDERS, db_path=db)
    ctx = {"vault": str(vault), "wiki_folder": "claude-wiki", "folders": FOLDERS, "db": db}
    return {"vault": vault, "ctx": ctx, "home": home, "cfg": cfg}


def _file(env, question="How does zebracorn ranking work?", **kw) -> Path:
    payload = {"question": question, "body": "Ranking uses bm25.\n\n### Sources\n- [[i1]]\n",
               "sources": ["i1", "i2", "d1"], "memory_sources": [], "topics": ["ranking"],
               "confidence": "high", "filed_by": "user"}
    payload.update(kw)
    return Path(wiki.file_page(env["ctx"], payload, D)["path"])


def _link_from_session(env, page: Path, form: str = "[[{stem}]]") -> None:
    _note(env["vault"], "claude-sessions", "s-link", "claude-session",
          body="see " + form.format(stem=page.stem))


def _scan(env, days=9999, project=None):
    return wp.scan(str(env["vault"]), "claude-sessions", "claude-insights", days, project=project)


def _classes(issues, page=None):
    return sorted(i.extra["signal_class"] for i in issues
                  if page is None or i.note_path == str(page))


def _mark_reviewed(page: Path) -> None:
    page.write_text(page.read_text().replace('filed_by: "user"\n', 'filed_by: "user"\nreviewed: true\n'))


# --- registry ----------------------------------------------------------------


def test_registered_in_the_default_sweep():
    assert "wiki-pages" in vault_doctor_checks.list_checks()
    assert wp in vault_doctor_checks.all_checks()


# --- clean baseline (the negative fixture for every reason) -------------------


def test_fresh_linked_user_page_has_no_rows(env):
    page = _file(env)
    _link_from_session(env, page)
    assert _scan(env) == []


# --- stale --------------------------------------------------------------------


def test_stale_when_a_source_changed(env):
    page = _file(env)
    _link_from_session(env, page)
    _note(env["vault"], "claude-insights", "i1", "claude-insight", body="changed body")
    assert _classes(_scan(env), page) == ["stale"]


# --- broken-source ------------------------------------------------------------


def test_broken_source_when_a_source_is_deleted(env):
    page = _file(env)
    _link_from_session(env, page)
    (env["vault"] / "claude-insights" / "i2.md").unlink()
    rows = _scan(env)
    assert "broken-source" in _classes(rows, page) and "stale" not in _classes(rows, page)


# --- reviewed-stale -----------------------------------------------------------


def test_reviewed_stale_replaces_stale_for_reviewed_pages(env):
    page = _file(env)
    _link_from_session(env, page)
    _mark_reviewed(page)
    _note(env["vault"], "claude-insights", "i1", "claude-insight", body="changed body")
    assert _classes(_scan(env), page) == ["reviewed-stale"]


def test_reviewed_fresh_page_has_no_rows(env):
    page = _file(env)
    _link_from_session(env, page)
    _mark_reviewed(page)
    assert _scan(env) == []


# --- auto-filed ---------------------------------------------------------------


def test_auto_filed_page_is_listed(env):
    page = _file(env, filed_by="auto", caller="ship")
    _link_from_session(env, page)
    assert _classes(_scan(env), page) == ["auto-filed"]


# --- orphan -------------------------------------------------------------------


def test_orphan_when_nothing_links_the_page(env):
    page = _file(env)
    assert _classes(_scan(env), page) == ["orphan"]


@pytest.mark.parametrize("form", ["[[{stem}|alias]]", "[[{stem}#Heading]]", "[[claude-wiki/queries/2026/{stem}]]"])
def test_alias_heading_and_path_links_count(env, form):
    page = _file(env)
    _link_from_session(env, page, form)
    assert _scan(env) == []


def test_index_and_log_links_do_not_count(env):
    page = _file(env)
    # index.md and log-2026.md already link the page; it is still an orphan.
    assert f"[[{page.stem}]]" in (env["vault"] / "claude-wiki" / "index.md").read_text()
    assert _classes(_scan(env), page) == ["orphan"]


def test_orphan_check_respects_days(env):
    page = _file(env)
    old = page.read_text().replace('updated: "2026-10-02"', 'updated: "2020-01-01"')
    page.write_text(old)
    # Re-render the index so index-drift does not join the rows.
    vault_index.ensure_index(str(env["vault"]), FOLDERS, db_path=env["ctx"]["db"])
    wiki.rebuild_wiki_index(env["ctx"])
    assert "orphan" not in _classes(_scan(env, days=30), page)
    assert "orphan" in _classes(_scan(env, days=99999), page)


# --- index-drift --------------------------------------------------------------


def test_index_drift_when_index_is_hand_edited(env):
    page = _file(env)
    _link_from_session(env, page)
    idx = env["vault"] / "claude-wiki" / "index.md"
    idx.write_text(idx.read_text() + "- [[hand-added]]\n")
    rows = _scan(env)
    assert _classes(rows) == ["index-drift"]
    assert rows[0].confidence == 1.0 and not rows[0].extra.get("unresolved")


def test_index_drift_when_index_is_missing(env):
    page = _file(env)
    _link_from_session(env, page)
    (env["vault"] / "claude-wiki" / "index.md").unlink()
    assert _classes(_scan(env)) == ["index-drift"]


def test_apply_rebuilds_index_backs_up_and_leaves_pages_alone(env, tmp_path):
    page = _file(env)
    _link_from_session(env, page)
    idx = env["vault"] / "claude-wiki" / "index.md"
    idx.write_text(idx.read_text() + "- [[hand-added]]\n")
    page_bytes = page.read_bytes()
    rows = _scan(env)
    results = wp.apply(rows, str(tmp_path / "backup"))
    assert [r.status for r in results] == ["applied"]
    assert "hand-added" not in idx.read_text()
    assert "hand-added" in (Path(results[0].backup_path) / "index.md").read_text()
    assert page.read_bytes() == page_bytes
    assert _scan(env) == []


def test_apply_skips_when_the_wiki_lock_is_held(env, tmp_path):
    page = _file(env)
    _link_from_session(env, page)
    idx = env["vault"] / "claude-wiki" / "index.md"
    idx.write_text(idx.read_text() + "- [[hand-added]]\n")
    rows = _scan(env)
    from note_writer import _acquire_lock, _release_lock

    lock, err = _acquire_lock(env["vault"] / "claude-wiki" / ".wiki")
    assert not err
    try:
        results = wp.apply(rows, str(tmp_path / "backup"))
    finally:
        _release_lock(lock)
    assert results[0].status == "skipped" and "another process" in results[0].error
    assert "hand-added" in idx.read_text()


def test_report_only_rows_are_unresolved(env):
    page = _file(env)
    rows = _scan(env)
    assert rows and all(r.extra.get("unresolved") and r.confidence == 0.0 for r in rows)
    assert all(r.status == "unresolved" for r in wp.apply(rows, "unused"))


# --- page-unreadable, project filter, wiki off ---------------------------------


_ROOT_SKIP = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                                reason="root ignores file permissions")


@_ROOT_SKIP
def test_unreadable_page_is_reported(env):
    page = _file(env)
    _link_from_session(env, page)
    bad = page.parent / "broken.md"
    bad.write_text('---\ntype: "claude-wiki"\n---\nbody\n')
    bad.chmod(0o000)
    try:
        rows = _scan(env)
    finally:
        bad.chmod(0o600)
    assert _classes(rows, bad) == ["page-unreadable"]


def test_page_with_bad_bytes_is_reported(env):
    page = _file(env)
    _link_from_session(env, page)
    bad = page.parent / "broken.md"
    bad.write_bytes(b"---\ntype: \"claude-wiki\"\n---\n\xff\xfe")
    assert _classes(_scan(env), bad) == ["page-unreadable"]


def test_unreadable_page_is_shown_even_under_project(env):
    page = _file(env)
    _link_from_session(env, page)
    bad = page.parent / "broken.md"
    bad.write_bytes(b"---\ntype: \"claude-wiki\"\n---\n\xff\xfe")
    assert _classes(_scan(env, project="demo"), bad) == ["page-unreadable"]
    assert _classes(_scan(env, project="nomatch"), bad) == ["page-unreadable"]


def test_project_filter_limits_page_rows(env):
    page = _file(env)
    assert _classes(_scan(env, project="demo"), page) == ["orphan"]
    assert _scan(env, project="nomatch") == []


def test_wiki_off_or_missing_reports_nothing(env, capsys):
    assert _scan(env) == []  # no wiki folder yet
    cfg = dict(env["cfg"], wiki_folder="")
    (env["home"] / ".claude" / "obsidian-brain-config.json").write_text(json.dumps(cfg))
    _file(env)
    assert _scan(env) == []
    assert "wiki is turned off" in capsys.readouterr().err


def test_invalid_wiki_folder_raises(env):
    cfg = dict(env["cfg"], wiki_folder="../outside")
    (env["home"] / ".claude" / "obsidian-brain-config.json").write_text(json.dumps(cfg))
    with pytest.raises(ValueError):
        _scan(env)


def test_corrupt_config_raises_instead_of_guessing(env):
    (env["home"] / ".claude" / "obsidian-brain-config.json").write_text("{not json")
    with pytest.raises(ValueError, match="cannot read"):
        _scan(env)


def test_config_is_read_from_home_at_call_time(env, monkeypatch):
    # A wiki folder name only the tmp config knows: proves the real config is never read.
    cfg = dict(env["cfg"], wiki_folder="tmp-only-wiki")
    (env["home"] / ".claude" / "obsidian-brain-config.json").write_text(json.dumps(cfg))
    (env["vault"] / "tmp-only-wiki" / "queries").mkdir(parents=True)
    (env["vault"] / "tmp-only-wiki" / "index.md").write_text("hand-made\n")
    assert _scan(env) and _classes(_scan(env)) == ["index-drift"]


def test_leftover_index_file_is_drift_but_a_user_file_is_not(env):
    page = _file(env)
    _link_from_session(env, page)
    w = env["vault"] / "claude-wiki"
    (w / "index-ideas.md").write_text("---\ntype: claude-insight\n---\nmine\n")
    assert _scan(env) == []
    (w / "index-old.md").write_text('---\ntype: "claude-wiki-index"\n---\n# old\n')
    rows = _scan(env)
    assert _classes(rows) == ["index-drift"] and rows[0].extra["files"]["extra"] == ["index-old.md"]


def test_a_self_link_does_not_save_a_page_from_orphan(env):
    page = _file(env)
    page.write_text(page.read_text() + f"\nSee also [[{page.stem}]].\n")
    vault_index.ensure_index(str(env["vault"]), FOLDERS, db_path=env["ctx"]["db"])
    assert "orphan" in _classes(_scan(env), page)


# --- review fixes (#399) ------------------------------------------------------


def test_empty_wiki_reports_nothing(env):
    w = env["vault"] / "claude-wiki"
    w.mkdir()
    assert _scan(env) == []
    (w / "queries").mkdir()
    assert _scan(env) == []
    # A user's own index-ideas.md is not a wiki index file.
    (w / "index-ideas.md").write_text("---\ntype: claude-insight\n---\nmine\n")
    assert _scan(env) == []


def test_empty_wiki_with_a_leftover_index_file_still_drifts(env):
    w = env["vault"] / "claude-wiki"
    w.mkdir()
    (w / "index-old.md").write_text('---\ntype: "claude-wiki-index"\n---\n# old\n')
    assert _classes(_scan(env)) == ["index-drift"]


@_ROOT_SKIP
def test_unreadable_queries_folder_crashes_the_check(env):
    page = _file(env)
    _link_from_session(env, page)
    q = env["vault"] / "claude-wiki" / "queries"
    q.chmod(0o000)
    try:
        with pytest.raises(OSError):
            _scan(env)
    finally:
        q.chmod(0o700)


@_ROOT_SKIP
def test_queries_folder_without_search_permission_crashes_the_check(env):
    # Readable but not searchable: scandir lists names, yet nothing inside
    # can be opened, so the access check must catch it.
    page = _file(env)
    _link_from_session(env, page)
    q = env["vault"] / "claude-wiki" / "queries"
    q.chmod(0o400)
    try:
        with pytest.raises(PermissionError, match="cannot read"):
            _scan(env)
    finally:
        q.chmod(0o700)


@_ROOT_SKIP
def test_unreadable_year_folder_crashes_the_check(env):
    page = _file(env)
    _link_from_session(env, page)
    year = page.parent
    year.chmod(0o000)
    try:
        with pytest.raises(OSError):
            _scan(env)
    finally:
        year.chmod(0o700)


@pytest.mark.parametrize("form", ["| [[{stem}\\|alias]] | x |", "[[{stem}.md]]", "[[{stem}.md\\|alias]]"])
def test_table_alias_and_md_links_count(env, form):
    page = _file(env)
    _link_from_session(env, page, form)
    assert _scan(env) == []


def test_odd_backslash_links_do_not_crash(env):
    page = _file(env)
    _note(env["vault"], "claude-sessions", "s-odd", "claude-session", body="[[a\\\\b]] [[\\]] [[x\\]]")
    assert _classes(_scan(env), page) == ["orphan"]
    links = wp._linked_stems(env["vault"], env["vault"] / "claude-wiki")
    assert "a\\\\b" in links and "x" in links and "" not in links


def test_memory_backed_page_with_deleted_memory_file_is_broken_source(env, tmp_path, monkeypatch):
    store = tmp_path / "home" / ".claude" / "projects" / "proj" / "memory"
    store.mkdir(parents=True)
    x, y = store / "x.md", store / "y.md"
    x.write_text("zebracorn memory\n")
    y.write_text("other\n")
    monkeypatch.setattr(wiki, "_memory_files", lambda: [x, y])
    page = _file(env, sources=["i1", "i2"], memory_sources=["proj/x.md"])
    _link_from_session(env, page)
    assert _scan(env) == []
    x.unlink()
    monkeypatch.setattr(wiki, "_memory_files", lambda: [y])
    rows = _scan(env)
    assert _classes(rows, page) == ["broken-source"]
    assert "missing: memory:proj/x.md" in rows[0].reason


def test_split_index_links_do_not_save_a_page_from_orphan(env, monkeypatch):
    monkeypatch.setattr(wiki, "INDEX_SPLIT", 0)
    page = _file(env)
    w = env["vault"] / "claude-wiki"
    assert f"[[{page.stem}]]" in (w / "index-demo.md").read_text()
    assert "[[index-demo]]" in (w / "index.md").read_text()
    assert _classes(_scan(env)) == ["orphan"]


def test_stale_crash_on_one_page_does_not_hide_the_next(env, monkeypatch):
    a = _file(env, question="alpha zebracorn question?")
    b = _file(env, question="beta zebracorn question?")
    real = wiki.stale

    def flaky(db, page, roots):
        if Path(page) == a:
            raise RuntimeError("kaput")
        return real(db, page, roots)

    monkeypatch.setattr(wiki, "stale", flaky)
    rows = _scan(env)
    assert "page-unreadable" in _classes(rows, a) and "kaput" in [r for r in rows if r.note_path == str(a)][0].reason
    assert _classes(rows, b) == ["orphan"]


def test_session_note_named_index_still_counts_as_a_link(env):
    page = _file(env)
    _note(env["vault"], "claude-sessions", "index", "claude-session", body=f"see [[{page.stem}]]")
    assert _scan(env) == []


def test_apply_backs_up_and_removes_a_leftover_index_file(env, tmp_path):
    page = _file(env)
    _link_from_session(env, page)
    old = env["vault"] / "claude-wiki" / "index-old.md"
    old.write_text('---\ntype: "claude-wiki-index"\n---\n# old\n')
    results = wp.apply(_scan(env), str(tmp_path / "backup"))
    assert [r.status for r in results] == ["applied"]
    assert not old.exists()
    assert "# old" in (Path(results[0].backup_path) / "index-old.md").read_text()


def test_apply_twice_in_a_row_releases_the_lock(env, tmp_path):
    page = _file(env)
    _link_from_session(env, page)
    idx = env["vault"] / "claude-wiki" / "index.md"
    idx.write_text(idx.read_text() + "- [[hand-added]]\n")
    rows = _scan(env)
    assert [r.status for r in wp.apply(rows, str(tmp_path / "b1"))] == ["applied"]
    assert [r.status for r in wp.apply(rows, str(tmp_path / "b2"))] == ["applied"]


def test_apply_error_keeps_the_backup_path_and_releases_the_lock(env, tmp_path, monkeypatch):
    page = _file(env)
    _link_from_session(env, page)
    idx = env["vault"] / "claude-wiki" / "index.md"
    idx.write_text(idx.read_text() + "- [[hand-added]]\n")
    rows = _scan(env)

    def boom(ctx):
        raise RuntimeError("disk full")

    real = wiki.rebuild_wiki_index
    monkeypatch.setattr(wiki, "rebuild_wiki_index", boom)
    [r] = wp.apply(rows, str(tmp_path / "backup"))
    assert r.status == "error" and "disk full" in r.error
    assert r.backup_path and "hand-added" in (Path(r.backup_path) / "index.md").read_text()
    monkeypatch.setattr(wiki, "rebuild_wiki_index", real)
    assert [x.status for x in wp.apply(rows, str(tmp_path / "backup2"))] == ["applied"]
