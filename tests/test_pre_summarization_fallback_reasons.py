"""Unit tests for the three pre-summarization fallback_reason values
in upgrade_unsummarized_note (issue #183)."""

import os
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))

import obsidian_utils  # noqa: E402
from obsidian_utils import upgrade_unsummarized_note  # noqa: E402


def test_unreadable_note_sets_fallback_reason(tmp_path):
    """OSError on note read -> fallback_reason='unreadable_note'."""
    sessions = tmp_path / "claude-sessions"
    sessions.mkdir()
    note_path = sessions / "2026-05-17-missing.md"  # intentionally not created

    status, elapsed_s, model_used, fallback_reason = upgrade_unsummarized_note(
        str(note_path), str(tmp_path), "claude-sessions", "test-proj",
    )

    assert status.startswith("Failed: cannot read"), status
    assert fallback_reason == "unreadable_note"
    assert model_used is None
    assert elapsed_s >= 0.0
