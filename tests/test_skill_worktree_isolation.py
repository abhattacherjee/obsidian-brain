"""Every SKILL.md shell block runs in a worktree-isolated session (#521).

A worktree-isolated Claude Code session refuses a Bash command whose
command substitution or backticks contain the text ``git`` (even ``digit``,
or Python source inside ``$(python3 ...)``), and a brace group holding it
(probed on Claude Code 2.1.330, 2026-11-21). ``isolation_problems`` is the
fail-closed check; this module runs it over every skill.
"""
from __future__ import annotations

import re
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


# The shell blocks `fenced_blocks` reads in each skill. Update when a skill adds
# or removes a shell block. A block that drops out (a fence that stops parsing)
# would otherwise go unchecked by every test in this module.
SHELL_BLOCK_COUNTS = {
    "check-items": 12,
    "compress": 9,
    "consolidate": 5,
    "decide": 6,
    "dev-test": 4,
    "emerge": 4,
    "error-log": 6,
    "lessons": 15,
    "link": 6,
    "obsidian-setup": 15,
    "recall": 8,
    "retro": 12,
    "standup": 20,
    "vault-ask": 9,
    "vault-config": 3,
    "vault-doctor": 3,
    "vault-import": 7,
    "vault-reindex": 2,
    "vault-search": 7,
    "vault-stats": 3,
}

# Skills with at least one `cd '<REPO_ROOT>'` block: the 16 that carry the
# "Repo root — run first" step, plus /retro (its own #518 wording).
REPO_ROOT_SKILLS = frozenset({
    "compress", "consolidate", "decide", "dev-test", "emerge", "error-log",
    "lessons", "link", "obsidian-setup", "recall", "retro", "standup",
    "vault-ask", "vault-config", "vault-import", "vault-search", "vault-stats",
})

# `fenced_blocks` does not read a fence inside a blockquote or one whose info
# string starts with `{`. A shell block hidden that way is never checked.
_QUOTED_FENCE = re.compile(r"^\s*>\s*(`{3,}|~{3,})\s*(\w*)")
_BRACE_INFO_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*\{")
# A quoted fence with one of these info strings is a shell example that nothing checks.
_QUOTED_SHELL_INFO = frozenset({"", "bash", "sh", "shell", "zsh"})


def heredoc_brace_ids(skill: str, text: str) -> set[str]:
    return {
        f"{skill}-{block.line}"
        for block in fenced_blocks(text)
        if heredoc_brace_problems(block.body)
    }


def test_the_glob_finds_every_skill():
    assert len(SKILL_FILES) >= 20, [p.parent.name for p in SKILL_FILES]


def test_every_skill_keeps_its_shell_blocks():
    counts = {
        path.parent.name: len(fenced_blocks(path.read_text(encoding="utf-8")))
        for path in SKILL_FILES
    }
    assert counts == SHELL_BLOCK_COUNTS


def hidden_fences(text: str) -> list[tuple[int, str]]:
    found = []
    in_quoted_example = False
    for number, line in enumerate(text.splitlines(), 1):
        quoted = _QUOTED_FENCE.match(line)
        if quoted and in_quoted_example:
            in_quoted_example = False  # The closer of an allowed example.
        elif _BRACE_INFO_FENCE.match(line) or (
            quoted and quoted.group(2).lower() in _QUOTED_SHELL_INFO
        ):
            found.append((number, line.strip()))
        elif quoted:
            in_quoted_example = True
    return found


def test_no_skill_hides_a_shell_fence_the_reader_skips():
    hidden = [
        f"{path.parent.name}/SKILL.md:{number}: {line}"
        for path in SKILL_FILES
        for number, line in hidden_fences(path.read_text(encoding="utf-8"))
    ]
    assert hidden == []


def test_the_hidden_fence_check_flags_each_hiding_form():
    for line in ("> ```bash", ">   ~~~", "```{.bash}", "~~~ {.sh}"):
        assert hidden_fences(line) == [(1, line.strip())], line
    # A quoted JSON example is not shell, so it is allowed, closer included.
    assert hidden_fences(">    ```json\n>    {}\n>    ```\n> ```bash") == [(4, "> ```bash")]


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


def test_the_skills_with_a_repo_root_block_are_the_expected_ones():
    with_blocks = set()
    for path in SKILL_FILES:
        text = path.read_text(encoding="utf-8")
        has_block = any("'<REPO_ROOT>'" in b.body for b in fenced_blocks(text))
        if has_block:
            with_blocks.add(path.parent.name)
        # A skill with the step but no block using it would never run the step.
        if REPO_ROOT_STEP in text:
            assert has_block, path.parent.name
    assert with_blocks == REPO_ROOT_SKILLS


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
