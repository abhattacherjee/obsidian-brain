"""Slice D (#231) — theme-query data layer in hooks/themes.py.

Covers recompute_activation (exact-value, fail-first), get_themes_in_window,
get_theme_member_previews, and get_unassigned_notes_in_window.
"""
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

import json

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))
import emerge_cli  # noqa: E402
import themes  # noqa: E402
import vault_index  # noqa: E402

# Anchored to the real UTC "today" (not a frozen calendar date) so fixture
# theme/member dates stay correctly inside/outside the 30-day window that
# emerge_cli.run_emerge_themes computes from the REAL datetime.now(timezone.utc)
# — a hardcoded NOW_D rots as real time advances past it (#264).
NOW_D = datetime.now(timezone.utc).date()
NOW = datetime.combine(NOW_D, datetime.min.time(), tzinfo=timezone.utc).isoformat()


@pytest.fixture
def db(tmp_vault, monkeypatch):
    p = str(tmp_vault / "c.db")
    vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=p)
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", p)
    monkeypatch.setenv("HOME", str(tmp_vault))
    return p


def _mk(conn, tid, project, members, updated_date="2026-06-14", activation=0.0):
    conn.execute(
        "INSERT INTO themes (id,name,summary,centroid,note_count,activation,created_date,updated_date,project) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (tid, f"t{tid}", "s", "{}", len(members), activation, "2026-01-01", updated_date, project),
    )
    for path, added in members:
        conn.execute(
            "INSERT INTO theme_members (theme_id,note_path,similarity,surprise,added_date) VALUES (?,?,?,?,?)",
            (tid, path, 0.9, 0.0, added),
        )


def _mk_note(conn, path, project, title, body, ndate, vec='{"x":1.0}', ntype="session"):
    conn.execute(
        "INSERT INTO notes (path, type, date, project, title, body, mtime, tfidf_vector) "
        "VALUES (?, ?, ?, ?, ?, ?, 1.0, ?)",
        (path, ntype, ndate, project, title, body, vec),
    )


def test_recompute_activation_exact(db):
    conn = vault_index._connect(db)
    d0 = NOW_D.isoformat()
    _mk(conn, 1, "p", [("a.md", d0), ("b.md", d0), ("c.md", d0)])
    d30 = (NOW_D - timedelta(days=30)).isoformat()
    d60 = (NOW_D - timedelta(days=60)).isoformat()
    _mk(conn, 2, "p", [("x.md", d30)])
    _mk(conn, 3, "p", [("y.md", d60)])
    conn.commit()
    n = themes.recompute_activation(conn, NOW)
    conn.commit()
    acts = {r[0]: r[1] for r in conn.execute("SELECT id, activation FROM themes").fetchall()}
    conn.close()
    assert n == 3
    assert acts[1] == 3.0          # 3 members today: 1+1+1
    assert acts[2] == 0.5          # one member at exactly one half-life
    assert acts[3] == 0.25         # one member at two half-lives


class _StubConn:
    """Minimal connection stub feeding canned rows to recompute_activation.

    theme_members rows include a 'garbage' (ValueError) and a None (TypeError)
    added_date — the latter cannot be stored in the NOT NULL added_date column,
    so it is injected here to exercise the TypeError arm of the guard. UPDATEs
    are captured so we can assert the resulting per-theme activation.
    """
    def __init__(self, member_rows, theme_ids):
        self._member_rows = member_rows
        self._theme_ids = theme_ids
        self.activations = {}

    def execute(self, sql, params=()):
        s = sql.strip().upper()
        if s.startswith("SELECT THEME_ID, ADDED_DATE FROM THEME_MEMBERS"):
            return _StubCursor(self._member_rows)
        if s.startswith("SELECT ID FROM THEMES"):
            return _StubCursor([(tid,) for tid in self._theme_ids])
        if s.startswith("UPDATE THEMES SET ACTIVATION"):
            act, tid = params
            self.activations[tid] = act
            return None
        raise AssertionError(f"unexpected SQL: {sql}")


class _StubCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows


