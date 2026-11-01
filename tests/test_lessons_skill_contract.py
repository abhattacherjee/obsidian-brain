"""Contract tests for skills/lessons/SKILL.md (#474).

The skill is a prompt, so these tests pin the promises that keep GitHub unchanged
without an explicit choice (#474 C.2): no issue creation or API POST, `gh issue
edit` only under the Raise step, `backfill --apply` only after a dry run and a
confirm question, and stdin closed on every `gh` and `python3` call.
"""
from __future__ import annotations

import re
import shlex
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


def bash_block_spans(text: str):
    """(start offset of the body, body) of each fenced bash block."""
    return [(m.start(1), m.group(1)) for m in re.finditer(r"^```bash\n(.*?)^```", text, re.M | re.S)]


def logical_lines(text: str):
    """(offset in text, line) for every line of every bash block, any indent, with backslash
    continuations joined into one line."""
    out = []
    for start, body in bash_block_spans(text):
        pos, pending, first = start, "", None
        for raw in body.split("\n"):
            if pending == "":
                first = pos
            if raw.endswith("\\"):
                pending += raw[:-1] + " "
            else:
                out.append((first, pending + raw))
                pending = ""
            pos += len(raw) + 1
        if pending:
            out.append((first, pending))
    return out


READ_ONLY_GH = {("issue", "list"), ("issue", "view"), ("label", "list"), ("repo", "view")}
GH_WRITE_TOKEN = re.compile(r"(?:-X|--method|-f|-F|--field|--raw-field|--input)")
# a `gh` word that starts a command: line start, or after whitespace, `;`, `&`, `|`, `(` or `$(`
GH_CALL = re.compile(r"(?:^|[\s;&|(])gh\s+(\S+)(?:\s+(\S+))?(.*)$")


def gh_calls(text: str):
    """(offset, subcommand pair, rest of line) for every gh invocation in the bash blocks."""
    calls = []
    for offset, line in logical_lines(text):
        for m in re.finditer(r"(?:^|[\s;&|(])gh(?=\s)", line):
            m2 = GH_CALL.match(line[m.start():])
            if m2:
                calls.append((offset, (m2.group(1), m2.group(2) or ""), (m2.group(2) or "") + m2.group(3)))
    return calls


def raise_span(text: str):
    m = re.search(r"^#### Step 4b.*?(?=^#{1,4} |\Z)", text, re.M | re.S)
    assert m
    return m.start(), m.end()


def gh_violations(text: str) -> list[str]:
    """Every gh call that is not read-only, allowing `gh issue edit` only inside the Raise step.

    The rest of each call is split with shlex, so quoted, attached and `=` forms of the method and
    field flags are all seen. A line shlex cannot split counts as a violation.
    """
    low, high = raise_span(text)
    bad = []
    for offset, pair, rest in gh_calls(text):
        try:
            tokens = shlex.split(pair[0] + " " + rest, comments=False)
        except ValueError as exc:
            bad.append(f"cannot parse gh call ({exc}): {rest.strip()}")
            continue
        if pair[0] == "api":
            if any(GH_WRITE_TOKEN.match(t) for t in tokens[1:]):
                bad.append(f"gh api with a write flag: {rest.strip()}")
        elif pair == ("issue", "edit"):
            if not low <= offset < high:
                bad.append("gh issue edit outside the Raise step")
        elif pair not in READ_ONLY_GH:
            bad.append(f"gh {pair[0]} {pair[1]}")
    return bad


def branch(text: str, label: str) -> tuple[int, int]:
    """Span of the `- **Yes**` or `- **No**` bullet under the Step 6 confirm question."""
    m = re.search(rf"^- \*\*{label}\*\* .*?(?=^- \*\*|^## |\Z)", text, re.M | re.S)
    assert m, label
    return m.start(), m.end()


def is_apply(line: str) -> bool:
    """A backfill call is an apply when any token is --apply or an abbreviation of it (--ap...)."""
    return any(t.startswith("--ap") for t in re.split(r"[\s=]+", line))


def apply_violations(text: str) -> list[str]:
    """backfill calls: exactly one with --apply, after a dry run and the confirm, inside the Yes bullet."""
    runs = [(o, l) for o, l in logical_lines(text) if re.search(r"retro_rollup\.py\"?\s+backfill\b", l)]
    applies = [(o, l) for o, l in runs if is_apply(l)]
    dry = [(o, l) for o, l in runs if (o, l) not in applies and "--accept" in l]
    bad = []
    if len(applies) != 1:
        return [f"{len(applies)} apply calls, want exactly 1"]
    at = applies[0][0]
    if not dry or not min(o for o, _ in dry) < at:
        bad.append("no dry run before the apply")
    m = re.search(r"Apply: N issues to file, .*lessons to trace\?", text)
    if not m or not (dry and min(o for o, _ in dry) < m.start() < at):
        bad.append("confirm question not between the dry run and the apply")
    yes = branch(text, "Yes")
    if not yes[0] <= at < yes[1]:
        bad.append("apply is not inside the Yes bullet")
    no = branch(text, "No")
    if no[0] <= at < no[1] or any(no[0] <= o < no[1] for o, _ in applies):
        bad.append("apply is inside the No bullet")
    return bad


