"""Keep Codex's repository policy hooks on the Claude-tested source files."""

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTRATIONS = (
    ROOT / ".codex/hooks.json",
    ROOT / ".claude/settings.json",
    ROOT / "hooks/hooks.json",
)


def test_codex_policy_hooks_use_one_source():
    assert not list((ROOT / ".codex/hooks").rglob("*.py"))
    manifest = json.loads((ROOT / ".codex/hooks.json").read_text())
    groups = manifest["hooks"]["PreToolUse"]
    assert len(groups) == 1
    assert groups[0]["matcher"] == "Bash"
    commands = [hook["command"] for hook in groups[0]["hooks"]]
    expected = {
        f'python3 "$(git rev-parse --show-toplevel)/.claude/hooks/{name}.py"'
        for name in (
            "prevent-direct-push",
            "require-preflight",
            "validate-branch-name",
            "update-changelog-before-pr",
            "enforce-pr-base-branch",
        )
    }
    assert set(commands) == expected
    assert len(commands) == len(expected)
    for name in (
        "prevent-direct-push",
        "require-preflight",
        "validate-branch-name",
        "update-changelog-before-pr",
        "enforce-pr-base-branch",
    ):
        assert (ROOT / ".claude/hooks" / f"{name}.py").is_file()
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
    for command in commands:
        result = subprocess.run(command, shell=True, cwd=ROOT, input=payload,
                                text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, (command, result.stderr)
        assert not result.stdout, command


def test_hook_registrations_have_no_machine_paths():
    for path in REGISTRATIONS:
        content = path.read_text()
        assert "/Users/" not in content, path
        assert "/home/" not in content, path
