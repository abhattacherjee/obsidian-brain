"""Tests for hooks/capture_cli.py (#425)."""

import json
import os
import time

import pytest

import capture_cli
import capture_state
import capture_worker

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _user(text, cwd="/nonexistent/demo"):
    return json.dumps({"type": "user", "cwd": cwd, "message": {"role": "user", "content": text}}) + "\n"


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "projects"
    (r / "-demo").mkdir(parents=True)
    monkeypatch.setattr(capture_worker, "_TRANSCRIPTS_ROOT", r)
    capture_state.installed_at()  # install time = now
    return r


def _transcript(root, sid, n=3, first="hello", age=0.0):
    path = root / "-demo" / f"{sid}.jsonl"
    path.write_text(_user(first) + "".join(_user(f"msg {i}") for i in range(n - 1)))
    t = time.time() + 5 - age
    os.utime(path, (t, t))
    return path


def _sid(i):
    return f"{i:08d}-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_pending_oldest_first_limit(root):
    for i, age in enumerate([1.0, 3.0, 2.0]):
        _transcript(root, _sid(i), age=age)
    assert [s for s, _ in capture_cli.pending_sessions(2)] == [_sid(1), _sid(2)]


def test_pending_skips_worker_transcripts(root):
    _transcript(root, SID, first=capture_worker.SENTINEL + "\n\nprompt")
    assert capture_cli.pending_sessions(5) == []


def test_pending_skips_before_install(root):
    path = _transcript(root, SID)
    os.utime(path, (1.0, 1.0))
    assert capture_cli.pending_sessions(5) == []


def test_pending_skips_fully_processed(root):
    path = _transcript(root, SID)
    capture_state.set_state(SID, offset=path.stat().st_size)
    assert capture_cli.pending_sessions(5) == []


def test_pending_skips_short_claimed_and_excluded(root):
    _transcript(root, _sid(1), n=2)
    _transcript(root, _sid(2))
    _transcript(root, _sid(3))
    capture_state.claim(_sid(2))
    assert capture_cli.pending_sessions(5) == [(_sid(3), root / "-demo" / f"{_sid(3)}.jsonl")]
    assert capture_cli.pending_sessions(5, exclude=_sid(3)) == []


def test_project_for_missing_cwd_uses_basename(root):
    assert capture_worker.project_for(_transcript(root, SID)) == "demo"


def test_sweep_launches_pending(root, monkeypatch):
    monkeypatch.delenv(capture_worker.DISABLE_ENV_VAR)
    _transcript(root, SID)
    calls = []
    monkeypatch.setattr(capture_worker, "launch", lambda *a, **k: calls.append((a, k)) or True)
    assert capture_cli.sweep() == [SID]
    assert calls[0][0][2] is None and calls[0][1] == {"if_due": False}


def test_sweep_disabled_scans_nothing(root, monkeypatch):
    monkeypatch.setattr(capture_cli, "pending_sessions", lambda *a, **k: pytest.fail("scanned"))
    assert capture_cli.sweep() == []


def test_write_cli_path(root):
    capture_cli.write_cli_path()
    f = capture_state.capture_dir() / "cli_path"
    assert f.read_text() == str((capture_cli.Path(capture_cli.__file__)).resolve())
    assert f.stat().st_mode & 0o777 == 0o600


def _run(capsys, *argv):
    rc = capture_cli.main(list(argv))
    return rc, capsys.readouterr().out


def test_cli_shapes(root, capsys, monkeypatch, mock_config):
    monkeypatch.setattr(capture_worker, "launch", lambda *a, **k: False)
    monkeypatch.setattr(capture_worker, "run_batch", lambda *a, **k: "empty")
    assert _run(capsys, "launch", SID, "--if-due") == (0, '{"launched": false}\n')
    assert _run(capsys, "run-batch", SID, "/x.jsonl", "demo") == (0, '{"outcome": "empty"}\n')
    assert _run(capsys, "sweep") == (0, '{"launched": []}\n')
    cid = capture_state.ledger_append({"session_id": SID, "title": "t", "status": "active"})
    rc, out = _run(capsys, "ledger-list", "--session", SID)
    assert rc == 0 and json.loads(out)[0]["id"] == cid
    assert json.loads(_run(capsys, "status")[1]) == {"next_batch_min": None, "pending_sweep": 0}


def test_cli_status_next_batch(root, capsys, mock_config):
    capture_state.set_state(SID, last_batch_at=time.time() - 4 * 60)
    out = json.loads(_run(capsys, "status", "--session", SID)[1])
    assert out["next_batch_min"] == 6


def test_cli_reject_unknown_and_usage(root, capsys, mock_config):
    assert _run(capsys, "reject", "nope")[0] == 1
    assert capture_cli.main(["bogus"]) == 2
    assert capture_cli.main([]) == 2


def test_pending_skips_worker_cwd_dir(root):
    d = root / "-private-tmp-ob-secure-capture-worker-cwd"
    d.mkdir()
    path = d / f"{SID}.jsonl"
    path.write_text("".join(_user(f"m {i}") for i in range(4)))
    assert capture_cli.pending_sessions(5) == []