FLAG_VALUE = re.compile(r"(--milestone|--add-label|--remove-label)(?:=|\s+)('(?:[^']|'\\'')*'\S*|\S*)")
WHOLE_QUOTED = re.compile(r"'(?:[^']|'\\'')*'")


def edit_values(text: str):
    found = []
    for _, line in logical_lines(text):
        if re.search(r"(?:^|[\s;&|(])gh\s+issue\s+edit\b", line):
            found += FLAG_VALUE.findall(line)
    return found


def quoting_violations(text: str) -> list[str]:
    found = edit_values(text)
    flags = {flag for flag, _ in found}
    bad = [f"no value found for {f}" for f in ("--milestone", "--add-label", "--remove-label") if f not in flags]
    bad += [f"{flag} value not wholly single-quoted: {value}" for flag, value in found
            if not WHOLE_QUOTED.fullmatch(value)]
    return bad


def test_frontmatter():
    assert TEXT.startswith("---\nname: lessons\n")
    assert "Use when:" in TEXT.split("---")[1]
    assert re.search(r"^metadata:\n  version: \d+\.\d+\.\d+$", TEXT, re.M)


def test_skill_makes_no_github_write_without_an_explicit_choice():
    calls = gh_calls(TEXT)
    assert len(calls) >= 5, f"positive control: only {len(calls)} gh calls found"
    assert gh_violations(TEXT) == []


WRITE_FORMS = [
    "gh api -X PATCH repos/o/r/issues/1 < /dev/null",
    "gh api -XPOST repos/o/r/issues < /dev/null",
    "gh api -X PUT repos/o/r/issues/1/lock < /dev/null",
    "gh api -X DELETE repos/o/r/issues/1 < /dev/null",
    "gh api --method=PUT repos/o/r/issues/1/lock < /dev/null",
    "gh api --method POST repos/o/r/issues < /dev/null",
    "gh api repos/o/r/issues -ftitle=x < /dev/null",
    "gh api repos/o/r/issues -F title=x < /dev/null",
    "gh api repos/o/r/issues --raw-field title=x < /dev/null",
    "gh api repos/o/r/issues --input body.json < /dev/null",
    "gh api \\\n  -X DELETE repos/o/r/issues/1 < /dev/null",
    "gh api 'repos/o/r/issues' '-XPOST' '-ftitle=Unapproved' < /dev/null",
    'gh api "repos/o/r/issues" "-XPOST" "-ftitle=Unapproved" < /dev/null',
    "gh api repos/o/r/issues '--method=PUT' < /dev/null",
    "gh issue create --title x < /dev/null",
    "gh issue new --title x < /dev/null",
    "gh issue close 1 < /dev/null",
    "gh issue delete 1 < /dev/null",
    "gh issue comment 1 --body x < /dev/null",
    "gh issue edit 1 --milestone 'x' < /dev/null",
    "gh pr merge 1 < /dev/null",
    "echo hi; gh issue close 1 < /dev/null",
    "true && gh issue close 1 < /dev/null",
    "echo hi | gh issue close 1 < /dev/null",
    "X=$(gh issue close 1 < /dev/null)",
    "    gh issue close 1 < /dev/null",
]


@pytest.mark.parametrize("form", WRITE_FORMS)
def test_github_write_guard_catches_each_write_form(form):
    mutated = TEXT.rstrip("\n") + f"\n\n```bash\n{form}\n```\n"
    assert gh_violations(mutated), form


def test_github_write_guard_allows_the_raise_edit_only_in_the_raise_step():
    low, high = raise_span(TEXT)
    assert any(low <= o < high for o, pair, _ in gh_calls(TEXT) if pair == ("issue", "edit"))
    assert gh_violations(TEXT) == []


def test_every_gh_issue_edit_sits_under_the_raise_step():
    def edits(text):
        return len(re.findall(r"gh issue edit", "\n".join(re.findall(r"^```bash\n(.*?)^```", text, re.M | re.S))))

    raise_step = step_text("Step 4b")
    assert "Raise" in raise_step.splitlines()[0]
    assert edits(TEXT) >= 1, "the raise step must show the edit command"
    assert edits(TEXT) == edits(raise_step)
    assert "Only for a change the user picked" in raise_step


def test_apply_only_after_dry_run_and_confirm():
    assert apply_violations(TEXT) == []
    assert any(is_apply(l) for _, l in logical_lines(TEXT))  # positive control


def _dry_run_line():
    return next(l for _, l in logical_lines(TEXT)
                if re.search(r"retro_rollup\.py\"?\s+backfill\b", l) and "--apply" not in l)


