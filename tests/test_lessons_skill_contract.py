"""Contract tests for skills/lessons/SKILL.md (#474).

The skill is a prompt, so these tests pin the promises that keep GitHub unchanged
without an explicit choice (#474 C.2): no issue creation or API POST, `gh issue
edit` only under the Raise step, `backfill --apply` only after a dry run and a
confirm question, and stdin closed on every `gh` and `python3` call.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent / "skills" / "lessons" / "SKILL.md"
TEXT = SKILL.read_text(encoding="utf-8")


def bash_blocks() -> list[str]:
    return re.findall(r"^```bash\n(.*?)^```", TEXT, re.M | re.S)


def logical_commands(block: str):
    """(first line, last line) of each gh/python3 command; a `python3 -c '` body spans lines."""
    lines = block.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.search(r"(?:^|[\s$(])(?:gh|python3)\s", line):
            j = i
            if line.count("'") % 2 == 1:  # unterminated quote: runs to the closing line
                j = i + 1
                while j < len(lines) and lines[j].count("'") % 2 == 0:
                    j += 1
            yield line, lines[min(j, len(lines) - 1)]
            i = j
        i += 1


def step_text(title: str) -> str:
    """Text from a `#### title` heading to the next heading of the same or a higher level."""
    m = re.search(rf"^#### {re.escape(title)}.*?(?=^#{{1,4}} |\Z)", TEXT, re.M | re.S)
    assert m, title
    return m.group(0)


def test_frontmatter():
    assert TEXT.startswith("---\nname: lessons\n")
    assert "Use when:" in TEXT.split("---")[1]
    assert re.search(r"^metadata:\n  version: \d+\.\d+\.\d+$", TEXT, re.M)


def test_skill_never_creates_issues_or_posts():
    code = "\n".join(bash_blocks())
    assert "gh issue create" not in code and "gh issue create" not in re.sub(
        r"never runs `gh issue create`", "", TEXT)
    assert not re.search(r"gh api[^\n]*(-X|--method)[ =]+POST", TEXT, re.I)
    assert not re.search(r"gh api[^\n]*(-f|-F|--field|--raw-field|--input)\b", code)
    assert "gh issue comment" not in code and "gh pr " not in code


def test_every_gh_issue_edit_sits_under_the_raise_step():
    def edits(text):
        return len(re.findall(r"gh issue edit", "\n".join(re.findall(r"^```bash\n(.*?)^```", text, re.M | re.S))))

    raise_step = step_text("Step 4b")
    assert "Raise" in raise_step.splitlines()[0]
    assert edits(TEXT) >= 1, "the raise step must show the edit command"
    assert edits(TEXT) == edits(raise_step)
    assert "Only for a change the user picked" in raise_step


def test_apply_only_after_dry_run_and_confirm():
    applies = [m.start() for m in re.finditer(r"backfill --apply", TEXT)]
    code_applies = [m.start() for m in re.finditer(r"^python3 .*backfill --apply", TEXT, re.M)]
    assert len(code_applies) == 1
    dry = [m.start() for m in re.finditer(r"^python3 .*backfill --accept", TEXT, re.M)]
    assert dry and dry[0] < code_applies[0]
    confirm = TEXT.index("Apply: N issues to file, M lessons to trace?")
    assert dry[0] < confirm < code_applies[0]
    assert applies  # the prose mentions it too, only the code line is an execution


def test_every_gh_and_python_command_closes_stdin():
    seen = 0
    for block in bash_blocks():
        for first, last in logical_commands(block):
            seen += 1
            assert "< /dev/null" in last, f"stdin not closed: {first!r} ... {last!r}"
    assert seen >= 10


def test_refresh_tracking_runs_before_report():
    assert TEXT.index("refresh-tracking <") < TEXT.index("report --json <")


def test_disabled_flag_stops_the_skill():
    i = TEXT.index("If `LESSONS_ENABLED` is `false`")
    assert "stop" in TEXT[i:i + 200].lower()
    assert "lessons_enabled" in TEXT[i:i + 200]


def test_snooze_boundary_is_stated():
    assert "on or after today" in TEXT


def test_accept_file_is_owner_only_and_in_the_state_dir():
    assert "mkstemp" in TEXT and ".claude/obsidian-brain" in TEXT
    assert "rm -f" in TEXT  # deleted after No and after apply


def test_accept_entry_fields_match_the_cli_contract():
    for field in ('"lesson_id"', '"file_issue": true', '"repo"', '"title"', '"body"',
                  '"milestone"', '"labels"', '"tracking_ref"'):
        assert field in TEXT, field
    for limit in ("1-256", "20,000", "1-100", "at most 10 labels"):
        assert limit in TEXT, limit


@pytest.mark.parametrize("i", range(20))
def test_bash_blocks_parse(i):
    blocks = bash_blocks()
    if i >= len(blocks):
        pytest.skip("fewer blocks")
    r = subprocess.run(["bash", "-n"], input=blocks[i], text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
