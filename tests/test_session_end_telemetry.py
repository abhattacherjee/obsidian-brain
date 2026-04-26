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


# ---------------------------------------------------------------------------
# Outcome wrapping — subprocess-driven (drives the real hook entry point)
# ---------------------------------------------------------------------------


def _read_log_lines(tmp_path):
    """Helper: return the SessionEnd lines from the hook log, or [] if missing."""
    log_path = tmp_path / ".claude" / "obsidian-brain-hook.log"
    if not log_path.exists():
        return []
    return [
        ln for ln in log_path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and "SessionEnd" in ln
    ]


def _run_session_end(tmp_path, payload):
    """Spawn the SessionEnd hook with HOME redirected to tmp_path."""
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    return subprocess.run(
        [sys.executable, _hook_script_path()],
        input=json.dumps(payload) if payload is not None else "",
        capture_output=True,
        text=True,
        env=env,
    )


def _projects_dir(tmp_path, project_slug):
    """Create a fake CC projects dir for a given project so transcript_path passes validation."""
    # Claude Code's path-encoded slug: leading dash, underscores->hyphens
    cc_slug = "-" + project_slug.replace("_", "-")
    d = tmp_path / ".claude" / "projects" / cc_slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_config(tmp_path, **overrides):
    cfg = {
        "vault_path": str(tmp_path / "vault"),
        "sessions_folder": "claude-sessions",
        "auto_log_enabled": True,
        "min_messages": 3,
        "min_duration_minutes": 2,
    }
    cfg.update(overrides)
    cfg_path = tmp_path / ".claude" / "obsidian-brain-config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    # Make sure the vault exists if vault_path is set so write attempts don't fail elsewhere.
    if cfg.get("vault_path"):
        (Path(cfg["vault_path"]) / cfg["sessions_folder"]).mkdir(parents=True, exist_ok=True)
    return cfg_path


def _make_jsonl(path, n_user_msgs, duration_sec):
    """Create a minimal JSONL with N user messages spanning duration_sec seconds."""
    import datetime as _dt
    start = _dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=_dt.timezone.utc)
    entries = []
    for i in range(n_user_msgs):
        ts = start + _dt.timedelta(seconds=i * (duration_sec / max(n_user_msgs, 1)))
        entries.append({
            "type": "user",
            "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "message": {"role": "user", "content": f"msg {i}"},
        })
    # Final assistant message at the end so duration metadata reflects duration_sec.
    end = start + _dt.timedelta(seconds=duration_sec)
    entries.append({
        "type": "assistant",
        "timestamp": end.isoformat().replace("+00:00", "Z"),
        "message": {"role": "assistant", "content": "ok"},
    })
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


class TestInvalidInputOutcomes:
    def test_empty_stdin_logs_skipped_invalid_input(self, tmp_path):
        result = _run_session_end(tmp_path, payload=None)
        assert result.returncode == 0
        lines = _read_log_lines(tmp_path)
        assert len(lines) == 1, f"expected one SessionEnd line, got {lines!r}"
        assert "outcome=SKIPPED_INVALID_INPUT" in lines[0]

    def test_missing_session_id_logs_skipped_invalid_input(self, tmp_path):
        # cwd present, transcript_path present (inside projects dir), no session_id
        projects_dir = tmp_path / ".claude" / "projects" / "-myproj"
        projects_dir.mkdir(parents=True, exist_ok=True)
        fake_transcript = projects_dir / "fake.jsonl"
        fake_transcript.write_text("{}\n", encoding="utf-8")
        payload = {
            "cwd": str(tmp_path),
            "transcript_path": str(fake_transcript),
        }
        result = _run_session_end(tmp_path, payload=payload)
        assert result.returncode == 0
        lines = _read_log_lines(tmp_path)
        assert len(lines) == 1
        assert "outcome=SKIPPED_INVALID_INPUT" in lines[0]

    def test_transcript_outside_projects_logs_outside_projects_outcome(self, tmp_path):
        # transcript_path that does NOT live under ~/.claude/projects
        bogus_transcript = tmp_path / "evil" / "transcript.jsonl"
        bogus_transcript.parent.mkdir(parents=True, exist_ok=True)
        bogus_transcript.write_text("{}\n", encoding="utf-8")

        payload = {
            "cwd": str(tmp_path),
            "session_id": "sid-outside-1234",
            "transcript_path": str(bogus_transcript),
        }
        result = _run_session_end(tmp_path, payload=payload)
        assert result.returncode == 0
        lines = _read_log_lines(tmp_path)
        assert len(lines) == 1, f"expected one SessionEnd line, got {lines!r}"
        assert "outcome=SKIPPED_TRANSCRIPT_OUTSIDE_PROJECTS" in lines[0]


