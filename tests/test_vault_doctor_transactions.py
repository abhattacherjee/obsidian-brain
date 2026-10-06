"""Doctor repairs preserve scan intent and concurrent/manual note changes."""
from pathlib import Path

from scripts.vault_doctor_checks import Issue
from scripts.vault_doctor_checks import (
    encoding_corruption, project_name_normalization, snapshot_migration,
)


def _project_note(vault):
    path = vault / "claude-insights" / "note.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntype: claude-insight\nproject: my_project\n---\nbody\n")
    return path


def test_doctor_refuses_edits_between_scan_and_apply(tmp_path):
    path = _project_note(tmp_path)
    issues = project_name_normalization.scan(str(tmp_path), "claude-sessions", "claude-insights", 9999)
    path.write_text(path.read_text() + "my later edit\n")
    before = path.read_bytes()
    result = project_name_normalization.apply(issues, str(tmp_path / "backups"))
    assert result[0].status == "error"
    assert "conflict" in result[0].error
    assert path.read_bytes() == before


def test_doctor_refuses_edit_during_backup(tmp_path, monkeypatch):
    import shutil
    path = _project_note(tmp_path)
    issues = project_name_normalization.scan(str(tmp_path), "claude-sessions", "claude-insights", 9999)
    original = shutil.copy2

    def edit(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        Path(source).write_text("my replacement\n")
        return result

    monkeypatch.setattr(shutil, "copy2", edit)
    result = project_name_normalization.apply(issues, str(tmp_path / "backups"))
    assert result[0].status == "error"
    assert "conflict" in result[0].error
    assert path.read_text() == "my replacement\n"


def test_encoding_repair_preserves_source_mode_and_exact_backup(tmp_path):
    path = _project_note(tmp_path)
    original = path.read_bytes() + b"\xff"
    path.write_bytes(original)
    path.chmod(0o640)
    issues = encoding_corruption.scan(str(tmp_path), "claude-sessions", "claude-insights", 9999)
    result = encoding_corruption.apply(issues, str(tmp_path / "backups"))
    assert result[0].status == "applied"
    assert Path(result[0].backup_path).read_bytes() == original
    assert path.read_text() == original.decode("utf-8", errors="replace")
    assert path.stat().st_mode & 0o777 == 0o640


def test_ordinary_doctor_repair_refuses_invalid_utf8(tmp_path):
    path = _project_note(tmp_path)
    original = path.read_bytes() + b"\xff"
    path.write_bytes(original)
    issue = Issue("project-name-normalization", str(path), "my_project", "", "", "",
                  extra={"original": "my_project", "normalized": "my-project", "vault_path": str(tmp_path)})
    result = project_name_normalization.apply([issue], str(tmp_path / "backups"))
    assert result[0].status == "error"
    assert path.read_bytes() == original


def test_doctor_legacy_issue_honors_configured_vault(tmp_path, tmp_path_factory, monkeypatch):
    import obsidian_utils
    selected = tmp_path / "selected"
    selected.mkdir()
    path = _project_note(tmp_path_factory.mktemp('outside-selected-vault'))
    before = path.read_bytes()
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: {"vault_path": str(selected)})
    issue = Issue("project-name-normalization", str(path), "my_project", "", "", "",
                  extra={"original": "my_project", "normalized": "my-project"})
    result = project_name_normalization.apply([issue], str(tmp_path / "backups"))
    assert result[0].status == "error"
    assert "outside" in result[0].error
    assert path.read_bytes() == before


def test_snapshot_rollback_preserves_edit_to_moved_note(tmp_path, monkeypatch):
    folder = tmp_path / "claude-sessions"
    folder.mkdir()
    source = folder / "old-snapshot.md"
    source.write_text("---\ntype: claude-snapshot\n---\noriginal\n")
    destination = folder / "new-snapshot.md"
    issue = Issue("snapshot-legacy-filename", str(source), "", "", "", "",
                  extra={"new_name": destination.name, "vault_path": str(tmp_path)})

    def edit_then_fail(*args, **kwargs):
        destination.write_text("my later moved-note edit\n")
        raise RuntimeError("link failure")

    monkeypatch.setattr(snapshot_migration, "_rewrite_wikilinks_in_vault", edit_then_fail)
    result = snapshot_migration.apply([issue], str(tmp_path / "backups"))
    assert result[0].status == "error"
    assert "rollback refused" in result[0].error
    assert result[0].note_path == str(destination)
    assert destination.read_text() == "my later moved-note edit\n"
    assert not source.exists()


def _dispatcher_note(vault):
    path = vault / "claude-sessions" / "note.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntype: claude-session\nproject: my_project\n---\n**User:** [[ -e foo ]]\n")
    return path


