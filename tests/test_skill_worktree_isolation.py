"""SKILL.md shell blocks avoid the forms a worktree-isolated session refuses (#521).

A worktree-isolated Claude Code session refuses a Bash command whose
command substitution or backticks contain the text ``git`` (even ``digit``,
or Python source inside ``$(python3 ...)``), and a brace group holding it
(probed on Claude Code 2.1.330, 2026-11-21). ``isolation_problems`` is a
conservative text check with known gaps (listed in ``skill_shell.py``); this
module runs it over every skill. Heredoc bodies with a quoted brace are also
refused; six check-items blocks still have one and are pinned below (#524).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from skill_shell import fenced_blocks, heredoc_brace_problems, isolation_problems

SKILLS = Path(__file__).resolve().parents[1] / "skills"
SKILL_FILES = sorted(SKILLS.glob("*/SKILL.md"))
REPO_ROOT_STEP = "### Repo root — run first"
_ROOT_COMMAND = "```bash\ngit rev-parse --show-toplevel\n```"
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
    "memory-consolidate": 6,
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

# `fenced_blocks` does not read a fence inside a blockquote (at any depth) or one
# whose info string starts with `{`. A shell block hidden that way is never checked.
_QUOTED_FENCE = re.compile(r"^\s*(?:>\s*)+(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)")
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
    for line in (
        "> ```bash", ">   ~~~", "```{.bash}", "~~~ {.sh}",
        ">> ```bash", "> > ~~~sh", ">>> ```", "> > ```bash:x", ">> ~~~zsh,x",
    ):
        assert hidden_fences(line) == [(1, line.strip())], line
    # A nested quoted example that is not shell stays allowed, closer included.
    assert hidden_fences("> > ```python\n> > x\n> > ```\n>> ```bash") == [(4, ">> ```bash")]
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


# A numbered or named step heading of any form the skills use: `### Step 1`,
# `## Step 1`, `### 1. Read`, `## 1 — Read`, `### Phase 1`, `## Phase 1`.
_STEP_HEADING = re.compile(r"^#{2,3}\s+(?:Step\b|Phase\b|\d)")


_ATX_HEADING = re.compile(r" {0,3}#{1,6}(?:\s|$)")


def _heading_lines(text: str) -> list[tuple[int, str]]:
    """(line number, heading) for each ATX heading outside a fence. CommonMark
    allows 0-3 leading spaces; they are removed from the returned line. Known
    limit: a Setext heading (text over `===` or `---`) is not read."""
    fenced = {
        line
        for fence in fenced_blocks(text, langs=None)
        for line in range(fence.line, fence.line + len(fence.body.splitlines()) + 2)
    }
    return [
        (number, line.lstrip(" ")) for number, line in enumerate(text.splitlines(), 1)
        if _ATX_HEADING.match(line) and number not in fenced
    ]


def root_step_problems(text: str, skill: str) -> list[str]:
    """Why the "Repo root - run first" step is missing, malformed or too late."""
    blocks = fenced_blocks(text)
    users = [b for b in blocks if "'<REPO_ROOT>'" in b.body]
    if not users:
        return []
    headings = _heading_lines(text)
    # /retro has its own wording of the same step (#518): it opens its Step 1.
    if skill == "retro":
        anchor = text.index("```bash\ngit rev-parse --show-toplevel\n```")
        first_line = text[:anchor].count("\n") + 1
        return [] if all(b.line > first_line for b in users) else ["a block runs before the step"]
    root_line = next((n for n, line in headings if REPO_ROOT_STEP in line), None)
    if root_line is None or text.count(REPO_ROOT_STEP) != 1:
        return ["the root step heading is not there exactly once"]

    lines = text.splitlines()
    end_line = next((n for n, _ in headings if n > root_line), len(lines) + 1)
    step = "\n".join(lines[root_line:end_line - 1])
    command = _ROOT_COMMAND.splitlines()
    command_line = next((
        b.line for b in blocks
        if root_line < b.line and b.line + len(command) <= end_line
        and lines[b.line - 1:b.line - 1 + len(command)] == command
    ), None)

    problems = []
    if command_line is None or "run `pwd` instead" not in step:
        problems.append("the root step lacks its command or its pwd fallback")
    if any(b.line <= (command_line or root_line) for b in users):
        problems.append("a block runs before the step")
    first_step = next((n for n, line in headings if _STEP_HEADING.match(line)), None)
    if first_step is not None and first_step < root_line:
        problems.append(f"the step heading at line {first_step} comes before the root step")
    return problems


@pytest.mark.parametrize("path", SKILL_FILES, ids=[p.parent.name for p in SKILL_FILES])
def test_the_repo_root_step_comes_before_any_block_that_needs_it(path):
    text = path.read_text(encoding="utf-8")
    assert root_step_problems(text, path.parent.name) == [], path.parent.name


_ROOT_STEP_BODY = f"{_ROOT_COMMAND}\nIf it fails, run `pwd` instead.\n\n"
_ROOT_STEP = f"{REPO_ROOT_STEP}\n\n{_ROOT_STEP_BODY}"
_ROOT_USER_BLOCK = "```bash\ncd '<REPO_ROOT>'\n```\n"


def test_a_root_command_after_its_first_use_is_flagged():
    text = (
        f"# Skill\n\n{REPO_ROOT_STEP}\n\n{_ROOT_USER_BLOCK}\n"
        f"## Step 1\n\n{_ROOT_STEP_BODY}"
    )
    problems = root_step_problems(text, "fixture")
    assert "the root step lacks its command or its pwd fallback" in problems


def test_a_root_command_before_its_first_use_passes():
    assert root_step_problems(_ROOT_STEP + _ROOT_USER_BLOCK, "fixture") == []


def test_a_root_use_between_the_heading_and_command_is_flagged():
    text = f"{REPO_ROOT_STEP}\n\n{_ROOT_USER_BLOCK}\n{_ROOT_STEP_BODY}"
    assert root_step_problems(text, "fixture") == ["a block runs before the step"]


def test_an_indented_heading_ends_the_root_step():
    """CommonMark allows 0-3 spaces before `#`, so `  ## Step 1` is a heading."""
    text = f"{REPO_ROOT_STEP}\n\n  ## Step 1\n\n{_ROOT_STEP_BODY}{_ROOT_USER_BLOCK}"
    problems = root_step_problems(text, "fixture")
    assert "the root step lacks its command or its pwd fallback" in problems
    # Four spaces is a code block, not a heading, so the step still holds.
    code = f"{REPO_ROOT_STEP}\n\n    ## Step 1\n\n{_ROOT_STEP_BODY}{_ROOT_USER_BLOCK}"
    assert root_step_problems(code, "fixture") == []


@pytest.mark.parametrize("heading", [
    "### Step 1 - Read config", "## Step 1 - Read config", "### 1. Read config",
    "## 1 - Read config", "### Phase 1 - Read config", "## Phase 1",
])
def test_a_root_step_below_any_numbered_heading_is_flagged(heading):
    late = f"{heading}\n\n" + _ROOT_STEP
    text = f"# Skill\n\n{late}```bash\ncd '<REPO_ROOT>'\n```\n"
    problems = root_step_problems(text, "fixture")
    assert any("comes before the root step" in problem for problem in problems), (heading, problems)
    early = f"# Skill\n\n{_ROOT_STEP}{heading}\n\n```bash\ncd '<REPO_ROOT>'\n```\n"
    assert root_step_problems(early, "fixture") == [], heading


def test_a_heading_inside_a_fence_is_not_a_step():
    text = (
        "# Skill\n\n```markdown\n### 1. Read config\n```\n\n" + _ROOT_STEP
        + "### Step 1\n\n```bash\ncd '<REPO_ROOT>'\n```\n"
    )
    assert root_step_problems(text, "fixture") == []


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
