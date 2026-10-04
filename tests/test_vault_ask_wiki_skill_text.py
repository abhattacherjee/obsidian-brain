"""Pins /vault-ask's wiki instructions (#395)."""
from pathlib import Path
SKILL = (Path(__file__).resolve().parent.parent / "skills/vault-ask/SKILL.md").read_text()


def test_read_only_statement_is_gone():
    assert "this skill is read-only" not in SKILL


def test_wiki_commands_are_used():
    for cmd in ("wiki.py\" lookup", "wiki.py\" stale", "wiki.py\" count", "wiki.py\" rule", "wiki.py\" file", "--caller"):
        assert cmd in SKILL, cmd


def test_payload_goes_through_a_file_not_a_shell_string():
    assert "wiki-payload-" in SKILL and "Never build the JSON in a shell string" in SKILL


def test_candidate_cap_and_index_exclusion():
    assert "at most 3" in SKILL and "claude-wiki-index" in SKILL


def test_reviewed_choices_offered():
    for opt in ("Keep my page", "Refresh and overwrite my edits", "Save the fresh answer as a new page"):
        assert opt in SKILL, opt
