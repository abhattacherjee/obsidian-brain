"""Tests for the session-coverage vault_doctor check module (#98).

Isolation strategy (mirrors test_vault_doctor_audit_historic_repairs.py):
  - HOME is redirected via monkeypatch so ~/.claude/projects and the
    obsidian-brain config file land under tmp_path.
  - Vault tree is created under tmp_path/vault.
  - The skip-logic we test (threshold, project-filter, mtime-window) requires
    real on-disk files with controlled content and timestamps.

CLI e2e NOTE: We do NOT e2e-test --reconstruct's actual replay via --apply.
The monkeypatched unit tests (TestApply) exercise apply()'s outcome mapping
against a fake subprocess.run, and TestApplyRealReplay runs apply() unmocked
in-process (the subprocess inherits the redirected HOME from os.environ). An
--apply e2e through the dispatcher would add a confirmation-prompt layer
without new coverage of the replay integration, which is already covered by
tests/test_replay_cli.py. The --reconstruct scan-only e2e (unresolved=False
in JSON) IS tested below.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import vault_doctor  # noqa: E402
import vault_doctor_checks  # noqa: E402
import vault_doctor_checks.session_coverage as sc  # noqa: E402
from vault_doctor_checks import Issue  # noqa: E402

_REPO_ROOT = Path(__file__).parent.parent
_REPLAY_SCRIPT = _REPO_ROOT / "scripts" / "dev-test" / "replay-sessionend.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sid_hash(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()[:4]


def _make_filename(date_str: str, slug: str, sid: str) -> str:
    return f"{date_str}-{slug}-{_sid_hash(sid)}.md"


def _write_jsonl(
    projects_root: Path,
    project_dir_name: str,
    sid: str,
    cwd: str,
    n_user: int = 5,
    duration_minutes: float = 10.0,
    start_ts: str = "2026-04-24T10:00:00.000Z",
    mtime_offset: float = 0,
    n_tool_result_user: int = 0,
) -> Path:
    """Write a synthetic JSONL under projects_root/<project_dir_name>/<sid>.jsonl.

    Creates enough entries to pass/fail threshold tests:
      n_user             — TEXT-BEARING user message count
      duration           — total window in minutes (derived from timestamp spacing)
      n_tool_result_user — extra type=="user" entries whose content is ONLY a
                           tool_result block (must NOT count toward thresholds)
    """
    proj_dir = projects_root / project_dir_name
    proj_dir.mkdir(parents=True, exist_ok=True)
    jsonl = proj_dir / f"{sid}.jsonl"

    records = []
    raw = start_ts
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    start_epoch = datetime.fromisoformat(raw).timestamp()
    total_secs = duration_minutes * 60

    for i in range(n_user):
        frac = i / max(n_user - 1, 1)
        ts_epoch = start_epoch + frac * total_secs
        ts_iso = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        records.append(json.dumps({
            "type": "user",
            "uuid": f"00000000-0000-0000-0000-{i:012d}",
            "timestamp": ts_iso,
            "cwd": cwd,
            "message": {"role": "user", "content": f"message {i}"},
        }))
        records.append(json.dumps({
            "type": "assistant",
            "uuid": f"11111111-0000-0000-0000-{i:012d}",
            "timestamp": ts_iso,
            "cwd": cwd,
            "message": {"role": "assistant", "content": f"reply {i}"},
        }))

    for i in range(n_tool_result_user):
        records.append(json.dumps({
            "type": "user",
            "uuid": f"22222222-0000-0000-0000-{i:012d}",
            "timestamp": start_ts,
            "cwd": cwd,
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}
                ],
            },
        }))

    jsonl.write_text("\n".join(records) + "\n", encoding="utf-8")

    if mtime_offset != 0:
        new_mtime = time.time() + mtime_offset
        os.utime(str(jsonl), (new_mtime, new_mtime))

    return jsonl


def _write_session_note(
    sessions_dir: Path,
    basename: str,
    session_id: str | None = None,
    project: str = "obsidian-brain",
    date: str = "2026-04-24",
) -> Path:
    """Write a minimal session note to sessions_dir/<basename>."""
    sessions_dir.mkdir(parents=True, exist_ok=True)
    content_lines = [
        "---",
        "type: claude-session",
        f"date: {date}",
        f"project: {project}",
    ]
    if session_id:
        content_lines.append(f"session_id: {session_id}")
    content_lines += ["---", "# Session body", ""]
    (sessions_dir / basename).write_text("\n".join(content_lines), encoding="utf-8")
    return sessions_dir / basename


def _write_insight(
    insights_dir: Path,
    basename: str,
    source_session: str,
    project: str = "obsidian-brain",
) -> Path:
    """Write a minimal insight note referencing source_session."""
    insights_dir.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        "type: claude-insight\n"
        "date: 2026-04-24\n"
        f"source_session: {source_session}\n"
        f"project: {project}\n"
        "---\n"
        "# Insight body\n"
    )
    (insights_dir / basename).write_text(content, encoding="utf-8")
    return insights_dir / basename


@pytest.fixture
def sc_env(tmp_path, monkeypatch):
    """Standard session-coverage test environment.

    Layout:
      tmp_path/
        .claude/
          projects/
          obsidian-brain-config.json
        vault/
          claude-sessions/
          claude-insights/
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    claude = tmp_path / ".claude"
    projects_root = claude / "projects"
    projects_root.mkdir(parents=True)

    config = {
        "vault_path": str(tmp_path / "vault"),
        "sessions_folder": "claude-sessions",
        "insights_folder": "claude-insights",
        "min_messages": 3,
        "min_duration_minutes": 2.0,
        "auto_log_enabled": True,
    }
    config_path = claude / "obsidian-brain-config.json"
    config_path.write_text(json.dumps(config))

    vault = tmp_path / "vault"
    sessions = vault / "claude-sessions"
    insights = vault / "claude-insights"
    sessions.mkdir(parents=True)
    insights.mkdir(parents=True)

    return {
        "tmp_path": tmp_path,
        "projects": projects_root,
        "vault": vault,
        "sessions": sessions,
        "insights": insights,
        "config_path": config_path,
    }


