"""Tests for obsidian_session_reaper — F3 orphan-session reaper (issue #125 / #100 Phase 3).

Task 5 smoke test: verifies ReaperOutcome dataclass shape and frozen semantics.
Task 6: watermark read/write helpers.
Subsequent tasks (7–18) add canary, log, main-loop, and wiring tests.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

# Make hooks/ importable for in-process tests.
_HOOKS_DIR = str(Path(__file__).parent.parent / "hooks")
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)

from obsidian_session_reaper import ReaperOutcome  # noqa: E402


class TestReaperOutcomeDataclass:
    """Smoke tests for the ReaperOutcome frozen dataclass."""

    def test_can_instantiate_with_all_fields(self):
        """ReaperOutcome can be created with all named fields."""
        outcome = ReaperOutcome(
            reaped=3,
            skipped_below_threshold=1,
            skipped_already_written=2,
            skipped_permission_blocked=False,
            timeout=False,
            wall_ms=42.5,
        )
        assert outcome.reaped == 3
        assert outcome.skipped_below_threshold == 1
        assert outcome.skipped_already_written == 2
        assert outcome.skipped_permission_blocked is False
        assert outcome.timeout is False
        assert outcome.wall_ms == pytest.approx(42.5)

    def test_is_frozen(self):
        """ReaperOutcome is frozen — mutation raises FrozenInstanceError."""
        outcome = ReaperOutcome(
            reaped=0,
            skipped_below_threshold=0,
            skipped_already_written=0,
            skipped_permission_blocked=False,
            timeout=False,
            wall_ms=0.0,
        )
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            outcome.reaped = 99  # type: ignore[misc]

    def test_zero_outcome(self):
        """ReaperOutcome with all-zero / False values is valid (no-op run)."""
        outcome = ReaperOutcome(
            reaped=0,
            skipped_below_threshold=0,
            skipped_already_written=0,
            skipped_permission_blocked=False,
            timeout=False,
            wall_ms=0.0,
        )
        assert outcome.reaped == 0
        assert outcome.timeout is False

    def test_timeout_flag(self):
        """timeout=True is valid (reaper hit wall-clock budget)."""
        outcome = ReaperOutcome(
            reaped=1,
            skipped_below_threshold=0,
            skipped_already_written=0,
            skipped_permission_blocked=False,
            timeout=True,
            wall_ms=5100.0,
        )
        assert outcome.timeout is True
        assert outcome.wall_ms > 5000

    def test_equality(self):
        """Two ReaperOutcome instances with same fields are equal."""
        a = ReaperOutcome(
            reaped=1,
            skipped_below_threshold=2,
            skipped_already_written=3,
            skipped_permission_blocked=False,
            timeout=False,
            wall_ms=10.0,
        )
        b = ReaperOutcome(
            reaped=1,
            skipped_below_threshold=2,
            skipped_already_written=3,
            skipped_permission_blocked=False,
            timeout=False,
            wall_ms=10.0,
        )
        assert a == b


def test_read_watermark_missing_returns_zero(tmp_path):
    from hooks.obsidian_session_reaper import _read_watermark
    assert _read_watermark(tmp_path / "no-such-file") == 0


def test_read_watermark_returns_int(tmp_path):
    from hooks.obsidian_session_reaper import _read_watermark
    p = tmp_path / "watermark"
    p.write_text("1714579200")
    assert _read_watermark(p) == 1714579200


def test_read_watermark_corrupt_returns_zero(tmp_path):
    from hooks.obsidian_session_reaper import _read_watermark
    p = tmp_path / "watermark"
    p.write_text("not-a-number")
    assert _read_watermark(p) == 0


def test_write_watermark_atomic_creates_file(tmp_path):
    from hooks.obsidian_session_reaper import _write_watermark_atomic
    p = tmp_path / "subdir" / "watermark"
    _write_watermark_atomic(p, 1714579200)
    assert p.exists()
    assert p.read_text().strip() == "1714579200"
    # 0o600 permission
    assert (p.stat().st_mode & 0o777) == 0o600


def test_write_watermark_atomic_creates_parent_dir(tmp_path):
    from hooks.obsidian_session_reaper import _write_watermark_atomic
    p = tmp_path / "deep" / "nested" / "watermark"
    _write_watermark_atomic(p, 42)
    assert p.exists()
    # Parent dir is 0o700 (existing convention)
    assert (p.parent.stat().st_mode & 0o777) == 0o700


def test_permission_canary_succeeds_in_writable_dir(tmp_path):
    from hooks.obsidian_session_reaper import _permission_canary
    assert _permission_canary(str(tmp_path)) is True
    # Cleanup leaves no canary residue
    assert not (tmp_path / ".obsidian-brain-canary").exists()


def test_permission_canary_fails_on_readonly_dir(tmp_path):
    from hooks.obsidian_session_reaper import _permission_canary
    tmp_path.chmod(0o500)
    try:
        assert _permission_canary(str(tmp_path)) is False
    finally:
        tmp_path.chmod(0o700)  # cleanup so pytest can rmdir


def test_permission_canary_returns_false_on_missing_dir():
    from hooks.obsidian_session_reaper import _permission_canary
    assert _permission_canary("/nonexistent/path/here") is False


# ---------------------------------------------------------------------------
# Task 8: _append_reaper_log helper
# ---------------------------------------------------------------------------

def test_append_reaper_log_writes_event_line(tmp_path, monkeypatch):
    from hooks.obsidian_utils import _append_reaper_log
    # Redirect log destination to tmp_path
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    log_path = tmp_path / ".claude" / "obsidian-brain-hook.log"

    _append_reaper_log(project="obsidian-brain", sid="abc12345",
                       event="REAPED_OK", detail="")
    content = log_path.read_text()
    assert "Reaper" in content
    assert "project=obsidian-brain" in content
    assert "sid=abc12345" in content
    assert "event=REAPED_OK" in content


def test_append_reaper_log_summary_omits_sid(tmp_path, monkeypatch):
    from hooks.obsidian_utils import _append_reaper_log
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    log_path = tmp_path / ".claude" / "obsidian-brain-hook.log"

    _append_reaper_log(project="obsidian-brain", sid=None,
                       event="SUMMARY",
                       detail="reaped=1 skipped_existing=4 wall_ms=87")
    content = log_path.read_text()
    assert "Reaper" in content
    assert "event=SUMMARY" in content
    assert "sid=" not in content  # summary line omits sid


def test_append_reaper_log_uses_0o600_perms(tmp_path, monkeypatch):
    from hooks.obsidian_utils import _append_reaper_log
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    log_path = tmp_path / ".claude" / "obsidian-brain-hook.log"

    _append_reaper_log(project="p", sid="s", event="REAPED_OK", detail="")
    assert (log_path.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# Task 10: F3 — Reaper happy path (canonical above-threshold write)
# ---------------------------------------------------------------------------


@pytest.fixture
def reaper_env(tmp_path, monkeypatch):
    """Self-contained reaper sandbox: tmp HOME, fake project jsonl dir,
    fake vault dir. Returns a dict of useful paths."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    project = "obsidian-brain"
    project_slug = "-Users-abhishek-dev-claude-workspace-obsidian-brain"
    project_jsonl_dir = home / ".claude" / "projects" / project_slug
    project_jsonl_dir.mkdir(parents=True)

    vault = tmp_path / "vault"
    sessions = vault / "claude-sessions"
    sessions.mkdir(parents=True)

    config = {
        "vault_path": str(vault),
        "sessions_folder": "claude-sessions",
        "min_messages": 3,
        "min_duration_minutes": 2,
        "reaper_enabled": True,
        "reaper_max_runtime_seconds": 5,
    }

    return {
        "home": home,
        "project": project,
        "project_jsonl_dir": project_jsonl_dir,
        "vault": vault,
        "sessions": sessions,
        "config": config,
    }


