#!/usr/bin/env python3
"""
PreToolUse Hook: Validate Git Flow Branch Naming

Enforces branch naming conventions: feature/*, release/v*, hotfix/*.
Validates semantic versioning for release branches.

Installed by /harden-repo into target repo's .claude/hooks/
"""
import json
import os
import sys
import re


def _targets_this_project(cmd: str) -> bool:
    """Check if the command targets a repo within this project.

    Hooks run in their own process (cwd = project dir), so git commands in
    the hook inspect the wrong repo when Claude does 'cd /other/repo && git checkout -b'.
    Parse the cd target from the command to determine the effective repo.
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        return True  # Can't determine scope, be safe

    project_dir = os.path.realpath(project_dir)

    cd_match = re.search(r'(?:^|[;&|]\s*)cd\s+("([^"]+)"|\'([^\']+)\'|(\S+))', cmd)
    if cd_match:
        target = cd_match.group(2) or cd_match.group(3) or cd_match.group(4)
        target = os.path.expanduser(target)
        target = os.path.expandvars(target)
        target = os.path.realpath(target)
        return target.startswith(project_dir)

    return True


_STDIN_CAP = 1_000_000


def _deny(reason):
    """Emit a PreToolUse deny decision and exit 0.

    Exit 0 with ``permissionDecision: "deny"`` is what BLOCKS. A non-zero exit
    is a non-blocking error in the PreToolUse contract — Claude Code surfaces
    it and lets the tool call proceed — so a security gate must never signal
    refusal that way.
    """
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def _read_hook_input(what):
    """Read the hook payload, capped, failing CLOSED on anything unusable.

    ``read(_STDIN_CAP + 1)`` makes an oversize payload an explicit condition
    rather than a silent truncation that only ever surfaces as a confusing
    parse error (CLAUDE.md's documented overflow-detection idiom). Both
    overflow and a parse failure deny: a gate that cannot read its input has
    not established that the command is safe.
    """
    raw = sys.stdin.read(_STDIN_CAP + 1)
    if len(raw) > _STDIN_CAP:
        _deny(f"{what} received a payload over {_STDIN_CAP} bytes. "
              "Blocking as a safety measure.")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _deny(f"{what} received invalid input. Blocking as a safety measure.")


input_data = _read_hook_input("Branch-name hook")

tool_name = input_data.get("tool_name", "")
tool_input = input_data.get("tool_input", {})
command = tool_input.get("command", "")

# Only validate git checkout -b commands
if tool_name != "Bash" or "git checkout -b" not in command:
    sys.exit(0)

# Skip if targeting a different repo
if not _targets_this_project(command):
    sys.exit(0)

# Extract branch name
match = re.search(r'git checkout -b\s+([^\s]+)', command)
if not match:
    sys.exit(0)

branch_name = match.group(1).strip("'\"")  # Strip shell quotes

# Allow main and develop branches
if branch_name in ["main", "develop"]:
    sys.exit(0)

# Validate Git Flow naming convention
if not re.match(r'^(feature|release|hotfix)/', branch_name):
    reason = f"""❌ Invalid Git Flow branch name: {branch_name}

Git Flow branches must follow these patterns:
  • feature/<descriptive-name>
  • release/v<MAJOR>.<MINOR>.<PATCH>
  • hotfix/<descriptive-name>

Examples:
  ✅ feature/user-authentication
  ✅ release/v1.2.0
  ✅ hotfix/critical-security-fix

Invalid:
  ❌ {branch_name} (missing Git Flow prefix)
  ❌ feat/something (use 'feature/' not 'feat/')
  ❌ fix/bug (use 'hotfix/' not 'fix/')

💡 Use Git Flow commands instead:
  /feature <name>  - Create feature branch
  /release <version> - Create release branch
  /hotfix <name>   - Create hotfix branch"""

    _deny(reason)

# Validate release version format
if branch_name.startswith("release/"):
    if not re.match(r'^release/v\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$', branch_name):
        reason = f"""❌ Invalid release version: {branch_name}

Release branches must follow semantic versioning:
  release/vMAJOR.MINOR.PATCH[-prerelease]

Valid examples:
  ✅ release/v1.0.0
  ✅ release/v2.1.3
  ✅ release/v1.0.0-beta.1

Invalid:
  ❌ release/1.0.0 (missing 'v' prefix)
  ❌ release/v1.0 (incomplete version)
  ❌ {branch_name}

💡 Use: /release v1.2.0"""

        _deny(reason)

# Allow the command
sys.exit(0)
