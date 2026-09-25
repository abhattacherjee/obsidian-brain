"""Keep Codex's repository policy hooks on the Claude-tested source files."""

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTRATIONS = (
    ROOT / ".codex/hooks.json",
    ROOT / ".claude/settings.json",
    ROOT / "hooks/hooks.json",
)


def _hook_env():
    return {key: value for key, value in os.environ.items()
            if not key.startswith("GIT_")}


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
                                text=True, capture_output=True, timeout=10,
                                env=_hook_env())
        assert result.returncode == 0, (command, result.stderr)
        assert not result.stdout, command


def _codex_command(path):
    return ("if root=$(git rev-parse --show-toplevel 2>/dev/null); "
            f'then python3 "$root/{path}"; '
            "else printf 'Codex policy hook skipped: no Git worktree\\n' >&2; fi")


def _feature_branch_repo(tmp_path):
    """A throwaway repo on a feature branch with this checkout's hooks.

    The push gate stands down on `release/*` and `hotfix/*` checkouts, because
    the release flow pushes to `main` from there. Probing in this checkout made
    the verdict depend on whichever branch it happens to be on, so this test
    failed the preflight of every release branch.
    """
    work = tmp_path / "probe"
    shutil.copytree(ROOT / ".claude/hooks", work / ".claude/hooks")
    env = dict(_hook_env(), GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, CLAUDE_PROJECT_DIR=str(work))
    for args in (["init", "-q", "-b", "feature/probe"],
                 ["-c", "user.email=t@example.invalid", "-c", "user.name=t",
                  "commit", "-q", "--allow-empty", "-m", "seed"]):
        subprocess.run(["git", "-C", str(work), *args], env=env,
                       check=True, capture_output=True)
    return work, env


def test_codex_policy_hook_denies_protected_push(tmp_path):
    manifest = json.loads((ROOT / ".codex/hooks.json").read_text())
    commands = [hook["command"] for group in manifest["hooks"]["PreToolUse"]
                for hook in group["hooks"]]
    push = next(command for command in commands if "prevent-direct-push.py" in command)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {
        "command": "git " + "push origin ma" + "in"}})
    work, env = _feature_branch_repo(tmp_path)
    result = subprocess.run(push, shell=True, cwd=work, input=payload,
                            text=True, capture_output=True, timeout=10,
                            env=env)
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
                                text=True, capture_output=True, timeout=10,
                                env=_hook_env())
        assert result.returncode == 0, (command, result.stderr)
        assert result.stdout == ""
        assert "no Git worktree" in result.stderr


def test_hook_registrations_have_no_machine_paths():
    for path in REGISTRATIONS:
        content = path.read_text()
        assert "/Users/" not in content, path
        assert "/home/" not in content, path