def test_apply_guard_catches_a_second_apply_in_any_form():
    dry = _dry_run_line()
    reordered = TEXT.replace(dry, dry.replace(" < /dev/null", " --apply < /dev/null"), 1)
    assert apply_violations(reordered)  # dry run turned into an apply, flags in another order
    abbreviated = TEXT.replace(dry, dry.replace(" < /dev/null", " --appl < /dev/null"), 1)
    assert apply_violations(abbreviated)  # argparse used to accept --appl as --apply
    extra = TEXT.replace("### Step 5", "```bash\n    python3 \"$HOOKS/retro_rollup.py\" backfill --accept X --apply < /dev/null\n```\n\n### Step 5", 1)
    assert apply_violations(extra)


def test_apply_guard_catches_apply_in_the_no_branch():
    swapped = (TEXT.replace("- **No** —", "@@").replace("- **Yes** —", "- **No** —").replace("@@", "- **Yes** —"))
    assert swapped != TEXT
    assert apply_violations(swapped)


def test_apply_guard_catches_apply_before_the_dry_run():
    dry = _dry_run_line()
    apply_line = next(l for _, l in logical_lines(TEXT) if "--apply" in l and "backfill" in l)
    mutated = TEXT.replace(dry, "@@DRY@@", 1).replace(apply_line, dry, 1).replace("@@DRY@@", apply_line, 1)
    assert apply_violations(mutated)


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


def test_raise_values_are_single_quoted_and_escaped():
    assert "write a `'` inside a value as `'\\''`" in TEXT
    assert len(edit_values(TEXT)) >= 3  # positive control: milestone, add-label, remove-label
    assert quoting_violations(TEXT) == []


@pytest.mark.parametrize("old,new", [
    ("--milestone '<MILESTONE TITLE>'", "--milestone=$MILESTONE"),
    ("--milestone '<MILESTONE TITLE>'", "--milestone '<MILESTONE TITLE>'x"),
    ("--milestone '<MILESTONE TITLE>'", "--milestone \"<MILESTONE TITLE>\""),
    ("--add-label '<NEW P LABEL>'", "--add-label=$NEW"),
    ("--add-label '<NEW P LABEL>'", "--add-label '<NEW P LABEL>'y"),
    ("--remove-label '<OLD P LABEL>'", "--remove-label $OLD"),
])
def test_quoting_guard_catches_unquoted_values(old, new):
    assert old in TEXT
    assert quoting_violations(TEXT.replace(old, new, 1))


def test_accept_file_is_owner_only_and_in_the_state_dir():
    assert "mkstemp" in TEXT and ".claude/obsidian-brain" in TEXT
    assert "rm -f" in TEXT  # deleted after No and after apply


def test_accept_entry_fields_match_the_cli_contract():
    for field in ('"lesson_id"', '"file_issue": true', '"repo"', '"title"', '"body"',
                  '"milestone"', '"labels"', '"tracking_ref"'):
        assert field in TEXT, field
    for limit in ("1-256", "20,000", "1-100", "at most 10 labels", "1-50"):
        assert limit in TEXT, limit


def test_every_fenced_bash_block_is_found():
    assert len(bash_blocks()) == len(re.findall(r"^```bash$", TEXT, re.M)) >= 10


@pytest.mark.parametrize("i", range(len(bash_blocks())))
def test_bash_blocks_parse(i):
    r = subprocess.run(["bash", "-n"], input=bash_blocks()[i], text=True, capture_output=True)
    assert r.returncode == 0, r.stderr


# --- wording promises from the #478 review ----------------------------------------------

def section(title: str) -> str:
    m = re.search(rf"^###+ {re.escape(title)}.*?(?=^###? |\Z)", TEXT, re.M | re.S)
    assert m, title
    return m.group(0)


def test_confirm_counts_pending_filings_separately():
    step6 = section("Step 6")
    assert "P pending reconcile (may file)" in step6
    assert "reason `pending reconcile`" in step6 or "`pending reconcile`" in step6


def test_confirm_counts_adopted_lessons_in_m():
    step6 = section("Step 6")
    assert "starts with `adopts existing issue`" in step6


def test_empty_walk_still_reaches_the_batch_confirm():
    step3 = section("Step 3")
    assert "go on to Step 5" in step3
    assert "stop only when Step 5 finds nothing either" in step3


def test_batch_confirm_skips_snoozed_and_dormant_lessons():
    step5 = section("Step 5")
    assert "`dormant` true" in step5 and "`snoozed_until` on or after today" in step5
    assert "snoozed in Step 4c" in step5


def test_file_is_offered_for_none_and_gone_tracking():
    step4 = section("Step 4")
    assert "`none` or `gone`" in step4


def test_failed_edit_and_failed_lookups_are_handled():
    raise_step = step_text("Step 4b")
    assert "fails, say so" in raise_step and "do not add the entry" in raise_step
    file_step = step_text("Step 4a")
    assert "If either lookup fails, say so" in file_step and "no milestone and no labels" in file_step
