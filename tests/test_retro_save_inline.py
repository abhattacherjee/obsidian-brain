"""
#384 - /retro save steps run inline, and Step 8 proves the note exists.

A retro whose Steps 5-8 were handed to a subagent ended with no proof the
note was written. These tests pin the two rules SKILL.md must state.
"""
import pathlib
import re

SKILL_PATH = pathlib.Path(__file__).parent.parent / "skills" / "retro" / "SKILL.md"


def _section(text, heading):
    """Return the body of a '### <heading>' section, up to the next ### heading."""
    m = re.search(
        r"^### " + re.escape(heading) + r".*?$(.*?)(?=^### |\Z)",
        text,
        re.M | re.S,
    )
    assert m, f"retro SKILL.md has no '### {heading}' section"
    return m.group(1)


def test_save_steps_must_run_inline():
    text = SKILL_PATH.read_text(encoding="utf-8")
    body = _section(text, "Steps 5 to 8 run inline")
    assert "main session" in body
    assert "subagent" in body
    assert "must not" in body.lower()


def test_inline_rule_precedes_step_5():
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert text.index("### Steps 5 to 8 run inline") < text.index(
        "### Step 5 — Derive session ID"
    )


def test_step_7_keeps_the_path_note_writer_printed():
    body = _section(
        SKILL_PATH.read_text(encoding="utf-8"), "Step 7 — Generate filename and write"
    )
    assert "NOTE_PATH" in body


def test_step_8_confirms_with_returned_path_and_existence_check():
    body = _section(SKILL_PATH.read_text(encoding="utf-8"), "Step 8 — Confirm")
    assert 'ls -l "$NOTE_PATH"' in body
    assert "`$NOTE_PATH`" in body
    # The old confirmation rebuilt the path from config instead of using
    # the one note_writer returned.
    assert "$VAULT_PATH/$INSIGHTS_FOLDER/<filename>" not in body
