"""Tests for the retro-classification gate (hooks/obsidian_retro_gate.py).

Covers:
  - Subprocess (e2e) tests that drive the hook with synthetic JSON stdin.
  - Unit tests for the three obsidian_utils helpers.

All subprocess tests pass HOME= in env so sentinels land in a tmp HOME
and cannot pollute the real ~/.claude/obsidian-brain/retro-gate/.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# conftest.py already inserts hooks/ onto sys.path.
import obsidian_utils

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = REPO_ROOT / "hooks" / "obsidian_retro_gate.py"

SID = "test-retro-session-abc123"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_hook(payload: dict, tmp_home: Path) -> subprocess.CompletedProcess:
    """Run the retro-gate hook with the given JSON payload and isolated HOME."""
    env = {**os.environ, "HOME": str(tmp_home)}
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def _write_sentinel(tmp_home: Path, session_id: str, retro_path: str = "/tmp/retro.md",
                    created_at: float | None = None) -> Path:
    """Write a sentinel file directly into the tmp HOME's retro-gate dir."""
    gate_dir = tmp_home / ".claude" / "obsidian-brain" / "retro-gate"
    gate_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    sanitized = "".join(c if c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-" else "_"
                        for c in session_id)
    sentinel = gate_dir / f"{sanitized}.json"
    payload = {
        "session_id": session_id,
        "retro_path": retro_path,
        "created_at": created_at if created_at is not None else time.time(),
    }
    sentinel.write_text(json.dumps(payload), encoding="utf-8")
    sentinel.chmod(0o600)
    return sentinel


# ---------------------------------------------------------------------------
# Subprocess (e2e) tests
# ---------------------------------------------------------------------------


class TestRetroGateSubprocess:
    def test_blocks_when_pending_fresh(self, tmp_path):
        """Fresh sentinel + stop_hook_active False → decision==block."""
        tmp_home = tmp_path / "home"
        tmp_home.mkdir()
        _write_sentinel(tmp_home, SID)

        result = _run_hook({"session_id": SID, "stop_hook_active": False}, tmp_home)

        assert result.returncode == 0
        stdout = result.stdout.strip()
        assert stdout, "Expected block output but got empty stdout"
        data = json.loads(stdout)
        assert data["decision"] == "block"
        assert "Step 7.5" in data["reason"]

    def test_allows_when_absent(self, tmp_path):
        """No sentinel → exit 0, empty stdout (no block)."""
        tmp_home = tmp_path / "home"
        tmp_home.mkdir()
        # Don't write a sentinel.

        result = _run_hook({"session_id": SID, "stop_hook_active": False}, tmp_home)

        assert result.returncode == 0
        stdout = result.stdout.strip()
        assert stdout == "", f"Expected empty stdout but got: {stdout!r}"

    def test_allows_and_clears_when_stop_hook_active(self, tmp_path):
        """Sentinel present + stop_hook_active True → no block + sentinel removed."""
        tmp_home = tmp_path / "home"
        tmp_home.mkdir()
        sentinel = _write_sentinel(tmp_home, SID)
        assert sentinel.exists()

        result = _run_hook({"session_id": SID, "stop_hook_active": True}, tmp_home)

        assert result.returncode == 0
        stdout = result.stdout.strip()
        assert stdout == "", f"Expected empty stdout but got: {stdout!r}"
        assert not sentinel.exists(), "Sentinel should have been cleared"

    def test_allows_and_clears_when_stale(self, tmp_path):
        """Stale sentinel (3 hours old) → no block + sentinel removed."""
        tmp_home = tmp_path / "home"
        tmp_home.mkdir()
        stale_ts = time.time() - 3 * 3600  # 3 hours ago
        sentinel = _write_sentinel(tmp_home, SID, created_at=stale_ts)
        assert sentinel.exists()

        result = _run_hook({"session_id": SID, "stop_hook_active": False}, tmp_home)

        assert result.returncode == 0
        stdout = result.stdout.strip()
        assert stdout == "", f"Expected empty stdout but got: {stdout!r}"
        assert not sentinel.exists(), "Stale sentinel should have been cleared"

    def test_fail_open_on_malformed_stdin(self, tmp_path):
        """Malformed JSON stdin → exit 0, no block output."""
        tmp_home = tmp_path / "home"
        tmp_home.mkdir()

        env = {**os.environ, "HOME": str(tmp_home)}
        result = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input="not valid json {{{{",
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 0
        stdout = result.stdout.strip()
        assert stdout == "", f"Expected empty stdout but got: {stdout!r}"