def _make_above_threshold_jsonl(path: Path, sid: str, n_user_msgs: int = 5):
    """Write a minimal above-threshold JSONL fixture."""
    lines = []
    for i in range(n_user_msgs):
        lines.append(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": f"hello {i}"},
            "timestamp": f"2026-04-24T12:0{i}:00Z",
            "sessionId": sid,
        }))
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": "ok"},
            "timestamp": f"2026-04-24T12:0{i}:30Z",
            "sessionId": sid,
        }))
    path.write_text("\n".join(lines) + "\n")


def test_reaper_writes_above_threshold_orphan(reaper_env):
    from hooks.obsidian_session_reaper import _reap_orphaned_sessions

    sid = "d2cc7e46-9778-41be-bebb-8fb22a491204"
    jsonl = reaper_env["project_jsonl_dir"] / f"{sid}.jsonl"
    _make_above_threshold_jsonl(jsonl, sid, n_user_msgs=5)

    out = _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=reaper_env["config"],
    )

    assert out.reaped == 1
    assert out.skipped_below_threshold == 0
    assert out.skipped_already_written == 0

    # Vault note exists
    written = list(reaper_env["sessions"].glob("*.md"))
    assert len(written) == 1
    content = written[0].read_text()
    assert "reconstructed: true" in content
    assert "claude/reconstructed" in content
    assert "Reconstructed by SessionStart reaper" in content
    # Regression guard: project field must reflect the real project, not "unknown"
    assert f"project: {reaper_env['project']}" in content


