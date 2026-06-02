"""Unit tests for the cross-plugin hook dedup guard (claim_hook_run)."""
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