# ---------------------------------------------------------------------------
# Cross-source (arm via get_session_context(), check via the Stop hook) — #330
# ---------------------------------------------------------------------------
#
# Every test above (and every unit test below) writes the sentinel directly
# with a hardcoded sid — it never exercises the real arm path, which resolves
# the sid through get_session_context() (the retro skill's Step 7 call site).
# These tests drive BOTH sides for real: arm via get_session_context() (with
# CLAUDE_CODE_SESSION_ID set, so resolution layer 0 supplies the id) and check
# via the hook subprocess fed the harness's own stdin session_id, exactly as
# the Stop hook receives it in production. #330's whole premise is that these
# two sources can disagree even though both sanitize identically — nothing
# before this class would have caught that.


class TestRetroGateCrossSource:
    @pytest.fixture(autouse=True)
    def _redirect_home(self, tmp_path, monkeypatch):
        self._tmp_home = tmp_path / "home"
        self._tmp_home.mkdir()
        monkeypatch.setenv("HOME", str(self._tmp_home))

    def _run_hook_here(self, session_id: str, stop_hook_active: bool = False) -> subprocess.CompletedProcess:
        return _run_hook(
            {"session_id": session_id, "stop_hook_active": stop_hook_active},
            self._tmp_home,
        )

    def test_arm_via_get_session_context_then_check_via_hook(self, monkeypatch):
        """The real arm->check path, end to end.

        Arm: resolve the sid via get_session_context() (layer 0 = the
        CLAUDE_CODE_SESSION_ID env var, same as skills/retro/SKILL.md Step 7
        does via ctx["session_id"]), then call
        mark_retro_classification_pending() with THAT resolved value.

        Check: drive hooks/obsidian_retro_gate.py as a subprocess with the
        SAME id in its synthetic stdin JSON, as the harness does. This is the
        pairing that was previously untested — every other test in this file
        writes the sentinel directly with a hardcoded sid.
        """
        sid = "e2e-cross-source-9f8e7d1c"
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)

        ctx = obsidian_utils.get_session_context()
        assert ctx["session_id"] == sid, "layer 0 should resolve the env var verbatim"

        result = obsidian_utils.mark_retro_classification_pending(ctx["session_id"], "/vault/e2e-retro.md")
        assert result, "Expected a non-empty sentinel path"

        blocked = self._run_hook_here(sid, stop_hook_active=False)
        assert blocked.returncode == 0
        stdout = blocked.stdout.strip()
        assert stdout, "Expected block output but got empty stdout"
        data = json.loads(stdout)
        assert data["decision"] == "block"

        # Clear (as the hook itself does on a stop_hook_active re-entry) and
        # confirm enforcement stops.
        cleared = self._run_hook_here(sid, stop_hook_active=True)
        assert cleared.returncode == 0
        assert cleared.stdout.strip() == ""

        after_clear = self._run_hook_here(sid, stop_hook_active=False)
        assert after_clear.returncode == 0
        assert after_clear.stdout.strip() == "", "Gate should no longer block after clearing"

    def test_crossing_regression_arm_a_check_b_does_not_block(self, monkeypatch):
        """Pins the #330 failure mode: arm under one sid, check under another.

        This documents the DAMAGE the issue reports, not desired behaviour —
        a real id-crossing bug (get_session_context() resolving a different
        session's id on two calls within one turn) reproduces exactly this
        shape: the gate is armed under sid A's key, the Stop hook looks up
        sid B, finds nothing, and enforcement silently vanishes. There is no
        fix for this test to pin beyond Tasks 1-3 making arm and check agree
        by construction — this test exists so a future regression that makes
        them disagree again is caught here, not discovered via a silently
        skipped classification step in production.
        """
        sid_a = "e2e-crossing-sid-a-111"
        sid_b = "e2e-crossing-sid-b-222"

        result = obsidian_utils.mark_retro_classification_pending(sid_a, "/vault/crossing.md")
        assert result, "Expected a non-empty sentinel path for sid A"

        checked_under_b = self._run_hook_here(sid_b, stop_hook_active=False)
        assert checked_under_b.returncode == 0
        assert checked_under_b.stdout.strip() == "", (
            "Gate armed under sid A must NOT block a Stop hook invocation for "
            "sid B — this is the silent enforcement loss #330 reports, not a "
            "desired outcome"
        )


