"""Tests for hooks/capture_worker.py (#425). No test runs a real claude -p."""

import json
import subprocess
import time
from types import SimpleNamespace

import pytest

import capture_state
import capture_worker

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _row(text, role="user"):
    if role == "user":
        return {"type": "user", "message": {"role": "user", "content": text}}
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    (root / "-demo").mkdir(parents=True)
    path = root / "-demo" / f"{SID}.jsonl"
    path.write_text("".join(json.dumps(_row(f"message {i} " + "x" * 200)) + "\n" for i in range(6)))
    monkeypatch.setattr(capture_worker, "_TRANSCRIPTS_ROOT", root)
    return path


def _envelope(items):
    return json.dumps({"type": "result", "is_error": False, "result": json.dumps(items)})


@pytest.fixture
def fake_claude(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(SimpleNamespace(argv=argv, **kw))
        out = fake_claude.outputs.pop(0) if fake_claude.outputs else _envelope([])
        if isinstance(out, BaseException):
            raise out
        rc, stdout = out if isinstance(out, tuple) else (0, out)
        return subprocess.CompletedProcess(argv, rc, stdout, "")

    fake_claude.outputs = []
    fake_claude.calls = calls
    monkeypatch.setattr(capture_worker.subprocess, "run", run)
    return fake_claude


@pytest.fixture
def saves(monkeypatch):
    saved = []

    def save(c, **kw):
        if c.get("type") == "poem":
            raise ValueError("bad type")
        saved.append((c, kw))
        return {"id": "x"}

    monkeypatch.setattr(capture_worker.capture_save, "save_candidate", save)
    monkeypatch.setattr(capture_worker.capture_save, "session_note_for", lambda sid, p, c: "note")
    return saved


@pytest.fixture(autouse=True)
def _allow_launch(monkeypatch):
    monkeypatch.delenv(capture_worker.DISABLE_ENV_VAR, raising=False)


CAND = {"type": "insight", "title": "t", "body": "b", "topic": "x"}


def test_build_prompt_sentinel_and_placeholders():
    p = capture_worker.build_prompt("USER: hi", "demo", ["Old junk"])
    assert p.splitlines()[0] == capture_worker.SENTINEL
    assert "USER: hi" in p and '"demo"' in p and "Old junk" in p and "{{" not in p
    assert "rejected these recent captures" not in capture_worker.build_prompt("x", "demo", [])


def test_worker_argv_has_no_tools_and_isolated_settings():
    argv = capture_worker.worker_argv()
    assert argv[:2] == ["claude", "-p"] and "sonnet" in argv
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" in argv
    assert {"Bash", "Write", "Edit", "Agent"} <= set(argv[argv.index("--disallowedTools") + 1].split(","))


@pytest.mark.parametrize("stdout, n", [
    (_envelope([CAND]), 1),
    (json.dumps({"result": "Here you go:\n```json\n" + json.dumps([CAND]) + "\n```"}), 1),
    (json.dumps({"result": "Notes: " + json.dumps([CAND, CAND]) + " done"}), 2),
    (_envelope([CAND, {"type": "insight"}, "junk"]), 1),  # incomplete items dropped
    (_envelope([]), 0),
])
def test_parse_candidates(stdout, n):
    assert len(capture_worker.parse_candidates(stdout)) == n


@pytest.mark.parametrize("stdout", [
    "not json at all",
    json.dumps({"is_error": True, "result": "rate limited"}),
    json.dumps({"result": "I found nothing worth noting."}),
])
def test_parse_candidates_failures(stdout):
    with pytest.raises(ValueError):
        capture_worker.parse_candidates(stdout)


def test_run_batch_saves_and_advances(transcript, fake_claude, saves, mock_config):
    fake_claude.outputs = [_envelope([CAND, CAND])]
    out = capture_worker.run_batch(SID, transcript, "demo", mock_config)
    assert out == "saved:2"
    st = capture_state.get_state(SID)
    assert st["offset"] == transcript.stat().st_size and st["project"] == "demo" and st["last_batch_at"] > 0
    call = fake_claude.calls[0]
    assert call.env.get("OBSIDIAN_BRAIN_WORKER") == "1" and call.input.startswith(capture_worker.SENTINEL)
    assert not capture_state.claim_is_live(SID)  # released


@pytest.mark.parametrize("failure, outcome", [
    ((1, ""), "worker-failed"),
    (subprocess.TimeoutExpired("claude", 600), "worker-failed"),
    ((0, "garbage"), "parse-failed"),
])
def test_run_batch_failure_keeps_offset(transcript, fake_claude, saves, mock_config, failure, outcome):
    fake_claude.outputs = [failure]
    assert capture_worker.run_batch(SID, transcript, "demo", mock_config) == outcome
    assert capture_state.get_state(SID)["offset"] == 0
    assert saves == [] and not capture_state.claim_is_live(SID)


def test_save_error_keeps_offset(transcript, fake_claude, monkeypatch, mock_config):
    monkeypatch.setattr(capture_worker.capture_save, "session_note_for", lambda *a: "")
    monkeypatch.setattr(capture_worker.capture_save, "save_candidate",
                        lambda c, **kw: (_ for _ in ()).throw(PermissionError("vault read-only")))
    fake_claude.outputs = [_envelope([CAND])]
    assert capture_worker.run_batch(SID, transcript, "demo", mock_config) == "save-failed"
    assert capture_state.get_state(SID)["offset"] == 0


def test_bad_candidate_is_skipped(transcript, fake_claude, saves, mock_config):
    fake_claude.outputs = [_envelope([{**CAND, "type": "poem"}, CAND])]
    assert capture_worker.run_batch(SID, transcript, "demo", mock_config) == "saved:1"
    assert capture_state.get_state(SID)["offset"] == transcript.stat().st_size


def test_three_slice_cap(transcript, fake_claude, saves, mock_config, monkeypatch):
    real = capture_worker.capture_slice.read_slice
    monkeypatch.setattr(capture_worker.capture_slice, "read_slice", lambda p, s: real(p, s, max_bytes=400))
    out = capture_worker.run_batch(SID, transcript, "demo", mock_config)
    assert out == "saved:0" and len(fake_claude.calls) == 3
    assert 0 < capture_state.get_state(SID)["offset"] < transcript.stat().st_size


def test_claimed_session_is_skipped(transcript, fake_claude, saves, mock_config):
    assert capture_state.claim(SID)
    assert capture_worker.run_batch(SID, transcript, "demo", mock_config) == "claimed"
    assert fake_claude.calls == [] and capture_state.claim_is_live(SID)


def test_nothing_new_is_empty(transcript, fake_claude, saves, mock_config):
    capture_state.set_state(SID, offset=transcript.stat().st_size)
    assert capture_worker.run_batch(SID, transcript, "demo", mock_config) == "empty"
    assert fake_claude.calls == []


def test_is_due_first_sighting_then_interval(transcript, monkeypatch):
    assert capture_worker.is_due(SID, 10, transcript) is False
    assert capture_state.get_state(SID)["last_batch_at"] > 0
    assert capture_worker.is_due(SID, 10, transcript) is False
    capture_state.set_state(SID, last_batch_at=time.time() - 601)
    assert capture_worker.is_due(SID, 10, transcript) is True


def test_is_due_flush_and_no_growth(transcript):
    capture_state.set_state(SID, last_batch_at=time.time())
    capture_state.set_flush(SID)
    assert capture_worker.is_due(SID, 10, transcript) is True
    assert capture_state.pop_flush(SID) is False  # consumed
    capture_state.set_state(SID, offset=transcript.stat().st_size, last_batch_at=1.0)
    assert capture_worker.is_due(SID, 10, transcript) is False


@pytest.fixture
def popen(monkeypatch):
    calls = []
    monkeypatch.setattr(capture_worker.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    return calls


def test_launch_detaches(transcript, popen, mock_config):
    assert capture_worker.launch(SID, transcript, "demo", if_due=False, config=mock_config) is True
    argv, kw = popen[0]
    assert argv[-4:] == ["run-batch", SID, str(transcript), "demo"]
    assert kw["start_new_session"] is True and kw["stdin"] == subprocess.DEVNULL


def test_launch_refuses(transcript, popen, mock_config, monkeypatch, tmp_path):
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text("{}\n")
    assert capture_worker.launch(SID, outside, "demo", if_due=False, config=mock_config) is False
    assert capture_worker.launch(SID, transcript, "demo", if_due=False,
                                 config={**mock_config, "auto_capture_enabled": False}) is False
    assert capture_worker.launch(SID, transcript, "demo", if_due=True, config=mock_config) is False  # first sighting
    capture_state.claim(SID)
    assert capture_worker.launch(SID, transcript, "demo", if_due=False, config=mock_config) is False
    capture_state.release(SID)
    monkeypatch.setenv("OBSIDIAN_BRAIN_WORKER", "1")
    assert capture_worker.launch(SID, transcript, "demo", if_due=False, config=mock_config) is False
    monkeypatch.delenv("OBSIDIAN_BRAIN_WORKER")
    monkeypatch.setenv(capture_worker.DISABLE_ENV_VAR, "1")
    assert capture_worker.launch(SID, transcript, "demo", if_due=False, config=mock_config) is False
    assert popen == []


def test_launch_uses_stored_path(transcript, popen, mock_config):
    capture_state.set_state(SID, transcript_path=str(transcript), project="demo")
    assert capture_worker.launch(SID, None, None, if_due=False, config=mock_config) is True
    assert popen[0][0][-2:] == [str(transcript), "demo"]


def test_launch_never_raises(transcript, mock_config, monkeypatch):
    monkeypatch.setattr(capture_worker.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert capture_worker.launch(SID, transcript, "demo", if_due=False, config=mock_config) is False


def test_launch_derives_project_from_transcript_cwd(tmp_path, popen, mock_config, monkeypatch):
    root = tmp_path / "projects"
    (root / "-w").mkdir(parents=True)
    t = root / "-w" / f"{SID}.jsonl"
    t.write_text(json.dumps({"type": "user", "cwd": str(tmp_path / "gone" / "myproj"),
                             "message": {"role": "user", "content": "hi"}}) + "\n")
    monkeypatch.setattr(capture_worker, "_TRANSCRIPTS_ROOT", root)
    assert capture_worker.launch(SID, t, None, if_due=False, config=mock_config) is True
    assert popen[0][0][-1] == "myproj"
    assert capture_state.get_state(SID)["project"] == "myproj"


def test_project_for_existing_cwd_is_canonical(tmp_path, monkeypatch):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "user", "cwd": str(tmp_path)}) + "\n")
    monkeypatch.setattr(capture_worker.obsidian_utils, "canonical_project_name", lambda cwd=None: "canon")
    assert capture_worker.project_for(t) == "canon"
    t.write_text("{}\n")
    assert capture_worker.project_for(t) == "unknown"