# ---------------------------------------------------------------------------
# Task 11: F3 — reaper skip-already-written
# ---------------------------------------------------------------------------

def test_reaper_skips_already_written(reaper_env):
    from hooks.obsidian_session_reaper import _reap_orphaned_sessions

    sid = "d2cc7e46-9778-41be-bebb-8fb22a491204"
    sid_short = sid[:8]
    jsonl = reaper_env["project_jsonl_dir"] / f"{sid}.jsonl"
    _make_above_threshold_jsonl(jsonl, sid, n_user_msgs=5)

    # Pre-write a matching vault note so the reaper sees it as already-handled.
    # The note must have session_id frontmatter — _build_existing_sid_set reads it.
    pre_existing = (reaper_env["sessions"] /
                    f"2026-04-24-obsidian-brain-{sid_short}.md")
    pre_existing.write_text(
        f"---\nsession_id: {sid}\n---\n# Pre-existing note\n"
    )
    pre_mtime = pre_existing.stat().st_mtime

    out = _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=reaper_env["config"],
    )

    assert out.reaped == 0
    assert out.skipped_already_written == 1
    # Pre-existing note unchanged (content and mtime)
    assert "Pre-existing note" in pre_existing.read_text()
    assert pre_existing.stat().st_mtime == pre_mtime


# ---------------------------------------------------------------------------
# Task 12: F3 — reaper skip-below-threshold (two-stage gate)
# ---------------------------------------------------------------------------

def _make_below_threshold_jsonl(path: Path, sid: str):
    """1 user message — below the 3-msg threshold."""
    lines = [json.dumps({
        "type": "user",
        "message": {"role": "user", "content": "just one"},
        "timestamp": "2026-04-24T12:00:00Z",
        "sessionId": sid,
    })]
    path.write_text("\n".join(lines) + "\n")


def test_reaper_skips_below_threshold(reaper_env):
    from hooks.obsidian_session_reaper import _reap_orphaned_sessions

    sid = "d63cc484-5ceb-483a-a0a7-b3ec9020ac50"
    jsonl = reaper_env["project_jsonl_dir"] / f"{sid}.jsonl"
    _make_below_threshold_jsonl(jsonl, sid)

    out = _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=reaper_env["config"],
    )

    assert out.reaped == 0
    assert out.skipped_below_threshold == 1
    # No vault file written
    assert list(reaper_env["sessions"].glob("*.md")) == []


# ---------------------------------------------------------------------------
# Task 13: F3 — watermark advancement on success/already-written/below-threshold
# ---------------------------------------------------------------------------

def test_watermark_advances_on_success(reaper_env):
    from hooks.obsidian_session_reaper import (
        _reap_orphaned_sessions, _read_watermark)

    sid_a = "aaaaaaaa-1111-2222-3333-444444444444"
    sid_b = "bbbbbbbb-1111-2222-3333-444444444444"
    jsonl_a = reaper_env["project_jsonl_dir"] / f"{sid_a}.jsonl"
    jsonl_b = reaper_env["project_jsonl_dir"] / f"{sid_b}.jsonl"
    _make_above_threshold_jsonl(jsonl_a, sid_a)
    _make_above_threshold_jsonl(jsonl_b, sid_b)
    # Stagger mtimes so we know "max" is well-defined
    os.utime(jsonl_a, (1714579200, 1714579200))
    os.utime(jsonl_b, (1714579201, 1714579201))

    out = _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=reaper_env["config"],
    )

    assert out.reaped == 2
    watermark_path = (reaper_env["home"] / ".claude" / "obsidian-brain" /
                      f"reaper-watermark-{reaper_env['project']}")
    assert _read_watermark(watermark_path) == 1714579201

    # Re-invoke: no work this time
    out2 = _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=reaper_env["config"],
    )
    assert out2.reaped == 0
    assert out2.skipped_already_written == 0  # mtime <= watermark short-circuits
    assert out2.skipped_below_threshold == 0


