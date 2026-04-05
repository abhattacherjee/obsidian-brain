#!/usr/bin/env python3
"""
Pre-PR Hook: Remind to Update Changelog Before Creating PR

Installed by /harden-repo into target repo's .claude/hooks/

This hook triggers before `gh pr create` commands and adds context
reminding Claude to update the changelog before proceeding.

It checks if there are commits on the current branch that might not
be reflected in the changelog.
"""

import json
import os
import re
import subprocess
import sys


def get_branch_commits():
    """Get commits on current branch not in the merge base."""
    try:
        cwd = os.environ.get("CLAUDE_PROJECT_DIR", ".")
        # Detect base: prefer develop, fall back to main
        for base in ["origin/develop", "origin/main"]:
            check = subprocess.run(
                ["git", "rev-parse", "--verify", base],
                capture_output=True, text=True, cwd=cwd
            )
            if check.returncode == 0:
                result = subprocess.run(
                    ["git", "log", f"{base}..HEAD", "--oneline", "--no-merges"],
                    capture_output=True, text=True, cwd=cwd
                )
                if result.returncode == 0:
                    commits = result.stdout.strip().split('\n')
                    return [c for c in commits if c]
        return []
    except Exception:
        return []


def check_changelog_updated(commits):
    """Check if changelog mentions any of the recent commits."""
    try:
        changelog_path = os.path.join(
            os.environ.get("CLAUDE_PROJECT_DIR", "."),
            "CHANGELOG.md"
        )
        if not os.path.exists(changelog_path):
            return False, "CHANGELOG.md not found"

        with open(changelog_path, 'r') as f:
            changelog_content = f.read()

        # Check if any commit messages or PR numbers are in changelog
        # This is a simple heuristic - not perfect but helpful
        for commit in commits[:5]:  # Check first 5 commits
            # Extract commit hash and message
            parts = commit.split(' ', 1)
            if len(parts) < 2:
                continue
            commit_hash, message = parts

            # Check for PR number in commit message
            if '#' in message:
                pr_num = message.split('#')[-1].split(')')[0].split(' ')[0]
                if pr_num.isdigit() and f"#{pr_num}" in changelog_content:
                    return True, f"PR #{pr_num} found in changelog"

        return False, "Recent commits may not be in changelog"
    except Exception as e:
        return False, str(e)


def add_context(message):
    """Add advisory context without blocking."""
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": message
        }
    }
    print(json.dumps(output))
    sys.exit(0)


def main():
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    command = tool_input.get("command", "")

    # Only check for gh pr create commands
    if tool_name != "Bash":
        sys.exit(0)

    if "gh pr create" not in command:
        sys.exit(0)

    # Skip if targeting a different repo
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

    # Get commits on this branch
    commits = get_branch_commits()

    if not commits:
        # No commits to check
        sys.exit(0)

    # Check if changelog seems updated
    is_updated, reason = check_changelog_updated(commits)

    if is_updated:
        # Changelog appears updated
        add_context(f"✅ Changelog check: {reason}")
    else:
        # Changelog may need updating
        commit_count = len(commits)
        commit_summary = '\n'.join(f"  - {c}" for c in commits[:5])
        if commit_count > 5:
            commit_summary += f"\n  ... and {commit_count - 5} more"

        add_context(f"""⚠️ CHANGELOG REMINDER: {commit_count} commits on this branch may not be in CHANGELOG.md

Recent commits:
{commit_summary}

Update CHANGELOG.md under [Unreleased] section before creating the PR.

Reason: {reason}""")


if __name__ == "__main__":
    main()
