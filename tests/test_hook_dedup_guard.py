"""Unit tests for the cross-plugin hook dedup guard (claim_hook_run)."""
import datetime as _dt
import importlib
import io
import json
import os
import stat
import subprocess
import sys as _sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
    # ...but a recent lock (the one the triggering claim just created) must be
    # preserved — a regression that pruned ALL locks would fail this assertion.
    recent = os.path.join(lock_dir, "new-sid-SessionEnd")
    assert os.path.exists(recent)


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


# ---------------------------------------------------------------------------
# Cross-process dedup simulation (two-process double-install scenario)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _dual_install_env(home: Path) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{_REPO_ROOT / 'hooks'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def test_two_concurrent_sessionend_fires_dedup_to_one_claim(tmp_path):
    """Two processes sharing HOME race on the same SessionEnd trigger; the
    lock under ~/.claude/obsidian-brain/locks/ must let exactly one proceed.

    We assert on the lock directory rather than a vault note so the test does
    not depend on a configured vault: both processes run claim_hook_run against
    the same shared HOME, and exactly one lock file ends up present.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = _dual_install_env(home)
    code = (
        "import obsidian_utils, sys; "
        "print('CLAIMED' if obsidian_utils.claim_hook_run('SessionEnd', 'race-sid') else 'SKIPPED')"
    )
    procs = [
        subprocess.Popen([_sys.executable, "-c", code], env=env,
                         stdout=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    outs = [p.communicate()[0].strip() for p in procs]
    assert outs.count("CLAIMED") == 1, f"expected exactly one CLAIMED, got {outs}"
    lock_dir = home / ".claude" / "obsidian-brain" / "locks"
    assert sorted(p.name for p in lock_dir.iterdir()) == ["race-sid-SessionEnd"]


# ---------------------------------------------------------------------------
# release_hook_run + fail-open branch coverage + telemetry (PR #197 review)
# ---------------------------------------------------------------------------


def test_release_hook_run_allows_reclaim_within_ttl(lock_dir):
    assert obsidian_utils.claim_hook_run("SessionEnd", "abc123") is True
    # Within TTL a second claim is normally blocked...
    assert obsidian_utils.claim_hook_run("SessionEnd", "abc123") is False
    # ...but releasing the lock lets the next fire re-claim immediately.
    obsidian_utils.release_hook_run("SessionEnd", "abc123")
    assert obsidian_utils.claim_hook_run("SessionEnd", "abc123") is True


def test_release_hook_run_empty_sid_is_noop(lock_dir):
    # No key to release; must not raise.
    obsidian_utils.release_hook_run("SessionEnd", "")


def test_release_hook_run_missing_lock_is_noop(lock_dir):
    # Releasing a never-claimed trigger is a safe no-op.
    obsidian_utils.release_hook_run("SessionEnd", "never-claimed")


def test_claim_fail_open_when_open_raises_oserror(lock_dir, monkeypatch):
    """The outer `except OSError` around _create() must fail open (return True)
    on a non-FileExistsError filesystem error, so a real FS fault never
    silently drops a hook."""
    import errno
    real_open = os.open

    def boom(path, *a, **kw):
        if isinstance(path, str) and path.startswith(lock_dir):
            raise OSError(errno.EMFILE, "too many open files")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(obsidian_utils.os, "open", boom)
    assert obsidian_utils.claim_hook_run("SessionEnd", "abc123") is True


def test_stale_reclaim_fail_open_when_recreate_raises(lock_dir, monkeypatch):
    """A stale lock whose unlink succeeds but whose re-create hits a non-
    FileExistsError OSError must fail open (return True) — exercises the
    re-claim OSError branch, not the outer one."""
    import errno
    assert obsidian_utils.claim_hook_run("SessionEnd", "abc123", ttl_seconds=1) is True
    lock_path = os.path.join(lock_dir, "abc123-SessionEnd")
    old = time.time() - 5
    os.utime(lock_path, (old, old))  # make the lock stale

    real_open = os.open
    calls = {"n": 0}

    def boom(path, *a, **kw):
        if path == lock_path:
            calls["n"] += 1
            if calls["n"] == 1:
                # first attempt: lock still present -> natural FileExistsError
                return real_open(path, *a, **kw)
            raise OSError(errno.EACCES, "denied")  # post-unlink re-create fails
        return real_open(path, *a, **kw)

    monkeypatch.setattr(obsidian_utils.os, "open", boom)
    assert obsidian_utils.claim_hook_run("SessionEnd", "abc123", ttl_seconds=1) is True


def test_lock_dir_isolated_from_real_home_during_tests():
    """The autouse conftest fixture must keep _LOCK_DIR out of the real
    ~/.claude so no test ever writes a dedup lock to the user's home.
    Self-defends the isolation against future refactors."""
    real_claude = os.path.realpath(os.path.expanduser("~/.claude"))
    assert not os.path.realpath(obsidian_utils._LOCK_DIR).startswith(real_claude), \
        obsidian_utils._LOCK_DIR


def test_sessionend_releases_lock_on_write_failure(tmp_path, monkeypatch):
    """Regression (PR #197 H1): a winning SessionEnd whose vault write fails
    must RELEASE the dedup lock so a sibling install (or re-fire) can still
    produce the note. Otherwise a transient write error turns a suppressed
    sibling into a permanently lost session note."""
    monkeypatch.setenv("HOME", str(tmp_path))
    import obsidian_session_log
    importlib.reload(obsidian_utils)
    importlib.reload(obsidian_session_log)

    cc_slug = "-myproj"
    proj = tmp_path / ".claude" / "projects" / cc_slug
    proj.mkdir(parents=True)
    transcript = proj / "sid-wf-1234.jsonl"
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
    (tmp_path / ".claude" / "obsidian-brain-config.json").write_text(
        json.dumps(cfg), encoding="utf-8")

    # Real guard claims the lock (winner); the write then fails.
    monkeypatch.setattr(obsidian_session_log, "write_vault_note",
                        lambda *a, **kw: "disk full")

    payload = json.dumps({
        "cwd": str(tmp_path),
        "session_id": "sid-wf-1234",
        "transcript_path": str(transcript),
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with pytest.raises(SystemExit) as exc_info:
        obsidian_session_log.main()
    assert exc_info.value.code == 0

    # WRITE_FAILED was logged...
    log_path = tmp_path / ".claude" / "obsidian-brain-hook.log"
    lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines()
             if "SessionEnd" in ln]
    assert any("outcome=WRITE_FAILED" in ln for ln in lines), lines

    # ...and the lock was released, so a re-fire can re-claim immediately.
    assert obsidian_utils.claim_hook_run("SessionEnd", "sid-wf-1234") is True

    monkeypatch.undo()
    importlib.reload(obsidian_utils)
    importlib.reload(obsidian_session_log)


def test_sessionstart_dedup_skip_logs_outcome(tmp_path, monkeypatch):
    """A SessionStart suppressed by the dedup guard must emit an
    outcome=SKIPPED_DEDUP line to the hook log (the documented diagnostic
    surface), not vanish silently (PR #197 M2)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    import obsidian_session_hint
    importlib.reload(obsidian_utils)
    importlib.reload(obsidian_session_hint)

    monkeypatch.setattr(obsidian_session_hint, "claim_hook_run", lambda *a, **kw: False)
    payload = json.dumps({"cwd": str(tmp_path), "session_id": "sid-ss-1234"})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with pytest.raises(SystemExit) as exc_info:
        obsidian_session_hint.main()
    assert exc_info.value.code == 0

    log_path = tmp_path / ".claude" / "obsidian-brain-hook.log"
    assert log_path.exists(), "hook log not created on dedup-skip"
    lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines()
             if "SessionStart" in ln]
    assert len(lines) == 1, lines
    assert "outcome=SKIPPED_DEDUP" in lines[0], lines[0]

    monkeypatch.undo()
    importlib.reload(obsidian_utils)
    importlib.reload(obsidian_session_hint)
