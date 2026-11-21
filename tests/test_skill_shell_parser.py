"""Contracts for the SKILL.md fence reader and worktree-isolation checks."""

import random
from pathlib import Path

import pytest

from skill_shell import (
    Block,
    _blank_quotes_and_heredocs,
    _scan,
    REFUSED_FORMS,
    fenced_blocks,
    heredoc_bodies,
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


@pytest.mark.parametrize(("info", "lang"), [
    ("bash:x", "bash"), ("sh,x", "sh"), ("bash{.x}", "bash"), ("Zsh;y", "zsh"),
])
def test_info_string_language_stops_at_the_first_other_character(info, lang):
    assert fenced_blocks(f"```{info}\ncmd\n```") == [Block(1, lang, "cmd")]


@pytest.mark.parametrize("info", ["c++", "shell-session", "{.bash}", "python"])
def test_info_string_languages_that_are_not_shell_stay_out(info):
    assert fenced_blocks(f"```{info}\ncmd\n```") == []


# Fix 1 (X-017): `<<` in arithmetic is a shift, not a heredoc.
@pytest.mark.parametrize("block", [
    "((mask << SHIFT))\nx=$(git rev-parse HEAD)",
    "x=$((a << 2))\ny=$(git rev-parse HEAD)",
    "if ((a << 1)); then\n  :\nfi\nx=`git rev-parse HEAD`",
])
def test_a_shift_is_not_a_heredoc_opener(block):
    assert "git inside a command substitution" in isolation_problems(block)
    assert heredoc_bodies(block) == []


def test_a_real_heredoc_after_arithmetic_is_still_read():
    block = "((a << 1))\ncat <<EOF\nbody\nEOF\nx"
    assert heredoc_bodies(block) == ["body"]


# Fix 2 (X-018): an unquoted heredoc expands `$(...)` and backticks.
@pytest.mark.parametrize("opener", ["<<EOF", "<<-EOF", "<< EOF"])
def test_an_unquoted_heredoc_body_is_scanned_for_substitutions(opener):
    assert "git inside a command substitution" in isolation_problems(
        f"cat {opener}\n$(git rev-parse HEAD)\nEOF"
    )


def test_an_unquoted_heredoc_body_is_scanned_for_backticks():
    assert "git inside a command substitution" in isolation_problems(
        "cat <<EOF\nx `git log` y\nEOF"
    )


@pytest.mark.parametrize("opener", ["<<'EOF'", '<<"EOF"', "<<\\EOF", "<<-'EOF'"])
def test_a_quoted_heredoc_body_is_inert(opener):
    assert isolation_problems(f"cat {opener}\n$(git rev-parse HEAD)\n`git`\nEOF") == []


def test_an_unquoted_heredoc_body_is_not_scanned_for_brace_groups():
    assert isolation_problems("cat <<EOF\n{ echo; }\nEOF") == []


def test_an_escaped_substitution_in_an_unquoted_heredoc_is_text():
    assert isolation_problems("cat <<EOF\n\\$(git log)\nEOF") == []


def test_heredoc_bodies_after_two_openers_on_one_line():
    assert heredoc_bodies("cat <<A <<'B'\none\nA\ntwo\nB\n") == ["one", "two"]


# Fix 3 (C-005): case-arm parens and backticks.
@pytest.mark.parametrize("block", [
    "x=$(case $y in a) echo;; esac; git log)",
    "x=$(case $y in (a) echo;; esac; git log)",
    "x=$(case $y in a|b) echo ok;; c) echo no;; esac; git log)",
    "x=$(case $y in a) case $z in b) echo;; esac;; esac; git log)",
])
def test_a_case_arm_paren_does_not_close_a_substitution(block):
    assert any("git log" in body for body in substitution_bodies(block))
    assert "git inside a command substitution" in isolation_problems(block)


def test_a_case_statement_outside_a_substitution_is_not_one():
    assert isolation_problems("x=$(case $y in a) echo;; esac)\ncase $y in a) git log;; esac") == []


def test_a_case_word_that_is_an_argument_does_not_open_a_case():
    assert substitution_bodies("x=$(echo case in a); y=$(git x)") == ["echo case in a", "git x"]


@pytest.mark.parametrize("block", [
    "x=`echo \\` ; git log`",
    "echo `git",
    "x=`echo a` y=`git log`",
    'echo "`git log`"',
])
def test_backticks_honour_escapes_and_run_to_the_end_when_unmatched(block):
    assert "git inside a command substitution" in isolation_problems(block)


def test_an_escaped_backtick_does_not_end_the_span():
    assert substitution_bodies("`a \\` b` c") == ["a \\` b"]


def test_backticks_in_single_quotes_are_text():
    assert isolation_problems("echo 'run `git log`'") == []


def test_non_ascii_text_before_a_substitution():
    assert isolation_problems("echo café") == []
    assert "git inside a command substitution" in isolation_problems("echo café $(git x)")


@pytest.mark.parametrize("block", [
    "x=`echo ok # comment`\ngit status",
    "y=`echo $(echo a # c) b`\ngit status",
    'y=`echo "$(echo a # c) b"`\ngit status',
])
def test_backtick_closes_during_comment(block):
    assert isolation_problems(block) == []


def test_escaped_backtick_in_a_comment_does_not_close_the_substitution():
    assert substitution_bodies(r"x=`echo ok # \` comment` git status") == [
        r"echo ok # \` comment",
    ]


def test_later_backtick_substitution_still_reports_git():
    assert "git inside a command substitution" in isolation_problems(
        "z=`echo ok # c`; w=`git z`"
    )
    assert substitution_bodies("z=`echo ok # c`; w=`git z`") == ["echo ok # c", "git z"]


def test_comment_in_parenthesis_substitution_ends_at_newline():
    assert "git inside a command substitution" in isolation_problems(
        "x=$(echo a # c\ngit y\n)"
    )


def test_scan_seeded_fuzz():
    rng = random.Random(526)
    alphabet = "$()`'\"#;&|<>{}[]\\\n\t =-_abcgitEOFéü中🙂"
    for _ in range(5000):
        block = "".join(rng.choices(alphabet, k=rng.randint(0, 60)))
        assert len(_scan(block)[0]) == len(block), repr(block)
        isolation_problems(block)


# Fix 4 (X-022): a reserved word is a word only at command position.
@pytest.mark.parametrize("block", [
    "echo then {",
    "printf do {",
    "echo a else {\n}",
    "echo elif {",
])
def test_a_reserved_word_that_is_an_argument_does_not_start_a_group(block):
    assert isolation_problems(block) == []


@pytest.mark.parametrize("block", [
    "if x; then { echo; }; fi",
    "if x\nthen\n{ echo; }\nfi",
    "for i in 1; do { echo; }; done",
    "if x; then :; else { echo; }; fi",
])
def test_a_reserved_word_at_command_position_still_starts_a_group(block):
    assert "brace group" in isolation_problems(block)