def test_watermark_advances_on_already_written_skip(reaper_env):
    from hooks.obsidian_session_reaper import (
        _reap_orphaned_sessions, _read_watermark)

    sid = "ccccccc1-1111-2222-3333-444444444444"
    sid_short = sid[:8]
    jsonl = reaper_env["project_jsonl_dir"] / f"{sid}.jsonl"
    _make_above_threshold_jsonl(jsonl, sid)
    os.utime(jsonl, (1714579500, 1714579500))

    # Pre-existing matching vault note (with session_id frontmatter)
    (reaper_env["sessions"] /
     f"2026-04-24-obsidian-brain-{sid_short}.md").write_text(
        f"---\nsession_id: {sid}\n---\n# pre\n"
    )

    out = _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=reaper_env["config"],
    )

    assert out.skipped_already_written == 1
    watermark_path = (reaper_env["home"] / ".claude" / "obsidian-brain" /
                      f"reaper-watermark-{reaper_env['project']}")
    assert _read_watermark(watermark_path) == 1714579500


def test_watermark_advances_on_below_threshold_skip(reaper_env):
    from hooks.obsidian_session_reaper import (
        _reap_orphaned_sessions, _read_watermark)

    sid = "ddddddd1-1111-2222-3333-444444444444"
    jsonl = reaper_env["project_jsonl_dir"] / f"{sid}.jsonl"
    _make_below_threshold_jsonl(jsonl, sid)
    os.utime(jsonl, (1714579600, 1714579600))

    out = _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=reaper_env["config"],
    )

    assert out.skipped_below_threshold == 1
    watermark_path = (reaper_env["home"] / ".claude" / "obsidian-brain" /
                      f"reaper-watermark-{reaper_env['project']}")
    assert _read_watermark(watermark_path) == 1714579600


def test_watermark_honored_on_old_jsonl(reaper_env):
    from hooks.obsidian_session_reaper import (
        _reap_orphaned_sessions, _write_watermark_atomic)

    sid = "eeeeeee1-1111-2222-3333-444444444444"
    jsonl = reaper_env["project_jsonl_dir"] / f"{sid}.jsonl"
    _make_above_threshold_jsonl(jsonl, sid)
    os.utime(jsonl, (1714579700, 1714579700))

    # Watermark already past the jsonl mtime
    watermark_path = (reaper_env["home"] / ".claude" / "obsidian-brain" /
                      f"reaper-watermark-{reaper_env['project']}")
    watermark_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_watermark_atomic(watermark_path, 1714579800)

    out = _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=reaper_env["config"],
    )

    assert out.reaped == 0
    assert out.skipped_already_written == 0
    assert out.skipped_below_threshold == 0
    assert list(reaper_env["sessions"].glob("*.md")) == []


# ---------------------------------------------------------------------------
# Task 15: F3 — canary runs once / skipped when no writes needed
# ---------------------------------------------------------------------------

def test_canary_runs_once_per_invocation(reaper_env, monkeypatch):
    from hooks.obsidian_session_reaper import _reap_orphaned_sessions
    import hooks.obsidian_session_reaper as reaper_mod

    # 5 above-threshold orphans
    for i in range(5):
        sid = f"2222222{i}-1111-2222-3333-444444444444"
        jsonl = reaper_env["project_jsonl_dir"] / f"{sid}.jsonl"
        _make_above_threshold_jsonl(jsonl, sid)
        os.utime(jsonl, (1714580000 + i, 1714580000 + i))

    canary_calls = []
    real_canary = reaper_mod._permission_canary
    def spy_canary(vault_path):
        canary_calls.append(vault_path)
        return real_canary(vault_path)
    monkeypatch.setattr(reaper_mod, "_permission_canary", spy_canary)

    _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=reaper_env["config"],
    )
    assert len(canary_calls) == 1


def test_canary_skipped_when_no_writes_needed(reaper_env, monkeypatch):
    """If only below-threshold + already-written orphans exist, canary
    must not be called — no need to test write permission."""
    from hooks.obsidian_session_reaper import _reap_orphaned_sessions
    import hooks.obsidian_session_reaper as reaper_mod

    # Below-threshold orphan
    sid_b = "33333331-1111-2222-3333-444444444444"
    _make_below_threshold_jsonl(
        reaper_env["project_jsonl_dir"] / f"{sid_b}.jsonl", sid_b)

    # Already-written orphan (with session_id frontmatter)
    sid_a = "44444441-1111-2222-3333-444444444444"
    sid_a_short = sid_a[:8]
    _make_above_threshold_jsonl(
        reaper_env["project_jsonl_dir"] / f"{sid_a}.jsonl", sid_a)
    (reaper_env["sessions"] /
     f"2026-04-24-obsidian-brain-{sid_a_short}.md").write_text(
        f"---\nsession_id: {sid_a}\n---\n# pre\n"
    )

    canary_calls = []
    monkeypatch.setattr(reaper_mod, "_permission_canary",
                        lambda v: (canary_calls.append(v), True)[1])

    _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=reaper_env["config"],
    )
    assert canary_calls == []


