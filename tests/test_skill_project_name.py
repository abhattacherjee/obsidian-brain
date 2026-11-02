"""Skills use the same canonical project name as session notes."""

import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parent.parent
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git required")

STEP_ONE_SKILLS = ["recall", "vault-ask", "vault-stats", "link"]

# Ways a skill can name the project after the cwd or the worktree folder.
_CWD_NAME_BANS = [
    r"basename\(os\.getcwd\(\)",
    r"basename\s+\"?\$\(pwd\)",
    r"basename\s+\"?\$PWD",
    r"Path\.cwd\(\)\.name",
    r"os\.getcwd\(\)\.split",
    r"os\.getcwd\(\)\.rsplit",
    r"PROJECT=\"?\$PWD",
]


def _skill_text(skill):
    return (REPO / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")


def _step_one_snippet(skill, text=None):
    text = _skill_text(skill) if text is None else text
    for match in re.finditer(r"python3 -c '(.*?)'", text, re.DOTALL):
        body = match.group(1)
        if "load_config" in body and "PROJECT=" in body:
            return body
    raise AssertionError(f"No Step 1 snippet found for {skill}")


def _calls_canonical(node):
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "canonical_project_name"
        for n in ast.walk(node)
    )


def _imports_canonical(tree):
    return any(
        isinstance(n, ast.ImportFrom)
        and n.module == "obsidian_utils"
        and any(a.name == "canonical_project_name" for a in n.names)
        for n in ast.walk(tree)
    )


def _step_one_problems(snippet):
    """Problems if the PROJECT printed by a Step 1 snippet is not canonical."""
    tree = ast.parse(snippet)
    problems = []
    if not _imports_canonical(tree):
        problems.append("canonical_project_name is not imported from obsidian_utils")
    printed = [
        n.args[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "print"
        and n.args
        and any(
            isinstance(c, ast.Constant) and c.value == "PROJECT="
            for c in ast.walk(n.args[0])
        )
    ]
    if len(printed) != 1:
        return problems + [f"expected one print of PROJECT=, found {len(printed)}"]
    expr = printed[0]
    if not _calls_canonical(expr):
        names = {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}
        assigned = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)
        ]
        if not any(_calls_canonical(a.value) for a in assigned):
            problems.append("PROJECT is not derived from a canonical_project_name() call")
    return problems


def _text_ban_hits(text):
    return [pat for pat in _CWD_NAME_BANS if re.search(pat, text)]


@pytest.mark.parametrize("skill", STEP_ONE_SKILLS)
def test_skill_uses_canonical_project_name(skill):
    text = _skill_text(skill)
    assert _step_one_problems(_step_one_snippet(skill, text)) == []
    assert _text_ban_hits(text) == []


# Step 1 of /recall before #89: names the project after the cwd.
_OLD_RECALL_SNIPPET = (
    "import os, sys\n"
    "from obsidian_utils import load_config, canonical_project_name\n"
    "# canonical_project_name()\n"
    "project = os.path.basename(os.getcwd()).lower().replace(' ', '-')\n"
    "print('PROJECT=' + project)\n"
)


def test_step_one_guard_rejects_cwd_name_with_comment():
    assert _step_one_problems(_OLD_RECALL_SNIPPET)
    assert _text_ban_hits(_OLD_RECALL_SNIPPET)


def test_step_one_guard_rejects_missing_import():
    snippet = _step_one_snippet("recall").replace(
        "from obsidian_utils import load_config, canonical_project_name",
        "from obsidian_utils import load_config",
    )
    assert _step_one_problems(snippet)


@pytest.mark.parametrize(
    "bad",
    [
        "Path.cwd().name",
        "os.getcwd().split('/')[-1]",
        'basename "$PWD"',
        'basename "$(pwd)"',
        "os.path.basename(os.getcwd())",
    ],
)
def test_text_bans_catch_each_cwd_spelling(bad):
    assert _text_ban_hits(f"x = {bad}")


