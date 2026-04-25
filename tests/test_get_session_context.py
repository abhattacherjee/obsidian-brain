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
