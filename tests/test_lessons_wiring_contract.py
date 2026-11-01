"""Contract tests for the lesson wiring in /retro, /standup and /emerge (#475).

The skills are prompts, so these tests pin the promises: /retro never waits or
asks, /standup offers /lessons only in weekly mode with lessons waiting, and
every skill reads the lessons through `report --json` and not the state file.
"""
from __future__ import annotations

import re
from pathlib import Path

SKILLS = Path(__file__).resolve().parent.parent / "skills"


def text(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def section(body: str, heading_start: str) -> str:
    m = re.search(rf"^### {re.escape(heading_start)}.*?(?=^### |^## |\Z)", body, re.M | re.S)
    assert m, heading_start
    return m.group(0)


def bash_code(sec: str) -> str:
    return "\n".join(re.findall(r"^```bash\n(.*?)^```", sec, re.M | re.S))


def test_retro_step_76_sits_between_75_and_8():
    body = text("retro")
    a, b, c = (body.index(f"### Step {s}") for s in ("7.5 ", "7.6 ", "8 "))
    assert a < b < c


def test_retro_step_76_is_detached_and_never_asks():
    step = section(text("retro"), "Step 7.6")
    for needle in ("nohup", "< /dev/null", "--note", "lesson update queued", "lessons-update.log"):
        assert needle in step, needle
    assert re.search(r"2>&1 &$", step, re.M), "the update must be started in the background"
    assert "AskUserQuestion" not in step
    assert "never asks" in step and "never waits" in step


def test_retro_step_76_checks_the_flag_before_starting():
    step = section(text("retro"), "Step 7.6")
    assert step.index("lessons_enabled") < step.index("nohup")
    assert '!= "off"' in step


def test_standup_lessons_section_uses_report_json_only():
    body = text("standup")
    sec = body[body.index("### Recurring lessons"):body.index("Rules for the header sections:")]
    assert "report --json" in sec
    assert "lessons-state.json" not in body
    assert "first 5" in sec and "`dormant`" in sec and "snoozed_until" in sec
    assert "on or after today" in sec
    assert "No lessons yet." in sec
    assert "refresh-tracking" in sec and "WEEKLY" in sec


def test_standup_offers_lessons_only_in_weekly_mode_with_lessons_waiting():
    body = text("standup")
    offer = section(body, "Step 13b")
    assert "`WEEKLY` is `true`" in offer
    assert "`LESSONS_ENABLED` is `true`" in offer
    assert "`LESSONS_WAITING` is above 0" in offer
    assert "lessons wait" in offer
    assert len(re.findall(r"invoke the `lessons` skill", body)) == 1  # the only call site


def test_standup_daily_mode_makes_no_gh_call_for_lessons():
    body = text("standup")
    code = bash_code(body[body.index("### Recurring lessons"):body.index("Rules for the header sections:")])
    assert not re.search(r"\bgh\b", code)
    assert 'if [ "$WEEKLY" = true ]; then python3' in code


def test_emerge_lessons_section_reads_member_dates_from_report_json():
    body = text("emerge")
    assert "Lessons growing in this window" in body
    assert "lessons-state.json" not in body
    step = body[body.index("### Step 4"):]
    assert "report --json" in step
    assert "members[].date" in step
    assert "`sessions` of at least 2" in step
    assert "includes its start date" in step
    assert "keep 10" in step


def test_new_blocks_close_stdin_on_python_calls():
    for name, start in (("retro", "Step 7.6"), ("standup", "Recurring lessons"), ("emerge", "Step 4")):
        body = text(name)
        if name == "emerge":
            sec = body[body.index("### Step 4"):]
        elif name == "standup":
            sec = body[body.index("### Recurring lessons"):body.index("Rules for the header sections:")]
        else:
            sec = section(body, start)
        calls = 0
        lines = bash_code(sec).split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            if re.search(r'python3 (-c|"\$HOOKS)', line) and "HOOKS=$(" not in line:
                calls += 1
                j = i
                if line.count("'") % 2 == 1:  # a multi-line python3 -c body ends at the closing quote
                    j = i + 1
                    while j < len(lines) and lines[j].count("'") % 2 == 0:
                        j += 1
                assert "< /dev/null" in lines[min(j, len(lines) - 1)], (name, line)
                i = j
            i += 1
        assert calls >= 1, name
