"""
Integration test 10 - /recall produces zero AskUserQuestion tool calls.

Verifies the Task 23 surgery: SKILL.md no longer instructs the orchestrator
to invoke AskUserQuestion.
"""
import os
import pathlib

SKILL_PATH = pathlib.Path(__file__).parent.parent / "skills" / "recall" / "SKILL.md"


def test_recall_skill_md_has_no_askuserquestion():
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "AskUserQuestion" not in text, \
        "/recall SKILL.md still references AskUserQuestion - deferral loop will recur"


def test_recall_skill_md_has_no_checkoff_machinery():
    text = SKILL_PATH.read_text(encoding="utf-8")
    forbidden_phrases = [
        "batch_cascade_checkoff",
        "Confirm checkoff",
        "Skip all — don't check off",
        "minItems",
        "maxItems",
        "candidate.text",
    ]
    found = [p for p in forbidden_phrases if p in text]
    assert not found, f"/recall still contains checkoff machinery: {found}"


def test_recall_skill_md_has_check_items_nudge():
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "run `/check-items` to triage" in text, \
        "/recall missing the /check-items footer nudge"


def test_recall_skill_md_task_manifest_is_three_tasks():
    text = SKILL_PATH.read_text(encoding="utf-8")
    task_create_lines = [l for l in text.splitlines() if l.lstrip().startswith("TaskCreate:")]
    assert len(task_create_lines) == 3, \
        f"expected 3 task-manifest entries, got {len(task_create_lines)}: {task_create_lines}"


def test_recall_skill_md_preserves_summarization_path():
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "### Step 2 — Summarize unsummarized notes" in text
    assert "### Step 3 — Build context brief" in text
    assert "Haiku" in text and ("parallel" in text.lower() or "Parallel" in text)