class TestConfigStateOutcomes:
    def test_auto_log_disabled_logs_skipped_auto_log_off(self, tmp_path):
        proj = _projects_dir(tmp_path, "myproj")
        transcript = proj / "sid-auto-off-12345.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        _write_config(tmp_path, auto_log_enabled=False)

        payload = {
            "cwd": str(tmp_path),  # cwd's basename derives the project slug
            "session_id": "sid-auto-off-12345",
            "transcript_path": str(transcript),
        }
        result = _run_session_end(tmp_path, payload=payload)
        assert result.returncode == 0
        lines = _read_log_lines(tmp_path)
        assert len(lines) == 1
        assert "outcome=SKIPPED_AUTO_LOG_OFF" in lines[0]

    def test_no_vault_path_logs_skipped_no_vault(self, tmp_path):
        proj = _projects_dir(tmp_path, "myproj")
        transcript = proj / "sid-no-vault-12345.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        _write_config(tmp_path, vault_path="")

        payload = {
            "cwd": str(tmp_path),
            "session_id": "sid-no-vault-12345",
            "transcript_path": str(transcript),
        }
        result = _run_session_end(tmp_path, payload=payload)
        assert result.returncode == 0
        lines = _read_log_lines(tmp_path)
        assert len(lines) == 1
        assert "outcome=SKIPPED_NO_VAULT" in lines[0]

    def test_empty_transcript_logs_skipped_no_transcript(self, tmp_path):
        proj = _projects_dir(tmp_path, "myproj")
        transcript = proj / "sid-empty-12345678.jsonl"
        transcript.write_text("", encoding="utf-8")  # truly empty
        _write_config(tmp_path)

        payload = {
            "cwd": str(tmp_path),
            "session_id": "sid-empty-12345678",
            "transcript_path": str(transcript),
        }
        result = _run_session_end(tmp_path, payload=payload)
        assert result.returncode == 0
        lines = _read_log_lines(tmp_path)
        assert len(lines) == 1
        assert "outcome=SKIPPED_NO_TRANSCRIPT" in lines[0]


class TestThresholdOutcomes:
    def test_too_few_messages_logs_skipped_below_threshold(self, tmp_path):
        # min_messages=3 in config; provide only 2 user messages
        proj = _projects_dir(tmp_path, "myproj")
        transcript = proj / "sid-fewmsg-12345.jsonl"
        _make_jsonl(transcript, n_user_msgs=2, duration_sec=600)
        _write_config(tmp_path)

        payload = {
            "cwd": str(tmp_path),
            "session_id": "sid-fewmsg-12345",
            "transcript_path": str(transcript),
        }
        result = _run_session_end(tmp_path, payload=payload)
        assert result.returncode == 0
        lines = _read_log_lines(tmp_path)
        assert len(lines) == 1
        assert "outcome=SKIPPED_BELOW_THRESHOLD" in lines[0]
        # Should record actual msg count for diagnosis
        assert "msgs=2" in lines[0]

    def test_below_duration_logs_skipped_below_threshold(self, tmp_path):
        # min_duration_minutes=2 in config; provide 5 messages over 10 seconds
        proj = _projects_dir(tmp_path, "myproj")
        transcript = proj / "sid-shortdur-1234.jsonl"
        _make_jsonl(transcript, n_user_msgs=5, duration_sec=10)
        _write_config(tmp_path)

        payload = {
            "cwd": str(tmp_path),
            "session_id": "sid-shortdur-1234",
            "transcript_path": str(transcript),
        }
        result = _run_session_end(tmp_path, payload=payload)
        assert result.returncode == 0
        lines = _read_log_lines(tmp_path)
        assert len(lines) == 1
        assert "outcome=SKIPPED_BELOW_THRESHOLD" in lines[0]
        assert "msgs=5" in lines[0]


