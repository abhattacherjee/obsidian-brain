"""Contracts for the SKILL.md fence reader and worktree-isolation checks."""

from pathlib import Path

import pytest

from skill_shell import Block, REFUSED_FORMS, fenced_blocks, isolation_problems, substitution_bodies


@pytest.mark.parametrize("marker", ["`", "~"])
@pytest.mark.parametrize("length", [3, 4, 5])
def test_fence_lengths(marker, length):
    fence = marker * length
    assert fenced_blocks(f"{fence}bash\ncmd\n{fence}") == [Block(1, "bash", "cmd")]


def test_shorter_fences_are_body_text():
    text = "````bash\n```sh\ncmd\n```\n````"
    assert fenced_blocks(text) == [Block(1, "bash", "```sh\ncmd\n```")]


def test_longer_closer():
    assert fenced_blocks("~~~bash\ncmd\n~~~~\nafter") == [Block(1, "bash", "cmd")]


def test_other_marker_cannot_close():
    assert fenced_blocks("```bash\n~~~\ncmd\n```") == [Block(1, "bash", "~~~\ncmd")]


@pytest.mark.parametrize("width", [2, 4])
def test_body_loses_up_to_opener_indent(width):
    indent = " " * width
    text = f"{indent}~~~bash\n{indent}  a\n b\nc\n{indent}~~~"
    assert fenced_blocks(text) == [Block(1, "bash", "  a\nb\nc")]


def test_unclosed_fence_runs_to_eof():
    assert fenced_blocks("~~~bash\none\ntwo\n") == [Block(1, "bash", "one\ntwo")]


def test_info_uses_lowercased_first_word():
    assert fenced_blocks("```BaSh title=x\ncmd\n```") == [Block(1, "bash", "cmd")]


def test_language_filter():
    text = "```python\npass\n```\n~~~\nplain\n~~~"
    assert fenced_blocks(text) == []
    assert fenced_blocks(text, langs=None) == [Block(1, "python", "pass"), Block(4, "", "plain")]
    assert fenced_blocks(text, langs={"python"}) == [Block(1, "python", "pass")]


def test_skipped_fences_consume_their_bodies():
    assert fenced_blocks("````markdown\n```bash\nexample\n```\n````") == []


def test_backtick_opener_rejects_backticks_in_info():
    text = "```bash `example`\n~~~sh\ncmd\n~~~"
    assert fenced_blocks(text) == [Block(2, "sh", "cmd")]


def test_line_is_one_based_opener_position():
    assert fenced_blocks("intro\n\n\t~~~sh\ncmd\n  ~~~\t") == [Block(3, "sh", "cmd")]


def test_substitution_bodies_balance_parentheses_and_keep_unclosed_tail():
    assert substitution_bodies("$(a $(b)) `c` $(d") == ["a $(b))", "b)", "d", "c"]


@pytest.mark.parametrize("block", REFUSED_FORMS.values(), ids=REFUSED_FORMS.keys())
def test_refused_forms(block):
    assert isolation_problems(block)


def test_bare_group_fails_closed():
    assert isolation_problems("  { echo ok; }") == ["brace group"]


@pytest.mark.parametrize("block", [
    "cd '/repo'\nif ! test -f f; then\n  echo missing >&2\n  exit 1\nfi\nx=$(python3 -c 'print(1)')",
    "git rev-parse --show-toplevel",
    "git -C '/x' remote get-url origin",
    "test -f f && echo OK || echo MISSING",
    'echo "ERROR: run git pull"',
    "( echo git word )",
    "python3 -c \"import subprocess; subprocess.run(['git', '--version'])\"",
    "FOO=1 python3 << 'PYEOF'\nprint([\"git\"])\nPYEOF",
])
def test_clean_blocks(block):
    assert isolation_problems(block) == []


def test_real_skill_fences_are_closed():
    skills = Path(__file__).resolve().parents[1] / "skills"
    paths = sorted(skills.glob("*/SKILL.md"))
    assert len(paths) >= 17
    unclosed = set()
    for path in paths:
        text = path.read_text(encoding="utf-8")
        blocks = fenced_blocks(text, langs=None)
        # Appended prose changes the last body only when its fence is still open.
        if fenced_blocks(text + "\nend of document", langs=None) != blocks:
            unclosed.add(path.parent.name)
    assert unclosed == set()
