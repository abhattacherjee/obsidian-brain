"""Recursive insight readers for T5b/R5, using only isolated temp state."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from vault_doctor_checks import (  # noqa: E402
    encoding_corruption,
    missing_frontmatter_fence,
    project_name_canonicalization,
    project_name_normalization,
)

CHECKS = (
    encoding_corruption,
    project_name_normalization,
    missing_frontmatter_fence,
    project_name_canonicalization,
)


@pytest.fixture(params=CHECKS, ids=lambda check: check.NAME)
def env(request, tmp_path, monkeypatch):
    # These checks need no DB, but any indirect/default DB access must still
    # have an explicit isolated destination. Also isolate the capture ledger
    # and cached hook paths (some were computed before HOME was redirected).
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    db = tmp_path / "index.db"
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", str(db))
    import obsidian_utils

    monkeypatch.setattr(obsidian_utils, "_SECURE_DIR", str(tmp_path / "secure"))
    vault = tmp_path / "vault"
    sessions = vault / "claude-sessions"
    sessions.mkdir(parents=True, mode=0o700)
    check = request.param
    if check is project_name_canonicalization:
        # Same real-git fixture style as the canonicalization check's tests.
        repo = tmp_path / "canonical-project"
        repo.mkdir(mode=0o700)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        _write(sessions / "source.md", (
            "---\ntype: claude-session\ndate: 2000-01-01\n"
            "project: canonical-project\nsession_id: source-sid\n"
            f'project_path: "{repo}"\n---\n\n# Source session\n'
        ).encode())
    return {
        "check": check, "vault": vault, "sessions": sessions,
        "backups": tmp_path / "backups", "db": db,
    }


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def _damaged_note(check, marker):
    project = "canonical-project--topic" if check is project_name_canonicalization else "my_project"
    content = (
        "---\ntype: claude-insight\ndate: 2000-01-01\n"
        f"project: {project}\nstatus: provisional\n"
        "source_session: source-sid\ntags:\n"
        f"  - claude/insight\n  - claude/project/{project}\n"
        f"---\n\n# Insight body\n{marker}: {project}\n"
    ).encode()
    if check is encoding_corruption:
        return content + b"bad bytes: \xff\xfe\n"
    if check is missing_frontmatter_fence:
        return content[4:]
    return content


def _scan(env, insights="claude-insights", **kwargs):
    return env["check"].scan(
        str(env["vault"]), "claude-sessions", insights, 1, **kwargs
    )


def _expected_repair(check, content):
    if check is encoding_corruption:
        return content.decode("utf-8", errors="replace").encode()
    if check is missing_frontmatter_fence:
        return b"---\n" + content
    frontmatter, body = content.split(b"\n---", 1)
    old, new = (
        (b"canonical-project--topic", b"canonical-project")
        if check is project_name_canonicalization else (b"my_project", b"my-project")
    )
    return frontmatter.replace(old, new) + b"\n---" + body


@pytest.mark.parametrize("insights", ["claude-insights", "knowledge/insights"])
def test_recursive_scan_repairs_and_preserves_distinct_backups(env, insights):
    """Same basenames at every depth, with rejected subtrees pruned entirely."""
    root = env["vault"] / insights
    check = env["check"]
    included = [root / "same.md", root / "_provisional/same.md",
                root / "nested/deeper/same.md", root / "_rejected-copy/same.md"]
    excluded = [root / "_rejected/same.md", root / "_rejected/deeper/same.md",
                root / "_provisional/_rejected/same.md", root / "same.txt"]
    originals = {}
    for index, path in enumerate(included + excluded):
        originals[path] = _damaged_note(check, f"unique body {index}")
        _write(path, originals[path])

    issues = _scan(env, insights)
    assert {Path(i.note_path) for i in issues} == set(included)
    assert all(path.read_bytes() == content for path, content in originals.items())

    results = check.apply(issues, str(env["backups"]))
    assert [r.status for r in results] == ["applied"] * len(included)
    assert len({r.backup_path for r in results}) == len(included)
    for result in results:
        note = Path(result.note_path)
        backup = Path(result.backup_path)
        prefix = env["backups"] if check is encoding_corruption else env["backups"] / check.NAME
        # The fence check has a public legacy flat backup layout for notes
        # directly in a configured folder. Nested notes retain the full path.
        expected_relative = note.relative_to(env["vault"])
        if check is missing_frontmatter_fence and note.parent == root:
            expected_relative = Path(note.name)
        assert backup == prefix / expected_relative
        assert backup.read_bytes() == originals[note]
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700
        assert note.read_bytes() == _expected_repair(check, originals[note])
        assert stat.S_IMODE(note.stat().st_mode) == 0o600
    assert all(path.read_bytes() == originals[path] for path in excluded)
    assert _scan(env, insights) == []
    assert not env["db"].exists()  # A filesystem-only check must not open a DB.


def test_sessions_stay_flat(env):
    top = _write(env["sessions"] / "top.md", _damaged_note(env["check"], "top"))
    _write(env["sessions"] / "nested/hidden.md", _damaged_note(env["check"], "nested"))
    _write(env["sessions"] / "_provisional/hidden.md", _damaged_note(env["check"], "nested"))
    assert {Path(i.note_path) for i in _scan(env)} == {top}


def test_project_filter_applies_to_provisional_notes(env):
    note = _write(env["vault"] / "claude-insights/_provisional/2000-my-project-note.md",
                  _damaged_note(env["check"], "filtered"))
    project = "canonical-project" if env["check"] is project_name_canonicalization else "my-project"
    if env["check"] is missing_frontmatter_fence:
        project = "my_project"
    assert [i.note_path for i in _scan(env, project=project)] == [str(note)]
    assert _scan(env, project="unrelated-project") == []


def test_unreadable_subdirectory_is_not_reported_clean(env, monkeypatch, capsys):
    blocked = env["vault"] / "claude-insights/_provisional"
    blocked.mkdir(parents=True)
    original = Path.iterdir

    def iterdir(path):
        if path == blocked:
            raise PermissionError("cannot list provisional notes")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    with pytest.raises(PermissionError, match="cannot list provisional"):
        _scan(env)

    import vault_doctor

    monkeypatch.setattr(sys, "argv", [
        "vault_doctor", "--vault", str(env["vault"]),
        "--sessions-folder", "claude-sessions", "--insights-folder", "claude-insights",
        "--check", env["check"].NAME, "--json",
    ])
    assert vault_doctor.main() == 2
    assert json.loads(capsys.readouterr().out)["crashed_checks"] == [env["check"].NAME]


def test_rejected_subdirectory_is_never_listed(env, monkeypatch):
    rejected = env["vault"] / "claude-insights/_provisional/_rejected"
    rejected.mkdir(parents=True)
    original = Path.iterdir

    def iterdir(path):
        if path == rejected:
            raise AssertionError("must prune rejected directory before listing")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    assert _scan(env) == []


def test_symlinks_do_not_escape_vault_or_reenter_rejected(env, tmp_path):
    root = env["vault"] / "claude-insights"
    outside = _write(tmp_path / "outside/note.md", _damaged_note(env["check"], "outside"))
    rejected = _write(root / "_rejected/note.md", _damaged_note(env["check"], "rejected"))
    (root / "external.md").symlink_to(outside)
    (root / "external-dir").symlink_to(outside.parent, target_is_directory=True)
    (root / "rejected-alias.md").symlink_to(rejected)
    (root / "rejected-alias").symlink_to(rejected.parent, target_is_directory=True)
    (root / "loop").symlink_to(root, target_is_directory=True)
    assert _scan(env) == []


def test_backup_escape_is_an_error_and_leaves_note_untouched(env, tmp_path):
    note = _write(env["vault"] / "claude-insights/_provisional/note.md",
                  _damaged_note(env["check"], "backup failure"))
    before = note.read_bytes()
    issues = _scan(env)
    outside = tmp_path / "escaped-backups"
    outside.mkdir()
    prefix = env["backups"]
    if env["check"] is not encoding_corruption:
        prefix /= env["check"].NAME
    prefix.mkdir(parents=True)
    (prefix / "claude-insights").symlink_to(outside, target_is_directory=True)
    results = env["check"].apply(issues, str(env["backups"]))
    assert [r.status for r in results] == ["error"]
    assert "backup" in results[0].error.lower()
    assert note.read_bytes() == before
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("folder", ["../outside", "absolute"])
def test_configured_folder_cannot_escape_vault(env, tmp_path, folder):
    outside = tmp_path / "outside"
    outside.mkdir()
    if folder == "absolute":
        folder = str(outside)
    with pytest.raises(ValueError, match="escapes vault"):
        _scan(env, folder)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("destination", ["outside", "_rejected"])
def test_stale_issue_cannot_repair_a_symlink_to_an_excluded_note(env, tmp_path, destination):
    note = _write(env["vault"] / "claude-insights/_provisional/note.md",
                  _damaged_note(env["check"], "moved"))
    before = note.read_bytes()
    issues = _scan(env)
    target = (tmp_path / "outside/note.md" if destination == "outside"
              else env["vault"] / "claude-insights/_rejected/note.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    note.rename(target)
    note.symlink_to(target)
    results = env["check"].apply(issues, str(env["backups"]))
    assert [r.status for r in results] == ["error"]
    assert note.is_symlink()
    assert target.read_bytes() == before
    assert not env["backups"].exists()


def test_dispatcher_exit_codes_for_provisional_findings(env):
    note = _write(env["vault"] / "claude-insights/_provisional/note.md",
                  _damaged_note(env["check"], "cli"))
    before = note.read_bytes()
    command = [sys.executable, str(_SCRIPTS_DIR / "vault_doctor.py"),
               "--vault", str(env["vault"]), "--sessions-folder", "claude-sessions",
               "--insights-folder", "claude-insights", "--check", env["check"].NAME,
               "--json"]
    dry_run = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert dry_run.returncode == 1, dry_run.stderr
    assert [i["note_path"] for i in json.loads(dry_run.stdout)["issues"]] == [str(note)]
    assert note.read_bytes() == before
    applied = subprocess.run(command + ["--apply", "--yes"], capture_output=True,
                             text=True, timeout=30)
    assert applied.returncode == 1, applied.stderr
    assert note.read_bytes() == _expected_repair(env["check"], before)
    clean = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert clean.returncode == 0, clean.stderr