def test_recompute_activation_skips_bad_added_date():
    # A member with a garbage added_date (ValueError) AND one with a None
    # added_date (TypeError) must be SKIPPED — not crash the recompute and not
    # mis-date the contribution. Fail-first: before the (ValueError, TypeError)
    # guard, the 'garbage' parse raised ValueError uncaught (only the bare->time
    # retry was caught) and the None parse raised TypeError uncaught.
    member_rows = [
        (1, NOW_D.isoformat()),  # theme 1: one good member today
        (1, "garbage"),     # theme 1: ValueError on parse -> skip
        (1, None),          # theme 1: TypeError on parse -> skip
        (2, (NOW_D - timedelta(days=30)).isoformat()),  # theme 2: one half-life
    ]
    conn = _StubConn(member_rows, theme_ids=[1, 2])

    n = themes.recompute_activation(conn, NOW)  # must NOT raise

    assert n == 2
    # Only the single good member contributes to theme 1 (garbage + None skipped,
    # NOT mis-dated to age 0). Theme 2's clean member computes correctly.
    assert conn.activations[1] == 1.0
    assert conn.activations[2] == 0.5  # one member at exactly one half-life


def test_recompute_activation_uses_named_constant(db):
    # The half-life is exposed as a module constant referenced by tests.
    assert themes.HALF_LIFE_DAYS == 30


def test_recompute_activation_returns_zero_for_memberless_theme(db):
    conn = vault_index._connect(db)
    _mk(conn, 1, "p", [])  # no members
    conn.commit()
    n = themes.recompute_activation(conn, NOW)
    conn.commit()
    act = conn.execute("SELECT activation FROM themes WHERE id=1").fetchone()[0]
    conn.close()
    assert n == 1
    assert act == 0.0


# --- get_themes_in_window -------------------------------------------------

def test_get_themes_in_window_filters_by_updated_date(db):
    conn = vault_index._connect(db)
    _mk(conn, 1, "p", [("a.md", "2026-06-15")], updated_date="2026-06-14")  # in window
    _mk(conn, 2, "p", [("b.md", "2026-06-15")], updated_date="2026-04-01")  # out of window
    conn.commit()
    conn.close()
    rows = themes.get_themes_in_window(db, "2026-06-01")
    ids = [r["id"] for r in rows]
    assert ids == [1]
    assert isinstance(rows[0], dict)
    assert set(rows[0]) >= {"id", "name", "summary", "note_count", "activation",
                            "project", "created_date", "updated_date"}


def test_get_themes_in_window_project_none_is_cross_project(db):
    conn = vault_index._connect(db)
    _mk(conn, 1, "alpha", [("a.md", "2026-06-15")], updated_date="2026-06-14")
    _mk(conn, 2, "beta", [("b.md", "2026-06-15")], updated_date="2026-06-14")
    _mk(conn, 3, None, [("c.md", "2026-06-15")], updated_date="2026-06-14")
    conn.commit()
    conn.close()
    rows = themes.get_themes_in_window(db, "2026-06-01", project=None)
    assert {r["id"] for r in rows} == {1, 2, 3}


def test_get_themes_in_window_project_scoped_plus_null(db):
    conn = vault_index._connect(db)
    _mk(conn, 1, "alpha", [("a.md", "2026-06-15")], updated_date="2026-06-14")
    _mk(conn, 2, "beta", [("b.md", "2026-06-15")], updated_date="2026-06-14")
    _mk(conn, 3, None, [("c.md", "2026-06-15")], updated_date="2026-06-14")
    conn.commit()
    conn.close()
    rows = themes.get_themes_in_window(db, "2026-06-01", project="alpha")
    assert {r["id"] for r in rows} == {1, 3}  # alpha + cross-project NULL, not beta


def test_get_themes_in_window_orders_by_activation_desc(db):
    conn = vault_index._connect(db)
    _mk(conn, 1, "p", [("a.md", "2026-06-15")], updated_date="2026-06-14", activation=1.0)
    _mk(conn, 2, "p", [("b.md", "2026-06-15")], updated_date="2026-06-14", activation=5.0)
    _mk(conn, 3, "p", [("c.md", "2026-06-15")], updated_date="2026-06-14", activation=3.0)
    conn.commit()
    conn.close()
    rows = themes.get_themes_in_window(db, "2026-06-01")
    assert [r["id"] for r in rows] == [2, 3, 1]


