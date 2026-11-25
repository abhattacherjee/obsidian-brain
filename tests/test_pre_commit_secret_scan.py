"""scripts/pre-commit.sh scans every added line (#532).

It filtered the staged diff with `grep '^+' | grep -v '^+++'` to drop the
`+++ b/<file>` header. An added line whose own text starts with `++` shows
as `+++...` in the diff, so it was dropped too and never scanned. harden-repo
v1.5.0 fixed its template the same way this does: `--output-indicator-new='>'`
marks added lines with `>`, which no header line starts with.

Fixtures build secret-shaped strings at runtime, so this file holds none.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "ghp" + "_" + "A1b2C3d4" * 4 + "E5f6"  # ghp_ + 36 characters


@pytest.fixture
def repo(tmp_path):
    work = tmp_path / "repo"
    (work / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "pre-commit.sh", work / "scripts" / "pre-commit.sh")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)

    def git(*args):
        subprocess.run(["git", "-C", str(work), "-c", "user.email=t@example.invalid",
                        "-c", "user.name=t", *args],
                       env=env, check=True, capture_output=True, timeout=15)

    git("init", "-q", "-b", "feature/probe")
    git("add", "scripts")
    git("commit", "-q", "-m", "seed")
    return work, env, git


def _scan(work, env):
    return subprocess.run(["bash", "scripts/pre-commit.sh"], cwd=work, env=env,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("line", [
    pytest.param(f"token = '{TOKEN}'\n", id="plain-control"),
    pytest.param(f"++{TOKEN}\n", id="double-plus"),
    pytest.param(f"++ b/{TOKEN}\n", id="header-shaped"),
])
def test_an_added_secret_is_caught(repo, line):
    work, env, git = repo
    (work / "app.py").write_text(line, encoding="utf-8")
    git("add", "app.py")
    proc = _scan(work, env)
    assert proc.returncode == 1, proc.stdout
    assert "Potential secret detected" in proc.stdout


def test_a_clean_change_passes(repo):
    work, env, git = repo
    (work / "app.py").write_text("++counter\nprint('ok')\n", encoding="utf-8")
    git("add", "app.py")
    proc = _scan(work, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_scan_that_cannot_run_fails_closed(repo):
    """grep exit 2 (here a pattern that does not compile) stops the commit
    instead of reading as "no secret"."""
    work, env, git = repo
    script = work / "scripts" / "pre-commit.sh"
    text = script.read_text(encoding="utf-8")
    start = text.index('SECRET_PATTERN="')
    end = text.index("\n", start)
    script.write_text(text[:start] + 'SECRET_PATTERN="(unclosed"' + text[end:], encoding="utf-8")
    (work / "app.py").write_text("print('ok')\n", encoding="utf-8")
    git("add", "app.py")
    proc = _scan(work, env)
    assert proc.returncode == 1, proc.stdout
    assert "could not run" in proc.stdout


def test_a_removed_secret_does_not_block(repo):
    """Only added lines count, so removing a leaked token can be committed."""
    work, env, git = repo
    (work / "app.py").write_text(f"++{TOKEN}\n", encoding="utf-8")
    git("add", "app.py")
    git("commit", "-q", "-m", "leak", "--no-verify")
    (work / "app.py").write_text("print('ok')\n", encoding="utf-8")
    git("add", "app.py")
    proc = _scan(work, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
