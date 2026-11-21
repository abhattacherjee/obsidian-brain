"""Every SKILL.md shell block runs in a worktree-isolated session (#521).

A worktree-isolated Claude Code session refuses a Bash command whose
command substitution or backticks contain the text ``git`` (even ``digit``,
or Python source inside ``$(python3 ...)``), and a brace group holding it
(probed on Claude Code 2.1.330, 2026-11-21). ``isolation_problems`` is the
fail-closed check; this module runs it over every skill.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skill_shell import fenced_blocks, heredoc_brace_problems, isolation_problems

SKILLS = Path(__file__).resolve().parents[1] / "skills"
SKILL_FILES = sorted(SKILLS.glob("*/SKILL.md"))
REPO_ROOT_STEP = "### Repo root — run first"
OLD_CD = 'cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"'

# Refused in a worktree-isolated session; tracked in #524. Remove an id when its block is fixed.
HEREDOC_BRACE_KNOWN = frozenset({
    "check-items-247",
    "check-items-307",
    "check-items-423",
    "check-items-592",
    "check-items-681",
    "check-items-811",
})


def heredoc_brace_ids(skill: str, text: str) -> set[str]:
    return {
        f"{skill}-{block.line}"
        for block in fenced_blocks(text)
        if heredoc_brace_problems(block.body)
    }


def test_the_glob_finds_every_skill():
    assert len(SKILL_FILES) >= 20, [p.parent.name for p in SKILL_FILES]


@pytest.mark.parametrize("path", SKILL_FILES, ids=[p.parent.name for p in SKILL_FILES])
def test_no_block_has_a_form_an_isolated_session_refuses(path):
    blocks = fenced_blocks(path.read_text(encoding="utf-8"))
    for block in blocks:
        assert isolation_problems(block.body) == [], f"{path.parent.name}:{block.line}"


def test_no_new_heredoc_with_a_quoted_brace():
    offenders = set()
    for path in SKILL_FILES:
        offenders.update(heredoc_brace_ids(path.parent.name, path.read_text(encoding="utf-8")))
    assert offenders == HEREDOC_BRACE_KNOWN


def test_checker_catches_a_heredoc_with_a_quoted_brace_added_to_a_real_skill():
    text = (SKILLS / "decide" / "SKILL.md").read_text(encoding="utf-8") + "\n\n"
    appended = text + """```bash
python3 << 'PYEOF'
x = {"a": 1}
PYEOF
```
"""
    new_id = f"decide-{len(text.splitlines()) + 1}"
    assert heredoc_brace_ids("decide", appended) - HEREDOC_BRACE_KNOWN == {new_id}


@pytest.mark.parametrize("path", SKILL_FILES, ids=[p.parent.name for p in SKILL_FILES])
def test_the_repo_root_step_comes_before_any_block_that_needs_it(path):
    text = path.read_text(encoding="utf-8")
    users = [b for b in fenced_blocks(text) if "'<REPO_ROOT>'" in b.body]
    if not users:
        return
    # /retro has its own wording of the same step (#518).
    if path.parent.name == "retro":
        anchor = text.index("```bash\ngit rev-parse --show-toplevel\n```")
    else:
        assert text.count(REPO_ROOT_STEP) == 1, path.parent.name
        anchor = text.index(REPO_ROOT_STEP)
        step = text[anchor:text.index("\n### ", anchor + 1)]
        assert "```bash\ngit rev-parse --show-toplevel\n```" in step
        assert "run `pwd` instead" in step
    first_line = text[:anchor].count("\n") + 1
    assert all(b.line > first_line for b in users), path.parent.name
    # The step must come before every `### Step` heading, so no entry path skips
    # it. /retro keeps its #518 layout: the step opens its Step 1.
    first_step = text.find("\n### Step ")
    if path.parent.name != "retro":
        assert first_step == -1 or anchor < first_step + 1, path.parent.name


def test_checker_catches_the_old_cd_line_put_back_into_a_real_skill():
    """Positive control on real text: undo the #521 fix in a scratch copy."""
    text = (SKILLS / "decide" / "SKILL.md").read_text(encoding="utf-8")
    assert "cd '<REPO_ROOT>'" in text
    reverted = text.replace("cd '<REPO_ROOT>'", OLD_CD, 1)
    found = [isolation_problems(b.body) for b in fenced_blocks(reverted)]
    assert ["git inside a command substitution"] in found


def test_checker_catches_the_old_guard_put_back_into_a_real_skill():
    text = (SKILLS / "decide" / "SKILL.md").read_text(encoding="utf-8")
    old = 'if ! test -f "$HOOKS/note_writer.py"; then '
    assert old in text
    start = text.index(old)
    end = text.index("; fi\n", start)
    guard = text[start:end + len("; fi")]
    body = guard[len(old):-len("; fi")]
    reverted = text.replace(guard, 'test -f "$HOOKS/note_writer.py" || { ' + body + "; }", 1)
    found = [p for b in fenced_blocks(reverted) for p in isolation_problems(b.body)]
    assert "brace group after || or &&" in found


def test_checker_catches_git_text_put_back_into_a_check_items_substitution():
    text = (SKILLS / "check-items" / "SKILL.md").read_text(encoding="utf-8")
    old = "SCOPE_PATH=\"$scope_path\" python3 << 'PYEOF'\nimport sys, os, glob, json, subprocess\n"
    assert old in text
    start = text.index(old)
    end = text.index("\nPYEOF\n", start) + len("\nPYEOF\n")
    reverted = text[:start] + "closed_path=$(" + text[start:end] + ")\n" + text[end:]
    found = [p for b in fenced_blocks(reverted) for p in isolation_problems(b.body)]
    assert "git inside a command substitution" in found
