"""Claude lifecycle handlers fail open when Codex auto-discovers them."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("script", "event"),
    [
        ("obsidian_session_log.py", "SessionEnd"),
        ("obsidian_session_hint.py", "SessionStart"),
        ("obsidian_context_snapshot.py", "PreCompact"),
        ("obsidian_retro_gate.py", "Stop"),
    ],
)
def test_claude_lifecycle_hook_skips_codex_host(tmp_path, script, event):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        HOME=str(tmp_path),
        CODEX_THREAD_ID="codex-native-id",
        CLAUDE_CODE_SESSION_ID="inherited-claude-id",
    )
    payload = {
        "session_id": "codex-native-id",
        "cwd": str(tmp_path),
        "transcript_path": str(tmp_path / "rollout.jsonl"),
    }
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert f"{event} outcome=SKIPPED_CODEX_HOST" in result.stderr
    assert "SKIPPED_TRANSCRIPT_OUTSIDE_PROJECTS" not in result.stderr
    assert not list(tmp_path.rglob("*.md"))

    if event == "SessionEnd":
        log = tmp_path / ".claude" / "obsidian-brain-hook.log"
        assert "SKIPPED_CODEX_HOST" in log.read_text()