def _scan(env, days=30, project=None, strict=False, reconstruct=False):
    return sc.scan(
        str(env["vault"]),
        "claude-sessions",
        "claude-insights",
        days,
        project=project,
        strict=strict,
        reconstruct=reconstruct,
    )


def _mock_replay_result(outcome: str, vault_writes=None, detail: str = ""):
    """Build a MagicMock mimicking subprocess.run of replay-sessionend.py --json."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    payload = {"outcome": outcome, "vault_writes": vault_writes or []}
    if detail:
        payload["detail"] = detail
    mock_result.stdout = json.dumps(payload)
    mock_result.stderr = ""
    return mock_result


# ---------------------------------------------------------------------------
# Test 1: gap detected
# ---------------------------------------------------------------------------

class TestGapDetected:
    def test_gap_above_threshold_emits_one_issue(self, sc_env):
        """JSONL above thresholds, no session note → 1 issue."""
        sid = "d2cc7e46-9778-41be-bebb-8fb22a491204"
        cwd = "/Users/abhishek/dev/claude_workspace/obsidian-brain"
        _write_jsonl(sc_env["projects"], "-Users-proj", sid, cwd, n_user=5, duration_minutes=10)

        issues = _scan(sc_env)
        assert len(issues) == 1
        i = issues[0]
        assert i.extra["signal_class"] == "session-coverage-gap"
        assert i.extra["unresolved"] is True
        assert i.extra["sid"] == sid
        assert i.project == "obsidian-brain"
        assert i.confidence == 0.0

    def test_expected_basename_matches_make_filename_formula(self, sc_env):
        """Expected basename in note_path matches _make_filename output."""
        sid = "aaaabbbb-1234-5678-9abc-def012345678"
        cwd = "/Users/abhishek/dev/claude_workspace/my-project"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=5)

        issues = _scan(sc_env)
        assert len(issues) == 1
        i = issues[0]

        # Expected note path should use _make_filename formula
        expected_hash = _sid_hash(sid)
        assert expected_hash in Path(i.note_path).name
        assert "my-project" in Path(i.note_path).name

    def test_unresolved_true_by_default(self, sc_env):
        sid = "ccccdddd-0000-0000-0000-000000000001"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=4, duration_minutes=5)
        issues = _scan(sc_env)
        assert len(issues) == 1
        assert issues[0].extra["unresolved"] is True


# ---------------------------------------------------------------------------
# Test 2: covered via session_id frontmatter
# ---------------------------------------------------------------------------

class TestCoveredViaSessionId:
    def test_covered_sid_no_issue(self, sc_env):
        """JSONL covered via session_id in frontmatter → no gap."""
        sid = "covered-sid-1111-2222-3333444455556"
        cwd = "/path/to/my-proj"
        date = "2026-04-24"
        basename = _make_filename(date, "my-proj", sid)
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=5)
        _write_session_note(sc_env["sessions"], basename, session_id=sid, date=date)

        issues = _scan(sc_env)
        assert issues == []

    def test_break_sid_index_lookup_makes_gap_fire(self, sc_env):
        """Fail-first: when sid_set lookup is broken, covered test fires a gap.

        This test confirms the session_id frontmatter index is load-bearing.
        We demonstrate it by creating a JSONL + session note, verifying clean
        scan, then running scan with the session note REMOVED — the gap
        fires again.
        """
        sid = "break-sid-test-1111-2222-3333444455"
        cwd = "/path/to/my-proj"
        date = "2026-04-24"
        basename = _make_filename(date, "my-proj", sid)

        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=5)
        note = _write_session_note(sc_env["sessions"], basename, session_id=sid, date=date)

        # Verify: covered → clean
        assert _scan(sc_env) == []

        # Mutate: remove the session note → gap should fire
        note.unlink()
        issues = _scan(sc_env)
        assert len(issues) == 1, (
            "Removing the session note should cause a gap to fire — "
            "the sid_set index is the only coverage signal for this note."
        )

    def test_renamed_note_covered_by_session_id_alone(self, sc_env):
        """A renamed note (filename hash does NOT match) whose session_id
        frontmatter matches → covered, no gap.

        This is the non-vacuous mutation killer for the sid_set arm of the
        coverage check: the filename-hash fallback CANNOT cover this fixture
        (the suffix is -0000, not the sid's hash), so a mutation that drops
        the `sid in sid_set` lookup makes this test fail.
        """
        sid = "renamed-note-sid-9999-8888-77776666"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=5)
        # Renamed note: hash suffix is -0000 (mismatched), but session_id matches.
        assert _sid_hash(sid) != "0000"
        _write_session_note(
            sc_env["sessions"], "2026-04-24-my-proj-0000.md",
            session_id=sid, date="2026-04-24",
        )

        issues = _scan(sc_env)
        assert issues == [], (
            "session_id frontmatter alone must cover a renamed note — "
            "if this fails, the sid_set arm of the coverage check is broken."
        )


# ---------------------------------------------------------------------------
# Test 3: covered via legacy filename-hash fallback
# ---------------------------------------------------------------------------

class TestCoveredViaLegacyHash:
    def test_covered_via_hash_no_session_id_field(self, sc_env):
        """Note without session_id frontmatter but matching hash suffix → no gap."""
        sid = "legacy-hash-sid-aaaa-bbbb-cccc-dddd-eeee"
        cwd = "/path/to/my-proj"
        date = "2026-04-24"
        # The filename has the hash suffix but no session_id frontmatter field.
        h4 = _sid_hash(sid)
        basename = f"{date}-my-proj-{h4}.md"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=5)
        # Write note WITHOUT session_id in frontmatter
        _write_session_note(sc_env["sessions"], basename, session_id=None, date=date)

        issues = _scan(sc_env)
        assert issues == []

    def test_wrong_hash_suffix_emits_gap(self, sc_env):
        """A note with a different 4-char suffix does NOT cover this JSONL."""
        sid = "legacy-hash-sid-ffff-0000-1111-2222-3333"
        cwd = "/path/to/my-proj"
        date = "2026-04-24"
        # Write a note with a WRONG hash suffix (all zeros)
        basename = f"{date}-my-proj-0000.md"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=5)
        _write_session_note(sc_env["sessions"], basename, session_id=None, date=date)

        issues = _scan(sc_env)
        assert len(issues) == 1  # hash doesn't match → gap

    def test_modern_note_hash_not_a_collision_candidate(self, sc_env):
        """A MODERN note (has session_id) whose filename hash happens to equal
        another sid's hash must NOT cover that other sid — only true-legacy
        notes (no session_id) enter the hash fallback pool.
        """
        gap_sid = "collision-victim-sid-0001"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", gap_sid, cwd, n_user=5, duration_minutes=5)

        # A modern note for a DIFFERENT session whose filename suffix equals
        # gap_sid's hash (simulating a 4-hex-char collision).
        h4 = _sid_hash(gap_sid)
        _write_session_note(
            sc_env["sessions"], f"2026-04-24-my-proj-{h4}.md",
            session_id="some-other-modern-sid", date="2026-04-24",
        )

        issues = _scan(sc_env)
        assert len(issues) == 1, (
            "Modern note's filename hash must not enter the legacy fallback "
            "pool — the colliding gap would be silently hidden otherwise."
        )
        assert issues[0].extra["sid"] == gap_sid

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
    def test_unreadable_note_falls_back_to_hash_and_warns(self, sc_env, capsys):
        """Unreadable session note → hash fallback still covers; stderr warns
        'sid-index degraded' and the unreadable counter appears in the summary."""
        sid = "unreadable-note-sid-0001"
        cwd = "/path/to/my-proj"
        date = "2026-04-24"
        h4 = _sid_hash(sid)
        basename = f"{date}-my-proj-{h4}.md"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=5)
        note = _write_session_note(sc_env["sessions"], basename, session_id=sid, date=date)
        note.chmod(0o000)
        try:
            issues = _scan(sc_env)
        finally:
            note.chmod(0o600)
        assert issues == []  # hash fallback covers it
        _, err = capsys.readouterr()
        assert "sid-index degraded" in err
        assert "1 unreadable session note(s)" in err


# ---------------------------------------------------------------------------
# Test 4: below-threshold JSONL
# ---------------------------------------------------------------------------

class TestBelowThreshold:
    def test_below_message_threshold_no_issue(self, sc_env, capsys):
        """JSONL with 1 user message (below min_messages=3) → no gap."""
        sid = "below-thresh-0001"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=1, duration_minutes=5)
        issues = _scan(sc_env)
        assert issues == []

        _, err = capsys.readouterr()
        # Summary should mention 1 below-threshold
        assert "below-threshold" in err

    def test_below_duration_threshold_no_issue(self, sc_env, capsys):
        """JSONL with duration < 2 min but sufficient messages → no gap."""
        sid = "below-dur-thresh-0002"
        cwd = "/path/to/my-proj"
        # 5 messages but duration 0.5 minutes (below 2.0 threshold)
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=0.5)
        issues = _scan(sc_env)
        assert issues == []

    def test_tool_result_only_user_entries_do_not_count(self, sc_env):
        """type=="user" entries whose content is only tool_result blocks must
        NOT count toward min_messages — mirrors the hook's
        extract_user_messages/_extract_text semantics. 1 text-bearing + 5
        tool_result-only entries is BELOW the threshold of 3.
        """
        sid = "tool-result-user-sid-0001"
        cwd = "/path/to/my-proj"
        _write_jsonl(
            sc_env["projects"], "-proj", sid, cwd,
            n_user=1, duration_minutes=10, n_tool_result_user=5,
        )
        issues = _scan(sc_env)
        assert issues == [], (
            "tool_result-only user entries counted toward the threshold — "
            "this is the raw-count bug that produced the dogfood false gaps."
        )

    def test_drop_threshold_logic_makes_gap_fire(self, sc_env):
        """Fail-first (behavioral): with thresholds disabled IN THE CONFIG
        (min_messages: 0, min_duration_minutes: 0), the below-threshold JSONL
        surfaces as a gap — proving the threshold skip is config-driven and
        load-bearing, without patching any internals.
        """
        sid = "no-thresh-test-0003"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=1, duration_minutes=0.3)

        # Normal: no gap (below threshold)
        assert _scan(sc_env) == []

        # Behavioral mutation: write zeroed thresholds into the seeded config.
        cfg = json.loads(sc_env["config_path"].read_text())
        cfg["min_messages"] = 0
        cfg["min_duration_minutes"] = 0
        sc_env["config_path"].write_text(json.dumps(cfg))

        issues = _scan(sc_env)
        assert len(issues) == 1, (
            "Without threshold filtering, a 1-message JSONL should surface as a gap."
        )


# ---------------------------------------------------------------------------
# Threshold boundary semantics (operators are <, not <=)
# ---------------------------------------------------------------------------

class TestThresholdBoundary:
    def test_exactly_at_thresholds_is_a_gap(self, sc_env):
        """Exactly 3 text-bearing user messages AND duration exactly 2.0 min →
        NOT skipped (should_skip_session uses strict <) → IS a gap.

        Kills off-by-one mutations: `user_count <= min_messages` or
        `duration <= min_duration` would wrongly classify this fixture as
        below-threshold and fail this test.
        """
        sid = "boundary-exact-sid-0001"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=3, duration_minutes=2.0)

        issues = _scan(sc_env)
        assert len(issues) == 1, (
            "exactly-at-threshold session must be a gap — the hook's "
            "should_skip_session uses strict less-than, not <=."
        )

    def test_one_below_message_threshold_is_skipped(self, sc_env):
        """2 text-bearing user messages (one below min_messages=3) → skipped."""
        sid = "boundary-below-msgs-sid-0002"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=2, duration_minutes=10)
        assert _scan(sc_env) == []

    def test_just_below_duration_threshold_is_skipped(self, sc_env):
        """Duration 1.9 min (just below 2.0) with enough messages → skipped."""
        sid = "boundary-below-dur-sid-0003"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=1.9)
        assert _scan(sc_env) == []


# ---------------------------------------------------------------------------
# auto_log_enabled gate
# ---------------------------------------------------------------------------

class TestAutoLogGate:
    def test_auto_log_disabled_returns_empty_with_note(self, sc_env, capsys):
        """auto_log_enabled: false → scan() returns [] immediately and notes
        why on stderr (the hook intentionally writes nothing; every gap would
        be a false positive)."""
        cfg = json.loads(sc_env["config_path"].read_text())
        cfg["auto_log_enabled"] = False
        sc_env["config_path"].write_text(json.dumps(cfg))

        sid = "autolog-off-sid-0001"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=10)

        issues = _scan(sc_env)
        assert issues == []
        _, err = capsys.readouterr()
        assert "auto_log_enabled is false" in err


# ---------------------------------------------------------------------------
# Config robustness (_load_thresholds)
# ---------------------------------------------------------------------------

class TestLoadThresholds:
    def test_bad_min_messages_keeps_good_min_duration(self, sc_env, capsys):
        """One bad key must not discard the others: bad min_messages falls
        back to 3 (with a warning) while a custom min_duration is honored."""
        cfg = json.loads(sc_env["config_path"].read_text())
        cfg["min_messages"] = "not-a-number"
        cfg["min_duration_minutes"] = 7.5
        sc_env["config_path"].write_text(json.dumps(cfg))

        mm, md, al = sc._load_thresholds(sc_env["tmp_path"])
        assert mm == 3       # default after coercion failure
        assert md == 7.5     # custom value survives
        assert al is True
        _, err = capsys.readouterr()
        assert "bad min_messages" in err

    def test_missing_config_is_silent(self, sc_env, capsys):
        """A missing config file applies defaults without warning (matches hook)."""
        sc_env["config_path"].unlink()
        mm, md, al = sc._load_thresholds(sc_env["tmp_path"])
        assert (mm, md, al) == (3, 2.0, True)
        _, err = capsys.readouterr()
        assert "WARNING" not in err

    def test_corrupt_config_warns(self, sc_env, capsys):
        """A corrupt (non-JSON) config warns to stderr and applies defaults."""
        sc_env["config_path"].write_text("{not json")
        mm, md, al = sc._load_thresholds(sc_env["tmp_path"])
        assert (mm, md, al) == (3, 2.0, True)
        _, err = capsys.readouterr()
        assert "could not read config" in err


# ---------------------------------------------------------------------------
# Test 5: referenced_by
# ---------------------------------------------------------------------------

class TestReferencedBy:
    def test_insight_references_gap_reason_mentions_count(self, sc_env):
        """Insight with source_session=sid → reason contains 'referenced by 1' and extra list."""
        sid = "refs-test-sid-0001"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=10)

        insight_basename = "2026-04-24-some-insight-abcd.md"
        _write_insight(sc_env["insights"], insight_basename, source_session=sid)

        issues = _scan(sc_env)
        assert len(issues) == 1
        i = issues[0]
        assert "referenced by 1" in i.reason
        assert insight_basename in i.extra["referenced_by"]

    def test_no_references_reason_says_zero(self, sc_env):
        sid = "refs-test-sid-0002"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=5)
        issues = _scan(sc_env)
        assert len(issues) == 1
        assert "referenced by 0" in issues[0].reason


# ---------------------------------------------------------------------------
# Test 6: strict mode
# ---------------------------------------------------------------------------

class TestStrictMode:
    def test_strict_with_reference_reason_starts_with_fail(self, sc_env):
        """strict=True + referenced JSONL → reason starts with FAIL:."""
        sid = "strict-test-sid-0001"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=10)
        _write_insight(sc_env["insights"], "2026-04-24-insight-abcd.md", source_session=sid)

        issues = _scan(sc_env, strict=True)
        assert len(issues) == 1
        i = issues[0]
        assert i.reason.startswith("FAIL:")
        assert i.extra["strict_fail"] is True

    def test_strict_without_reference_stays_warn(self, sc_env):
        """strict=True but unreferenced gap → reason starts with WARN: (not FAIL:)."""
        sid = "strict-test-sid-0002"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=10)

        issues = _scan(sc_env, strict=True)
        assert len(issues) == 1
        i = issues[0]
        assert i.reason.startswith("WARN:")
        assert i.extra["strict_fail"] is False

    def test_non_strict_referenced_gap_is_warn(self, sc_env):
        """strict=False (default) → always WARN: regardless of references."""
        sid = "strict-test-sid-0003"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=10)
        _write_insight(sc_env["insights"], "2026-04-24-ins-bbbb.md", source_session=sid)

        issues = _scan(sc_env, strict=False)
        assert len(issues) == 1
        assert issues[0].reason.startswith("WARN:")


# ---------------------------------------------------------------------------
# Test 7: reconstruct=True → unresolved=False
# ---------------------------------------------------------------------------

class TestReconstruct:
    def test_reconstruct_true_sets_unresolved_false(self, sc_env):
        """reconstruct=True → issues have unresolved=False."""
        sid = "reconstruct-test-sid-0001"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=10)

        issues = _scan(sc_env, reconstruct=True)
        assert len(issues) == 1
        assert issues[0].extra["unresolved"] is False

    def test_reconstruct_false_sets_unresolved_true(self, sc_env):
        sid = "reconstruct-test-sid-0002"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=5, duration_minutes=10)

        issues = _scan(sc_env, reconstruct=False)
        assert len(issues) == 1
        assert issues[0].extra["unresolved"] is True


# ---------------------------------------------------------------------------
# Test 8: project filter
# ---------------------------------------------------------------------------

class TestProjectFilter:
    def test_project_filter_excludes_other_projects(self, sc_env, capsys):
        """project="obsidian-brain" excludes JSONLs from other project dirs."""
        sid_ob = "proj-filter-ob-0001"
        sid_other = "proj-filter-other-0002"
        cwd_ob = "/Users/abhishek/dev/claude_workspace/obsidian-brain"
        cwd_other = "/Users/abhishek/dev/other-project"

        _write_jsonl(sc_env["projects"], "-ob", sid_ob, cwd_ob, n_user=5, duration_minutes=10)
        _write_jsonl(sc_env["projects"], "-other", sid_other, cwd_other, n_user=5, duration_minutes=10)

        issues = _scan(sc_env, project="obsidian-brain")
        # Only the obsidian-brain JSONL should appear
        sids = [i.extra["sid"] for i in issues]
        assert sid_ob in sids
        assert sid_other not in sids

        _, err = capsys.readouterr()
        assert "project-filtered" in err

    def test_project_filter_counted_in_summary(self, sc_env, capsys):
        sid_other = "proj-filter-count-0003"
        cwd_other = "/Users/abhishek/dev/other-project"
        _write_jsonl(sc_env["projects"], "-other", sid_other, cwd_other, n_user=5, duration_minutes=10)

        _scan(sc_env, project="does-not-exist")
        _, err = capsys.readouterr()
        m = re.search(r"(\d+) project-filtered", err)
        assert m and int(m.group(1)) >= 1


# ---------------------------------------------------------------------------
# Test 9: mtime window
# ---------------------------------------------------------------------------

class TestMtimeWindow:
    def test_old_jsonl_outside_window_not_scanned(self, sc_env, capsys):
        """JSONL whose mtime is older than --days window is not scanned."""
        sid = "old-jsonl-window-0001"
        cwd = "/path/to/my-proj"
        # mtime 40 days ago, but window is 30 days
        _write_jsonl(
            sc_env["projects"], "-proj", sid, cwd,
            n_user=5, duration_minutes=10,
            mtime_offset=-(40 * 86400),
        )
        issues = _scan(sc_env, days=30)
        assert issues == []

        _, err = capsys.readouterr()
        # Scanned 0 JSONLs (the old one was skipped before parsing)
        m = re.search(r"scanned (\d+) jsonl", err)
        # Should either not print (0 project dirs) or show 0 scanned
        if m:
            assert int(m.group(1)) == 0

    def test_recent_jsonl_in_window_is_scanned(self, sc_env):
        """JSONL within the window is scanned and surfaces as a gap."""
        sid = "recent-jsonl-window-0002"
        cwd = "/path/to/my-proj"
        # mtime 1 hour ago — definitely in the 30-day window
        _write_jsonl(
            sc_env["projects"], "-proj", sid, cwd,
            n_user=5, duration_minutes=10,
            mtime_offset=-3600,
        )
        issues = _scan(sc_env, days=30)
        assert len(issues) == 1


# ---------------------------------------------------------------------------
# cwd derivation: first parseable line that HAS a cwd
# ---------------------------------------------------------------------------

class TestCwdDerivation:
    def test_cwd_from_first_line_that_has_one(self, sc_env):
        """Production JSONLs often start with summary/file-history lines that
        carry no cwd. Project derivation must use the first parseable line
        that HAS a cwd: malformed line 1, cwd-less line 2, valid line 3."""
        sid = "cwd-derivation-sid-0001"
        proj_dir = sc_env["projects"] / "-proj"
        proj_dir.mkdir(parents=True, exist_ok=True)
        jsonl = proj_dir / f"{sid}.jsonl"

        lines = ["{malformed json line"]
        # cwd-less but parseable summary line
        lines.append(json.dumps({
            "type": "summary", "summary": "compacted context",
        }))
        # valid user lines with cwd, enough to pass thresholds
        for i in range(5):
            lines.append(json.dumps({
                "type": "user",
                "timestamp": f"2026-04-24T10:{i * 3:02d}:00.000Z",
                "cwd": "/Users/abhishek/dev/real-project",
                "message": {"role": "user", "content": f"msg {i}"},
            }))
        jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")

        issues = _scan(sc_env)
        assert len(issues) == 1
        assert issues[0].project == "real-project"
        assert issues[0].extra["cwd"] == "/Users/abhishek/dev/real-project"


# ---------------------------------------------------------------------------
# Test 10: apply() behaviour (mocked subprocess)
# ---------------------------------------------------------------------------

class TestApply:
    def _make_issue(self, sid: str, jsonl_path: str, cwd: str, note_path: str) -> Issue:
        return Issue(
            check=sc.NAME,
            note_path=note_path,
            project="my-proj",
            current_source=f"{sid}.jsonl (100 bytes)",
            proposed_source=f"[[2026-04-24-my-proj-{_sid_hash(sid)}]]",
            reason="WARN: JSONL exists (100 bytes) but session note missing; referenced by 0 note(s)",
            confidence=0.0,
            extra={
                "unresolved": False,  # reconstruct=True was used
                "signal_class": "session-coverage-gap",
                "sid": sid,
                "jsonl_path": jsonl_path,
                "jsonl_bytes": 100,
                "jsonl_mtime": "2026-04-24T10:00:00+00:00",
                "cwd": cwd,
                "referenced_by": [],
                "strict_fail": False,
            },
        )

    def test_apply_success_outcome_returns_applied(self, tmp_path, monkeypatch):
        """Mocked subprocess returns outcome=OK_RAW_NOTE_ONLY (the real hook
        success outcome — "OK" is reserved, never emitted today) with
        non-empty vault_writes → status 'applied' with the ACTUAL written path."""
        issue = self._make_issue(
            "apply-ok-sid-0001",
            str(tmp_path / "apply-ok-sid-0001.jsonl"),
            "/path/to/my-proj",
            str(tmp_path / "2026-04-24-my-proj-xxxx.md"),
        )

        actual = str(tmp_path / "vault" / "claude-sessions" / "2026-06-09-my-proj-xxxx.md")
        mock_run = MagicMock(return_value=_mock_replay_result(
            "OK_RAW_NOTE_ONLY", vault_writes=[[actual, 1234]],
        ))
        monkeypatch.setattr(subprocess, "run", mock_run)

        results = sc.apply([issue], str(tmp_path / "backup"))

        assert len(results) == 1
        assert results[0].status == "applied"
        # Result carries the ACTUAL written path (ground truth), not the
        # scan-predicted one — _first_seen_date can shift the date.
        assert results[0].note_path == actual
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "--jsonl" in cmd
        assert "--json" in cmd

    def test_apply_success_with_empty_vault_writes_is_error(self, tmp_path, monkeypatch):
        """Success outcome but EMPTY vault_writes → broken contract → 'error'."""
        issue = self._make_issue(
            "apply-empty-writes-sid-0006",
            str(tmp_path / "apply-empty-writes-sid-0006.jsonl"),
            "/path/to/my-proj",
            str(tmp_path / "2026-04-24-my-proj-wwww.md"),
        )

        monkeypatch.setattr(subprocess, "run", MagicMock(
            return_value=_mock_replay_result("OK_RAW_NOTE_ONLY", vault_writes=[]),
        ))
        results = sc.apply([issue], str(tmp_path / "backup"))
        assert len(results) == 1
        assert results[0].status == "error"
        assert "wrote nothing" in (results[0].error or "")

    def test_apply_skipped_outcome_returns_skipped(self, tmp_path, monkeypatch):
        """Mocked subprocess returns outcome=SKIPPED_BELOW_THRESHOLD → status 'skipped'."""
        issue = self._make_issue(
            "apply-skip-sid-0002",
            str(tmp_path / "apply-skip-sid-0002.jsonl"),
            "/path/to/my-proj",
            str(tmp_path / "2026-04-24-my-proj-yyyy.md"),
        )

        monkeypatch.setattr(subprocess, "run", MagicMock(
            return_value=_mock_replay_result(
                "SKIPPED_BELOW_THRESHOLD", detail="too few messages",
            ),
        ))
        results = sc.apply([issue], str(tmp_path / "backup"))

        assert len(results) == 1
        r = results[0]
        assert r.status == "skipped"
        assert "SKIPPED_BELOW_THRESHOLD" in (r.error or "")

    def test_apply_non_ok_non_skipped_returns_error(self, tmp_path, monkeypatch):
        """Any non-success, non-SKIPPED_* outcome → status 'error'."""
        issue = self._make_issue(
            "apply-err-sid-0003",
            str(tmp_path / "apply-err-sid-0003.jsonl"),
            "/path/to/my-proj",
            str(tmp_path / "2026-04-24-my-proj-zzzz.md"),
        )

        monkeypatch.setattr(subprocess, "run", MagicMock(
            return_value=_mock_replay_result("EXCEPTION", detail="something blew up"),
        ))
        results = sc.apply([issue], str(tmp_path / "backup"))

        assert len(results) == 1
        r = results[0]
        assert r.status == "error"

    def test_apply_unresolved_issue_returns_unresolved(self, tmp_path, monkeypatch):
        """Unresolved issue → status 'unresolved', subprocess never called."""
        issue = self._make_issue(
            "apply-unresolved-sid-0004",
            str(tmp_path / "unresolved.jsonl"),
            "/path/to/my-proj",
            str(tmp_path / "note.md"),
        )
        issue.extra["unresolved"] = True

        mock_run = MagicMock()
        monkeypatch.setattr(subprocess, "run", mock_run)
        results = sc.apply([issue], str(tmp_path / "backup"))

        assert len(results) == 1
        assert results[0].status == "unresolved"
        mock_run.assert_not_called()

    def test_apply_note_already_exists_returns_skipped(self, tmp_path, monkeypatch):
        """If the expected note already exists on disk, skip without calling replay."""
        note_path = tmp_path / "2026-04-24-my-proj-aaaa.md"
        note_path.write_text("---\ntype: claude-session\n---\n")

        issue = self._make_issue(
            "apply-exists-sid-0005",
            str(tmp_path / "apply-exists-sid-0005.jsonl"),
            "/path/to/my-proj",
            str(note_path),
        )

        mock_run = MagicMock()
        monkeypatch.setattr(subprocess, "run", mock_run)
        results = sc.apply([issue], str(tmp_path / "backup"))

        assert len(results) == 1
        assert results[0].status == "skipped"
        mock_run.assert_not_called()

    def test_apply_no_cwd_returns_error_without_fabricating(self, tmp_path, monkeypatch):
        """A gap with no recorded cwd must NOT fabricate --cwd — return a
        clear error with guidance, never launch the replay."""
        issue = self._make_issue(
            "apply-no-cwd-sid-0007",
            str(tmp_path / "apply-no-cwd-sid-0007.jsonl"),
            "",  # no cwd recorded
            str(tmp_path / "2026-04-24-unknown-vvvv.md"),
        )

        mock_run = MagicMock()
        monkeypatch.setattr(subprocess, "run", mock_run)
        results = sc.apply([issue], str(tmp_path / "backup"))

        assert len(results) == 1
        r = results[0]
        assert r.status == "error"
        assert "no recorded cwd" in (r.error or "")
        assert "replay-sessionend.py" in (r.error or "")
        mock_run.assert_not_called()

    def test_apply_wrong_signal_class_raises_runtime_error(self, tmp_path):
        """Defense-in-depth: apply() with wrong signal_class raises RuntimeError."""
        issue = Issue(
            check=sc.NAME,
            note_path=str(tmp_path / "note.md"),
            project="proj",
            current_source="...",
            proposed_source="...",
            reason="...",
            confidence=0.0,
            extra={
                "unresolved": False,  # resolvable — so the guard fires
                "signal_class": "wrong-class",
                "sid": "xxx",
                "jsonl_path": str(tmp_path / "xxx.jsonl"),
                "jsonl_bytes": 0,
                "jsonl_mtime": "",
                "cwd": "",
                "referenced_by": [],
                "strict_fail": False,
            },
        )

        with pytest.raises(RuntimeError, match="refuses signal_class"):
            sc.apply([issue], str(tmp_path / "backup"))

    def test_replay_script_exists_in_repo(self):
        """apply() shells out to replay-sessionend.py — pin its in-repo path
        so a rename/move breaks loudly here, not silently at apply-time."""
        assert _REPLAY_SCRIPT.exists(), (
            f"replay-sessionend.py missing at {_REPLAY_SCRIPT} — "
            f"session_coverage.apply() depends on this path."
        )


# ---------------------------------------------------------------------------
# apply() against the REAL replay script (no mocking)
# ---------------------------------------------------------------------------

class TestApplyRealReplay:
    def test_apply_real_replay_writes_note(self, sc_env, monkeypatch):
        """Integration: scan(reconstruct=True) → apply() unmocked → the real
        replay-sessionend.py runs the production SessionEnd hook code path and
        writes a raw session note (fast — no claude -p; summarization is
        deferred by design). Asserts status 'applied' and the note exists at
        the vault_writes-reported path.

        The subprocess inherits the redirected HOME from os.environ
        (monkeypatch.setenv mutates os.environ), so the replay's config read,
        projects-containment check, hook log, and vault writes all land under
        tmp_path. _REAL_VAULT_GUARD adds the production-vault sentinel.
        """
        monkeypatch.setenv("_REAL_VAULT_GUARD", "1")

        sid = "real-replay-sid-0001"
        cwd = "/Users/abhishek/dev/claude_workspace/obsidian-brain"
        # Stage the JSONL inside the redirected ~/.claude/projects so the
        # hook's transcript-containment check passes without re-staging.
        _write_jsonl(
            sc_env["projects"],
            "-Users-abhishek-dev-claude_workspace-obsidian-brain",
            sid, cwd, n_user=5, duration_minutes=10,
        )

        issues = _scan(sc_env, reconstruct=True)
        assert len(issues) == 1
        assert issues[0].extra["unresolved"] is False

        results = sc.apply(issues, str(sc_env["tmp_path"] / "backup"))
        assert len(results) == 1
        r = results[0]
        assert r.status == "applied", f"expected applied, got {r.status}: {r.error}"
        # The applied Result carries the ACTUAL written path (from
        # vault_writes) — assert the note really exists on disk.
        written = Path(r.note_path)
        assert written.exists(), f"note not on disk: {written}"
        assert written.parent == sc_env["sessions"], (
            f"note written outside the seeded sessions folder: {written}"
        )
        # And a re-scan shows the gap as covered now.
        assert _scan(sc_env) == []


# ---------------------------------------------------------------------------
# Test 11: summary partition
# ---------------------------------------------------------------------------

class TestSummaryPartition:
    def test_summary_counts_sum_to_scanned(self, sc_env, capsys):
        """Mixed fixture: gap + covered + below-threshold + unparsable →
        all counts sum to the scanned total."""
        cwd = "/path/to/my-proj"

        # 1 gap (above threshold, no note)
        sid_gap = "summary-gap-sid-0001"
        _write_jsonl(sc_env["projects"], "-proj", sid_gap, cwd, n_user=5, duration_minutes=10)

        # 1 covered (session_id in note)
        sid_cov = "summary-cov-sid-0002"
        date = "2026-04-24"
        basename_cov = _make_filename(date, "my-proj", sid_cov)
        _write_jsonl(sc_env["projects"], "-proj2", sid_cov, cwd, n_user=5, duration_minutes=10)
        _write_session_note(sc_env["sessions"], basename_cov, session_id=sid_cov, date=date)

        # 1 below-threshold
        sid_bt = "summary-bt-sid-0003"
        _write_jsonl(sc_env["projects"], "-proj3", sid_bt, cwd, n_user=1, duration_minutes=5)

        # 1 completely unparsable (no line is valid JSON)
        unparsable_dir = sc_env["projects"] / "-proj4"
        unparsable_dir.mkdir(parents=True, exist_ok=True)
        (unparsable_dir / "summary-unparsable-sid-0004.jsonl").write_text(
            "not json at all\n{broken\n", encoding="utf-8"
        )

        issues = _scan(sc_env)
        assert len(issues) == 1  # only the gap

        _, err = capsys.readouterr()
        m = re.search(
            r"scanned (\d+) jsonl\(s\) across (\d+) project dir\(s\):"
            r" (\d+) gaps, (\d+) covered, (\d+) below-threshold,"
            r" (\d+) unparsable, (\d+) project-filtered",
            err,
        )
        assert m, f"summary line not found in:\n{err}"
        total, _dirs, gaps, covered, bt, unparsable, proj_filt = map(int, m.groups())

        assert gaps == 1
        assert covered == 1
        assert bt == 1
        assert unparsable == 1
        assert gaps + covered + bt + unparsable + proj_filt == total


# ---------------------------------------------------------------------------
# Test 12: CLI e2e
# ---------------------------------------------------------------------------

class TestCLIE2E:
    """End-to-end tests via subprocess with a synthetic HOME + vault."""

    _SCRIPT = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"

    def _run(self, env, *args: str, check: str | None = "session-coverage") -> subprocess.CompletedProcess:
        cmd = [
            sys.executable, str(self._SCRIPT),
            "--vault", str(env["vault"]),
            "--sessions-folder", "claude-sessions",
            "--insights-folder", "claude-insights",
            "--json",
        ]
        if check:
            cmd += ["--check", check]
        cmd += list(args)
        proc_env = {
            **os.environ,
            "HOME": str(env["tmp_path"]),
        }
        return subprocess.run(
            cmd, capture_output=True, text=True, env=proc_env,
        )

    def test_gap_causes_exit_1(self, sc_env):
        """JSONL above thresholds, no note → exit 1, 1 issue, signal_class session-coverage-gap."""
        sid = "cli-e2e-gap-sid-0001"
        cwd = "/Users/abhishek/dev/claude_workspace/obsidian-brain"
        _write_jsonl(sc_env["projects"], "-ob", sid, cwd, n_user=5, duration_minutes=10)

        r = self._run(sc_env)
        assert r.returncode == 1, f"expected 1, got {r.returncode}:\n{r.stderr}"

        payload = json.loads(r.stdout)
        assert payload["total_issues"] == 1
        row = payload["issues"][0]
        assert row["signal_class"] == "session-coverage-gap"
        # Conditionally-surfaced extras: present for session-coverage rows.
        assert row["sid"] == sid
        assert row["jsonl_path"].endswith(f"{sid}.jsonl")
        assert row["strict_fail"] is False
        assert row["referenced_by_count"] == 0

    def test_strict_flag_makes_reason_start_with_fail(self, sc_env):
        """--strict + referenced JSONL → top-level signal_class still session-coverage-gap,
        reason in the issue starts with FAIL:."""
        sid = "cli-e2e-strict-sid-0002"
        cwd = "/Users/abhishek/dev/claude_workspace/obsidian-brain"
        _write_jsonl(sc_env["projects"], "-ob", sid, cwd, n_user=5, duration_minutes=10)
        _write_insight(sc_env["insights"], "2026-04-24-insight-cccc.md", source_session=sid)

        r = self._run(sc_env, "--strict")
        assert r.returncode == 1, f"expected 1, got {r.returncode}:\n{r.stderr}"

        payload = json.loads(r.stdout)
        assert payload["total_issues"] == 1
        row = payload["issues"][0]
        assert row["signal_class"] == "session-coverage-gap"
        assert row["reason"].startswith("FAIL:")
        assert row["strict_fail"] is True
        assert row["referenced_by_count"] == 1

    def test_reconstruct_flag_marks_issue_resolvable(self, sc_env):
        """--reconstruct (scan-only, no --apply) → issues[0].unresolved is False."""
        sid = "cli-e2e-reconstruct-sid-0003"
        cwd = "/Users/abhishek/dev/claude_workspace/obsidian-brain"
        _write_jsonl(sc_env["projects"], "-ob", sid, cwd, n_user=5, duration_minutes=10)

        r = self._run(sc_env, "--reconstruct")
        assert r.returncode == 1, f"expected 1, got {r.returncode}:\n{r.stderr}"

        payload = json.loads(r.stdout)
        assert payload["total_issues"] == 1
        assert payload["issues"][0]["unresolved"] is False

    def test_clean_vault_exit_0(self, sc_env):
        """No JSONLs → exit 0."""
        r = self._run(sc_env)
        assert r.returncode == 0, f"expected 0, got {r.returncode}:\n{r.stderr}"

    def test_covered_note_no_issue(self, sc_env):
        """JSONL + matching session note → exit 0."""
        sid = "cli-e2e-covered-sid-0003"
        cwd = "/Users/abhishek/dev/claude_workspace/obsidian-brain"
        date = "2026-04-24"
        basename = _make_filename(date, "obsidian-brain", sid)

        _write_jsonl(sc_env["projects"], "-ob", sid, cwd, n_user=5, duration_minutes=10)
        _write_session_note(sc_env["sessions"], basename, session_id=sid, date=date)

        r = self._run(sc_env)
        assert r.returncode == 0, f"expected 0, got {r.returncode}:\n{r.stderr}"

    def test_full_sweep_with_reconstruct_is_usage_error(self, sc_env):
        """Default sweep (no --check) + --reconstruct → exit 3 with a clear
        usage error. session-coverage (the only EXTRA_SCAN_FLAGS consumer) is
        OPT_IN, so without this guard the flag would silently evaporate and
        the user would believe reconstruction was attempted."""
        r = self._run(sc_env, "--reconstruct", check=None)
        assert r.returncode == 3, (
            f"expected usage error (3), got {r.returncode}:\n{r.stderr}"
        )
        assert "--reconstruct is only consumed by an opt-in check" in r.stderr
        assert "--check session-coverage" in r.stderr

    def test_full_sweep_with_strict_is_usage_error(self, sc_env):
        """Default sweep (no --check) + --strict → exit 3 (same guard)."""
        r = self._run(sc_env, "--strict", check=None)
        assert r.returncode == 3, (
            f"expected usage error (3), got {r.returncode}:\n{r.stderr}"
        )
        assert "--strict is only consumed by an opt-in check" in r.stderr

    def test_full_sweep_without_extra_flags_unaffected(self, sc_env):
        """Default sweep WITHOUT --strict/--reconstruct stays on the normal
        exit contract (0 clean / 1 issues) and never tracebacks — the guard
        only fires on truthy unconsumed flags."""
        r = self._run(sc_env, check=None)
        assert r.returncode in (0, 1), (
            f"default sweep regressed: exit {r.returncode}\n{r.stderr}"
        )
        assert "Traceback" not in r.stderr


# ---------------------------------------------------------------------------
# Dispatcher EXTRA_SCAN_FLAGS contract
# ---------------------------------------------------------------------------

class TestExtraScanFlagsContract:
    def test_declared_flag_without_argparse_attr_raises(self, tmp_path):
        """A module declaring an EXTRA_SCAN_FLAGS entry with no matching
        argparse attribute is a contract violation → AttributeError, not a
        silent drop."""
        fake_mod = types.SimpleNamespace(
            NAME="fake-flags",
            EXTRA_SCAN_FLAGS=("bogus_flag",),
            scan=lambda *a, **kw: [],
            apply=lambda *a, **kw: [],
        )
        args = vault_doctor._build_parser().parse_args([])
        cfg = {
            "vault": str(tmp_path),
            "sessions_folder": "claude-sessions",
            "insights_folder": "claude-insights",
        }
        with pytest.raises(AttributeError):
            vault_doctor._run_scan(fake_mod, cfg, 30, None, args=args)

    def test_declared_flags_forwarded_unconditionally(self, tmp_path):
        """Declared flags reach scan() as real bools even when False."""
        received = {}

        def _fake_scan(vault, sf, inf, days, project=None, strict=None, reconstruct=None):
            received["strict"] = strict
            received["reconstruct"] = reconstruct
            return []

        fake_mod = types.SimpleNamespace(
            NAME="fake-flags-2",
            EXTRA_SCAN_FLAGS=("strict", "reconstruct"),
            scan=_fake_scan,
            apply=lambda *a, **kw: [],
        )
        args = vault_doctor._build_parser().parse_args([])  # both default False
        cfg = {
            "vault": str(tmp_path),
            "sessions_folder": "claude-sessions",
            "insights_folder": "claude-insights",
        }
        vault_doctor._run_scan(fake_mod, cfg, 30, None, args=args)
        assert received == {"strict": False, "reconstruct": False}


# ---------------------------------------------------------------------------
# Test: registry (opt-in)
# ---------------------------------------------------------------------------

def test_session_coverage_excluded_from_default_sweep():
    """session-coverage is OPT-IN — excluded from the default all-checks sweep
    (heavy ~/.claude/projects walk + standing-audit semantics)."""
    names = [m.NAME for m in vault_doctor_checks.all_checks()]
    assert sc.NAME not in names


def test_session_coverage_reachable_via_get_check():
    mod = vault_doctor_checks.get_check(sc.NAME)
    assert mod is sc