# --- get_theme_member_previews -------------------------------------------

def test_get_theme_member_previews_top_n_by_similarity(db):
    conn = vault_index._connect(db)
    conn.execute(
        "INSERT INTO themes (id,name,summary,centroid,note_count,activation,created_date,updated_date,project) "
        "VALUES (1,'t','s','{}',3,0.0,'2026-01-01','2026-06-14','p')")
    for path, sim in [("a.md", 0.5), ("b.md", 0.9), ("c.md", 0.7)]:
        _mk_note(conn, path, "p", f"title-{path}", f"body of {path}", "2026-06-10")
        conn.execute(
            "INSERT INTO theme_members (theme_id,note_path,similarity,surprise,added_date) VALUES (?,?,?,?,?)",
            (1, path, sim, 0.0, "2026-06-10"))
    conn.commit()
    rows = themes.get_theme_member_previews(conn, 1, top_n=2)
    conn.close()
    assert [r["note_path"] for r in rows] == ["b.md", "c.md"]  # similarity DESC
    assert rows[0]["title"] == "title-b.md"
    assert rows[0]["excerpt"] == "body of b.md"
    assert set(rows[0]) >= {"note_path", "title", "excerpt", "similarity",
                            "surprise", "project", "date"}


def test_get_theme_member_previews_truncates_body(db):
    conn = vault_index._connect(db)
    conn.execute(
        "INSERT INTO themes (id,name,summary,centroid,note_count,activation,created_date,updated_date,project) "
        "VALUES (1,'t','s','{}',1,0.0,'2026-01-01','2026-06-14','p')")
    long_body = "z" * 2000
    _mk_note(conn, "a.md", "p", "T", long_body, "2026-06-10")
    conn.execute(
        "INSERT INTO theme_members (theme_id,note_path,similarity,surprise,added_date) VALUES (1,'a.md',0.9,0.0,'2026-06-10')")
    conn.commit()
    rows = themes.get_theme_member_previews(conn, 1, body_chars=600)
    conn.close()
    assert len(rows[0]["excerpt"]) == 600


def test_get_theme_member_previews_title_fallback_on_empty_body(db):
    conn = vault_index._connect(db)
    conn.execute(
        "INSERT INTO themes (id,name,summary,centroid,note_count,activation,created_date,updated_date,project) "
        "VALUES (1,'t','s','{}',1,0.0,'2026-01-01','2026-06-14','p')")
    _mk_note(conn, "a.md", "p", "Fallback Title", "", "2026-06-10")
    conn.execute(
        "INSERT INTO theme_members (theme_id,note_path,similarity,surprise,added_date) VALUES (1,'a.md',0.9,0.0,'2026-06-10')")
    conn.commit()
    rows = themes.get_theme_member_previews(conn, 1)
    conn.close()
    assert rows[0]["excerpt"] == "Fallback Title"


# --- get_unassigned_notes_in_window --------------------------------------

def test_get_unassigned_notes_excludes_themed(db):
    conn = vault_index._connect(db)
    _mk_note(conn, "themed.md", "p", "Themed", "body", "2026-06-10")
    _mk_note(conn, "free.md", "p", "Free", "body", "2026-06-10")
    conn.execute(
        "INSERT INTO themes (id,name,summary,centroid,note_count,activation,created_date,updated_date,project) "
        "VALUES (1,'t','s','{}',1,0.0,'2026-01-01','2026-06-14','p')")
    conn.execute(
        "INSERT INTO theme_members (theme_id,note_path,similarity,surprise,added_date) VALUES (1,'themed.md',0.9,0.0,'2026-06-10')")
    conn.commit()
    conn.close()
    rows = themes.get_unassigned_notes_in_window(db, "2026-06-01")
    assert [r["note_path"] for r in rows] == ["free.md"]
    assert set(rows[0]) >= {"note_path", "title", "excerpt", "project", "date"}


