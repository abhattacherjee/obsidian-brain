#!/usr/bin/env python3
"""
PreToolUse Hook: Prevent Direct Push to Protected Branches

Blocks git push to main/develop. Allows Git Flow operations,
tag pushes, and feature branch pushes.

Installed by /harden-repo into target repo's .claude/hooks/
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

# Only validate git push commands
if tool_name != "Bash" or "git push" not in command:
    sys.exit(0)

# --- Project-scope guard ---
# Skip this hook if the command targets a repo outside this project.
# Hooks run in their own process (cwd = project dir), so git commands in
# the hook inspect the wrong repo when Claude does "cd /other/repo && git push".
def _targets_this_project(cmd: str) -> bool:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        return True  # Can't determine scope, be safe

    project_dir = os.path.realpath(project_dir)

    # Extract the first "cd /path" from the command (handles "cd X && git push")
    cd_match = re.search(r'(?:^|[;&|]\s*)cd\s+("([^"]+)"|\'([^\']+)\'|(\S+))', cmd)
    if cd_match:
        target = cd_match.group(2) or cd_match.group(3) or cd_match.group(4)
        target = os.path.expanduser(target)
        target = os.path.expandvars(target)
        target = os.path.realpath(target)
        return target.startswith(project_dir)

    # No cd in command — assume it targets the project repo
    return True

if not _targets_this_project(command):
    sys.exit(0)

# Allow tag-only pushes (refs/tags/*)
if "refs/tags/" in command:
    sys.exit(0)

# Allow branch deletion (--delete) for release/hotfix cleanup
if "--delete" in command and ("release/" in command or "hotfix/" in command):
    sys.exit(0)

# Get current branch
try:
    current_branch = subprocess.check_output(
        ["git", "branch", "--show-current"],
        stderr=subprocess.DEVNULL,
        text=True
    ).strip()
except (subprocess.CalledProcessError, FileNotFoundError):
    current_branch = ""

# Allow Git Flow finish operations
# Release/hotfix branches push to both main and develop
is_release_or_hotfix_finish = (
    current_branch.startswith("release/") or
    current_branch.startswith("hotfix/")
)

if is_release_or_hotfix_finish:
    sys.exit(0)

# Git Flow finish: on main or develop, HEAD is a merge from a Git Flow branch
if current_branch in ["main", "develop"]:
    try:
        # Check if HEAD is a merge commit (has 2+ parents)
        subprocess.check_output(
            ["git", "rev-parse", "HEAD^2"],
            stderr=subprocess.DEVNULL,
            text=True
        )
        # Check if the merge message references a Git Flow branch
        merge_msg = subprocess.check_output(
            ["git", "log", "-1", "--format=%s", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()
        # main: only release/hotfix merges (features never merge to main)
        # develop: feature/release/hotfix merges + main sync
        if current_branch == "main":
            allowed = ["release/", "hotfix/"]
        else:
            allowed = ["feature/", "release/", "hotfix/", "Merge main into develop"]
        if any(pattern in merge_msg for pattern in allowed):
            sys.exit(0)
    except subprocess.CalledProcessError:
        # HEAD is not a merge commit — check for version bump after Git Flow finish
        if current_branch == "develop":
            try:
                recent_msgs = subprocess.check_output(
                    ["git", "log", "-5", "--format=%s", "HEAD"],
                    stderr=subprocess.DEVNULL,
                    text=True
                ).strip()
                if any(p in recent_msgs for p in ["release/", "hotfix/"]):
                    sys.exit(0)
            except subprocess.CalledProcessError:
                pass

# Check if command or current branch targets protected branches
targets_protected = (
    "origin main" in command or
    "origin develop" in command or
    current_branch in ["main", "develop"]
)

# Block direct push to main/develop (including force pushes)
if targets_protected:
    if current_branch in ["main", "develop"] or "origin main" in command or "origin develop" in command:
        reason = f"""❌ Direct push to main/develop is not allowed!

Protected branches:
  - main (production)
  - develop (integration)

Git Flow workflow:
  1. Create a feature branch:
     git checkout -b feature/<name>

  2. Make your changes and commit

  3. Push feature branch:
     git push origin feature/<name>

  4. Create pull request:
     gh pr create

  5. After PR approval, merge via GitHub

For releases:
  git checkout -b release/v<version> develop
  (prepare release, then merge to main + tag + merge back to develop)

For hotfixes:
  git checkout -b hotfix/<name> main
  (fix + merge to main + tag + merge back to develop)

Current branch: {current_branch}

💡 If the superpowers plugin is installed, use /feature, /release, /hotfix, /finish for automated workflows."""

        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason
            }
        }
        print(json.dumps(output))
        sys.exit(0)

# Allow the command
sys.exit(0)
