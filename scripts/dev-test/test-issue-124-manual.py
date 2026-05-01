#!/usr/bin/env python3
"""
Manual smoke runner for #124: replay each fixture through replay-sessionend.py
in both modes and print a colorized PASS/FAIL summary against expected outcomes.

Usage:
    python3 scripts/dev-test/test-issue-124-manual.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPLAY = _REPO_ROOT / "scripts" / "dev-test" / "replay-sessionend.py"
_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "dropped-sessions"

_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"


def _make_isolated_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="ob-124-"))
    (home / ".claude").mkdir()
    (home / "vault" / "sessions").mkdir(parents=True)
    config = {
        "vault_path": str(home / "vault"),
        "sessions_folder": "sessions",
        "min_messages": 3,
        "min_duration_minutes": 2,
        "auto_log_enabled": True,
    }
    (home / ".claude" / "obsidian-brain-config.json").write_text(json.dumps(config))
    return home


def _run(home: Path, fixture: str, mode: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_REPLAY),
         "--jsonl", str(_FIXTURES / fixture),
         "--cwd", "/Users/abhishek/dev/claude_workspace/obsidian-brain",
         "--mode", mode, "--json"],
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(home), "_REAL_VAULT_GUARD": "1"},
        cwd=str(_REPO_ROOT),
    )


CASES = [
    ("d63cc484-3min-14msg.jsonl", "sessionend", 0, "SKIPPED_BELOW_THRESHOLD"),
    ("6fa4f267-2min-5msg.jsonl", "sessionend", 0, "SKIPPED_BELOW_THRESHOLD"),
    ("d2cc7e46-long-617min.jsonl", "sessionend", 0, "SKIPPED_BELOW_THRESHOLD"),
    ("d2cc7e46-long-617min.jsonl", "reaper", 2, None),
    ("87b15f72-worktree-deleted.jsonl", "reaper", 2, None),
    ("7c71d4da-worktree-deleted.jsonl", "reaper", 2, None),
]


def main() -> int:
    home = _make_isolated_home()
    failures = 0
    for fixture, mode, expected_rc, expected_outcome in CASES:
        result = _run(home, fixture, mode)
        rc_ok = result.returncode == expected_rc
        outcome_ok = True
        if expected_outcome and rc_ok:
            try:
                outcome_ok = json.loads(result.stdout)["outcome"] == expected_outcome
            except Exception:
                outcome_ok = False
        passed = rc_ok and outcome_ok
        marker = f"{_GREEN}PASS{_RESET}" if passed else f"{_RED}FAIL{_RESET}"
        print(f"{marker} {mode:10s} {fixture}  rc={result.returncode}  expected_rc={expected_rc}  expected_outcome={expected_outcome}")
        if not passed:
            failures += 1
            print(f"      stdout: {result.stdout.strip()[:200]}")
            print(f"      stderr: {result.stderr.strip()[:200]}")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
