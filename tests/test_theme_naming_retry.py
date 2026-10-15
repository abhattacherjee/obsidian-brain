"""Failed naming must remain retryable without rebuilding theme membership."""

import json
import sqlite3
from unittest.mock import patch

import consolidate_cli
import vault_index


def test_retry_preserves_concurrent_theme_edit(selected_host_context, tmp_vault, monkeypatch):
    db = str(tmp_vault / "concurrent-naming.db")
    vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db)
    monkeypatch.setattr(consolidate_cli, "_default_db_path", lambda: db)
    monkeypatch.setattr(consolidate_cli, "load_config", lambda: {"vault_path": str(tmp_vault)})
    with sqlite3.connect(db) as conn:
        for path in ("a.md", "b.md", "c.md"):
            conn.execute("INSERT INTO notes (path, type, project, title, body, mtime, tfidf_vector) "
                         "VALUES (?, 'session', 'proj', ?, 'synthetic', 1.0, ?)",
                         (path, path, json.dumps({"alpha": 1.0})))
    with patch("consolidate_cli.generate_theme_names", lambda *a, **k: (None, "native_inventory_closed")):
        consolidate_cli.run_consolidate()

    def concurrent_name(*args, **kwargs):
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE themes SET name = 'Concurrent manual name'")
        return ([{"name": "Stale proposed name", "summary": "Stale summary"}], None)

    with patch("consolidate_cli.generate_theme_names", concurrent_name):
        consolidate_cli.run_consolidate()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT name FROM themes").fetchall() == [("Concurrent manual name",)]
        assert conn.execute("SELECT COUNT(*) FROM theme_naming_pending").fetchone()[0] == 1


def test_split_failed_naming_can_be_retried(selected_host_context, tmp_vault, monkeypatch):
    db = str(tmp_vault / "split-naming-retry.db")
    vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db)
    monkeypatch.setattr(consolidate_cli, "_default_db_path", lambda: db)
    monkeypatch.setattr(consolidate_cli, "load_config", lambda: {"vault_path": str(tmp_vault)})
    with sqlite3.connect(db) as conn:
        for path, term in (("a.md", "alpha"), ("b.md", "alpha"), ("c.md", "alpha"),
                           ("d.md", "beta"), ("e.md", "beta"), ("f.md", "beta")):
            conn.execute("INSERT INTO notes (path, type, project, title, body, mtime, tfidf_vector) "
                         "VALUES (?, 'session', 'proj', ?, 'synthetic', 1.0, ?)",
                         (path, path, json.dumps({term: 1.0})))
    with patch("consolidate_cli.generate_theme_names", lambda *a, **k: ([
        {"name": "Alpha", "summary": "Alpha work"}, {"name": "Beta", "summary": "Beta work"}], None)):
        consolidate_cli.run_consolidate()
    with sqlite3.connect(db) as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM themes ORDER BY id")]
    assert consolidate_cli.run_merge(*ids) == 0
    with patch("consolidate_cli.generate_theme_names", lambda *a, **k: (None, "native_inventory_closed")):
        assert consolidate_cli.run_split(ids[0]) == 0
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT * FROM theme_members ORDER BY theme_id, note_path").fetchall()
    with patch("consolidate_cli.generate_theme_names", lambda *a, **k: ([
        {"name": "Recovered alpha", "summary": "Alpha work"},
        {"name": "Recovered beta", "summary": "Beta work"}], None)):
        consolidate_cli.run_consolidate()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT name FROM themes ORDER BY id").fetchall() == [
            ("Recovered alpha",), ("Recovered beta",)]
        assert conn.execute("SELECT * FROM theme_members ORDER BY theme_id, note_path").fetchall() == before


def test_retry_failed_naming_preserves_theme_and_members(selected_host_context, tmp_vault, monkeypatch):
    db = str(tmp_vault / "naming-retry.db")
    vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db)
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", db)
    monkeypatch.setattr(consolidate_cli, "_default_db_path", lambda: db)
    monkeypatch.setattr(consolidate_cli, "load_config", lambda: {"vault_path": str(tmp_vault)})
    with sqlite3.connect(db) as conn:
        for path in ("a.md", "b.md", "c.md"):
            conn.execute(
                "INSERT INTO notes (path, type, project, title, body, mtime, tfidf_vector) "
                "VALUES (?, 'session', 'proj', ?, 'synthetic', 1.0, ?)",
                (path, path, json.dumps({"alpha": 1.0})),
            )
    with patch("consolidate_cli.generate_theme_names", lambda *a, **k: (None, "native_inventory_closed")):
        consolidate_cli.run_consolidate(full=False)
    with sqlite3.connect(db) as conn:
        theme_id = conn.execute("SELECT id FROM themes").fetchone()[0]
        before = conn.execute("SELECT * FROM theme_members ORDER BY note_path").fetchall()
        original_notes = conn.execute("SELECT path, body, tfidf_vector FROM notes ORDER BY path").fetchall()
    with patch("consolidate_cli.generate_theme_names", lambda *a, **k: (None, "native_inventory_closed")):
        consolidate_cli.run_consolidate(full=False)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT id, name FROM themes").fetchall() == [(theme_id, "alpha")]
        assert conn.execute("SELECT COUNT(*) FROM theme_naming_pending").fetchone()[0] == 1
    with patch("consolidate_cli.generate_theme_names", lambda *a, **k: ([{
        "name": "Recovered name", "summary": "Verified naming after permission recovery."
    }], None)):
        consolidate_cli.run_consolidate(full=False)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT id, name, summary FROM themes").fetchall() == [
            (theme_id, "Recovered name", "Verified naming after permission recovery.")]
        assert conn.execute("SELECT * FROM theme_members ORDER BY note_path").fetchall() == before
        assert conn.execute("SELECT path, body, tfidf_vector FROM notes ORDER BY path").fetchall() == original_notes
        assert conn.execute("SELECT COUNT(*) FROM theme_naming_pending").fetchone()[0] == 0
    with patch("consolidate_cli.generate_theme_names", side_effect=AssertionError("completed naming must not repeat")):
        consolidate_cli.run_consolidate(full=False)
