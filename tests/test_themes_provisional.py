"""#458: themes, /consolidate and /emerge leave provisional notes out; /consolidate prunes old provisional members."""

import json
import sqlite3
from unittest.mock import patch

import pytest

import consolidate_cli
import themes
import vault_index

# Odd spellings the shared NOT_PROVISIONAL_SQL must catch (#455).
PROVISIONAL = ["provisional", "Provisional", '"provisional"', "provisional # x"]


@pytest.fixture
def db(tmp_vault, monkeypatch):
    p = str(tmp_vault / "t.db")
    vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=p)
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", p)
    monkeypatch.setattr(consolidate_cli, "load_config", lambda: {"vault_path": str(tmp_vault)})
    return p


def _seed(db, specs):
    """specs: (path, status, vec[, date])."""
    conn = sqlite3.connect(db)
    for spec in specs:
        path, status, vec = spec[:3]
        d = spec[3] if len(spec) > 3 else "2026-10-20"
        conn.execute(
            "INSERT INTO notes (path, type, project, title, body, mtime, tfidf_vector, status, date) "
            "VALUES (?, 'claude-insight', 'proj', ?, 'b', 1.0, ?, ?, ?)",
            (path, path, json.dumps(vec), status, d))
    conn.commit()
    conn.close()


def _fake_names(clusters, model="haiku", timeout=120):
    return ([{"name": f"Theme {i}", "summary": "s"} for i in range(len(clusters))], None)


def _members(db):
    conn = sqlite3.connect(db)
    try:
        return sorted(r[0] for r in conn.execute("SELECT note_path FROM theme_members"))
    finally:
        conn.close()


def _cluster_specs():
    """Three kept notes and one note per provisional spelling, all in one tight cluster."""
    specs = [(f"keep-{i}.md", s, {"x": 1.0, "y": 0.9}) for i, s in enumerate([None, "promoted", "active"])]
    specs += [(f"prov-{i}.md", s, {"x": 1.0, "y": 0.9}) for i, s in enumerate(PROVISIONAL)]
    return specs


# --- readers ---------------------------------------------------------------

def test_readers_skip_every_provisional_spelling(db):
    _seed(db, _cluster_specs())
    want = ["keep-0.md", "keep-1.md", "keep-2.md"]
    assert sorted(p for p, _, _ in themes.get_unassigned_notes(db)) == want
    assert sorted(p for p, _, _ in themes.get_all_vectorized_notes(db)) == want
    assert sorted(p for p, _, _ in themes.get_unassigned_notes(db, project="proj")) == want
    assert sorted(p for p, _, _ in themes.get_all_vectorized_notes(db, project="proj")) == want


@pytest.mark.parametrize("full", [False, True], ids=["incremental", "full"])
def test_consolidate_never_adds_a_provisional_note(db, full):
    _seed(db, _cluster_specs())
    with patch("consolidate_cli.generate_theme_names", _fake_names):
        consolidate_cli.run_consolidate(full=full)
    assert _members(db) == ["keep-0.md", "keep-1.md", "keep-2.md"]


@pytest.mark.parametrize("status", PROVISIONAL)
def test_assign_to_theme_returns_none_for_a_provisional_note(db, status):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO themes (name, summary, centroid, note_count, created_date, updated_date, project) "
                 "VALUES ('T','s',?,1,'2026-01-01','2026-01-01','proj')", (json.dumps({"x": 1.0}),))
    conn.commit()
    conn.close()
    _seed(db, [("prov.md", status, {"x": 1.0}), ("kept.md", None, {"x": 1.0})])
    assert themes.assign_to_theme(db, "prov.md", project="proj") is None
    assert _members(db) == []
    assert themes.assign_to_theme(db, "kept.md", project="proj") is not None  # positive control
    assert _members(db) == ["kept.md"]


def test_emerge_candidates_skip_provisional(db):
    _seed(db, [("prov.md", "Provisional", {"x": 1.0}, "2026-10-24"), ("kept.md", None, {"x": 1.0}, "2026-10-23")])
    assert [r["note_path"] for r in themes.get_unassigned_notes_in_window(db, "2026-10-01")] == ["kept.md"]


# --- prune_provisional_members ---------------------------------------------