class TestWriteFailedOutcome:
    def test_write_failure_logs_write_failed(self, tmp_path):
        """When the vault sessions folder is unwritable, _run logs WRITE_FAILED."""
        # Set up a valid above-threshold session
        cc_slug = "-myproj"
        proj = tmp_path / ".claude" / "projects" / cc_slug
        proj.mkdir(parents=True)
        transcript = proj / "sid-writefail-12.jsonl"
        _make_jsonl(transcript, n_user_msgs=10, duration_sec=600)

        # Vault path points at a directory we can read but cannot write to.
        vault = tmp_path / "vault"
        vault.mkdir()
        sessions = vault / "claude-sessions"
        sessions.mkdir()
        # Make the sessions folder read-only so write_vault_note() fails.
        os.chmod(sessions, 0o500)

        cfg = {
            "vault_path": str(vault),
            "sessions_folder": "claude-sessions",
            "auto_log_enabled": True,
            "min_messages": 3,
            "min_duration_minutes": 2,
        }
        cfg_path = tmp_path / ".claude" / "obsidian-brain-config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        payload = {
            "cwd": str(tmp_path),
            "session_id": "sid-writefail-12",
            "transcript_path": str(transcript),
        }
        try:
            result = _run_session_end(tmp_path, payload=payload)
            assert result.returncode == 0
            lines = _read_log_lines(tmp_path)
            assert len(lines) == 1, f"expected one line, got {lines!r}"
            assert "outcome=WRITE_FAILED" in lines[0]
        finally:
            # Restore permissions so pytest's tmp_path cleanup can rm it.
            os.chmod(sessions, 0o700)


class TestSuccessOutcome:
    def test_successful_write_logs_ok_raw_note_only(self, tmp_path):
        """A normal above-threshold session that writes a vault note logs OK_RAW_NOTE_ONLY."""
        cc_slug = "-myproj"
        proj = tmp_path / ".claude" / "projects" / cc_slug
        proj.mkdir(parents=True)
        transcript = proj / "sid-success-1234.jsonl"
        _make_jsonl(transcript, n_user_msgs=10, duration_sec=600)

        vault = tmp_path / "vault"
        (vault / "claude-sessions").mkdir(parents=True)

        cfg = {
            "vault_path": str(vault),
            "sessions_folder": "claude-sessions",
            "auto_log_enabled": True,
            "min_messages": 3,
            "min_duration_minutes": 2,
        }
        cfg_path = tmp_path / ".claude" / "obsidian-brain-config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        payload = {
            "cwd": str(tmp_path),
            "session_id": "sid-success-1234",
            "transcript_path": str(transcript),
        }
        result = _run_session_end(tmp_path, payload=payload)
        assert result.returncode == 0
        lines = _read_log_lines(tmp_path)
        assert len(lines) == 1, f"expected one line, got {lines!r}"
        assert "outcome=OK_RAW_NOTE_ONLY" in lines[0]
        assert "msgs=10" in lines[0]
        # A vault note was actually written:
        notes = list((vault / "claude-sessions").glob("*.md"))
        assert len(notes) == 1, f"expected one vault note, got {[n.name for n in notes]}"
