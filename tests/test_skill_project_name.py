"""Skills use the same canonical project name as session notes."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")


def _step_one_snippet(skill):
    text = (REPO / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    for match in re.finditer(r"python3 -c '(.*?)'", text, re.DOTALL):
        body = match.group(1)
        if "load_config" in body and "PROJECT=" in body:
            return body
    raise AssertionError(f"No Step 1 snippet found for {skill}")


@pytest.mark.parametrize("skill", ["recall", "vault-ask", "vault-stats", "link"])
def test_skill_uses_canonical_project_name(skill):
    text = (REPO / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    assert "canonical_project_name()" in text
    assert "basename(os.getcwd())" not in text
    assert 'basename "$(pwd)"' not in text


@pytest.fixture
def worktree_dir(tmp_path):
    main_repo = tmp_path / "main-repo"
    main_repo.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test User"],
        ["commit", "-q", "--allow-empty", "-m", "init"],
        ["worktree", "add", "-q", "../main-repo--issue-9", "-b", "feature/x"],
    ):
        subprocess.run(
            ["git", *args], cwd=main_repo, check=True,
            capture_output=True, text=True, timeout=60,
        )
    return tmp_path / "main-repo--issue-9"


@pytest.fixture
def snippet_env(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".claude"
    config_dir.mkdir(parents=True)
    vault = tmp_path / "vault"
    vault.mkdir()
    (config_dir / "obsidian-brain-config.json").write_text(
        json.dumps({"vault_path": str(vault)}), encoding="utf-8"
    )
    env = os.environ.copy()
    env.update(
        HOME=str(home),
        PYTHONPATH=str(REPO / "hooks"),
        OBSIDIAN_BRAIN_DB=str(tmp_path / "i.db"),
    )
    return env


def _run_snippet(skill, cwd, env):
    result = subprocess.run(
        [sys.executable, "-c", _step_one_snippet(skill)],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


@pytest.mark.parametrize("skill", ["recall", "vault-ask", "link"])
def test_worktree_project_name(skill, worktree_dir, snippet_env):
    assert "PROJECT=main-repo" in _run_snippet(skill, worktree_dir, snippet_env)


def test_outside_git_project_name(tmp_path, snippet_env):
    cwd = tmp_path / "My_Dir"
    cwd.mkdir()
    snippet_env["GIT_CEILING_DIRECTORIES"] = str(tmp_path)
    assert "PROJECT=my-dir" in _run_snippet("recall", cwd, snippet_env)


def test_worktree_fixture_discriminates(worktree_dir):
    assert os.path.basename(worktree_dir) != "main-repo"
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=worktree_dir,
        check=True, capture_output=True, text=True, timeout=60,
    )
    assert result.stdout.strip().endswith("main-repo--issue-9")
