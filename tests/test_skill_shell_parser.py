"""Contracts for the SKILL.md fence reader and worktree-isolation checks."""

from pathlib import Path

import pytest

from skill_shell import (
    Block,
    _blank_quotes_and_heredocs,
    REFUSED_FORMS,
    fenced_blocks,
    heredoc_brace_problems,
    isolation_problems,
    substitution_bodies,
)


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
    assert substitution_bodies("$(a $(b)) `c` $(d") == ["a $(b)", "b", "d", "c"]


@pytest.mark.parametrize(("block", "bodies"), [
    ("$(printf '%s' ')' && git x)", ["printf '%s' ')' && git x"]),
    ('$(echo ")" ; git x)', ['echo ")" ; git x']),
    (r"$(echo \) ; git x)", [r"echo \) ; git x"]),
    ('"$(dirname "$(echo ")")")"', ['dirname "$(echo ")")"', 'echo ")"']),
    ("<(git log) >(cat)", ["git log", "cat"]),
    ("$(cat <<'E'\n)\nE\n)", ["cat <<'E'\n)\nE\n"]),
])
def test_substitution_bodies_skip_parens_in_quotes_escapes_and_heredocs(block, bodies):
    assert substitution_bodies(block) == bodies


def test_blank_quotes_and_heredocs_keeps_length_and_newlines():
    block = "a '{ b' \"{ c\" d\ne <<-E\n\t{ x }\n\tE\nf # { g\nh"
    blank = _blank_quotes_and_heredocs(block)
    assert len(blank) == len(block)
    assert blank.count("\n") == block.count("\n")
    assert "{" not in blank
    assert blank.startswith("a '   ' \"   \" d\ne <<-E\n")
    assert blank.endswith("\tE\nf      \nh")


@pytest.mark.parametrize("name", REFUSED_FORMS)
def test_refused_forms(name):
    block, expected = REFUSED_FORMS[name]
    assert expected in isolation_problems(block), name


def test_bare_group_fails_closed():
    assert isolation_problems("  { echo ok; }") == ["brace group"]


@pytest.mark.parametrize("block", [
    "cd '/repo'\nif ! test -f f; then\n  echo missing >&2\n  exit 1\nfi\nx=$(python3 -c 'print(1)')",
    "git rev-parse --show-toplevel",
    "git -C '/x' remote get-url origin",
    "test -f f && echo OK || echo MISSING",
    'echo "ERROR: run git pull"',
    "( echo git word )",
    'echo "${HOME}"',
    "python3 -c \"print({'a': 1})\"",
    'x="a { b"',
    "cat << 'E'\n{'a': 1}\nif x; then { y; }; fi\nE",
    "f() { echo ok; }",
    "echo {a,b}",
    "python3 -c \"import subprocess; subprocess.run(['git', '--version'])\"",
    "FOO=1 python3 << 'PYEOF'\nprint([\"git\"])\nPYEOF",
])
def test_clean_blocks(block):
    assert isolation_problems(block) == []


@pytest.mark.parametrize("body", [
    'x = {"a": 1}',
    'x = {"a": 1, "b": 2}',
    'x = {"a": [], "b": []}',
    """print(f"{d['r']}: {d['r']}")""",
])
def test_heredoc_quoted_braces_are_flagged(body):
    assert heredoc_brace_problems(f"python3 << 'PYEOF'\n{body}\nPYEOF") == [body]


@pytest.mark.parametrize("body", [
    "x = {}",
    "x = [1, 2]",
    'print(f"... {exc}")',
])
def test_heredoc_bodies_that_ran_are_allowed(body):
    assert heredoc_brace_problems(f"python3 << 'PYEOF'\n{body}\nPYEOF") == []


def test_heredoc_unquoted_brace_expansion_is_allowed():
    assert heredoc_brace_problems("cat << 'EOF'\n{a,b}\nEOF") == []


@pytest.mark.parametrize("block", [
    """python3 -c '\nx = {"a": 1}\n'""",
    'cat << EOF\nhello\nEOF\n{"a": 1}',
    'cat <<< EOF\n{"a": 1}',
])
def test_quoted_braces_outside_heredoc_bodies_are_allowed(block):
    assert heredoc_brace_problems(block) == []


@pytest.mark.parametrize(("opener", "closer"), [
    ("<<-EOF", "\tEOF"),
    ('<< "EOF"', "EOF"),
    ("<< 'OB_NOTE_EOF_<eof4>'", "OB_NOTE_EOF_<eof4>"),
])
def test_heredoc_delimiter_forms(opener, closer):
    block = f'cat {opener}\n\t{{"a": 1}}  \n{closer}\n{{"outside": 2}}'
    assert heredoc_brace_problems(block) == ['{"a": 1}']


def test_heredoc_scan_resumes_after_the_delimiter_and_keeps_an_unclosed_body():
    block = """cat << FIRST
{"a": 1}
FIRST
{"outside": 2}
cat << SECOND
{"b": 3}"""
    assert heredoc_brace_problems(block) == ['{"a": 1}', '{"b": 3}']


def test_an_indented_delimiter_does_not_end_a_plain_heredoc():
    block = "python3 << 'PYEOF'\n PYEOF\nx = {\"a\": 1}\nPYEOF\nafter"
    assert heredoc_brace_problems(block) == ['x = {"a": 1}']


def test_a_space_indented_delimiter_does_not_end_a_dash_heredoc():
    block = "cat <<-EOF\n  EOF\n{\"a\": 1}\n\tEOF\n{\"outside\": 2}"
    assert heredoc_brace_problems(block) == ['{"a": 1}']


def test_a_tab_indented_delimiter_ends_a_dash_heredoc():
    block = "cat <<-EOF\n\t{\"a\": 1}\n\t\tEOF\n{\"outside\": 2}"
    assert heredoc_brace_problems(block) == ['{"a": 1}']


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