def test_get_unassigned_notes_excludes_snapshots(db):
    # BH-001: transient claude-snapshot notes must not surface as /emerge
    # New Candidates (parity with the retired collect_vault_corpus default).
    conn = vault_index._connect(db)
    _mk_note(conn, "snap.md", "p", "Snap", "snapshot body", "2026-06-10",
             ntype="claude-snapshot")
    _mk_note(conn, "normal.md", "p", "Normal", "normal body", "2026-06-10")
    conn.commit()
    conn.close()
    rows = themes.get_unassigned_notes_in_window(db, "2026-06-01")
    paths = {r["note_path"] for r in rows}
    assert "snap.md" not in paths
    assert "normal.md" in paths


def test_get_unassigned_notes_window_and_limit(db):
    conn = vault_index._connect(db)
    _mk_note(conn, "old.md", "p", "Old", "body", "2026-01-01")     # out of window
    _mk_note(conn, "new1.md", "p", "New1", "body", "2026-06-12")
    _mk_note(conn, "new2.md", "p", "New2", "body", "2026-06-11")
    _mk_note(conn, "novec.md", "p", "NoVec", "body", "2026-06-12", vec="")  # no tfidf
    conn.commit()
    conn.close()
    rows = themes.get_unassigned_notes_in_window(db, "2026-06-01", limit=1)
    assert len(rows) == 1
    assert rows[0]["note_path"] == "new1.md"  # date DESC, limit 1
    all_rows = themes.get_unassigned_notes_in_window(db, "2026-06-01")
    assert {r["note_path"] for r in all_rows} == {"new1.md", "new2.md"}  # old + novec excluded


# --- run_emerge_themes ----------------------------------------------------

@pytest.fixture
def emerge_db(db, tmp_vault, monkeypatch):
    """db fixture + load_config patched to point /emerge at the temp vault."""
    monkeypatch.setattr(
        emerge_cli,
        "load_config",
        lambda: {"vault_path": str(tmp_vault), "insights_folder": "claude-insights"},
    )
    return db


def _recent(days_ago):
    return (NOW_D - timedelta(days=days_ago)).isoformat()


def test_run_emerge_themes_sparse(emerge_db, capsys):
    conn = vault_index._connect(emerge_db)
    _mk(conn, 1, "p", [("a.md", _recent(1))], updated_date=_recent(1))  # only ONE theme
    conn.commit()
    conn.close()

    emerge_cli.run_emerge_themes(30)

    out = capsys.readouterr().out
    assert "STATUS=SPARSE:1" in out
    # Sparse guard must NOT write the corpus JSON.
    assert not os.path.exists(emerge_cli._themes_json_path())


def test_run_emerge_themes_ok(emerge_db, capsys):
    conn = vault_index._connect(emerge_db)
    # Two windowed themes, each with a recent member.
    _mk(conn, 1, "alpha", [], updated_date=_recent(2))
    _mk(conn, 2, "beta", [], updated_date=_recent(2))
    for tid, path in [(1, "m1.md"), (2, "m2.md")]:
        _mk_note(conn, path, "alpha" if tid == 1 else "beta", f"Title {path}", f"body {path}", _recent(2))
        conn.execute(
            "INSERT INTO theme_members (theme_id,note_path,similarity,surprise,added_date) VALUES (?,?,?,?,?)",
            (tid, path, 0.9, 0.0, _recent(2)),
        )
    # One unassigned windowed note (has tfidf, no theme membership).
    _mk_note(conn, "free.md", "gamma", "Free", "free body", _recent(1))
    conn.commit()
    # Pre-condition: activation starts at the seeded 0.0.
    assert conn.execute("SELECT MAX(activation) FROM themes").fetchone()[0] == 0.0
    conn.close()

    emerge_cli.run_emerge_themes(30)

    out = capsys.readouterr().out
    assert "STATUS=OK:2:1" in out

    path = emerge_cli._themes_json_path()
    assert os.path.exists(path)
    with open(path) as f:
        corpus = json.load(f)
    assert len(corpus["themes"]) >= 2
    for t in corpus["themes"]:
        assert "members" in t
    assert len(corpus["themes"][0]["members"]) >= 1
    assert len(corpus["unassigned_candidates"]) == 1
    assert set(corpus) >= {"generated_at", "window_days", "date_range",
                           "projects", "themes", "unassigned_candidates"}

    # Activation refresh: the stored column is no longer the seeded 0.0.
    conn = vault_index._connect(emerge_db)
    max_act = conn.execute("SELECT MAX(activation) FROM themes").fetchone()[0]
    conn.close()
    assert max_act > 0.0


