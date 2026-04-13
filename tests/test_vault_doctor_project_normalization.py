"""Tests for vault_doctor_checks/project_name_normalization.py."""

from __future__ import annotations

import os
import sys

import pytest

# Ensure scripts/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


@pytest.fixture
def norm_vault(tmp_path):
    """Create a minimal vault with underscore project names."""
    sessions = tmp_path / "claude-sessions"
    insights = tmp_path / "claude-insights"
    sessions.mkdir()
    insights.mkdir()
    return {"vault": tmp_path, "sessions": sessions, "insights": insights}


def _write_note(path, project, note_type="claude-session"):
    path.write_text(
        f"---\n"
        f"type: {note_type}\n"
        f"date: 2026-04-13\n"
        f"project: {project}\n"
        f"session_id: abc123\n"
        f"tags:\n"
        f"  - claude/{note_type.split('-')[1]}\n"
        f"  - claude/project/{project}\n"
        f"---\n\n# Test note\n",
        encoding="utf-8",
    )


def test_scan_finds_underscored_projects(norm_vault):
    import vault_doctor_checks.project_name_normalization as check

    _write_note(norm_vault["sessions"] / "session1.md", "personal_ws")
    _write_note(norm_vault["insights"] / "insight1.md", "personal_ws", "claude-insight")

    issues = check.scan(
        str(norm_vault["vault"]), "claude-sessions", "claude-insights", 9999
    )
    assert len(issues) == 2
    for issue in issues:
        assert issue.extra["original"] == "personal_ws"
        assert issue.extra["normalized"] == "personal-ws"


def test_scan_ignores_hyphenated_projects(norm_vault):
    import vault_doctor_checks.project_name_normalization as check

    _write_note(norm_vault["sessions"] / "session1.md", "personal-ws")

    issues = check.scan(
        str(norm_vault["vault"]), "claude-sessions", "claude-insights", 9999
    )
    assert len(issues) == 0


def test_scan_respects_project_filter(norm_vault):
    import vault_doctor_checks.project_name_normalization as check

    _write_note(norm_vault["sessions"] / "s1.md", "personal_ws")
    _write_note(norm_vault["sessions"] / "s2.md", "other_project")

    # Filter for personal_ws — should match despite _ vs -
    issues = check.scan(
        str(norm_vault["vault"]), "claude-sessions", "claude-insights", 9999,
        project="personal-ws",
    )
    assert len(issues) == 1
    assert issues[0].extra["original"] == "personal_ws"


def test_apply_normalizes_project_and_tag(norm_vault):
    import vault_doctor_checks.project_name_normalization as check

    note = norm_vault["sessions"] / "session1.md"
    _write_note(note, "personal_ws")

    issues = check.scan(
        str(norm_vault["vault"]), "claude-sessions", "claude-insights", 9999
    )
    assert len(issues) == 1

    backup_root = str(norm_vault["vault"] / ".backups")
    results = check.apply(issues, backup_root)
    assert len(results) == 1
    assert results[0].status == "applied"
    assert results[0].backup_path is not None

    # Verify the note was updated
    content = note.read_text(encoding="utf-8")
    assert "project: personal-ws" in content
    assert "claude/project/personal-ws" in content
    assert "personal_ws" not in content

    # Verify backup exists
    assert os.path.isfile(results[0].backup_path)


def test_apply_creates_backup(norm_vault):
    import vault_doctor_checks.project_name_normalization as check

    note = norm_vault["insights"] / "insight1.md"
    _write_note(note, "my_app", "claude-insight")

    issues = check.scan(
        str(norm_vault["vault"]), "claude-sessions", "claude-insights", 9999
    )
    results = check.apply(issues, str(norm_vault["vault"] / ".backups"))

    assert results[0].status == "applied"
    # Backup should have original content
    backup_content = open(results[0].backup_path, encoding="utf-8").read()
    assert "project: my_app" in backup_content


def test_apply_does_not_touch_body(norm_vault):
    import vault_doctor_checks.project_name_normalization as check

    note = norm_vault["sessions"] / "session1.md"
    note.write_text(
        "---\n"
        "type: claude-session\n"
        "project: my_project\n"
        "tags:\n"
        "  - claude/project/my_project\n"
        "---\n\n"
        "# Body with my_project reference\n"
        "The project my_project uses underscores.\n",
        encoding="utf-8",
    )

    issues = check.scan(
        str(norm_vault["vault"]), "claude-sessions", "claude-insights", 9999
    )
    results = check.apply(issues, str(norm_vault["vault"] / ".backups"))
    assert results[0].status == "applied"

    content = note.read_text(encoding="utf-8")
    # Frontmatter normalized
    assert "project: my-project" in content
    assert "claude/project/my-project" in content
    # Body untouched
    assert "The project my_project uses underscores." in content
