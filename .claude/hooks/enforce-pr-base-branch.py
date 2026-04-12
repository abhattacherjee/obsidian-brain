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
# Use command-boundary regex to avoid false positives on strings containing
# "gh pr create" (e.g. echo, grep, heredocs).
if re.search(r'(?:^|[;&|]\s*)gh\s+pr\s+create\b', command):
    branch = get_current_branch()
    if branch.startswith("feature/"):
        # Check if --base is specified (supports both --base X and --base=X)
        base_match = re.search(r'--base[=\s]+(\S+)', command)
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
# Handles "gh pr merge 30", "gh pr merge --squash 30", and "gh pr merge" (no number).
if re.search(r'(?:^|[;&|]\s*)gh\s+pr\s+merge\b', command):
    # Extract PR number from anywhere in the args (handles flags before number)
    pr_number_match = re.search(r'gh\s+pr\s+merge\b.*?(\d+)', command)
    pr_number = pr_number_match.group(1) if pr_number_match else None

    if not pr_number:
        # Resolve PR number from current branch
        try:
            pr_number = subprocess.check_output(
                ["gh", "pr", "view", "--json", "number", "--jq", ".number"],
                stderr=subprocess.DEVNULL, text=True
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            deny(
                "⚠️ Cannot determine PR number from current branch.\n\n"
                "Merge blocked because the base-branch safety check could not run.\n"
                "Specify the PR number explicitly: gh pr merge <number> --squash"
            )

    if not pr_number:
        deny(
            "⚠️ No open PR found for the current branch.\n\n"
            "Merge blocked because the base-branch safety check could not run."
        )

    # Get both base and head ref from the PR itself (not current branch)
    try:
        pr_info = subprocess.check_output(
            ["gh", "pr", "view", pr_number, "--json", "baseRefName,headRefName",
             "--jq", ".baseRefName + \" \" + .headRefName"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
        base_ref, head_ref = pr_info.split(" ", 1)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        deny(
            f"⚠️ Cannot verify PR #{pr_number} base branch (gh pr view failed).\n\n"
            "Merge blocked because the safety check could not run.\n"
            "Common causes: gh auth expired, network issue, invalid PR number."
        )

    # Use the PR's head ref (not current branch) for Git Flow classification
    if head_ref.startswith("feature/") and base_ref != "develop":
        deny(
            f"❌ PR #{pr_number} targets '{base_ref}' but feature branches must merge to 'develop'!\n\n"
            f"PR head: {head_ref}\n"
            f"PR base: {base_ref}\n\n"
            f"Fix: close this PR and recreate with --base develop:\n"
            f"  gh pr close {pr_number}\n"
            f"  gh pr create --base develop"
        )

    # Release/hotfix branches merge to main
    if (head_ref.startswith("release/") or head_ref.startswith("hotfix/")) and base_ref != "main":
        deny(
            f"❌ PR #{pr_number} targets '{base_ref}' but {head_ref.split('/')[0]} branches must merge to 'main'!\n\n"
            f"PR head: {head_ref}\n"
            f"PR base: {base_ref}\n\n"
            f"Fix: close this PR and recreate with --base main:\n"
            f"  gh pr close {pr_number}\n"
            f"  gh pr create --base main"
        )

    sys.exit(0)

# Not a PR command — allow
sys.exit(0)