def _theme_with(db, members):
    """A theme whose centroid is the mean of its members' vectors, as create_theme stores it."""
    conn = vault_index._connect(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cen = themes.compute_centroid([v for _, v in members])
        tid = themes.create_theme(conn, "T", "s", cen, members, "proj", "2026-10-01T00:00:00+00:00")
        conn.commit()
        return tid
    finally:
        conn.close()


def _prune(db):
    conn = vault_index._connect(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        n = themes.prune_provisional_members(conn, "2026-10-24T00:00:00+00:00")
        conn.commit()
        return n
    finally:
        conn.close()


def _theme_row(db, tid):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT centroid, note_count, updated_date FROM themes WHERE id = ?", (tid,)).fetchone()
    finally:
        conn.close()


def test_prune_reverse_folds_the_centroid_and_decrements_the_count(db):
    vecs = {"a.md": {"x": 1.0, "y": 0.2}, "b.md": {"x": 0.5, "z": 0.8}, "p1.md": {"y": 1.0, "w": 0.4},
            "p2.md": {"x": 0.3, "w": 0.9}}
    _seed(db, [("a.md", None, vecs["a.md"]), ("b.md", "promoted", vecs["b.md"]),
               ("p1.md", "provisional", vecs["p1.md"]), ("p2.md", "Provisional # x", vecs["p2.md"])])
    tid = _theme_with(db, list(vecs.items()))
    assert _prune(db) == 2
    cen, count, updated = _theme_row(db, tid)
    want = themes.compute_centroid([vecs["a.md"], vecs["b.md"]])
    got = json.loads(cen)
    assert count == 2 and updated == "2026-10-24T00:00:00+00:00"
    assert set(got) == set(want)
    assert all(abs(got[t] - want[t]) < 1e-9 for t in want)
    assert _members(db) == ["a.md", "b.md"]
    assert _prune(db) == 0  # idempotent
    assert _theme_row(db, tid)[1] == 2


def test_prune_drops_a_theme_left_with_no_members(db):
    _seed(db, [("p1.md", "provisional", {"x": 1.0}), ("p2.md", "provisional", {"x": 0.5})])
    tid = _theme_with(db, [("p1.md", {"x": 1.0}), ("p2.md", {"x": 0.5})])
    assert _prune(db) == 2
    assert _theme_row(db, tid) is None
    assert _members(db) == []


def test_prune_treats_a_bad_centroid_as_empty(db):
    _seed(db, [("a.md", None, {"x": 1.0}), ("p.md", "provisional", {"x": 1.0})])
    tid = _theme_with(db, [("a.md", {"x": 1.0}), ("p.md", {"x": 1.0})])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE themes SET centroid = 'not json' WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    assert _prune(db) == 1
    cen, count, _ = _theme_row(db, tid)
    # Same as _delete_note: the bad centroid is read as {} and folded, (0 * 2 - 1.0) / 1 for x.
    assert count == 1 and json.loads(cen) == vault_index._reverse_fold_centroid({}, {"x": 1.0}, 2) == {"x": -1.0}


@pytest.mark.parametrize("full", [False, True], ids=["incremental", "full"])
def test_consolidate_prunes_old_provisional_members_and_prints_the_count(db, full, capsys):
    _seed(db, [("a.md", None, {"q": 1.0}), ("b.md", None, {"q": 1.0}), ("p.md", "provisional", {"q": 1.0})])
    tid = _theme_with(db, [("a.md", {"q": 1.0}), ("b.md", {"q": 1.0}), ("p.md", {"q": 1.0})])
    with patch("consolidate_cli.generate_theme_names", _fake_names):
        consolidate_cli.run_consolidate(full=full)
    out = capsys.readouterr().out
    assert "PRUNED_PROVISIONAL=1" in out
    assert "p.md" not in _members(db)
    if not full:
        assert _theme_row(db, tid)[1] == 2
    with patch("consolidate_cli.generate_theme_names", _fake_names):
        consolidate_cli.run_consolidate(full=full)
    assert "PRUNED_PROVISIONAL=0" in capsys.readouterr().out


def test_a_themed_note_demoted_later_is_pruned_on_the_next_run(db, capsys):
    _seed(db, [("a.md", None, {"q": 1.0}), ("b.md", None, {"q": 1.0}), ("c.md", "promoted", {"q": 1.0})])
    _theme_with(db, [("a.md", {"q": 1.0}), ("b.md", {"q": 1.0}), ("c.md", {"q": 1.0})])
    with patch("consolidate_cli.generate_theme_names", _fake_names):
        consolidate_cli.run_consolidate(full=False)
    assert "PRUNED_PROVISIONAL=0" in capsys.readouterr().out
    conn = sqlite3.connect(db)  # what capture_select does when a stronger note displaces it
    conn.execute("UPDATE notes SET status = 'provisional' WHERE path = 'c.md'")
    conn.commit()
    conn.close()
    with patch("consolidate_cli.generate_theme_names", _fake_names):
        consolidate_cli.run_consolidate(full=False)
    assert "PRUNED_PROVISIONAL=1" in capsys.readouterr().out
    assert "c.md" not in _members(db)


def test_an_error_after_the_prune_rolls_it_back_and_exits_1(db, monkeypatch):
    _seed(db, [("a.md", None, {"q": 1.0}), ("b.md", None, {"q": 1.0}), ("p.md", "provisional", {"q": 1.0}),
               ("c1.md", None, {"x": 1.0}), ("c2.md", None, {"x": 1.0}), ("c3.md", None, {"x": 1.0})])
    _theme_with(db, [("a.md", {"q": 1.0}), ("b.md", {"q": 1.0}), ("p.md", {"q": 1.0})])
    calls = []
    real = themes.prune_provisional_members

    def counted(conn, now):
        calls.append(real(conn, now))
        return calls[-1]

    def fail(*a, **k):  # after the real prune, in the same transaction
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr(consolidate_cli.themes, "prune_provisional_members", counted)
    monkeypatch.setattr(consolidate_cli.themes, "create_theme", fail)
    with patch("consolidate_cli.generate_theme_names", _fake_names):
        with pytest.raises(SystemExit) as exc:
            consolidate_cli.run_consolidate(full=False)
    assert exc.value.code == 1
    assert calls == [1]
    assert _members(db) == ["a.md", "b.md", "p.md"]  # rolled back: no half-pruned state, no new theme
