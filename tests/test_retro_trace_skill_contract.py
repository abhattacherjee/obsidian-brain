"""Contract tests: the /retro and /check-items skills call retro_trace the way it works (#470)."""

from __future__ import annotations

from pathlib import Path

import retro_trace

REPO_ROOT = Path(__file__).resolve().parents[1]
RETRO = (REPO_ROOT / "skills" / "retro" / "SKILL.md").read_text(encoding="utf-8")
CHECK_ITEMS = (REPO_ROOT / "skills" / "check-items" / "SKILL.md").read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    body = text[text.index(start):]
    return body[:body.index(end)]


STEP_75 = _section(RETRO, "### Step 7.5", "### Step 8")


def test_step_75_numbers_items_then_applies_traces_before_clearing():
    items = STEP_75.index('retro_trace.py" items')
    apply = STEP_75.index('retro_trace.py" apply')
    clear = STEP_75.index("clear_retro_classification_pending(")
    assert items < apply < clear


def test_step_75_names_every_trace_kind_the_cli_accepts():
    for kind in ('"kind": "issue"', '"kind": "memory"', '"kind": "path"', '"kind": "skipped"'):
        assert kind in STEP_75


def test_step_75_example_payload_is_accepted_by_the_grammar():
    # The rendered form of the skill's example entry must parse as a trace.
    assert retro_trace.parse_trace("- [ ] x → skipped: <why>") == {"kind": "skipped", "reason": "<why>"}


def test_declined_is_a_trace_and_cancel_writes_nothing():
    assert "skipped: user declined" in STEP_75
    assert "Cancel is not decline" in STEP_75


def test_clear_step_reports_blocked_when_items_are_untraced():
    assert "retro_gate_untraced" in STEP_75
    assert 'print(f"blocked: {untraced}' in STEP_75


def test_apply_heredoc_terminator_is_per_invocation():
    assert "<<'OB_TRACES_EOF_<eof4>'" in STEP_75
    assert "\nOB_TRACES_EOF_<eof4>\n" in STEP_75


def test_check_items_step_2b_runs_closed_candidates_and_prints_failures():
    step = _section(CHECK_ITEMS, "## Step 2b", "## Step 3")
    assert "from retro_trace import closed_candidates" in step
    assert "closed-issue lookup failed" in step


def test_check_items_shows_and_flips_closed_candidates():
    assert "=== Closed on GitHub ===" in CHECK_ITEMS
    assert "[closed on GH]" in CHECK_ITEMS
    assert "[closed on GH: not planned]" in CHECK_ITEMS
    step8 = _section(CHECK_ITEMS, "## Step 8", "## Step 9")
    assert "Closed on GitHub" in step8 and "dry run" in step8
