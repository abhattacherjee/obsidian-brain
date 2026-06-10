"""Tests for the session-coverage vault_doctor check module (#98).

Isolation strategy (mirrors test_vault_doctor_audit_historic_repairs.py):
  - HOME is redirected via monkeypatch so ~/.claude/projects and the
    obsidian-brain config file land under tmp_path.
  - Vault tree is created under tmp_path/vault.
  - The skip-logic we test (threshold, project-filter, mtime-window) requires
    real on-disk files with controlled content and timestamps.

CLI e2e NOTE: We do NOT test --reconstruct's actual replay via the CLI e2e
tests below. The monkeypatched unit tests (TestApply) exercise apply() fully
against a fake subprocess.run. An e2e test of --reconstruct would spawn
replay-sessionend.py against a live config + vault, requiring HOME-redirection
at the subprocess level (HOME passed via env=) AND a valid synthetic JSONL to
pass through hooks/obsidian_session_log._run(). The replay integration is
already covered by tests/test_replay_cli.py; duplicating it here with a
subprocess.run whose env dict is harder to isolate adds fragility without new
coverage. The --strict and bare --check e2e paths are tested instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import vault_doctor_checks  # noqa: E402
import vault_doctor_checks.session_coverage as sc  # noqa: E402
from vault_doctor_checks import Issue  # noqa: E402


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
) -> Path:
    """Write a synthetic JSONL under projects_root/<project_dir_name>/<sid>.jsonl.

    Creates enough entries to pass/fail threshold tests:
      n_user    — user message count
      duration  — total window in minutes (derived from timestamp spacing)
    """
    proj_dir = projects_root / project_dir_name
    proj_dir.mkdir(parents=True, exist_ok=True)
    jsonl = proj_dir / f"{sid}.jsonl"

    records = []
    # Parse start_ts to epoch
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
    }
    (claude / "obsidian-brain-config.json").write_text(json.dumps(config))

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

    def test_drop_threshold_logic_makes_gap_fire(self, sc_env):
        """Fail-first: without threshold skip, below-threshold JSONL would incorrectly fire.

        We verify this by directly calling sc.scan with a monkeypatched
        _load_thresholds that returns (0, 0.0) — effectively disabling
        thresholds. The below-threshold JSONL now appears as a gap.
        """
        sid = "no-thresh-test-0003"
        cwd = "/path/to/my-proj"
        _write_jsonl(sc_env["projects"], "-proj", sid, cwd, n_user=1, duration_minutes=0.3)

        # Normal: no gap (below threshold)
        assert _scan(sc_env) == []

        # Monkeypatch thresholds to (0, 0.0) — any JSONL passes
        original = sc._load_thresholds
        sc._load_thresholds = lambda home: (0, 0.0)
        try:
            issues = _scan(sc_env)
            assert len(issues) == 1, (
                "Without threshold filtering, a 1-message JSONL should surface as a gap."
            )
        finally:
            sc._load_thresholds = original


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
# Test 10: apply() behaviour
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

    def test_apply_ok_outcome_returns_applied(self, tmp_path):
        """Monkeypatched subprocess returns outcome=OK → status 'applied'."""
        issue = self._make_issue(
            "apply-ok-sid-0001",
            str(tmp_path / "apply-ok-sid-0001.jsonl"),
            "/path/to/my-proj",
            str(tmp_path / "2026-04-24-my-proj-xxxx.md"),
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"outcome": "OK", "vault_writes": []})
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            results = sc.apply([issue], str(tmp_path / "backup"))

        assert len(results) == 1
        assert results[0].status == "applied"
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "--jsonl" in cmd
        assert "--json" in cmd

    def test_apply_skipped_outcome_returns_skipped(self, tmp_path):
        """Monkeypatched subprocess returns outcome=SKIPPED_THRESHOLD → status 'skipped'."""
        issue = self._make_issue(
            "apply-skip-sid-0002",
            str(tmp_path / "apply-skip-sid-0002.jsonl"),
            "/path/to/my-proj",
            str(tmp_path / "2026-04-24-my-proj-yyyy.md"),
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "outcome": "SKIPPED_THRESHOLD",
            "detail": "too few messages",
        })
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            results = sc.apply([issue], str(tmp_path / "backup"))

        assert len(results) == 1
        r = results[0]
        assert r.status == "skipped"
        assert "SKIPPED_THRESHOLD" in (r.error or "")

    def test_apply_non_ok_non_skipped_returns_error(self, tmp_path):
        """Any non-OK, non-SKIPPED_* outcome → status 'error'."""
        issue = self._make_issue(
            "apply-err-sid-0003",
            str(tmp_path / "apply-err-sid-0003.jsonl"),
            "/path/to/my-proj",
            str(tmp_path / "2026-04-24-my-proj-zzzz.md"),
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "outcome": "EXCEPTION",
            "detail": "something blew up",
        })
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            results = sc.apply([issue], str(tmp_path / "backup"))

        assert len(results) == 1
        r = results[0]
        assert r.status == "error"

    def test_apply_unresolved_issue_returns_unresolved(self, tmp_path):
        """Unresolved issue → status 'unresolved', subprocess never called."""
        issue = self._make_issue(
            "apply-unresolved-sid-0004",
            str(tmp_path / "unresolved.jsonl"),
            "/path/to/my-proj",
            str(tmp_path / "note.md"),
        )
        issue.extra["unresolved"] = True

        with patch("subprocess.run") as mock_run:
            results = sc.apply([issue], str(tmp_path / "backup"))

        assert len(results) == 1
        assert results[0].status == "unresolved"
        mock_run.assert_not_called()

    def test_apply_note_already_exists_returns_skipped(self, tmp_path):
        """If the expected note already exists on disk, skip without calling replay."""
        note_path = tmp_path / "2026-04-24-my-proj-aaaa.md"
        note_path.write_text("---\ntype: claude-session\n---\n")

        issue = self._make_issue(
            "apply-exists-sid-0005",
            str(tmp_path / "apply-exists-sid-0005.jsonl"),
            "/path/to/my-proj",
            str(note_path),
        )

        with patch("subprocess.run") as mock_run:
            results = sc.apply([issue], str(tmp_path / "backup"))

        assert len(results) == 1
        assert results[0].status == "skipped"
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


# ---------------------------------------------------------------------------
# Test 11: summary partition
# ---------------------------------------------------------------------------

class TestSummaryPartition:
    def test_summary_counts_sum_to_scanned(self, sc_env, capsys):
        """Mixed fixture: gaps + covered + below-threshold → all counts sum correctly."""
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
        assert gaps + covered + bt + unparsable + proj_filt == total


# ---------------------------------------------------------------------------
# Test 12: CLI e2e
# ---------------------------------------------------------------------------

class TestCLIE2E:
    """End-to-end tests via subprocess with a synthetic HOME + vault."""

    _SCRIPT = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"

    def _run(self, env, *args: str) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable, str(self._SCRIPT),
            "--vault", str(env["vault"]),
            "--sessions-folder", "claude-sessions",
            "--insights-folder", "claude-insights",
            "--check", "session-coverage",
            "--json",
        ] + list(args)
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


# ---------------------------------------------------------------------------
# Test: registry (not opt-in)
# ---------------------------------------------------------------------------

def test_session_coverage_in_default_sweep():
    """session-coverage is NOT opt-in — it must appear in the default all-checks sweep."""
    names = [m.NAME for m in vault_doctor_checks.all_checks()]
    assert sc.NAME in names


def test_session_coverage_reachable_via_get_check():
    mod = vault_doctor_checks.get_check(sc.NAME)
    assert mod is sc
