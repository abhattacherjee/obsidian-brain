"""Unit tests for the cross-plugin hook dedup guard (claim_hook_run)."""
import datetime as _dt
import importlib
import io
import json
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import obsidian_utils


@pytest.fixture
def lock_dir(tmp_path, monkeypatch):
    secure = str(tmp_path / "obsidian-brain")
    monkeypatch.setattr("obsidian_utils._SECURE_DIR", secure)
    monkeypatch.setattr("obsidian_utils._LOCK_DIR", os.path.join(secure, "locks"))
    return os.path.join(secure, "locks")


def test_first_claim_succeeds(lock_dir):
    assert obsidian_utils.claim_hook_run("SessionEnd", "abc123") is True


def test_second_claim_within_ttl_fails(lock_dir):
    assert obsidian_utils.claim_hook_run("SessionEnd", "abc123") is True
    assert obsidian_utils.claim_hook_run("SessionEnd", "abc123") is False


def test_different_event_same_sid_not_blocked(lock_dir):
    assert obsidian_utils.claim_hook_run("SessionEnd", "abc123") is True
    assert obsidian_utils.claim_hook_run("PreCompact", "abc123") is True


def test_different_sid_same_event_not_blocked(lock_dir):
    assert obsidian_utils.claim_hook_run("SessionStart", "sid-one") is True
    assert obsidian_utils.claim_hook_run("SessionStart", "sid-two") is True


def test_claim_after_ttl_reclaims(lock_dir):
    assert obsidian_utils.claim_hook_run("SessionStart", "abc123", ttl_seconds=1) is True
    lock_path = os.path.join(lock_dir, "abc123-SessionStart")
    old = time.time() - 5
    os.utime(lock_path, (old, old))
    assert obsidian_utils.claim_hook_run("SessionStart", "abc123", ttl_seconds=1) is True


def test_empty_session_id_proceeds(lock_dir):
    assert obsidian_utils.claim_hook_run("SessionStart", "") is True


def test_lock_file_is_0o600(lock_dir):
    obsidian_utils.claim_hook_run("SessionEnd", "abc123")
    lock_path = os.path.join(lock_dir, "abc123-SessionEnd")
    mode = stat.S_IMODE(os.stat(lock_path).st_mode)
    assert mode == 0o600


def test_session_id_sanitized_into_filename(lock_dir):
    obsidian_utils.claim_hook_run("SessionEnd", "../../etc/passwd")
    names = os.listdir(lock_dir)
    assert len(names) == 1
    assert names[0] == "______etc_passwd-SessionEnd"


def test_fail_open_when_lock_dir_unwritable(tmp_path, monkeypatch):
    # Point the lock dir at a path whose parent is a file -> makedirs raises.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setattr("obsidian_utils._LOCK_DIR", str(blocker / "locks"))
    assert obsidian_utils.claim_hook_run("SessionEnd", "abc123") is True


def test_concurrent_claims_exactly_one_winner(lock_dir):
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: obsidian_utils.claim_hook_run("SessionEnd", "race"), range(8)))
    assert results.count(True) == 1
    assert results.count(False) == 7


def test_cleanup_removes_old_locks(lock_dir):
    obsidian_utils.claim_hook_run("SessionEnd", "old-sid")
    stale = os.path.join(lock_dir, "old-sid-SessionEnd")
    old = time.time() - (3 * 24 * 3600)
    os.utime(stale, (old, old))
    # A fresh claim for a different key triggers opportunistic cleanup.
    obsidian_utils.claim_hook_run("SessionEnd", "new-sid")
    assert not os.path.exists(stale)


def test_sessionend_has_dedup_outcome_constant():
    import obsidian_session_log
    assert obsidian_session_log._Outcome.SKIPPED_DEDUP == "SKIPPED_DEDUP"


def _make_jsonl(path, n_user_msgs, duration_sec):
    """Minimal JSONL with N user messages spanning duration_sec seconds."""
    start = _dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=_dt.timezone.utc)
    entries = []
    for i in range(n_user_msgs):
        ts = start + _dt.timedelta(seconds=i * (duration_sec / max(n_user_msgs, 1)))
        entries.append({
            "type": "user",
            "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "message": {"role": "user", "content": f"msg {i}"},
        })
    end = start + _dt.timedelta(seconds=duration_sec)
    entries.append({
        "type": "assistant",
        "timestamp": end.isoformat().replace("+00:00", "Z"),
        "message": {"role": "assistant", "content": "ok"},
    })
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def test_sessionend_loser_path_logs_dedup_and_skips_write(tmp_path, monkeypatch):
    """When claim_hook_run returns False, _run() must log SKIPPED_DEDUP and
    NOT write a vault note — proves the guard is wired into the write path.

    Sanity check: flip the guard to always-proceed (lambda: True) and this
    test fails (a note is written, no SKIPPED_DEDUP line) — confirming it
    exercises the wiring, not a tautology.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    import obsidian_session_log
    importlib.reload(obsidian_utils)
    importlib.reload(obsidian_session_log)

    # Above-threshold session so the run reaches the guard (not a skip path).
    cc_slug = "-myproj"
    proj = tmp_path / ".claude" / "projects" / cc_slug
    proj.mkdir(parents=True)
    transcript = proj / "sid-dedup-1234.jsonl"
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

    # Force the loser path and spy on write_vault_note.
    write_calls = []
    monkeypatch.setattr(obsidian_session_log, "claim_hook_run", lambda *a, **kw: False)
    monkeypatch.setattr(
        obsidian_session_log, "write_vault_note",
        lambda *a, **kw: write_calls.append((a, kw)),
    )

    payload = json.dumps({
        "cwd": str(tmp_path),
        "session_id": "sid-dedup-1234",
        "transcript_path": str(transcript),
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with pytest.raises(SystemExit) as exc_info:
        obsidian_session_log.main()
    assert exc_info.value.code == 0

    # No vault write happened on the loser path.
    assert write_calls == [], f"expected zero writes, got {write_calls!r}"
    # And no actual note file landed in the vault either.
    notes = list((vault / "claude-sessions").glob("*.md"))
    assert notes == [], f"expected no vault note, got {[n.name for n in notes]}"

    # Telemetry logged SKIPPED_DEDUP exactly once.
    log_path = tmp_path / ".claude" / "obsidian-brain-hook.log"
    assert log_path.exists(), "hook log was not created"
    lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines()
             if "SessionEnd" in ln]
    assert len(lines) == 1, f"expected one SessionEnd line, got {lines!r}"
    assert "outcome=SKIPPED_DEDUP" in lines[0], f"got: {lines[0]!r}"

    # Restore module state for subsequent tests.
    monkeypatch.undo()
    importlib.reload(obsidian_utils)
    importlib.reload(obsidian_session_log)


def test_sessionstart_guard_importable_and_single_install_proceeds(lock_dir):
    import obsidian_session_hint
    assert hasattr(obsidian_session_hint, "claim_hook_run")
    # Single-install: first claim wins -> caller proceeds.
    assert obsidian_session_hint.claim_hook_run("SessionStart", "sid-x") is True
