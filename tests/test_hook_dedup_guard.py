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


def test_two_concurrent_sessionend_fires_dedup_to_one_claim(selected_host_context):
    """Two native child writers retain one session and one copy of each fact."""
    context = selected_host_context
    config = dict(context.config, min_messages=1, min_duration_minutes=0)
    context.config_path.write_text(json.dumps(config))
    directory = context.native_home / ('projects' if context.host == 'claude' else 'sessions') / 'concurrent'
    directory.mkdir(parents=True)
    source = directory / 'synthetic.jsonl'
    rows = []
    if context.host == 'codex':
        rows.append({'type': 'session_meta', 'payload': {'id': context.native_session_id}})
    for index, text in enumerate(('First concurrent native fact.', 'Second concurrent native fact.')):
        if context.host == 'claude':
            rows.append({'type': 'user', 'sessionId': context.native_session_id,
                         'uuid': 'concurrent-' + str(index),
                         'message': {'role': 'user', 'content': text}})
        else:
            rows.append({'type': 'response_item', 'payload': {'type': 'message',
                'id': 'concurrent-' + str(index), 'role': 'user',
                'content': [{'type': 'input_text', 'text': text}],
                'internal_chat_message_metadata_passthrough': {'turn_id': 'concurrent-turn'}}})
    source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    original_source = source.read_bytes()
    command = [_sys.executable, str(_REPO_ROOT / 'tests/native_hook_test_driver.py'),
        '--host', context.host, '--client', context.client,
        '--session-id', context.native_session_id, '--cwd', str(context.worktree),
        '--vault', str(context.vault_path), '--config', str(context.config_path),
        '--resource-root', str(context.resource_root), '--index', str(context.index_path),
        '--state', str(context.state_path), '--transcript', str(source),
        str(_REPO_ROOT / 'hooks/obsidian_session_log.py')]
    payload = json.dumps({'session_id': context.native_session_id,
                          'cwd': str(context.worktree), 'transcript_path': str(source)})
    def child():
        return subprocess.run(command, input=payload, capture_output=True, text=True,
            cwd=context.worktree, env=dict(os.environ, CLAUDE_CODE_SESSION_ID='foreign-inherited-id',
                                          CODEX_THREAD_ID='foreign-inherited-thread'), timeout=10)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: child(), range(2)))
    proofs = []
    for result in results:
        assert result.returncode == 0, result.stderr
        proof_line = next(line for line in result.stderr.splitlines()
                          if line.startswith('NATIVE_CONTEXT_PROOF:'))
        proof = json.loads(proof_line.split(':', 1)[1])
        for field in ('host', 'client', 'native_session_id', 'worktree', 'vault_path',
                      'config_path', 'resource_root', 'index_path', 'state_path'):
            assert proof[field] == str(getattr(context, field))
            assert all(actor[field] == proof[field] for actor in proof['mutation_contexts'])
        proofs.append(proof)
    assert any(proof['mutation_contexts'] for proof in proofs)
    notes = list(context.vault_path.rglob('*.md'))
    assert len(notes) == 1
    content = notes[0].read_text()
    assert obsidian_utils.parse_frontmatter_field(content, 'agent_provider') == context.host
    assert obsidian_utils.parse_frontmatter_field(content, 'agent_session_id') == context.native_session_id
    for text in ('First concurrent native fact.', 'Second concurrent native fact.'):
        assert content.count(text) == 1
    assert 'foreign-inherited' not in content
    assert source.read_bytes() == original_source


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


# ---------------------------------------------------------------------------
# Adversarial-review follow-ups: PID-ownership release (C-003) + PreCompact
# release-on-write-failure (C-001)
# ---------------------------------------------------------------------------