def _run_two_checks(vault, monkeypatch):
    import sys
    from scripts import vault_doctor
    from scripts.vault_doctor_checks import spurious_wikilinks
    monkeypatch.setattr(vault_doctor.vault_doctor_checks, "all_checks",
                        lambda: [project_name_normalization, spurious_wikilinks])
    monkeypatch.setattr(vault_doctor, "_iso_now", lambda: "test-doctor-run")
    monkeypatch.setattr(sys, "argv", ["vault_doctor", "--vault", str(vault),
                        "--sessions-folder", "claude-sessions", "--insights-folder", "claude-insights",
                        "--apply", "--yes"])
    # Dispatcher backups use a private test home; never the live user directory.
    monkeypatch.setenv("HOME", str(vault / "test-home"))
    return vault_doctor.main()


def test_dispatcher_different_checks_can_repair_same_scanned_note(tmp_path, monkeypatch):
    path = _dispatcher_note(tmp_path)
    assert _run_two_checks(tmp_path, monkeypatch) == 1
    text = path.read_text()
    assert "project: my-project" in text
    assert "**User:** \\[\\[ -e foo ]]" in text


def test_dispatcher_manual_edit_between_checks_is_preserved(tmp_path, monkeypatch):
    path = _dispatcher_note(tmp_path)
    original = project_name_normalization.apply

    def repair_then_edit(issues, backup_root):
        result = original(issues, backup_root)
        path.write_text(path.read_text() + "my manual edit after first repair\n")
        return result

    monkeypatch.setattr(project_name_normalization, "apply", repair_then_edit)
    assert _run_two_checks(tmp_path, monkeypatch) == 2
    text = path.read_text()
    assert "project: my-project" in text
    assert "**User:** [[ -e foo ]]" in text
    assert "my manual edit after first repair" in text


def test_standalone_doctor_cli_uses_shared_writer(tmp_path, tmp_path_factory):
    import subprocess
    import sys
    import os
    path = _project_note(tmp_path)
    path.write_bytes(path.read_bytes() + b"\xff")
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path_factory.mktemp("doctor-child-account"))
    environment["OBSIDIAN_BRAIN_DB"] = str(tmp_path / "state" / "index.sqlite3")
    environment["XDG_STATE_HOME"] = str(tmp_path_factory.mktemp("doctor-child-coordination"))
    script = Path(__file__).resolve().parents[1] / "scripts" / "vault_doctor.py"
    result = subprocess.run(
        [sys.executable, str(script), "--vault", str(tmp_path),
         "--sessions-folder", "claude-sessions", "--insights-folder", "claude-insights",
         "--check", "encoding-corruption", "--apply", "--yes"],
        env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 1, result.stderr
    assert "applied" in result.stderr
    assert path.read_text().endswith("\ufffd")
    import hashlib
    details = tmp_path.stat()
    vault_key = hashlib.sha256((str(details.st_dev) + ":" + str(details.st_ino)).encode()).hexdigest()
    journal = Path(environment["XDG_STATE_HOME"]) / "obsidian-brain" / "vaults" / vault_key / "state.sqlite3"
    assert journal.is_file()
    assert not list(Path(environment["HOME"]).rglob("state.sqlite3"))


# Every scoped operation uses the same selected temporary vault.
from selected_legacy_vault import selected_host_context, native_ai_frontend  # noqa: F401,E402
import pytest

pytestmark = pytest.mark.usefixtures("selected_host_context")


def test_restored_damage_is_repaired_in_a_new_invocation(tmp_path):
    path = _project_note(tmp_path)
    damaged = path.read_bytes()
    for _ in range(2):
        issues = project_name_normalization.scan(str(tmp_path), 'claude-sessions', 'claude-insights', 9999)
        results = project_name_normalization.apply(issues, str(tmp_path / 'backups'))
        assert results[0].status == 'applied'
        assert 'project: my-project' in path.read_text()
        path.write_bytes(damaged)


def test_repair_scope_does_not_hold_vault_lock_during_user_work(tmp_path):
    import threading
    from scripts.vault_doctor_checks import repair_scope
    from note_transactions import context_for_vault, ownership_lock
    path = _project_note(tmp_path)
    issues = project_name_normalization.scan(str(tmp_path), 'claude-sessions', 'claude-insights', 9999)
    outcome = []
    context = context_for_vault(tmp_path)
    def other_writer():
        try:
            with ownership_lock(context):
                outcome.append('owned')
        except Exception as exc:
            outcome.append(type(exc).__name__)
    with repair_scope(issues):
        thread = threading.Thread(target=other_writer)
        thread.start()
        thread.join(timeout=1)
        assert outcome == ['owned']