# ---------------------------------------------------------------------------
# Unit tests for obsidian_utils helpers
# ---------------------------------------------------------------------------


class TestRetroGateHelpers:
    """In-process tests for mark/get/clear helpers.

    HOME is redirected via monkeypatch so helpers write to tmp_path.
    """

    @pytest.fixture(autouse=True)
    def _redirect_home(self, tmp_path, monkeypatch):
        """Redirect HOME so retro-gate helpers land in tmp_path."""
        self._tmp_home = tmp_path / "home"
        self._tmp_home.mkdir()
        monkeypatch.setenv("HOME", str(self._tmp_home))
        # Also patch Path.home() by overriding it in the module under test.
        # Python's Path.home() reads HOME env, so monkeypatching HOME is enough
        # (Path.home() re-evaluates on each call on CPython when HOME changes).

    def _gate_dir(self) -> Path:
        return self._tmp_home / ".claude" / "obsidian-brain" / "retro-gate"

    def test_mark_creates_sentinel(self):
        path = obsidian_utils.mark_retro_classification_pending(SID, "/vault/retro.md")
        assert path, "Expected a non-empty path"
        sentinel = Path(path)
        assert sentinel.exists()
        data = json.loads(sentinel.read_text(encoding="utf-8"))
        assert data["session_id"] == SID
        assert data["retro_path"] == "/vault/retro.md"
        assert isinstance(data["created_at"], float)

    def test_mark_refuses_unusable_session_id(self):
        """#330 task 4: empty, whitespace-only, and the literal "unknown"
        must refuse to arm the gate — an explicit error string, not "".

        Arming under a dead key is worse than not arming: the Stop hook
        looks up the HARNESS's own session_id and would never find a
        sentinel filed under "unknown" or blank, so the gate would appear
        armed to the skill (a truthy return) while the Stop hook enforces
        nothing — the exact silent-loss-of-enforcement failure #330 reports.
        A visible refusal lets the skill surface it instead.
        """
        gate_dir = self._gate_dir()
        before = set(gate_dir.glob("*.json")) if gate_dir.exists() else set()

        for bad_sid in ("", "   ", "unknown", None):
            result = obsidian_utils.mark_retro_classification_pending(bad_sid, "/vault/retro.md")  # type: ignore[arg-type]
            assert result, f"Expected a non-empty error string for {bad_sid!r}, got {result!r}"
            assert result.lower().startswith("failed"), (
                f"Expected an explicit refusal string for {bad_sid!r}, got {result!r}"
            )

        after = set(gate_dir.glob("*.json")) if gate_dir.exists() else set()
        assert after == before, "No sentinel file should be written for any refused session_id"

    def test_mark_still_arms_for_a_valid_session_id(self):
        """Regression guard for the refusal guard above: a normal, non-empty,
        non-"unknown" session_id must still arm the gate exactly as before."""
        path = obsidian_utils.mark_retro_classification_pending(SID, "/vault/retro.md")
        assert path, "Expected a non-empty sentinel path"
        assert not path.lower().startswith("failed")
        assert Path(path).exists()

    def test_mark_returns_failed_on_mkdir_oserror(self, monkeypatch):
        """#330 review item 3: a gate-dir mkdir OSError is a genuinely
        reachable failure path and must return an explicit "Failed: ..."
        string, not a bare "" that the skill would print as a blank line
        and mistake for success."""
        gate_dir = self._gate_dir()
        real_mkdir = Path.mkdir

        def fake_mkdir(self, *args, **kwargs):
            if self == gate_dir:
                raise OSError("simulated mkdir failure")
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)
        result = obsidian_utils.mark_retro_classification_pending(SID, "/vault/retro.md")
        assert result.startswith("Failed:")
        assert "simulated mkdir failure" in result
        assert not gate_dir.exists()

    def test_mark_returns_failed_when_sentinel_escapes_gate_dir(self, monkeypatch):
        """#330 review item 3: a sentinel path that resolves outside the gate
        dir is a genuinely reachable failure path and must return an
        explicit "Failed: ..." string."""
        gate_dir = self._gate_dir()
        gate_dir.mkdir(parents=True, exist_ok=True)
        sanitized = obsidian_utils._RETRO_SID_SAFE.sub("_", SID)
        expected_resolved = (gate_dir / f"{sanitized}.json").resolve()

        real_relative_to = Path.relative_to

        def fake_relative_to(self, *args, **kwargs):
            if self == expected_resolved:
                raise ValueError("simulated escape")
            return real_relative_to(self, *args, **kwargs)

        monkeypatch.setattr(Path, "relative_to", fake_relative_to)
        result = obsidian_utils.mark_retro_classification_pending(SID, "/vault/retro.md")
        assert result.startswith("Failed:")
        assert "escapes" in result.lower()

    def test_mark_returns_failed_on_atomic_write_oserror(self, monkeypatch):
        """#330 review item 3: an OSError from the atomic os.replace() write
        is a genuinely reachable failure path and must return an explicit
        "Failed: ..." string."""

        def fake_replace(*args, **kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(obsidian_utils.os, "replace", fake_replace)
        result = obsidian_utils.mark_retro_classification_pending(SID, "/vault/retro.md")
        assert result.startswith("Failed:")
        assert "simulated replace failure" in result
        # No stray temp file left behind by the finally-block cleanup.
        gate_dir = self._gate_dir()
        leftovers = list(gate_dir.glob("*.tmp"))
        assert leftovers == [], f"Expected temp file cleanup, found: {leftovers}"

    def test_get_returns_dict_after_mark(self):
        obsidian_utils.mark_retro_classification_pending(SID, "/vault/retro.md")
        data = obsidian_utils.get_retro_classification_pending(SID)
        assert data is not None
        assert data["session_id"] == SID
        assert data["retro_path"] == "/vault/retro.md"

    def test_get_returns_none_when_absent(self):
        data = obsidian_utils.get_retro_classification_pending("no-such-session-xyz")
        assert data is None

    def test_get_returns_none_for_falsy_session_id(self):
        assert obsidian_utils.get_retro_classification_pending("") is None
        assert obsidian_utils.get_retro_classification_pending(None) is None  # type: ignore[arg-type]

    def test_clear_returns_true_when_present(self):
        obsidian_utils.mark_retro_classification_pending(SID, "/vault/retro.md")
        result = obsidian_utils.clear_retro_classification_pending(SID)
        assert result is True

    def test_clear_returns_false_when_absent(self):
        result = obsidian_utils.clear_retro_classification_pending("no-such-session-xyz")
        assert result is False

    def test_clear_then_get_returns_none(self):
        obsidian_utils.mark_retro_classification_pending(SID, "/vault/retro.md")
        obsidian_utils.clear_retro_classification_pending(SID)
        data = obsidian_utils.get_retro_classification_pending(SID)
        assert data is None

    def test_clear_idempotent(self):
        obsidian_utils.mark_retro_classification_pending(SID, "/vault/retro.md")
        obsidian_utils.clear_retro_classification_pending(SID)
        # Second clear should return False, not raise.
        result = obsidian_utils.clear_retro_classification_pending(SID)
        assert result is False

    def test_sentinel_permissions(self):
        path = obsidian_utils.mark_retro_classification_pending(SID, "/vault/retro.md")
        sentinel = Path(path)
        mode = sentinel.stat().st_mode & 0o777
        assert mode == 0o600, f"Expected 0o600 but got {oct(mode)}"

    def test_gate_dir_permissions(self):
        obsidian_utils.mark_retro_classification_pending(SID, "/vault/retro.md")
        gate_dir = self._gate_dir()
        mode = gate_dir.stat().st_mode & 0o777
        assert mode == 0o700, f"Expected 0o700 but got {oct(mode)}"

    def test_mark_sanitizes_session_id(self):
        """Special chars in session_id are replaced with underscores."""
        sid_with_specials = "session/id with spaces!@#"
        path = obsidian_utils.mark_retro_classification_pending(sid_with_specials, "/vault/r.md")
        assert path
        sentinel = Path(path)
        assert sentinel.exists()
        # Verify we can round-trip via get using the ORIGINAL session_id.
        data = obsidian_utils.get_retro_classification_pending(sid_with_specials)
        assert data is not None
        assert data["session_id"] == sid_with_specials

    def test_mark_reaps_stale_orphan(self):
        """mark_ removes orphaned sentinels older than RETRO_GATE_TTL_SECONDS."""
        orphan_sid = "orphan-stale-session-xyz"
        new_sid = "new-session-abc456"
        # Write the orphan sentinel manually into the gate dir.
        gate_dir = self._gate_dir()
        gate_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        orphan_file = gate_dir / f"{orphan_sid}.json"
        orphan_file.write_text(
            json.dumps({"session_id": orphan_sid, "retro_path": "/v/r.md",
                        "created_at": time.time() - (obsidian_utils.RETRO_GATE_TTL_SECONDS + 600)}),
            encoding="utf-8",
        )
        orphan_file.chmod(0o600)
        # Age it via mtime so the reaper's mtime check fires.
        old_ts = time.time() - (obsidian_utils.RETRO_GATE_TTL_SECONDS + 600)
        os.utime(orphan_file, (old_ts, old_ts))
        assert orphan_file.exists()

        # mark_ for a new sid triggers the opportunistic reap.
        new_path = obsidian_utils.mark_retro_classification_pending(new_sid, "/vault/new.md")

        assert new_path, "mark_ should return a non-empty path"
        assert not orphan_file.exists(), "Stale orphan sentinel should have been reaped"
        assert Path(new_path).exists(), "New sentinel must still exist after reap"

    def test_mark_keeps_fresh_orphan(self):
        """mark_ does NOT reap sentinels whose mtime is within RETRO_GATE_TTL_SECONDS."""
        orphan_sid = "orphan-fresh-session-xyz"
        new_sid = "new-session-def789"
        # Write the orphan with a fresh mtime (just now).
        gate_dir = self._gate_dir()
        gate_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        orphan_file = gate_dir / f"{orphan_sid}.json"
        orphan_file.write_text(
            json.dumps({"session_id": orphan_sid, "retro_path": "/v/r.md",
                        "created_at": time.time()}),
            encoding="utf-8",
        )
        orphan_file.chmod(0o600)
        # mtime is current — file is fresh, should NOT be reaped.
        assert orphan_file.exists()

        new_path = obsidian_utils.mark_retro_classification_pending(new_sid, "/vault/new.md")

        assert new_path, "mark_ should return a non-empty path"
        assert orphan_file.exists(), "Fresh orphan sentinel must NOT be reaped"
        assert Path(new_path).exists(), "New sentinel must exist"

    def test_reap_handles_missing_dir(self):
        """_reap_stale_retro_sentinels returns 0 and does not raise when the gate dir is absent."""
        # HOME is redirected by the autouse fixture; gate dir was never created.
        gate_dir = self._gate_dir()
        assert not gate_dir.exists(), "Pre-condition: gate dir should not exist"

        result = obsidian_utils._reap_stale_retro_sentinels()

        assert result == 0

    def test_reap_removes_multiple_stale_orphans(self):
        """All stale orphans are reaped (not just the first), and the count is returned."""
        gate_dir = self._gate_dir()
        gate_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        old_ts = time.time() - (obsidian_utils.RETRO_GATE_TTL_SECONDS + 600)
        orphans = []
        for i in range(3):
            f = gate_dir / f"stale-orphan-{i}.json"
            f.write_text(json.dumps({"session_id": f"s{i}", "retro_path": "/v/r.md",
                                     "created_at": old_ts}), encoding="utf-8")
            os.utime(f, (old_ts, old_ts))
            orphans.append(f)
        result = obsidian_utils._reap_stale_retro_sentinels()
        assert result == 3
        for f in orphans:
            assert not f.exists(), f"{f.name} should have been reaped"

    def test_reap_ignores_non_json_files(self):
        """Files not matching *.json are left untouched even when stale."""
        gate_dir = self._gate_dir()
        gate_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        old_ts = time.time() - (obsidian_utils.RETRO_GATE_TTL_SECONDS + 600)
        non_json = gate_dir / "leftover.tmp"
        non_json.write_text("junk", encoding="utf-8")
        os.utime(non_json, (old_ts, old_ts))
        result = obsidian_utils._reap_stale_retro_sentinels()
        assert result == 0
        assert non_json.exists(), ".tmp file must not be reaped"
