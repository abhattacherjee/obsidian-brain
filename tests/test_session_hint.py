# tests/test_session_hint.py
"""Tests for find_latest_session() used by obsidian_session_hint hook."""

import obsidian_utils


def test_hint_finds_latest_session(sample_session_note, tmp_vault):
    """Returns most recent session for the project."""
    result = obsidian_utils.find_latest_session(str(tmp_vault), "claude-sessions", "test-project")
    assert result is not None
    assert result["date"] == "2026-04-10"
    assert "frobulator" in result["summary"]
    assert "integration tests" in result["next_steps"]


def test_hint_no_sessions(tmp_vault):
    """Returns None when no sessions exist."""
    result = obsidian_utils.find_latest_session(str(tmp_vault), "claude-sessions", "test-project")
    assert result is None


def test_hint_wrong_project(sample_session_note, tmp_vault):
    """Doesn't return sessions from other projects."""
    result = obsidian_utils.find_latest_session(str(tmp_vault), "claude-sessions", "totally-different-project")
    assert result is None