def test_release_hook_run_skips_lock_owned_by_other_pid(lock_dir):
    """C-003: release_hook_run must NOT unlink a lock whose payload PID is not
    this process — that lock was re-claimed by a legitimate later fire, and
    unlinking it would let a sibling re-claim and write a duplicate."""
    assert obsidian_utils.claim_hook_run("SessionEnd", "abc123") is True
    lock_path = os.path.join(lock_dir, "abc123-SessionEnd")
    # Simulate takeover by a different process (rewrite payload PID).
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write(f"{os.getpid() + 1234567} 1700000000.000\n")
    obsidian_utils.release_hook_run("SessionEnd", "abc123")
    assert os.path.exists(lock_path), "must not unlink a lock owned by another PID"


def test_release_hook_run_unlinks_own_lock(lock_dir):
    """Sanity counterpart to the PID-ownership guard: a lock created by THIS
    process is released normally."""
    assert obsidian_utils.claim_hook_run("SessionEnd", "abc123") is True
    lock_path = os.path.join(lock_dir, "abc123-SessionEnd")
    obsidian_utils.release_hook_run("SessionEnd", "abc123")
    assert not os.path.exists(lock_path)


def test_release_hook_run_unparseable_payload_falls_through_to_unlink(lock_dir):
    """If the lock payload is unreadable/unparseable, fall through to the
    best-effort unlink (lost note is worse than a rare duplicate)."""
    assert obsidian_utils.claim_hook_run("SessionEnd", "abc123") is True
    lock_path = os.path.join(lock_dir, "abc123-SessionEnd")
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write("not-a-pid\n")
    obsidian_utils.release_hook_run("SessionEnd", "abc123")
    assert not os.path.exists(lock_path)

from native_capture_test_helpers import selected_host_context, native_batch, checkpoint
import capture
import note_transactions


def test_native_loser_never_publishes_without_ownership(selected_host_context,native_batch):
    from threading import Event
    entered=Event();release=Event()
    def holder():
        with note_transactions.ownership_lock(selected_host_context):
            entered.set()
            assert release.wait(1)
    with ThreadPoolExecutor(max_workers=1) as executor:
        active=executor.submit(holder)
        assert entered.wait(1)
        try:
            result=capture.capture_checkpoint(selected_host_context,capture.CaptureEvent('session_end'),
                time.monotonic()+0.02)
            assert result.status=='pending'
            assert result.pending_sources==1
            assert list(selected_host_context.vault_path.rglob('*.md'))==[]
        finally:
            release.set()
        active.result(timeout=1)
    assert checkpoint(selected_host_context,'session_end').status=='complete'
    assert len(list(selected_host_context.vault_path.rglob('*.md')))==1


def _native_failed_writer_retry(context,module,event,monkeypatch,capsys):
    original=capture.apply_mutations
    def failed(*args,**kwargs):
        raise OSError(28,'Synthetic disk full')
    monkeypatch.setattr(capture,'apply_mutations',failed)
    module._run(payload={'trigger':'manual'})
    assert 'capture failed: OSError' in capsys.readouterr().err
    assert list(context.vault_path.rglob('*.md'))==[]
    def next_owner():
        with note_transactions.ownership_lock(context,deadline=time.monotonic()+0.2):
            return True
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(next_owner).result(timeout=1)
    monkeypatch.setattr(capture,'apply_mutations',original)
    module._run(payload={'trigger':'manual'})
    assert capsys.readouterr().err==''
    paths=list(context.vault_path.rglob('*.md'))
    assert len(paths)==(2 if event=='pre_compact' else 1)
    assert all('First native fact.' in path.read_text() for path in paths)
    before={path:path.read_bytes() for path in paths}
    module._run(payload={'trigger':'manual'})
    assert {path:path.read_bytes() for path in context.vault_path.rglob('*.md')}==before


def test_native_sessionend_releases_ownership_on_write_failure(selected_host_context,native_batch,monkeypatch,capsys):
    import obsidian_session_log
    _native_failed_writer_retry(selected_host_context,obsidian_session_log,'session_end',monkeypatch,capsys)


def test_native_precompact_releases_ownership_on_write_failure(selected_host_context,native_batch,monkeypatch,capsys):
    import obsidian_context_snapshot
    _native_failed_writer_retry(selected_host_context,obsidian_context_snapshot,'pre_compact',monkeypatch,capsys)
