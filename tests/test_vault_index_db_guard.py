"""Regression tests for the #192 production-index-DB pollution guard."""
from __future__ import annotations

import importlib
import os

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


import sqlite3

import pytest


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
