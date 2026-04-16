"""Verifies batch access logging: one connection, one commit per search call."""

import sqlite3

import pytest

import vault_index


@pytest.fixture
def indexed_vault(tmp_vault):
    """Vault with three indexed notes matching the query 'foo'."""
    sessions = tmp_vault / "claude-sessions"
    for i, slug in enumerate(["a", "b", "c"]):
        (sessions / f"2026-04-16-proj-{slug}.md").write_text(
            "---\n"
            "type: claude-session\n"
            "date: 2026-04-16\n"
            "project: proj\n"
            f"title: Note {slug}\n"
            "tags:\n  - claude/session\n"
            "status: summarized\n"
            "---\n"
            "foo bar baz\n",
            encoding="utf-8",
        )
    db_path = str(tmp_vault / "test.db")
    vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)
    return db_path


def test_search_vault_logs_all_results_once(indexed_vault):
    """One search returning N results must insert exactly N rows into access_log."""
    results = vault_index.search_vault(indexed_vault, "foo", project="proj", limit=10)
    assert len(results) >= 1, "Query should match at least one note"

    conn = sqlite3.connect(indexed_vault)
    count = conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
    conn.close()
    assert count == len(results)


def test_search_vault_uses_single_connection(indexed_vault, monkeypatch):
    """log_access should NOT be called in a loop — batch insert only."""
    call_count = {"n": 0}
    original = vault_index.log_access

    def counting(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(vault_index, "log_access", counting)
    vault_index.search_vault(indexed_vault, "foo", project="proj", limit=10)
    assert call_count["n"] == 0, (
        f"log_access was called {call_count['n']} times — "
        "search_vault should use batch insert on its own connection instead."
    )
