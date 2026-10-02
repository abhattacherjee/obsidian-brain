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
    assert "inline in the main session" in body
    assert "**must not** be delegated to a subagent" in body


def test_inline_rule_precedes_step_5():
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert text.index("### Steps 5 to 8 run inline") < text.index(
        "### Step 5 — Derive session ID"
    )


def test_step_7_keeps_the_path_note_writer_printed():
    body = _section(
        SKILL_PATH.read_text(encoding="utf-8"), "Step 7 — Generate filename and write"
    )
    assert "<NOTE_PATH>" in body
    # The gate is armed with the returned path, not one rebuilt from config.
    assert "\"<current-session-id>\" '<NOTE_PATH>'" in body
    assert "'\\''" in body
    assert "$VAULT_PATH/$INSIGHTS_FOLDER/<filename>" not in body


def test_note_path_is_never_a_shell_variable():
    # Shell state does not persist between Bash calls, so "$NOTE_PATH" would
    # expand to an empty string and ls -l would fail on a good save.
    assert "$NOTE_PATH" not in SKILL_PATH.read_text(encoding="utf-8")


def test_step_8_checks_existence_before_saying_saved():
    body = _section(SKILL_PATH.read_text(encoding="utf-8"), "Step 8 — Confirm")
    assert "ls -l '<NOTE_PATH>'" in body
    assert "`<NOTE_PATH>`" in body
    assert 'Never print "saved" without this check' in body
    assert body.index("ls -l '<NOTE_PATH>'") < body.index("Retrospective saved!")
    assert "$VAULT_PATH/$INSIGHTS_FOLDER/<filename>" not in body


def test_note_path_is_single_quoted():
    # Double quotes would let the shell expand a $ or backtick in the path.
    assert '"<NOTE_PATH>"' not in SKILL_PATH.read_text(encoding="utf-8")
