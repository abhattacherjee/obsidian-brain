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

    def test_mark_returns_empty_for_falsy_session_id(self):
        assert obsidian_utils.mark_retro_classification_pending("", "/vault/retro.md") == ""
        assert obsidian_utils.mark_retro_classification_pending(None, "/vault/retro.md") == ""  # type: ignore[arg-type]

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
