#!/usr/bin/env python3
"""
PreToolUse Hook: Enforce correct PR base branch for Git Flow

- gh pr create: feature/* branches must target develop (--base develop)
- gh pr merge: verifies the PR's base branch matches Git Flow expectations
  before allowing the merge

Prevents the mistake of merging feature work directly to main.
"""
import json
import os
import re
import sys
import subprocess

try:
    input_data = json.load(sys.stdin)
except json.JSONDecodeError as e:
    print(f"Error: Invalid JSON input: {e}", file=sys.stderr)
    sys.exit(1)

tool_name = input_data.get("tool_name", "")
tool_input = input_data.get("tool_input", {})
command = tool_input.get("command", "")

if tool_name != "Bash":
    sys.exit(0)

# --- Project-scope guard ---
def _targets_this_project(cmd: str) -> bool:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        return True
    project_dir = os.path.realpath(project_dir)
    cd_match = re.search(r'(?:^|[;&|]\s*)cd\s+("([^"]+)"|\'([^\']+)\'|(\S+))', cmd)
    if cd_match:
        target = cd_match.group(2) or cd_match.group(3) or cd_match.group(4)
        target = os.path.expanduser(target)
        target = os.path.expandvars(target)
        target = os.path.realpath(target)
        return target.startswith(project_dir)
    return True

if not _targets_this_project(command):
    sys.exit(0)


def deny(reason: str):
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason
        }
    }
    print(json.dumps(output))
    sys.exit(0)


def get_current_branch() -> str:
    try:
        return subprocess.check_output(
            ["git", "branch", "--show-current"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


# ── gh pr create: enforce --base develop for feature branches ──
if "gh pr create" in command:
    branch = get_current_branch()
    if branch.startswith("feature/"):
        # Check if --base is specified
        base_match = re.search(r'--base\s+(\S+)', command)
        if not base_match:
            deny(
                f"❌ PR base branch not specified!\n\n"
                f"Feature branch '{branch}' must target develop.\n"
                f"Add --base develop to your gh pr create command:\n\n"
                f"  gh pr create --base develop ...\n\n"
                f"Without --base, GitHub defaults to main, which bypasses Git Flow."
            )
        elif base_match.group(1) != "develop":
            specified_base = base_match.group(1)
            deny(
                f"❌ Wrong PR base branch!\n\n"
                f"Feature branch '{branch}' targets '{specified_base}' but must target 'develop'.\n"
                f"Change --base to develop:\n\n"
                f"  gh pr create --base develop ..."
            )
    sys.exit(0)

# ── gh pr merge: verify base branch before merging ──
pr_merge_match = re.search(r'gh pr merge\s+(\d+)', command)
if pr_merge_match:
    pr_number = pr_merge_match.group(1)
    try:
        base_ref = subprocess.check_output(
            ["gh", "pr", "view", pr_number, "--json", "baseRefName", "--jq", ".baseRefName"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Can't determine base — allow but warn
        sys.exit(0)

    branch = get_current_branch()

    # Feature branches should only merge to develop
    if branch.startswith("feature/") and base_ref != "develop":
        deny(
            f"❌ PR #{pr_number} targets '{base_ref}' but feature branches must merge to 'develop'!\n\n"
            f"Current branch: {branch}\n"
            f"PR base: {base_ref}\n\n"
            f"Fix: close this PR and recreate with --base develop:\n"
            f"  gh pr close {pr_number}\n"
            f"  gh pr create --base develop"
        )

    # Release/hotfix branches merge to main
    if (branch.startswith("release/") or branch.startswith("hotfix/")) and base_ref != "main":
        deny(
            f"❌ PR #{pr_number} targets '{base_ref}' but {branch.split('/')[0]} branches must merge to 'main'!\n\n"
            f"Current branch: {branch}\n"
            f"PR base: {base_ref}\n\n"
            f"Fix: close this PR and recreate with --base main:\n"
            f"  gh pr close {pr_number}\n"
            f"  gh pr create --base main"
        )

    sys.exit(0)

# Not a PR command — allow
sys.exit(0)
