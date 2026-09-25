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
@pytest.mark.parametrize(
    ("case", "codex_marker", "codex_path", "should_skip"),
    [
        ("codex-marker", True, True, True),
        ("codex-payload-only", False, True, True),
        ("claude", False, False, False),
        ("claude-inside-codex", True, False, False),
    ],
)
def test_lifecycle_host_guard(tmp_path, script, event, case, codex_marker, codex_path, should_skip):
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("GIT_", "CODEX_")) and key != "CLAUDE_CODE_SESSION_ID"
    }
    env.update(
        HOME=str(tmp_path),
        CODEX_HOME=str(tmp_path / ".codex"),
        CLAUDE_CODE_SESSION_ID="inherited-claude-id",
    )
    if codex_marker:
        env["CODEX_THREAD_ID"] = "codex-native-id"
    if codex_path:
        transcript_path = tmp_path / ".codex" / "sessions" / "2026" / "09" / "25" / "rollout-codex-native-id.jsonl"
        session_id = "codex-native-id"
    else:
        transcript_path = tmp_path / ".claude" / "projects" / "example" / "claude-native-id.jsonl"
        session_id = "claude-native-id"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text("")
    payload = {
        "session_id": session_id,
        "cwd": str(tmp_path),
        "transcript_path": str(transcript_path),
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
    assert (f"{event} outcome=SKIPPED_CODEX_HOST" in result.stderr) is should_skip, case
    assert "SKIPPED_TRANSCRIPT_OUTSIDE_PROJECTS" not in result.stderr
    assert not list(tmp_path.rglob("*.md"))

    if event == "SessionEnd":
        log = tmp_path / ".claude" / "obsidian-brain-hook.log"
        log_text = log.read_text()
        assert ("SKIPPED_CODEX_HOST" in log_text) is should_skip
        assert f"sid={session_id[:8]}" in log_text
        assert "sid=inherite" not in log_text
