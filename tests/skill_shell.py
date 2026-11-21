"""Helpers for testing shell commands in SKILL.md files."""

from __future__ import annotations

import re
from dataclasses import dataclass

SHELL_LANGS = frozenset({"bash", "sh", "shell", "zsh"})
_OPENER = re.compile(r"([ \t]*)(`{3,}|~{3,})(.*)")


@dataclass(frozen=True)
class Block:
    line: int
    info: str
    body: str


def fenced_blocks(text: str, langs=SHELL_LANGS) -> list[Block]:
    """Read whole fences before filtering by language."""
    blocks = []
    lines = enumerate(text.splitlines(), start=1)
    for number, line in lines:
        match = _OPENER.fullmatch(line)
        if not match:
            continue
        indent, fence, raw_info = match.groups()
        if fence.startswith("`") and "`" in raw_info:
            continue
        words = raw_info.split()
        info = words[0].lower() if words else ""
        width = len(indent.expandtabs(4))
        body = []
        for _, line in lines:
            marker = line.strip(" \t")
            if marker.startswith(fence) and not marker.strip(fence[0]):
                break
            spaces = len(line) - len(line.lstrip(" "))
            body.append(line[min(width, spaces):])
        if langs is None or info in langs:
            blocks.append(Block(number, info, "\n".join(body)))
    return blocks


def substitution_bodies(block: str) -> list[str]:
    """The text inside every `$( ... )` (parens balanced; unclosed runs to the end)
    and every backtick pair."""
    bodies = []
    for m in re.finditer(r"\$\(", block):
        depth, k = 1, m.end()
        while k < len(block) and depth:
            depth += {"(": 1, ")": -1}.get(block[k], 0)
            k += 1
        bodies.append(block[m.end():k])
    bodies += re.findall(r"`([^`]*)`", block)
    return bodies


# The 2026-11-21 worktree-isolation probe refused both digit and legit in
# substitutions, so match git as a substring.
GIT_TEXT = re.compile(r"git", re.IGNORECASE)


def isolation_problems(block: str, *, heredoc: bool = False) -> list[str]:
    """Report refused shell forms and optionally enforce a skill-specific heredoc ban."""
    problems = []
    if any(GIT_TEXT.search(body) for body in substitution_bodies(block)):
        problems.append("git inside a command substitution")
    if re.search(r"(\|\||&&)\s*\{", block):
        problems.append("brace group after || or &&")
    # A bare group containing git was refused. Flag every bare group to fail closed.
    if re.search(r"^[ \t]*\{(\s|$)", block, re.M):
        problems.append("brace group")
    if heredoc and "<<" in block:
        problems.append("heredoc")
    return problems


REFUSED_FORMS: dict[str, str] = {
    "plain": 'cd "$(git rev-parse --show-toplevel)"',
    "command": 'x=$(command git rev-parse HEAD)',
    "cd-and": 'cd "$(cd . && git rev-parse --show-toplevel)"',
    "abs-path": 'x=$(/usr/bin/git rev-parse HEAD)',
    "space": 'x=$( git rev-parse HEAD)',
    "backticks": 'x=`git rev-parse HEAD`',
    "or-group": 'test -f f || { echo missing; exit 1; }',
    "and-group-tight": 'test -f f &&{ echo present; }',
    "or-group-next-line": 'test -f f ||\n{\n  echo missing\n}',
    "python-word": 'x=$(python3 -c "\nimport subprocess\nsubprocess.run([\'git\', \'--version\'])\n")',
    "heredoc-in-sub": "out=$(FOO=1 python3 << 'PYEOF'\nprint(\"git\")\nPYEOF\n)",
    "dquoted-sub": 'cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"',
    "digit": 'x=$(echo digit)',
    "legit": """x=$(python3 -c "print('legit')")""",
    "upper": 'x=$(echo GIT)',
    "bare-group": '{ echo git word; }',
    "guard-msg": 'test -f f || { echo "ERROR: run git pull" >&2; exit 1; }',
}
