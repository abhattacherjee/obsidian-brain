"""Tests for SessionEnd telemetry: _append_sessionend_log helper + _Outcome enum wraps."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Make hooks/ importable for in-process tests.
_HOOKS_DIR = str(Path(__file__).parent.parent / "hooks")
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)


def _hook_script_path() -> str:
    return str(Path(__file__).parent.parent / "hooks" / "obsidian_session_log.py")


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


class TestAppendSessionEndLog:
    @pytest.fixture(autouse=True)
    def _restore_obsidian_utils(self):
        """Reload obsidian_utils after each test to undo HOME-monkeypatched module constants."""
        yield
        import importlib
        import obsidian_utils
        importlib.reload(obsidian_utils)

    def test_appends_one_line_with_expected_fields(self, tmp_path, monkeypatch):
        """Helper appends exactly one line with timestamp, event tag, project, sid, outcome, msgs, dur, detail."""
        monkeypatch.setenv("HOME", str(tmp_path))
        # Re-import obsidian_utils so the HOME-derived log path is picked up if cached.
        import importlib
        import obsidian_utils
        importlib.reload(obsidian_utils)

        obsidian_utils._append_sessionend_log(
            project="myproj",
            session_id="sid-deadbeef-1234",
            outcome="OK_RAW_NOTE_ONLY",
            msgs=42,
            dur_min=12.5,
            detail="",
        )

        log_path = tmp_path / ".claude" / "obsidian-brain-hook.log"
        assert log_path.exists(), "telemetry log file was not created"
        content = log_path.read_text(encoding="utf-8")
        lines = [ln for ln in content.splitlines() if ln.strip()]
        assert len(lines) == 1, f"expected 1 line, got {len(lines)}: {content!r}"

        line = lines[0]
        # Format: "<UTC ISO8601> SessionEnd project=<p> sid=<s8> outcome=<O> msgs=<N> dur=<F> detail=<str>"
        assert "SessionEnd" in line
        assert "project=myproj" in line
        # sid is short-form (8 chars)
        assert "sid=sid-dead" in line
        assert "outcome=OK_RAW_NOTE_ONLY" in line
        assert "msgs=42" in line
        # dur is formatted with one decimal
        assert "dur=12.5" in line
        # detail field is present even when empty
        assert "detail=" in line

    def test_rotates_when_log_exceeds_cap(self, tmp_path, monkeypatch):
        """When log exceeds 100KB, helper rotates to .1 and starts a new file."""
        monkeypatch.setenv("HOME", str(tmp_path))
        import importlib
        import obsidian_utils
        importlib.reload(obsidian_utils)

        log_dir = tmp_path / ".claude"
        log_dir.mkdir()
        log_path = log_dir / "obsidian-brain-hook.log"
        # Pre-fill the log past the rotation threshold.
        log_path.write_text("x" * (150 * 1024), encoding="utf-8")

        obsidian_utils._append_sessionend_log(
            project="rot", session_id="sid-rotate-aa", outcome="OK_RAW_NOTE_ONLY"
        )

        rotated = log_dir / "obsidian-brain-hook.log.1"
        assert rotated.exists(), "rotated file was not created"
        # New log file contains only the one fresh line.
        new_content = log_path.read_text(encoding="utf-8")
        new_lines = [ln for ln in new_content.splitlines() if ln.strip()]
        assert len(new_lines) == 1, f"new log should have only the single fresh line, got: {new_content!r}"
        assert "outcome=OK_RAW_NOTE_ONLY" in new_lines[0]

    def test_handles_missing_fields_with_defaults(self, tmp_path, monkeypatch):
        """Helper accepts only the required args (project, session_id, outcome) and uses safe defaults."""
        monkeypatch.setenv("HOME", str(tmp_path))
        import importlib
        import obsidian_utils
        importlib.reload(obsidian_utils)

        obsidian_utils._append_sessionend_log(
            project="", session_id="", outcome="EXCEPTION"
        )

        log_path = tmp_path / ".claude" / "obsidian-brain-hook.log"
        content = log_path.read_text(encoding="utf-8")
        # Empty project becomes "unknown"; empty sid becomes "unknown"[:8]
        assert "project=unknown" in content
        assert "sid=unknown" in content
        assert "outcome=EXCEPTION" in content
        assert "msgs=0" in content
        assert "dur=0.0" in content
