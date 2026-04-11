"""Tests for scripts/vault_doctor.py and the check registry."""

import importlib
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))


def test_registry_lists_source_sessions_check():
    """The registry auto-discovers the source_sessions check module."""
    import vault_doctor_checks
    importlib.reload(vault_doctor_checks)
    names = vault_doctor_checks.list_checks()
    assert "source-sessions" in names


def test_issue_and_result_dataclasses_importable():
    """Shared Issue and Result types are importable from the registry."""
    from vault_doctor_checks import Issue, Result
    issue = Issue(
        check="source-sessions",
        note_path="/tmp/foo.md",
        project="testproj",
        current_source="[[old]]",
        proposed_source="[[new]]",
        reason="mtime falls inside different session window",
        confidence=0.95,
    )
    assert issue.check == "source-sessions"
    assert issue.confidence == 0.95
    assert issue.extra == {}  # default empty dict

    result = Result(
        check="source-sessions",
        note_path="/tmp/foo.md",
        status="applied",
        backup_path="/tmp/backup/foo.md",
        error=None,
    )
    assert result.status == "applied"
    assert result.backup_path == "/tmp/backup/foo.md"