def _develop_skill_text(skill):
    for ref in ("develop", "origin/develop"):
        try:
            out = subprocess.run(
                ["git", "show", f"{ref}:skills/{skill}/SKILL.md"],
                cwd=REPO, capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0:
            return out.stdout
    return None


@needs_git
def test_step_one_guard_rejects_develop_recall():
    """Positive control: recall on develop (before #89) must fail the check."""
    text = _develop_skill_text("recall")
    if text is None:
        pytest.skip("develop branch not available")
    if "basename(os.getcwd" not in text:
        pytest.skip("develop already has the canonical project name")
    assert _step_one_problems(_step_one_snippet("recall", text))
    assert _text_ban_hits(text)


# --- /check-items: three blocks derive the project ---

_DQ_BLOCK_RE = re.compile(r'python3 -c "\n(.*?)\n"[^\n]*\)', re.DOTALL)
_HEREDOC_BLOCK_RE = re.compile(r"python3\s+<<\s*'(\w+)'\n(.*?)\n\1\n", re.DOTALL)


def _check_items_blocks(text):
    """Python source of each `python3 -c "..."` and PYEOF block in the skill."""
    blocks = []
    for m in _DQ_BLOCK_RE.finditer(text):
        # Undo the shell's double-quote escapes, as bash does before python runs.
        blocks.append(re.sub(r'\\([\"\\$`])', r"\1", m.group(1)))
    blocks += [m.group(2) for m in _HEREDOC_BLOCK_RE.finditer(text)]
    return blocks


def _check_items_problems(text):
    problems = []
    found = 0
    for src in _check_items_blocks(text):
        tree = ast.parse(src)
        if not any(
            isinstance(n, ast.Constant) and n.value == "--show-toplevel"
            for n in ast.walk(tree)
        ):
            continue
        found += 1
        if not _imports_canonical(tree):
            problems.append(f"block {found}: canonical_project_name not imported")
        if not _calls_canonical(tree):
            problems.append(f"block {found}: no canonical_project_name() call")
        if "os.path.basename(res.stdout" in src:
            problems.append(f"block {found}: still uses os.path.basename(res.stdout")
    if found != 3:
        problems.append(f"expected 3 project-deriving blocks, found {found}")
    return problems


def test_check_items_uses_canonical_project_name():
    assert _check_items_problems(_skill_text("check-items")) == []


def test_check_items_guard_rejects_deleted_import():
    text = _skill_text("check-items")
    marker = "from obsidian_utils import canonical_project_name\n"
    # Delete the import from the Step 2b heredoc only; prose and the other
    # two import sites are untouched, so the old count check stayed green.
    head, _, tail = text.partition("closed_path=$(SCOPE_PATH")
    assert marker in tail
    mutated = head + "closed_path=$(SCOPE_PATH" + tail.replace(marker, "", 1)
    assert _check_items_problems(mutated)


def test_check_items_guard_rejects_basename_with_comment():
    text = _skill_text("check-items")
    old = "project = canonical_project_name() if res.returncode == 0 else None"
    assert text.count(old) == 1
    mutated = text.replace(
        old,
        "project = os.path.basename(res.stdout.strip()) if res.returncode == 0 else None"
        "  # canonical_project_name()",
    )
    assert _check_items_problems(mutated)


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


@needs_git
@pytest.mark.parametrize("skill", ["recall", "vault-ask", "vault-stats", "link"])
def test_worktree_project_name(skill, worktree_dir, snippet_env):
    assert "PROJECT=main-repo" in _run_snippet(skill, worktree_dir, snippet_env)


@needs_git
def test_outside_git_project_name(tmp_path, snippet_env):
    cwd = tmp_path / "My_Dir"
    cwd.mkdir()
    snippet_env["GIT_CEILING_DIRECTORIES"] = str(tmp_path)
    assert "PROJECT=my-dir" in _run_snippet("recall", cwd, snippet_env)


@needs_git
def test_worktree_fixture_discriminates(worktree_dir):
    assert os.path.basename(worktree_dir) != "main-repo"
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=worktree_dir,
        check=True, capture_output=True, text=True, timeout=60,
    )
    assert result.stdout.strip().endswith("main-repo--issue-9")


@needs_git
def test_check_items_step2_current_project_is_canonical(worktree_dir, snippet_env, tmp_path):
    """Run the real Step 2 block from a worktree: it must read main-repo's notes."""
    text = _skill_text("check-items")
    start = text.index('raw_path=$(python3 -c "')
    end = text.index('" "$scope_path")', start) + len('" "$scope_path")')
    script = text[start:end] + '\nprintf "%s" "$raw_path"\n'

    config = json.loads(
        (Path(snippet_env["HOME"]) / ".claude" / "obsidian-brain-config.json").read_text()
    )
    sessions = Path(config["vault_path"]) / "claude-sessions"
    sessions.mkdir()
    for name, project in (("2026-01-02-a.md", "main-repo"), ("2026-01-01-b.md", "main-repo--issue-9")):
        (sessions / name).write_text(
            f"---\ntype: claude-session\nproject: {project}\n---\n\n"
            f"## Open Questions / Next Steps\n- [ ] item for {project}\n",
            encoding="utf-8",
        )
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({"mode": "current", "project": None}), encoding="utf-8")

    env = dict(snippet_env, scope_path=str(scope))
    result = subprocess.run(
        ["bash", "-c", script], cwd=worktree_dir, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    items = json.loads(Path(result.stdout.strip()).read_text(encoding="utf-8"))
    assert [i["project"] for i in items] == ["main-repo"]
    assert [i["text"] for i in items] == ["item for main-repo"]