# ---------------------------------------------------------------------------
# Task 14: F3 — watermark NON-advancement on permission-block / write-failure
# ---------------------------------------------------------------------------

def test_watermark_does_not_advance_on_permission_block(reaper_env, monkeypatch):
    from hooks.obsidian_session_reaper import (
        _reap_orphaned_sessions, _read_watermark)
    import hooks.obsidian_session_reaper as reaper_mod

    sid = "fffffff1-1111-2222-3333-444444444444"
    jsonl = reaper_env["project_jsonl_dir"] / f"{sid}.jsonl"
    _make_above_threshold_jsonl(jsonl, sid)

    monkeypatch.setattr(reaper_mod, "_permission_canary", lambda *_: False)

    out = _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=reaper_env["config"],
    )
    assert out.skipped_permission_blocked is True
    assert out.reaped == 0
    watermark_path = (reaper_env["home"] / ".claude" / "obsidian-brain" /
                      f"reaper-watermark-{reaper_env['project']}")
    # Watermark file was never created (start = 0, never advanced)
    assert not watermark_path.exists()


def test_watermark_does_not_advance_on_write_failure(reaper_env, monkeypatch):
    from hooks.obsidian_session_reaper import (
        _reap_orphaned_sessions, _read_watermark)
    import obsidian_utils as utils_mod  # unqualified: matches reaper's from-import
    assert utils_mod is sys.modules.get('obsidian_utils'), \
        "patch target mismatch — reaper uses a different obsidian_utils object"

    sid = "11111111-1111-2222-3333-444444444444"
    jsonl = reaper_env["project_jsonl_dir"] / f"{sid}.jsonl"
    _make_above_threshold_jsonl(jsonl, sid)

    # Patch write_vault_note on the bare 'obsidian_utils' module object —
    # the same sys.modules entry the reaper binds at call time via
    # `from obsidian_utils import write_vault_note`.
    monkeypatch.setattr(utils_mod, "write_vault_note",
                        lambda *args, **kw: "OSError: [Errno 28] No space left")

    out = _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=reaper_env["config"],
    )
    assert out.reaped == 0
    watermark_path = (reaper_env["home"] / ".claude" / "obsidian-brain" /
                      f"reaper-watermark-{reaper_env['project']}")
    assert not watermark_path.exists()


# ---------------------------------------------------------------------------
# Task 16: F3 — timeout advances watermark to last-processed
# ---------------------------------------------------------------------------

def test_timeout_advances_watermark_to_last_processed(reaper_env, monkeypatch):
    from hooks.obsidian_session_reaper import (
        _reap_orphaned_sessions, _read_watermark)
    import hooks.obsidian_session_reaper as reaper_mod

    # 3 above-threshold orphans with staggered mtimes
    sids = []
    for i in range(3):
        sid = f"5555555{i}-1111-2222-3333-444444444444"
        sids.append(sid)
        jsonl = reaper_env["project_jsonl_dir"] / f"{sid}.jsonl"
        _make_above_threshold_jsonl(jsonl, sid)
        os.utime(jsonl, (1714581000 + i, 1714581000 + i))

    # Force timeout after the first iteration by making the second iteration
    # observe an elapsed wall-clock past max_runtime
    config = dict(reaper_env["config"], reaper_max_runtime_seconds=0.01)

    # Patch time.monotonic to fake elapsed time
    real_monotonic = time.monotonic
    state = {"calls": 0, "start": real_monotonic()}
    def fake_monotonic():
        state["calls"] += 1
        # First two calls: under budget. Third+: over budget.
        if state["calls"] >= 3:
            return state["start"] + 1.0
        return state["start"]
    monkeypatch.setattr(reaper_mod.time, "monotonic", fake_monotonic)

    out = _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=config,
    )
    assert out.timeout is True
    # At least one orphan should have been reaped before the timeout
    assert out.reaped >= 1
    # Watermark advanced to the last successfully processed mtime, NOT to start_ts
    watermark_path = (reaper_env["home"] / ".claude" / "obsidian-brain" /
                      f"reaper-watermark-{reaper_env['project']}")
    wm = _read_watermark(watermark_path)
    # At minimum the first orphan's mtime
    assert wm >= 1714581000


