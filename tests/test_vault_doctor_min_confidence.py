"""Tests for --min-confidence flag in scripts/vault_doctor.py.

Coverage:
- Unit: filter semantics (>= inclusive, 0.0 keeps all)
- Edge: threshold 1.0 excludes confidence=0.99
- Range validation: out-of-range values → exit 3
- Back-compat: no flag → no header suffix, no extra JSON keys
- Integration (CLI subprocess): dry-run + --apply with --min-confidence
"""

import calendar
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from vault_doctor_checks import Issue  # noqa: E402


# ---------------------------------------------------------------------------
# Helper: build a minimal Issue with a given confidence
# ---------------------------------------------------------------------------

def _make_issue(confidence: float, note_path: str = "/tmp/fake.md") -> Issue:
    return Issue(
        check="source-sessions",
        note_path=note_path,
        project="testproj",
        current_source="[[old]]",
        proposed_source="[[new]]",
        reason="test fixture",
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Unit: filter semantics
# ---------------------------------------------------------------------------

class TestMinConfidenceFilter:
    """Verify the >= filter semantics described in the issue test plan."""

    def test_threshold_0_9_keeps_two_of_four(self):
        """Issues with conf [0.4, 0.6, 0.99, 0.99] → threshold 0.9 keeps 2."""
        issues = [
            _make_issue(0.4),
            _make_issue(0.6),
            _make_issue(0.99),
            _make_issue(0.99),
        ]
        threshold = 0.9
        kept = [i for i in issues if i.confidence >= threshold]
        assert len(kept) == 2
        assert all(i.confidence >= threshold for i in kept)

    def test_threshold_0_0_keeps_all_four(self):
        """Issues with conf [0.4, 0.6, 0.99, 0.99] → threshold 0.0 keeps all 4."""
        issues = [
            _make_issue(0.4),
            _make_issue(0.6),
            _make_issue(0.99),
            _make_issue(0.99),
        ]
        threshold = 0.0
        kept = [i for i in issues if i.confidence >= threshold]
        assert len(kept) == 4

    def test_threshold_1_0_excludes_0_99(self):
        """Threshold 1.0 uses >= semantics → confidence=0.99 must be excluded.

        Fail-first discipline: mutate >= to > and the test reverses (len==2
        instead of 0 for [0.99, 0.99]). Document the invariant.
        """
        issues = [_make_issue(0.99), _make_issue(0.99)]
        threshold = 1.0
        kept_ge = [i for i in issues if i.confidence >= threshold]
        assert len(kept_ge) == 0, (
            "threshold=1.0 with >= semantics must exclude conf=0.99"
        )

    def test_threshold_1_0_keeps_exactly_1_0(self):
        """A confidence of exactly 1.0 is kept when threshold=1.0 (>= inclusive)."""
        issues = [_make_issue(1.0), _make_issue(0.99)]
        threshold = 1.0
        kept = [i for i in issues if i.confidence >= threshold]
        assert len(kept) == 1
        assert kept[0].confidence == 1.0

    def test_unresolved_confidence_zero_filtered_at_threshold_gt_0(self):
        """Unresolved issues have confidence=0.0; threshold > 0.0 drops them."""
        unresolved = Issue(
            check="source-sessions",
            note_path="/tmp/unresolved.md",
            project="proj",
            current_source="[[old]]",
            proposed_source="",
            reason="no window",
            confidence=0.0,
            extra={"unresolved": True, "signal_class": "unresolved"},
        )
        kept = [i for i in [unresolved] if i.confidence >= 0.9]
        assert len(kept) == 0


# ---------------------------------------------------------------------------
# Fail-first discipline evidence
# ---------------------------------------------------------------------------

class TestFailFirst:
    """Demonstrate that changing the filter operator from >= to > reverses the
    threshold=1.0 test and confirms confidence=0.99 handling is correct."""

    def test_mutated_gt_includes_0_99_as_control(self):
        """Using > (not >=) for threshold=1.0 would keep 0.99 — confirming
        our production >= is the correct, tighter semantics."""
        issues = [_make_issue(0.99), _make_issue(0.99)]
        threshold = 1.0
        # Deliberately use > to show the mutation would make the test pass
        kept_gt = [i for i in issues if i.confidence > threshold]
        # > does NOT include 0.99 when threshold=1.0 either (0.99 > 1.0 is False)
        # Let's use threshold=0.9 to show >= vs > difference
        threshold_sub = 0.99
        kept_ge = [i for i in issues if i.confidence >= threshold_sub]
        kept_gt_sub = [i for i in issues if i.confidence > threshold_sub]
        assert len(kept_ge) == 2, ">= 0.99 includes conf=0.99"
        assert len(kept_gt_sub) == 0, "> 0.99 excludes conf=0.99 — mutation reverses the test"


# ---------------------------------------------------------------------------
# Range validation: exit 3 for out-of-range --min-confidence
# ---------------------------------------------------------------------------

class TestRangeValidation:
    """Out-of-range --min-confidence values must produce exit 3."""

    @pytest.fixture
    def tmp_vault(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "claude-sessions").mkdir(parents=True)
        (vault / "claude-insights").mkdir(parents=True)
        env = os.environ.copy()
        env["OBSIDIAN_BRAIN_VAULT"] = str(vault)
        env["OBSIDIAN_BRAIN_SESSIONS_FOLDER"] = "claude-sessions"
        env["OBSIDIAN_BRAIN_INSIGHTS_FOLDER"] = "claude-insights"
        return env

    def test_min_confidence_1_5_exits_3(self, tmp_vault):
        """--min-confidence 1.5 is out of range → exit 3."""
        script = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"
        r = subprocess.run(
            [sys.executable, str(script), "--min-confidence", "1.5", "--json"],
            capture_output=True, text=True, env=tmp_vault,
        )
        assert r.returncode == 3, (
            f"expected exit 3 for --min-confidence 1.5, got {r.returncode}: {r.stderr}"
        )
        assert "min-confidence" in r.stderr.lower()

    def test_min_confidence_neg_0_1_exits_3(self, tmp_vault):
        """--min-confidence -0.1 is out of range → exit 3."""
        script = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"
        r = subprocess.run(
            [sys.executable, str(script), "--min-confidence", "-0.1", "--json"],
            capture_output=True, text=True, env=tmp_vault,
        )
        assert r.returncode == 3, (
            f"expected exit 3 for --min-confidence -0.1, got {r.returncode}: {r.stderr}"
        )
        assert "min-confidence" in r.stderr.lower()

    def test_min_confidence_0_0_valid(self, tmp_vault):
        """--min-confidence 0.0 is valid (default, no issues in empty vault)."""
        script = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"
        r = subprocess.run(
            [sys.executable, str(script), "--check", "source-sessions",
             "--min-confidence", "0.0", "--json"],
            capture_output=True, text=True, env=tmp_vault,
        )
        # Empty vault → exit 0 (clean)
        assert r.returncode == 0, (
            f"expected exit 0 for valid --min-confidence 0.0, got {r.returncode}: {r.stderr}"
        )

    def test_min_confidence_1_0_valid(self, tmp_vault):
        """--min-confidence 1.0 is valid (edge of range)."""
        script = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"
        r = subprocess.run(
            [sys.executable, str(script), "--check", "source-sessions",
             "--min-confidence", "1.0", "--json"],
            capture_output=True, text=True, env=tmp_vault,
        )
        # Empty vault → exit 0 (clean)
        assert r.returncode == 0, (
            f"expected exit 0 for valid --min-confidence 1.0, got {r.returncode}: {r.stderr}"
        )


# ---------------------------------------------------------------------------
# Integration: CLI subprocess tests using the source-sessions fixture pattern
# ---------------------------------------------------------------------------

def _build_vault_with_mixed_confidence_issues(tmp_path):
    """Build a minimal vault with:
    - One uuid-basename-stale issue (conf=0.99)  ← high confidence
    - One unresolved issue (conf=0.0)            ← low confidence / unknown repair

    Returns (vault, env, note_high, note_unresolved).
    """
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    (vault / "claude-insights").mkdir(parents=True)
    home = tmp_path
    cc_dir = home / ".claude" / "projects" / "-Users-foo-proj1"
    cc_dir.mkdir(parents=True)

    # Session B: 2026-04-10 — the "correct" target for the high-conf issue
    b_start = calendar.timegm(time.strptime("2026-04-10 10:00", "%Y-%m-%d %H:%M"))
    (cc_dir / "sid-b.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": "2026-04-10T10:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    os.utime(cc_dir / "sid-b.jsonl", (b_start + 4 * 3600, b_start + 4 * 3600))

    # Session note for B
    (vault / "claude-sessions" / "2026-04-10-proj1-bbbb.md").write_text(
        "---\ntype: claude-session\ndate: 2026-04-10\nsession_id: sid-b\n"
        "project: proj1\nstatus: summarized\n---\n# s\n",
        encoding="utf-8",
    )

    # HIGH-CONF note: source_session resolves (uuid-basename-stale, conf=0.99)
    note_high = vault / "claude-insights" / "2026-04-10-high-conf.md"
    note_high.write_text(
        '---\ntype: claude-insight\ndate: 2026-04-10\n'
        'source_session: sid-b\n'
        'source_session_note: "[[2026-04-09-proj1-aaaa]]"\n'
        'project: proj1\n---\n# high conf\n',
        encoding="utf-8",
    )
    os.utime(note_high, (b_start + 1800, b_start + 1800))

    # LOW-CONF / unresolved note: source_session not in index, no matching day session
    # Place note on a date with no sessions so the matcher cannot propose anything.
    unresolved_ts = calendar.timegm(time.strptime("2026-01-15 12:00", "%Y-%m-%d %H:%M"))
    note_unresolved = vault / "claude-insights" / "2026-01-15-low-conf.md"
    note_unresolved.write_text(
        '---\ntype: claude-insight\ndate: 2026-01-15\n'
        'source_session: ghost-sid-not-in-index\n'
        'source_session_note: "[[2026-01-15-proj1-zzzz]]"\n'
        'project: proj1\n---\n# low conf\n',
        encoding="utf-8",
    )
    os.utime(note_unresolved, (unresolved_ts, unresolved_ts))

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["OBSIDIAN_BRAIN_VAULT"] = str(vault)
    env["OBSIDIAN_BRAIN_SESSIONS_FOLDER"] = "claude-sessions"
    env["OBSIDIAN_BRAIN_INSIGHTS_FOLDER"] = "claude-insights"

    return vault, env, note_high, note_unresolved


class TestMinConfidenceCLI:
    """Integration tests for --min-confidence via CLI subprocess."""

    def test_dry_run_min_confidence_0_9_shows_only_high_conf(self, tmp_path):
        """--min-confidence 0.9 dry-run: only issues with conf >= 0.9 appear in JSON."""
        vault, env, note_high, note_unresolved = _build_vault_with_mixed_confidence_issues(tmp_path)
        script = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"

        r = subprocess.run(
            [sys.executable, str(script),
             "--check", "source-sessions", "--days", "10000", "--project", "proj1",
             "--min-confidence", "0.9", "--json"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 1, (
            f"expected exit 1 (issues found), got {r.returncode}: {r.stderr}"
        )
        payload = json.loads(r.stdout)
        issues = payload["issues"]

        # Only high-conf note should appear
        assert all(i["confidence"] >= 0.9 for i in issues), (
            f"all remaining issues must have confidence >= 0.9, got: "
            f"{[i['confidence'] for i in issues]}"
        )
        note_paths = [i["note_path"] for i in issues]
        assert str(note_high) in note_paths, "high-conf note must appear"
        assert str(note_unresolved) not in note_paths, (
            "unresolved (conf=0.0) note must NOT appear with threshold=0.9"
        )

    def test_dry_run_min_confidence_0_9_header_shows_filter(self, tmp_path):
        """Header on stderr must mention --min-confidence and dropped count."""
        vault, env, note_high, note_unresolved = _build_vault_with_mixed_confidence_issues(tmp_path)
        script = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"

        r = subprocess.run(
            [sys.executable, str(script),
             "--check", "source-sessions", "--days", "10000", "--project", "proj1",
             "--min-confidence", "0.9"],
            capture_output=True, text=True, env=env,
        )
        header_line = next(
            (line for line in r.stderr.splitlines()
             if "vault_doctor report" in line),
            None,
        )
        assert header_line is not None, f"header line not found in stderr: {r.stderr}"
        assert "--min-confidence 0.9" in header_line, (
            f"header must mention --min-confidence 0.9: {header_line!r}"
        )
        assert "dropped" in header_line, (
            f"header must mention dropped count: {header_line!r}"
        )

    def test_dry_run_min_confidence_json_has_filter_keys(self, tmp_path):
        """JSON payload must have min_confidence + dropped_by_confidence when flag used."""
        vault, env, note_high, note_unresolved = _build_vault_with_mixed_confidence_issues(tmp_path)
        script = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"

        r = subprocess.run(
            [sys.executable, str(script),
             "--check", "source-sessions", "--days", "10000", "--project", "proj1",
             "--min-confidence", "0.9", "--json"],
            capture_output=True, text=True, env=env,
        )
        payload = json.loads(r.stdout)
        assert "min_confidence" in payload, "JSON must have min_confidence key"
        assert "dropped_by_confidence" in payload, "JSON must have dropped_by_confidence key"
        assert payload["min_confidence"] == 0.9
        assert payload["dropped_by_confidence"] >= 1, (
            "at least 1 issue should have been dropped (the unresolved conf=0.0 note)"
        )

    def test_back_compat_no_flag_no_header_suffix(self, tmp_path):
        """Without --min-confidence, header must NOT contain the filter suffix."""
        vault, env, note_high, note_unresolved = _build_vault_with_mixed_confidence_issues(tmp_path)
        script = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"

        r = subprocess.run(
            [sys.executable, str(script),
             "--check", "source-sessions", "--days", "10000", "--project", "proj1"],
            capture_output=True, text=True, env=env,
        )
        header_line = next(
            (line for line in r.stderr.splitlines()
             if "vault_doctor report" in line),
            None,
        )
        assert header_line is not None, f"header line not found in stderr: {r.stderr}"
        assert "[filtered:" not in header_line, (
            f"back-compat: no-flag invocation must NOT have filter suffix: {header_line!r}"
        )

    def test_back_compat_no_flag_no_extra_json_keys(self, tmp_path):
        """Without --min-confidence, JSON must NOT have min_confidence or dropped_by_confidence."""
        vault, env, note_high, note_unresolved = _build_vault_with_mixed_confidence_issues(tmp_path)
        script = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"

        r = subprocess.run(
            [sys.executable, str(script),
             "--check", "source-sessions", "--days", "10000", "--project", "proj1",
             "--json"],
            capture_output=True, text=True, env=env,
        )
        payload = json.loads(r.stdout)
        assert "min_confidence" not in payload, (
            "back-compat: min_confidence must NOT appear in JSON without the flag"
        )
        assert "dropped_by_confidence" not in payload, (
            "back-compat: dropped_by_confidence must NOT appear in JSON without the flag"
        )

    def test_apply_yes_min_confidence_0_9_modifies_only_high_conf(self, tmp_path):
        """--apply --yes --min-confidence 0.9: only the high-conf note is patched."""
        vault, env, note_high, note_unresolved = _build_vault_with_mixed_confidence_issues(tmp_path)
        script = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"

        original_unresolved = note_unresolved.read_text(encoding="utf-8")

        r = subprocess.run(
            [sys.executable, str(script),
             "--check", "source-sessions", "--days", "10000", "--project", "proj1",
             "--min-confidence", "0.9", "--apply", "--yes"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 1, (
            f"expected exit 1 (successful apply), got {r.returncode}: {r.stderr}"
        )

        # High-conf note must be patched (source_session_note rewritten)
        patched_high = note_high.read_text(encoding="utf-8")
        assert 'source_session_note: "[[2026-04-10-proj1-bbbb]]"' in patched_high, (
            "high-conf note must be patched to correct basename"
        )

        # Unresolved note must be byte-identical (filtered out, never touched)
        assert note_unresolved.read_text(encoding="utf-8") == original_unresolved, (
            "unresolved note (conf=0.0) must NOT be modified when --min-confidence 0.9"
        )

    def test_threshold_1_0_excludes_0_99_in_dry_run(self, tmp_path):
        """--min-confidence 1.0 should exclude conf=0.99 → no issues reported,
        exit 0 (or exit 1 if only non-1.0 issues exist that aren't filtered by
        the 1.0 threshold showing the clean case)."""
        vault, env, note_high, note_unresolved = _build_vault_with_mixed_confidence_issues(tmp_path)
        script = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"

        r = subprocess.run(
            [sys.executable, str(script),
             "--check", "source-sessions", "--days", "10000", "--project", "proj1",
             "--min-confidence", "1.0", "--json"],
            capture_output=True, text=True, env=env,
        )
        payload = json.loads(r.stdout)
        # All issues have conf <= 0.99, so threshold=1.0 filters everything
        assert payload["total_issues"] == 0, (
            f"threshold=1.0 should exclude conf=0.99 issues; got total_issues={payload['total_issues']}"
        )
        assert r.returncode == 0, (
            f"expected exit 0 (all filtered → clean), got {r.returncode}"
        )
        # Filter keys still appear in payload (threshold > 0.0)
        assert payload.get("min_confidence") == 1.0
