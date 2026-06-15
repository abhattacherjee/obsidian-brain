"""Slice D (#231) — theme-query data layer in hooks/themes.py.

Covers recompute_activation (exact-value, fail-first), get_themes_in_window,
get_theme_member_previews, and get_unassigned_notes_in_window.
"""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))
import themes  # noqa: E402
import vault_index  # noqa: E402

NOW = "2026-06-15T00:00:00+00:00"
NOW_D = date(2026, 6, 15)


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


def _mk_note(conn, path, project, title, body, ndate, vec='{"x":1.0}'):
    conn.execute(
        "INSERT INTO notes (path, type, date, project, title, body, mtime, tfidf_vector) "
        "VALUES (?, 'session', ?, ?, ?, ?, 1.0, ?)",
        (path, ndate, project, title, body, vec),
    )


def test_recompute_activation_exact(db):
    conn = vault_index._connect(db)
    _mk(conn, 1, "p", [("a.md", "2026-06-15"), ("b.md", "2026-06-15"), ("c.md", "2026-06-15")])
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