# ---------------------------------------------------------------------------
# Task 17: F3 — read-failure does NOT advance watermark
# ---------------------------------------------------------------------------

def test_read_failure_does_not_advance_watermark(reaper_env, monkeypatch):
    from hooks.obsidian_session_reaper import (
        _reap_orphaned_sessions, _read_watermark)
    import obsidian_utils as utils_mod  # unqualified: matches reaper's from-import
    assert utils_mod is sys.modules.get('obsidian_utils'), \
        "patch target mismatch — reaper uses a different obsidian_utils object"

    sid = "66666661-1111-2222-3333-444444444444"
    jsonl = reaper_env["project_jsonl_dir"] / f"{sid}.jsonl"
    jsonl.write_text("not valid jsonl content\n")

    # Patch read_transcript to raise
    def boom(*_args, **_kw):
        raise ValueError("malformed transcript")
    monkeypatch.setattr(utils_mod, "read_transcript", boom)

    out = _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=reaper_env["config"],
    )
    assert out.reaped == 0
    # Watermark file was not created
    watermark_path = (reaper_env["home"] / ".claude" / "obsidian-brain" /
                      f"reaper-watermark-{reaper_env['project']}")
    assert not watermark_path.exists()


# ---------------------------------------------------------------------------
# Task 18: F3 — config-flag short-circuit + SessionStart wiring
# ---------------------------------------------------------------------------


def _make_hint_stdin(reaper_env: dict, session_id: str = "test-session-1") -> str:
    """Build a minimal SessionStart hook input dict as JSON string."""
    import json as _json
    return _json.dumps({
        "session_id": session_id,
        "cwd": "/Users/abhishek/dev/claude_workspace/obsidian-brain",
    })


def test_session_hint_does_not_invoke_reaper_when_disabled(reaper_env, monkeypatch):
    """Setting reaper_enabled=False prevents the reaper from running at SessionStart."""
    import io
    # The hint does `import obsidian_session_reaper` (bare, hooks/ is on sys.path).
    # Patch the bare module — that is the object the hint's lazy import resolves to.
    import obsidian_session_reaper as reaper_mod
    import hooks.obsidian_session_hint as hint_mod

    # Above-threshold orphan that would normally be reaped
    sid = "77777771-1111-2222-3333-444444444444"
    jsonl = reaper_env["project_jsonl_dir"] / f"{sid}.jsonl"
    _make_above_threshold_jsonl(jsonl, sid)

    reap_calls = []

    def spy_reap(*args, **kwargs):
        reap_calls.append((args, kwargs))
        return reaper_mod.ReaperOutcome(
            reaped=0, skipped_below_threshold=0, skipped_already_written=0,
            skipped_permission_blocked=False, timeout=False, wall_ms=0.0,
        )

    monkeypatch.setattr(reaper_mod, "_reap_orphaned_sessions", spy_reap)

    # Config with reaper_enabled = False.
    # Patch both obsidian_utils.load_config AND hint_mod.load_config to ensure
    # the hint uses the test config regardless of import binding.
    config = dict(reaper_env["config"], reaper_enabled=False)
    monkeypatch.setattr("obsidian_utils.load_config", lambda: config)
    monkeypatch.setattr(hint_mod, "load_config", lambda: config)

    # Patch find_latest_session on the hint module directly
    monkeypatch.setattr(hint_mod, "find_latest_session", lambda *_a, **_kw: None)

    # Redirect stdin
    monkeypatch.setattr("sys.stdin", io.StringIO(_make_hint_stdin(reaper_env)))

    hint_mod._run()

    assert reap_calls == []  # reaper not invoked when disabled
    # No watermark file created
    watermark_path = (reaper_env["home"] / ".claude" / "obsidian-brain" /
                      f"reaper-watermark-{reaper_env['project']}")
    assert not watermark_path.exists()