# --- _strip_leading_frontmatter -------------------------------------------

def test_strip_leading_frontmatter_removes_block():
    text = "---\ntitle: x\n---\n\n## Body\n- item\n"
    out = emerge_cli._strip_leading_frontmatter(text)
    assert out.startswith("## Body")
    assert "title: x" not in out
    assert "---" not in out


def test_strip_leading_frontmatter_no_frontmatter_unchanged():
    text = "## Body\n- item\n"
    assert emerge_cli._strip_leading_frontmatter(text) == text


def test_strip_leading_frontmatter_no_closing_delimiter_unchanged():
    # A leading '---' with no closing '---' must NOT be corrupted.
    text = "---\ntitle: x\n## Body still here\n"
    assert emerge_cli._strip_leading_frontmatter(text) == text


def test_strip_leading_frontmatter_delimiter_only_later_unchanged():
    # '---' appears only mid-body (a horizontal rule), not at the top.
    text = "## Body\nsome text\n---\nmore text\n"
    assert emerge_cli._strip_leading_frontmatter(text) == text


# --- run_build_note -------------------------------------------------------

def test_run_build_note(emerge_db, tmp_vault, capsys):
    os.makedirs(emerge_cli._emerge_dir(), exist_ok=True)
    corpus = {
        "generated_at": NOW,
        "window_days": 30,
        "date_range": "2026-05-16 to 2026-06-15",
        "projects": ["alpha", "beta"],
        "themes": [{"id": 1, "name": "T1"}, {"id": 2, "name": "T2"}],
        "unassigned_candidates": [],
    }
    with open(emerge_cli._themes_json_path(), "w") as f:
        json.dump(corpus, f)
    with open(emerge_cli._analysis_path(), "w") as f:
        # Sub-agent mistakenly prepended its own YAML frontmatter. The final
        # note must contain exactly ONE frontmatter block (the run_build_note
        # claude-emerge block); the injected `title: T` must not appear.
        f.write("---\ntitle: T\n---\n\n## Growing Themes\n- T1 is hot\n")

    emerge_cli.run_build_note()

    out = capsys.readouterr().out
    assert "SAVED:" in out
    assert "---REPORT---" in out

    ins_dir = tmp_vault / "claude-insights"
    notes = list(ins_dir.glob("*-emerge-patterns-*.md"))
    assert len(notes) == 1
    text = notes[0].read_text()
    assert "type: claude-emerge" in text
    assert "theme_count: 2" in text
    assert "## Growing Themes" in text
    # Exactly ONE frontmatter block: the stray sub-agent block was stripped.
    # Fail-first: without _strip_leading_frontmatter the body embeds a second
    # `\n---\n`-delimited block, so this count would be 2 and `title: T` present.
    assert text.count("\n---\n") == 1
    assert "title: T" not in text
    # Temp files cleaned up on success.
    assert not os.path.exists(emerge_cli._themes_json_path())
    assert not os.path.exists(emerge_cli._analysis_path())


def test_run_emerge_themes_recompute_failure_exits_clean(emerge_db, capsys, monkeypatch):
    # A sqlite3.Error inside recompute_activation must roll back and surface the
    # clean ERROR/exit-1 contract, not a traceback.
    conn = vault_index._connect(emerge_db)
    _mk(conn, 1, "alpha", [("a.md", _recent(2))], updated_date=_recent(2))
    _mk(conn, 2, "beta", [("b.md", _recent(2))], updated_date=_recent(2))
    conn.commit()
    conn.close()

    def _boom(*a, **k):
        raise sqlite3.Error("boom")

    monkeypatch.setattr(emerge_cli.themes, "recompute_activation", _boom)
    with pytest.raises(SystemExit) as exc:
        emerge_cli.run_emerge_themes(30)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ERROR" in err


def test_run_build_note_missing_artifact_exits_clean(emerge_db, tmp_vault, capsys):
    # Fresh HOME, no emerge-themes.json present.
    assert not os.path.exists(emerge_cli._themes_json_path())
    with pytest.raises(SystemExit) as exc:
        emerge_cli.run_build_note()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ERROR could not read emerge artifacts" in err
