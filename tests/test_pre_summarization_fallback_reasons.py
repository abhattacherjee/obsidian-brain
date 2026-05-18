"""Unit tests for the three pre-summarization fallback_reason values
in upgrade_unsummarized_note (issue #183)."""

import sys
from pathlib import Path

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


def test_no_session_id_sets_fallback_reason(tmp_path):
    """Frontmatter missing session_id -> fallback_reason='no_session_id'."""
    sessions = tmp_path / "claude-sessions"
    sessions.mkdir()
    note_path = sessions / "2026-05-17-no-sid.md"
    note_path.write_text(
        "---\n"
        "project: test-proj\n"
        "status: auto-logged\n"
        "date: 2026-05-17\n"
        "---\n"
        "\n"
        "## Conversation (raw)\n"
        "\n"
        "**User:** hello\n"
        "**Assistant:** hi\n",
        encoding="utf-8",
    )

    status, elapsed_s, model_used, fallback_reason = upgrade_unsummarized_note(
        str(note_path), str(tmp_path), "claude-sessions", "test-proj",
    )

    assert status.startswith("Failed: no session_id"), status
    assert fallback_reason == "no_session_id"
    assert model_used is None


def test_no_conversation_content_sets_fallback_reason(tmp_path, monkeypatch):
    """Valid frontmatter but no extractable messages -> fallback_reason='no_conversation_content'."""
    sessions = tmp_path / "claude-sessions"
    sessions.mkdir()
    note_path = sessions / "2026-05-17-empty-convo.md"
    note_path.write_text(
        "---\n"
        "session_id: test-sid-001\n"
        "project: test-proj\n"
        "status: auto-logged\n"
        "date: 2026-05-17\n"
        "---\n"
        "\n"
        "Some prose body with no conversation section at all.\n",
        encoding="utf-8",
    )

    # Force the JSONL lookup to return None so the function falls through to
    # raw-note extraction (which finds no `## Conversation (raw)` section).
    monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda sid: None)
    # Guard: verify the monkeypatch actually intercepted the lookup. A future
    # refactor that switches to `from .other import find_transcript_jsonl`
    # would silently bypass this patch.
    assert obsidian_utils.find_transcript_jsonl("test-sid-001") is None

    status, elapsed_s, model_used, fallback_reason = upgrade_unsummarized_note(
        str(note_path), str(tmp_path), "claude-sessions", "test-proj",
    )

    assert status.startswith("Failed: no conversation content"), status
    assert fallback_reason == "no_conversation_content"
    assert model_used is None


def test_unreadable_note_encoding_error_sets_fallback_reason(tmp_path):
    """Invalid UTF-8 in a note -> fallback_reason='unreadable_note' (encoding case)."""
    sessions = tmp_path / "claude-sessions"
    sessions.mkdir()
    note_path = sessions / "2026-05-17-bad-encoding.md"
    # Latin-1 / mixed garbage bytes that cannot be decoded as UTF-8
    note_path.write_bytes(b"---\nproject: test-proj\n---\n\xff\xfe garbage \xc3\x28\n")

    status, elapsed_s, model_used, fallback_reason = upgrade_unsummarized_note(
        str(note_path), str(tmp_path), "claude-sessions", "test-proj",
    )

    assert status.startswith("Failed: cannot read"), status
    assert fallback_reason == "unreadable_note"
    assert model_used is None
