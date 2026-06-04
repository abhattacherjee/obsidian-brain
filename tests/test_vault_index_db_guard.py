"""Regression tests for the #192 production-index-DB pollution guard."""
from __future__ import annotations

import importlib
import os
import sqlite3

import pytest

import vault_index


def test_default_db_path_honors_env_override(monkeypatch, tmp_path):
    target = tmp_path / "override.db"
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", str(target))
    assert vault_index._default_db_path() == str(target)


def test_default_db_path_falls_back_without_env(monkeypatch):
    monkeypatch.delenv("OBSIDIAN_BRAIN_DB", raising=False)
    expected = os.path.join(os.path.expanduser("~"), ".claude", "obsidian-brain-vault.db")
    assert vault_index._default_db_path() == expected


def test_in_test_ctx_true_under_pytest():
    # pytest always sets PYTEST_CURRENT_TEST while a test runs.
    assert vault_index._in_test_ctx() is True


def test_autouse_fixture_redirects_default_db(tmp_path):
    # Under the autouse fixture, OBSIDIAN_BRAIN_DB is set to a per-test tmp path,
    # so an un-isolated default call never resolves to the real prod DB.
    resolved = vault_index._default_db_path()
    assert resolved != vault_index._REAL_PROD_DB
    assert "OBSIDIAN_BRAIN_DB" in os.environ


def test_connect_raises_on_real_prod_path_under_test():
    with pytest.raises(RuntimeError, match="refusing to open production index DB"):
        vault_index._connect(vault_index._REAL_PROD_DB)


def test_connect_allows_isolated_path(tmp_path):
    db = tmp_path / "isolated.db"
    conn = vault_index._connect(str(db))
    try:
        assert isinstance(conn, sqlite3.Connection)
        assert db.exists()
    finally:
        conn.close()


def test_log_access_routes_through_connect():
    with pytest.raises(RuntimeError, match="refusing to open production index DB"):
        vault_index.log_access(
            vault_index._REAL_PROD_DB, "/some/note.md", "recall", "proj"
        )


def test_batch_activations_routes_through_connect():
    with pytest.raises(RuntimeError, match="refusing to open production index DB"):
        vault_index.batch_activations(vault_index._REAL_PROD_DB, ["/some/note.md"])


def test_parent_session_routes_through_connect():
    with pytest.raises(RuntimeError, match="refusing to open production index DB"):
        vault_index._parent_session_for_snapshot("/some/snap.md", vault_index._REAL_PROD_DB)


def test_indirect_ensure_index_lands_in_isolated_db(tmp_path, monkeypatch):
    # Simulate the indirect leak path: call ensure_index with NO db_path.
    # The autouse fixture has pointed OBSIDIAN_BRAIN_DB at a tmp file; override
    # it here to a path we control so we can assert rows landed there.
    iso_db = tmp_path / "iso.db"
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", str(iso_db))

    vault = tmp_path / "vault"
    sessions = vault / "claude-sessions"
    sessions.mkdir(parents=True)
    (sessions / "2026-06-03-demo-aaaa.md").write_text(
        "---\ntype: claude-session\nproject: demo\n---\n\n# Demo\nbody text\n",
        encoding="utf-8",
    )

    returned = vault_index.ensure_index(str(vault), ["claude-sessions"])  # noqa: no-default-db — intentional: guards the indirect (no db_path) leak path
    assert returned == str(iso_db)
    assert iso_db.exists()

    # The real prod DB must be untouched (guard would have raised otherwise).
    assert os.path.realpath(str(iso_db)) != vault_index._REAL_PROD_DB


def test_log_access_degrades_on_sqlite_error(tmp_path):
    # A non-prod path that sqlite3 cannot open (a directory) triggers a
    # connect-phase sqlite3.Error — log_access must degrade (return None), NOT raise.
    bad = tmp_path / "a-directory.db"
    bad.mkdir()
    assert vault_index.log_access(str(bad), "/some/note.md", "recall", "demo") is None


def test_batch_activations_degrades_on_sqlite_error(tmp_path):
    # Same connect-phase sqlite3.Error path: batch_activations must degrade to the
    # zero-filled result dict, NOT raise.
    bad = tmp_path / "a-directory.db"
    bad.mkdir()
    note = "/some/note.md"
    result = vault_index.batch_activations(str(bad), [note])
    assert result == {note: 0.0}


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported on this platform")
def test_connect_blocks_symlink_to_prod_db(tmp_path):
    # A symlink whose realpath resolves to the real prod DB must NOT evade the guard.
    link = tmp_path / "sneaky.db"
    try:
        os.symlink(vault_index._REAL_PROD_DB, str(link))
    except (OSError, NotImplementedError):
        pytest.skip("could not create symlink")
    with pytest.raises(RuntimeError, match="refusing to open production index DB"):
        vault_index._connect(str(link))


def test_default_db_path_empty_env_falls_back(monkeypatch):
    # OBSIDIAN_BRAIN_DB="" is falsy → the `or` must fall back to the real default
    # (regression guard against refactoring to os.environ.get(key, default)).
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", "")
    expected = os.path.join(os.path.expanduser("~"), ".claude", "obsidian-brain-vault.db")
    assert vault_index._default_db_path() == expected