def test_session_hint_invokes_reaper_by_default(reaper_env, monkeypatch):
    """Default config (no reaper_enabled key) still triggers the reaper."""
    import io
    # The hint does `import obsidian_session_reaper` (bare, hooks/ is on sys.path).
    import obsidian_session_reaper as reaper_mod
    import hooks.obsidian_session_hint as hint_mod

    sid = "88888881-1111-2222-3333-444444444444"
    jsonl = reaper_env["project_jsonl_dir"] / f"{sid}.jsonl"
    _make_above_threshold_jsonl(jsonl, sid)

    reap_calls = []

    def spy_reap(*args, **kwargs):
        reap_calls.append(args)
        return reaper_mod.ReaperOutcome(
            reaped=1, skipped_below_threshold=0, skipped_already_written=0,
            skipped_permission_blocked=False, timeout=False, wall_ms=10.0,
        )

    monkeypatch.setattr(reaper_mod, "_reap_orphaned_sessions", spy_reap)

    # Note: config does NOT include reaper_enabled;
    # the wiring uses .get("reaper_enabled", True) — default True.
    # Patch hint_mod.load_config directly to guarantee the test config is used.
    config = {k: v for k, v in reaper_env["config"].items() if k != "reaper_enabled"}
    monkeypatch.setattr("obsidian_utils.load_config", lambda: config)
    monkeypatch.setattr(hint_mod, "load_config", lambda: config)

    monkeypatch.setattr(hint_mod, "find_latest_session", lambda *_a, **_kw: None)

    monkeypatch.setattr("sys.stdin", io.StringIO(_make_hint_stdin(reaper_env)))

    hint_mod._run()

    assert len(reap_calls) == 1


def test_session_hint_reaper_failure_does_not_propagate(reaper_env, monkeypatch):
    """A reaper exception must NOT crash the SessionStart hook (_run returns normally)."""
    import io
    # The hint does `import obsidian_session_reaper` (bare, hooks/ is on sys.path).
    import obsidian_session_reaper as reaper_mod
    import hooks.obsidian_session_hint as hint_mod

    def boom(*args, **kwargs):
        raise RuntimeError("reaper exploded")

    monkeypatch.setattr(reaper_mod, "_reap_orphaned_sessions", boom)

    config = dict(reaper_env["config"])  # reaper_enabled = True
    monkeypatch.setattr("obsidian_utils.load_config", lambda: config)
    monkeypatch.setattr(hint_mod, "load_config", lambda: config)

    monkeypatch.setattr(hint_mod, "find_latest_session", lambda *_a, **_kw: None)

    monkeypatch.setattr("sys.stdin", io.StringIO(_make_hint_stdin(reaper_env)))

    # Should NOT raise — reaper failures are caught and swallowed
    hint_mod._run()


# ---------------------------------------------------------------------------
# Coverage gap tests: edge paths in obsidian_session_reaper.py
# ---------------------------------------------------------------------------

def test_write_watermark_atomic_exception_cleanup(tmp_path, monkeypatch):
    """When os.replace fails, the temp file is removed and the exception re-raised."""
    from hooks.obsidian_session_reaper import _write_watermark_atomic
    import hooks.obsidian_session_reaper as reaper_mod

    unlocked = []

    real_replace = os.replace

    def boom_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom_replace)

    p = tmp_path / "wm"
    with pytest.raises(OSError, match="simulated replace failure"):
        _write_watermark_atomic(p, 12345)
    # Temp file must have been cleaned up (the finally unlink branch, lines 96-99)
    tmp_files = [f for f in tmp_path.iterdir() if f.name.startswith(".watermark.")]
    assert len(tmp_files) == 0


def test_resolve_project_jsonl_dir_no_projects_base(tmp_path, monkeypatch):
    """When ~/.claude/projects doesn't exist, returns a NONEXISTENT sentinel path."""
    from hooks.obsidian_session_reaper import _resolve_project_jsonl_dir

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    # No ~/.claude/projects dir created
    result = _resolve_project_jsonl_dir("myproject")
    assert "-NONEXISTENT-myproject" in result.name


def test_resolve_project_jsonl_dir_oserror_iterdir(tmp_path, monkeypatch):
    """An OSError from iterdir() returns the NONEXISTENT sentinel."""
    from hooks.obsidian_session_reaper import _resolve_project_jsonl_dir
    import hooks.obsidian_session_reaper as reaper_mod

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    base = tmp_path / ".claude" / "projects"
    base.mkdir(parents=True)

    # Make iterdir raise OSError
    original_iterdir = base.iterdir

    def boom_iterdir(self):
        raise OSError("permission denied on iterdir")

    monkeypatch.setattr(type(base), "iterdir", boom_iterdir)

    result = _resolve_project_jsonl_dir("myproject")
    assert "-NONEXISTENT-myproject" in result.name


def test_resolve_project_jsonl_dir_no_candidates(tmp_path, monkeypatch):
    """When no dirs match the project suffix, returns NONEXISTENT sentinel."""
    from hooks.obsidian_session_reaper import _resolve_project_jsonl_dir

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    base = tmp_path / ".claude" / "projects"
    base.mkdir(parents=True)
    (base / "some-other-project").mkdir()

    result = _resolve_project_jsonl_dir("myproject")
    assert "-NONEXISTENT-myproject" in result.name


