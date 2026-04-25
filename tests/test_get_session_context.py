# tests/test_get_session_context.py
"""Tests for first-seen-date marker, hash-resolver, and basename invariants
introduced for obsidian-brain#101 (subsumes #86)."""

from __future__ import annotations

import datetime
import json
import os
import shutil
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

import obsidian_utils


def _unique_sid() -> str:
    return f"test-sid-{uuid.uuid4().hex}"


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect `~/.claude/obsidian-brain/sessions/` into tmp_path so marker
    writes do not pollute the real user directory across tests."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    yield tmp_path


def test_first_seen_date_lazy_writes_and_returns_today(isolated_home):
    sid = _unique_sid()
    today = datetime.date.today().isoformat()

    result = obsidian_utils._first_seen_date(sid)

    assert result == today
    marker = isolated_home / ".claude" / "obsidian-brain" / "sessions" / f"{sid}.json"
    assert marker.exists()
    assert oct(marker.stat().st_mode)[-3:] == "600"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["first_seen_date"] == today
    assert "first_seen_iso" in payload


def test_first_seen_date_idempotent_across_calls(isolated_home):
    sid = _unique_sid()
    first = obsidian_utils._first_seen_date(sid)
    second = obsidian_utils._first_seen_date(sid)
    third = obsidian_utils._first_seen_date(sid)
    assert first == second == third


def test_first_seen_date_survives_today_advance(isolated_home):
    """Cross-midnight invariant: once the marker exists, advancing
    date.today() must not change the returned value."""
    sid = _unique_sid()
    day_n = datetime.date(2026, 4, 25)
    day_n_plus_1 = datetime.date(2026, 4, 26)

    class _FrozenDate:
        @staticmethod
        def today():
            return _FrozenDate._now

    _FrozenDate._now = day_n
    with patch.object(obsidian_utils.datetime, "date", _FrozenDate):
        first = obsidian_utils._first_seen_date(sid)
        assert first == day_n.isoformat()

    _FrozenDate._now = day_n_plus_1
    with patch.object(obsidian_utils.datetime, "date", _FrozenDate):
        second = obsidian_utils._first_seen_date(sid)
        assert second == day_n.isoformat()  # still day-N, not day-N+1


def test_first_seen_date_corruption_self_heals(isolated_home):
    sid = _unique_sid()
    marker_dir = isolated_home / ".claude" / "obsidian-brain" / "sessions"
    marker_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = marker_dir / f"{sid}.json"
    marker.write_text("not valid json {", encoding="utf-8")

    today = datetime.date.today().isoformat()
    result = obsidian_utils._first_seen_date(sid)
    assert result == today
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["first_seen_date"] == today

    # Subsequent call returns the rewritten value, no further mutation
    result2 = obsidian_utils._first_seen_date(sid)
    assert result2 == today


def test_first_seen_date_rejects_path_traversal_sid(isolated_home, capsys):
    """A sid shaped like a path-traversal attempt must NOT escape the
    marker directory; helper falls back to today's date and warns."""
    today = datetime.date.today().isoformat()
    result = obsidian_utils._first_seen_date("../../../etc/passwd")
    assert result == today
    # No marker file should have been created anywhere outside sessions/
    sessions_dir = isolated_home / ".claude" / "obsidian-brain" / "sessions"
    if sessions_dir.exists():
        assert list(sessions_dir.glob("*passwd*")) == []
    captured = capsys.readouterr()
    assert "unsafe sid" in captured.err.lower() or "refusing" in captured.err.lower()


def test_first_seen_date_chmods_existing_loose_mode_dir(isolated_home):
    """mkdir(mode=0o700, exist_ok=True) is a no-op on a pre-existing dir;
    helper must explicitly chmod 0o700 if mode is too permissive."""
    sessions = isolated_home / ".claude" / "obsidian-brain" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    os.chmod(sessions, 0o755)  # simulate a previously-buggy permission
    sid = _unique_sid()
    obsidian_utils._first_seen_date(sid)
    assert oct(sessions.stat().st_mode)[-3:] == "700"


def test_get_session_context_fallback_uses_marker_date(isolated_home, tmp_path, monkeypatch):
    """get_session_context() fallback must compose its basename from
    _first_seen_date(sid), not date.today() — so cross-midnight insights
    and SessionEnd writes agree on the filename."""
    sid = _unique_sid()
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: sid)
    monkeypatch.setattr(obsidian_utils, "canonical_project_name", lambda *a, **kw: "obsidian-brain")

    # Pre-write a marker pointing at day-N
    marker_dir = isolated_home / ".claude" / "obsidian-brain" / "sessions"
    marker_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (marker_dir / f"{sid}.json").write_text(
        json.dumps({"first_seen_date": "2026-04-25", "first_seen_iso": "x"}),
        encoding="utf-8",
    )

    vault = tmp_path / "vault"
    sessions = vault / "claude-sessions"
    sessions.mkdir(parents=True)

    ctx = obsidian_utils.get_session_context(str(vault), "claude-sessions")
    assert ctx["session_note_name"].startswith("2026-04-25-obsidian-brain-")
    # Must be byte-equal to make_filename(...)[:-3]
    expected = obsidian_utils.make_filename("2026-04-25", "obsidian-brain", sid)[:-3]
    assert ctx["session_note_name"] == expected
