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
