"""Keep Codex's repository policy hooks on the Claude-tested source files."""

import json
import shlex
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
    claude = json.loads((ROOT / ".claude/settings.json").read_text())
    codex = json.loads((ROOT / ".codex/hooks.json").read_text())
    claude_groups = claude["hooks"]["PreToolUse"]
    codex_groups = codex["hooks"]["PreToolUse"]
    assert all(group["matcher"] == "Bash" for group in claude_groups)
    assert len(codex_groups) == 1
    assert codex_groups[0]["matcher"] == "Bash"
    source_commands = [hook["command"] for group in claude_groups
                       for hook in group["hooks"]]
    source_paths = []
    for command in source_commands:
        executable, path = shlex.split(command)
        assert executable == "python3"
        assert path.startswith(".claude/hooks/") and path.endswith(".py")
        assert (ROOT / path).is_file()
        source_paths.append(path)
    assert len(source_paths) == len(set(source_paths))
    commands = [hook["command"] for hook in codex_groups[0]["hooks"]]
    expected = {_codex_command(path) for path in source_paths}
    assert len(commands) == len(expected)
    assert set(commands) == expected
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
    for command in commands:
        result = subprocess.run(command, shell=True, cwd=ROOT, input=payload,
                                text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, (command, result.stderr)
        assert not result.stdout, command


def _codex_command(path):
    return ("if root=$(git rev-parse --show-toplevel 2>/dev/null); "
            f'then python3 "$root/{path}"; '
            "else printf 'Codex policy hook skipped: no Git worktree\\n' >&2; fi")


def test_codex_policy_hook_denies_protected_push():
    manifest = json.loads((ROOT / ".codex/hooks.json").read_text())
    commands = [hook["command"] for group in manifest["hooks"]["PreToolUse"]
                for hook in group["hooks"]]
    push = next(command for command in commands if "prevent-direct-push.py" in command)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {
        "command": "git push origin main"}})
    result = subprocess.run(push, shell=True, cwd=ROOT, input=payload,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"


def test_codex_policy_hook_allows_missing_worktree(tmp_path):
    manifest = json.loads((ROOT / ".codex/hooks.json").read_text())
    commands = [hook["command"] for group in manifest["hooks"]["PreToolUse"]
                for hook in group["hooks"]]
    payload = json.dumps({"tool_name": "Bash", "tool_input": {
        "command": "git status"}})
    for command in commands:
        result = subprocess.run(command, shell=True, cwd=tmp_path, input=payload,
                                text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, (command, result.stderr)
        assert result.stdout == ""
        assert "no Git worktree" in result.stderr


def test_hook_registrations_have_no_machine_paths():
    for path in REGISTRATIONS:
        content = path.read_text()
        assert "/Users/" not in content, path
        assert "/home/" not in content, path