def test_build_existing_sid_set_missing_sessions_dir(tmp_path):
    """Returns empty set when the sessions folder doesn't exist."""
    from hooks.obsidian_session_reaper import _build_existing_sid_set

    result = _build_existing_sid_set(str(tmp_path), "nonexistent-folder", "myproject")
    assert result == set()


def test_reap_orphaned_sessions_no_project_jsonl_dir(reaper_env, monkeypatch):
    """When the project JSONL dir doesn't exist, returns a no-op outcome immediately."""
    from hooks.obsidian_session_reaper import _reap_orphaned_sessions
    import hooks.obsidian_session_reaper as reaper_mod

    # Override _resolve_project_jsonl_dir to return a nonexistent path
    monkeypatch.setattr(reaper_mod, "_resolve_project_jsonl_dir",
                        lambda project: reaper_env["home"] / ".claude" / "projects" / "NONEXISTENT-x")

    out = _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=reaper_env["config"],
    )

    assert out.reaped == 0
    assert out.timeout is False
    assert out.skipped_permission_blocked is False


def test_reap_stage2_duration_threshold_skip(reaper_env, monkeypatch):
    """Sessions that pass stage-1 (msg count) but fail stage-2 (duration) are skipped."""
    from hooks.obsidian_session_reaper import _reap_orphaned_sessions
    import obsidian_utils as utils_mod

    sid = "99999991-1111-2222-3333-444444444444"
    jsonl = reaper_env["project_jsonl_dir"] / f"{sid}.jsonl"
    # 5 user messages — passes stage 1 (min_messages=3)
    _make_above_threshold_jsonl(jsonl, sid, n_user_msgs=5)

    # Patch extract_session_metadata to return a short duration (fails stage 2)
    original_esm = utils_mod.extract_session_metadata

    def fake_esm(messages, cwd):
        meta = original_esm(messages, cwd)
        meta["duration_minutes"] = 0.5  # below min_duration_minutes=2
        return meta

    monkeypatch.setattr(utils_mod, "extract_session_metadata", fake_esm)

    # Use a config where stage-2 gate is strict: need ≥2 min duration
    config = dict(reaper_env["config"], min_messages=3, min_duration_minutes=2)

    out = _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=config,
    )

    assert out.skipped_below_threshold >= 1
    assert out.reaped == 0


def test_reap_watermark_write_failed_logged(reaper_env, monkeypatch):
    """When _write_watermark_atomic raises, WATERMARK_WRITE_FAILED is logged."""
    from hooks.obsidian_session_reaper import _reap_orphaned_sessions
    import hooks.obsidian_session_reaper as reaper_mod

    sid = "aaaa0001-1111-2222-3333-444444444444"
    jsonl = reaper_env["project_jsonl_dir"] / f"{sid}.jsonl"
    _make_below_threshold_jsonl(jsonl, sid)  # below threshold — watermark advances
    os.utime(jsonl, (1714590000, 1714590000))

    # Patch _write_watermark_atomic to raise
    monkeypatch.setattr(reaper_mod, "_write_watermark_atomic",
                        lambda path, val: (_ for _ in ()).throw(OSError("disk full")))

    log_lines = []
    orig_log = reaper_mod._append_reaper_log if hasattr(reaper_mod, "_append_reaper_log") else None

    import obsidian_utils as utils_mod
    original_arl = utils_mod._append_reaper_log

    def spy_log(*args, **kwargs):
        log_lines.append(kwargs.get("event", ""))
        original_arl(*args, **kwargs)

    monkeypatch.setattr(utils_mod, "_append_reaper_log", spy_log)

    _reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=reaper_env["config"],
    )

    assert "WATERMARK_WRITE_FAILED" in log_lines


def test_reap_orphaned_sessions_public_wrapper_exception(reaper_env, monkeypatch):
    """reap_orphaned_sessions() catches exceptions from _reap_orphaned_sessions and returns zero outcome."""
    from hooks.obsidian_session_reaper import reap_orphaned_sessions
    import hooks.obsidian_session_reaper as reaper_mod

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected inner error")

    monkeypatch.setattr(reaper_mod, "_reap_orphaned_sessions", boom)

    out = reap_orphaned_sessions(
        project=reaper_env["project"],
        vault_path=str(reaper_env["vault"]),
        sessions_folder="claude-sessions",
        config=reaper_env["config"],
    )

    assert out.reaped == 0
    assert out.timeout is False
    assert out.wall_ms == 0.0
