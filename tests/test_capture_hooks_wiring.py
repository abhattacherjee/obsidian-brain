"""Tests that the lifecycle hooks drive auto-capture (#425)."""

import io
import json
import os
from pathlib import Path

import pytest

import capture_cli
import capture_state
import capture_worker
import obsidian_context_snapshot
import obsidian_session_hint
import obsidian_session_log
import obsidian_utils

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def launches(monkeypatch):
    calls = []
    monkeypatch.setattr(capture_worker, "launch", lambda *a, **k: calls.append((a, k)) or True)
    return calls


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def test_sessionend_launches_capture(monkeypatch, tmp_path, launches):
    t = tmp_path / f"{SID}.jsonl"
    monkeypatch.setattr(obsidian_utils, "canonical_project_name", lambda cwd=None: "demo")
    _stdin(monkeypatch, {"session_id": SID, "cwd": str(tmp_path), "transcript_path": str(t)})
    obsidian_session_log._run()
    assert launches == [((SID, str(t), "demo"), {"if_due": False})]


def test_sessionend_no_launch_without_session(monkeypatch, launches):
    _stdin(monkeypatch, {})
    obsidian_session_log._run()
    assert launches == []


def test_sessionend_unknown_project_falls_back(monkeypatch, tmp_path, launches):
    monkeypatch.setattr(obsidian_utils, "canonical_project_name", lambda cwd=None: "unknown")
    obsidian_session_log._launch_capture({"cwd": str(tmp_path)}, SID)
    assert launches[0][0] == (SID, None, None)


def test_sessionend_no_launch_when_disabled(monkeypatch, tmp_path, mock_config):
    monkeypatch.delenv(capture_worker.DISABLE_ENV_VAR, raising=False)
    monkeypatch.setattr(capture_worker, "_TRANSCRIPTS_ROOT", tmp_path)
    (tmp_path / "x.jsonl").write_text("{}\n")
    spawned = []
    monkeypatch.setattr(capture_worker.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(obsidian_utils, "load_config", lambda fresh=False: {**mock_config, "auto_capture_enabled": False})
    obsidian_session_log._launch_capture({"transcript_path": str(tmp_path / "x.jsonl")}, SID)
    assert spawned == []


def test_suite_never_spawns_workers():
    assert __import__("os").environ.get(capture_worker.DISABLE_ENV_VAR) == "1"


def test_sessionend_capture_failure_is_contained(monkeypatch, capsys):
    monkeypatch.setattr(capture_worker, "launch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    obsidian_session_log._launch_capture({}, SID)
    assert "capture launch failed: boom" in capsys.readouterr().err


@pytest.fixture
def sweeps(monkeypatch):
    calls = []
    monkeypatch.setattr(capture_cli, "sweep", lambda limit=3, exclude=None: calls.append(exclude) or [])
    monkeypatch.setattr(obsidian_session_hint, "load_config", lambda: {})
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")  # an interactive start
    return calls


def test_sessionstart_writes_cli_path(monkeypatch, sweeps):
    _stdin(monkeypatch, {"session_id": SID, "cwd": "/tmp"})
    obsidian_session_hint._run()
    f = capture_state.capture_dir() / "cli_path"
    assert f.read_text() == str(capture_cli.Path(capture_cli.__file__).resolve())
    assert f.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("winner, expected", [(True, [SID]), (False, [])])
def test_sessionstart_runs_sweep_once(monkeypatch, sweeps, winner, expected):
    monkeypatch.setattr(obsidian_session_hint, "claim_hook_run", lambda *a, **k: winner)
    _stdin(monkeypatch, {"session_id": SID, "cwd": "/tmp"})
    obsidian_session_hint._run()
    assert sweeps == expected


def test_sessionstart_headless_does_not_sweep(monkeypatch, sweeps):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-py")
    obsidian_session_hint._capture_startup(SID)
    assert sweeps == []
    assert (capture_state.capture_dir() / "cli_path").exists()  # the mod can still find the CLI


def test_capture_log_is_isolated(tmp_path):
    import pwd

    capture_state.append_capture_log("demo", SID, "saved:1")
    real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".claude" / "obsidian-brain-hook.log"
    assert capture_state._log_dir() != real.parent
    assert "Capture project=demo" in (capture_state._log_dir() / "obsidian-brain-hook.log").read_text()


def test_sessionstart_sweep_respects_disabled(monkeypatch, sweeps):
    monkeypatch.setattr(obsidian_session_hint, "load_config", lambda: {"auto_capture_enabled": False})
    obsidian_session_hint._capture_startup(SID)
    assert sweeps == []


def test_precompact_sets_flush(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    t = tmp_path / ".claude" / "projects" / "-demo" / f"{SID}.jsonl"
    t.parent.mkdir(parents=True)
    t.write_text("{}\n")
    monkeypatch.setattr(obsidian_context_snapshot, "claim_hook_run", lambda *a, **k: True)
    monkeypatch.setattr(obsidian_context_snapshot, "load_config", lambda: {})
    _stdin(monkeypatch, {"session_id": SID, "cwd": str(tmp_path), "transcript_path": str(t)})
    obsidian_context_snapshot._run()
    assert capture_state.pop_flush(SID) is True
