"""Recursive doctor readers (#465 T5b), isolated from live state."""

import json
import os
import sqlite3
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import vault_doctor
from vault_doctor_checks import audit_historic_repairs as audit
from vault_doctor_checks import crossed_source_session as crossed
from vault_doctor_checks import session_coverage as coverage
from vault_doctor_checks import source_sessions as sources


@pytest.fixture(params=["claude-insights", "knowledge/ideas"])
def env(tmp_path, monkeypatch, request):
    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / "test-vault.db"
    with sqlite3.connect(db):
        pass
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", str(db))
    vault = tmp_path / "vault"
    sessions = vault / "claude-sessions"
    insights = vault / request.param
    sessions.mkdir(parents=True)
    insights.mkdir(parents=True)
    projects = tmp_path / ".claude" / "projects" / "-Users-test-proj1"
    projects.mkdir(parents=True)
    config = {
        "vault_path": str(vault), "sessions_folder": "claude-sessions",
        "insights_folder": request.param, "min_messages": 3,
        "min_duration_minutes": 2, "auto_log_enabled": True,
    }
    (tmp_path / ".claude" / "obsidian-brain-config.json").write_text(json.dumps(config))
    backups = tmp_path / "historic-backups"
    backups.mkdir()
    monkeypatch.setenv("OBSIDIAN_BRAIN_DOCTOR_BACKUP_ROOT", str(backups))
    return dict(vault=vault, sessions=sessions, insights=insights,
                folder=request.param, projects=projects, backups=backups,
                tmp=tmp_path, db=db)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _insight(sid="sid-good", link="2026-04-11-proj1-wrong"):
    return (
        "---\ntype: claude-insight\ndate: 2026-04-10\nproject: proj1\n"
        f'source_session: {sid}\nsource_session_note: "[[{link}]]"\n'
        "status: provisional\n---\n# Insight\nBody stays intact.\n"
    )


def _session(env, sid="sid-good", stem="2026-04-10-proj1-good", subdir=""):
    return _write(env["sessions"] / subdir / f"{stem}.md",
                  "---\ntype: claude-session\ndate: 2026-04-10\n"
                  f"project: proj1\nsession_id: {sid}\n---\n# Session\n")


def _jsonl(env, sid="sid-good"):
    records = [
        {"type": "user", "timestamp": f"2026-04-10T10:{minute:02d}:00Z",
         "cwd": "/Users/test/proj1",
         "message": {"role": "user", "content": f"message {minute}"}}
        for minute in (0, 5, 10, 15)
    ]
    path = _write(env["projects"] / f"{sid}.jsonl",
                  "\n".join(json.dumps(r) for r in records) + "\n")
    # Finished historic sessions must not trigger the live-session exemption.
    finished = datetime(2026, 4, 10, 10, 15, tzinfo=timezone.utc).timestamp()
    os.utime(path, (finished, finished))
    return path


def _notes(env, text):
    # The same basename at different depths exercises backup collisions.
    locations = ("", "_provisional", "_provisional/deeper", "archive/deeper",
                 "_rejected/deeper", "_provisional/_rejected/deeper")
    notes = [_write(env["insights"] / folder / "2026-04-10-proj1-note.md", text)
             for folder in locations]
    return notes[:4], notes[4:]


def _scan(check, env, **kwargs):
    return check.scan(str(env["vault"]), "claude-sessions", env["folder"],
                      days=10000, **kwargs)


def _historic(env, relative, text, days=1, project="proj1"):
    run = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H-%M-%S+00-00")
    return _write(env["backups"] / run / project / relative, text)


def test_crossed_scans_provisional_and_skips_rejected(env):
    target = _session(env, sid="sid-other")
    visible, rejected = _notes(env, _insight(link=target.stem))
    before = {p: p.read_bytes() for p in visible + rejected}
    issues = _scan(crossed, env, project="proj1")
    assert {i.note_path for i in issues} == {str(p) for p in visible}
    backup = env["tmp"] / "apply-backups"
    assert {r.status for r in crossed.apply(issues, str(backup))} == {"skipped"}
    assert not backup.exists()
    assert all(p.read_bytes() == data for p, data in before.items())


def test_source_sessions_repairs_with_vault_relative_backups(env):
    target = _session(env)
    _jsonl(env)
    visible, rejected = _notes(env, _insight())
    before = {p: p.read_bytes() for p in visible + rejected}
    mtimes = {p: p.stat().st_mtime_ns for p in visible}
    issues = _scan(sources, env, project="proj1")
    assert {i.note_path for i in issues} == {str(p) for p in visible}
    assert {i.extra["signal_class"] for i in issues} == {"uuid-basename-stale"}
    root = env["tmp"] / "apply-backups"
    results = sources.apply(issues, str(root))
    assert {r.status for r in results} == {"applied"}
    for result in results:
        note = Path(result.note_path)
        backup = root / "proj1" / note.relative_to(env["vault"])
        assert Path(result.backup_path) == backup
        assert backup.read_bytes() == before[note]
        assert target.stem in note.read_text()
        assert note.read_text().endswith("# Insight\nBody stays intact.\n")
        assert abs(note.stat().st_mtime_ns - mtimes[note]) < 1000
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        assert stat.S_IMODE(note.stat().st_mode) == 0o600
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o700
                   for p in backup.parents if p == root or root in p.parents)
    assert all(p.read_bytes() == before[p] for p in rejected)
    assert _scan(sources, env) == []


def test_session_coverage_counts_provisional_references(env):
    _jsonl(env)
    visible, rejected = _notes(env, _insight())
    before = {p: p.read_bytes() for p in visible + rejected}
    issues = _scan(coverage, env, project="proj1", strict=True)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.extra["referenced_by"] == [p.name for p in visible]
    assert issue.extra["strict_fail"] is True
    assert "referenced by 4 note(s)" in issue.reason
    # This check reconstructs missing SESSION notes; it does not edit insights.
    assert coverage.apply(issues, str(env["tmp"] / "unused"))[0].status == "unresolved"
    assert all(p.read_bytes() == data for p, data in before.items())
    for note in visible:
        note.unlink()
    issue = _scan(coverage, env, strict=True)[0]
    assert issue.extra["referenced_by"] == []
    assert issue.extra["strict_fail"] is False


def test_session_coverage_reconstructs_session_referenced_by_provisional(env, monkeypatch):
    monkeypatch.setenv("_REAL_VAULT_GUARD", "1")
    _jsonl(env)
    visible, rejected = _notes(env, _insight())
    before = {p: p.read_bytes() for p in visible + rejected}
    issues = _scan(coverage, env, strict=True, reconstruct=True)
    assert len(issues) == 1
    assert len(issues[0].extra["referenced_by"]) == len(visible)
    root = env["tmp"] / "unused-backups"
    result = coverage.apply(issues, str(root))[0]
    assert result.status == "applied", result.error
    written = Path(result.note_path)
    assert written.parent == env["sessions"]
    assert written.is_file()
    assert not root.exists()  # reconstruction creates a new session note
    assert all(p.read_bytes() == data for p, data in before.items())
    assert _scan(coverage, env, strict=True) == []


def test_audit_preserves_provisional_folder_hints_and_relative_backups(env):
    visible, rejected = _notes(env, _insight("sid-corrupt"))
    before = {p: p.read_bytes() for p in visible + rejected}
    for n, note in enumerate(visible + rejected):
        _historic(env, note.relative_to(env["vault"]),
                  _insight(f"sid-original-{n}", f"2026-04-10-proj1-orig-{n}"))
    issues = _scan(audit, env)
    assert {i.note_path for i in issues} == {str(p) for p in visible}
    for issue in issues:
        n = visible.index(Path(issue.note_path))
        assert issue.extra["orig_sid"] == f"sid-original-{n}"
        assert issue.extra["category"] == "A"
    # The dispatcher may supply a run beneath ancestors that do not exist yet.
    root = env["tmp"] / "new-working-dir" / "restore-backups"
    results = audit.apply(issues, str(root))
    assert {r.status for r in results} == {"applied"}
    for result in results:
        note = Path(result.note_path)
        backup = root / audit.NAME / note.relative_to(env["vault"])
        assert Path(result.backup_path) == backup
        assert backup.read_bytes() == before[note]
        assert f"source_session: sid-original-{visible.index(note)}\n" in note.read_text()
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        assert stat.S_IMODE(note.stat().st_mode) == 0o600
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o700
                   for p in backup.parents if p == root or root in p.parents)
    assert all(p.read_bytes() == before[p] for p in rejected)
    assert _scan(audit, env) == []


def test_audit_legacy_backup_finds_nested_note_but_not_rejected(env):
    visible = _write(env["insights"] / "_provisional/deep/2026-04-10-only.md",
                     _insight("sid-corrupt"))
    _historic(env, visible.name, _insight("sid-original", "2026-04-10-proj1-original"))
    rejected = _write(env["insights"] / "_rejected/2026-04-10-rejected.md", _insight())
    _historic(env, rejected.name, _insight("sid-original", "2026-04-10-proj1-original"))
    assert [i.note_path for i in _scan(audit, env)] == [str(visible)]


def test_audit_parent_only_provisional_hint_avoids_top_level_collision(env):
    visible, _ = _notes(env, _insight("sid-corrupt"))
    _historic(env, Path("_provisional") / visible[1].name,
              _insight("sid-original", "2026-04-10-proj1-original"))
    assert [i.note_path for i in _scan(audit, env)] == [str(visible[1])]
    visible[1].unlink()
    assert _scan(audit, env) == []  # a missing hinted note must not fall back


def test_audit_project_named_like_note_folder_keeps_source_hint(env):
    visible, _ = _notes(env, _insight("sid-corrupt"))
    _historic(env, visible[1].relative_to(env["vault"]),
              _insight("sid-original", "2026-04-10-proj1-original"),
              project="claude-decisions")
    assert [i.note_path for i in _scan(audit, env)] == [str(visible[1])]


@pytest.mark.parametrize("check", [crossed, sources, coverage, audit], ids=lambda c: c.NAME)
def test_nested_listing_error_is_visible(env, monkeypatch, capsys, check):
    _session(env, sid="sid-other")
    _jsonl(env)
    visible, _ = _notes(env, _insight(link="2026-04-10-proj1-good"))
    _historic(env, visible[1].relative_to(env["vault"]),
              _insight("sid-original", "2026-04-10-proj1-original"))
    blocked = env["insights"] / "_provisional"
    original = Path.iterdir

    def iterdir(path):
        if path == blocked:
            raise PermissionError("nested listing denied")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    if check is crossed:
        _scan(check, env)
        assert "nested listing denied" in capsys.readouterr().err
    else:
        with pytest.raises(PermissionError, match="nested listing denied"):
            _scan(check, env)


@pytest.mark.parametrize("check", [crossed, sources, coverage, audit], ids=lambda c: c.NAME)
def test_sessions_remain_flat(env, check):
    target = _session(env, sid="sid-other" if check is crossed else "sid-good",
                      subdir="nested")
    _jsonl(env)
    _write(env["insights"] / "_provisional/2026-04-10-proj1-note.md", _insight(link=target.stem))
    _historic(env, target.relative_to(env["vault"]),
              _insight("sid-original", "2026-04-10-proj1-original"))
    issues = _scan(check, env)
    if check in (crossed, audit):
        assert issues == []
    elif check is sources:
        assert len(issues) == 1
        assert issues[0].extra["signal_class"] == "missing-session-note"
        assert issues[0].extra["unresolved"] is True
    else:
        assert len(issues) == 1
        assert issues[0].extra["signal_class"] == "session-coverage-gap"


def test_provisional_reference_keeps_doctor_exit_code(env, capsys, monkeypatch):
    _jsonl(env)
    note = _write(env["insights"] / "_provisional/2026-04-10-proj1-note.md", _insight())
    args = ["--check", "session-coverage", "--strict", "--json",
            "--vault", str(env["vault"]), "--sessions-folder", "claude-sessions",
            "--insights-folder", env["folder"], "--days", "10000"]
    monkeypatch.setattr(sys, "argv", ["vault_doctor.py", *args])
    assert vault_doctor.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["issues"][0]["strict_fail"] is True
    note.unlink()
    _session(env)
    assert vault_doctor.main() == 0


@pytest.mark.parametrize("check", [sources, audit], ids=lambda c: c.NAME)
def test_backup_failure_leaves_provisional_note_unchanged(env, monkeypatch, check):
    _session(env)
    _jsonl(env)
    note = _write(env["insights"] / "_provisional/2026-04-10-proj1-note.md", _insight())
    original = note.read_bytes()
    _historic(env, note.relative_to(env["vault"]),
              _insight("sid-original", "2026-04-10-proj1-original"))
    issues = _scan(check, env)
    assert len(issues) == 1

    # The backup is written and fsynced before the note is touched; failing
    # its fsync leaves a partial temp file the helper must clean up.
    def failed_fsync(fd):
        raise OSError("backup disk full")

    monkeypatch.setattr(os, "fsync", failed_fsync)
    root = env["tmp"] / "apply-backups"
    result = check.apply(issues, str(root))[0]
    assert result.status == "error"
    assert "backup disk full" in result.error
    assert note.read_bytes() == original
    assert not list(root.rglob("*.md"))
    assert not list(root.rglob("*.tmp"))


@pytest.mark.parametrize("check", [sources, audit], ids=lambda c: c.NAME)
def test_backup_symlink_escape_is_refused_before_writing(env, check):
    _session(env)
    _jsonl(env)
    note = _write(env["insights"] / "_provisional/2026-04-10-proj1-note.md", _insight())
    _historic(env, note.relative_to(env["vault"]),
              _insight("sid-original", "2026-04-10-proj1-original"))
    original = note.read_bytes()
    outside = env["tmp"] / "outside-backups"
    outside.mkdir()
    root = env["tmp"] / "apply-backups"
    prefix = "proj1" if check is sources else audit.NAME
    (root / prefix).mkdir(parents=True)
    (root / prefix / Path(env["folder"]).parts[0]).symlink_to(outside, target_is_directory=True)
    result = check.apply(_scan(check, env), str(root))[0]
    assert result.status == "error"
    assert "escape backup_root" in result.error
    assert note.read_bytes() == original
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("check", [crossed, sources, coverage, audit], ids=lambda c: c.NAME)
def test_recursive_reader_prunes_rejected_and_symlink_directories(env, monkeypatch, check):
    _session(env, sid="sid-other" if check is crossed else "sid-good")
    _jsonl(env)
    visible, _ = _notes(env, _insight(link="2026-04-10-proj1-good"))
    _historic(env, visible[1].relative_to(env["vault"]),
              _insight("sid-original", "2026-04-10-proj1-original"))
    # Following this cycle would recurse forever; following the other symlinks
    # would expose a rejected note or data outside the vault.
    (env["insights"] / "_provisional/loop").symlink_to(env["insights"], target_is_directory=True)
    outside = env["tmp"] / "outside-vault"
    _write(outside / "2026-04-10-proj1-outside.md", _insight())
    (env["insights"] / "outside").symlink_to(outside, target_is_directory=True)
    (env["insights"] / "alias").symlink_to(env["insights"] / "_rejected", target_is_directory=True)
    original = Path.iterdir

    def guarded_iterdir(path):
        assert "_rejected" not in path.parts
        assert path != outside
        return original(path)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    issues = _scan(check, env)
    if check is crossed:
        assert len(issues) == 4
    elif check is sources:
        assert issues == []  # correct source pair; the reader still visits it
    elif check is coverage:
        assert issues == []  # covered by the flat session note
    else:
        assert [i.note_path for i in issues] == [str(visible[1])]


def test_source_sessions_provisional_mtime_cutoff_is_inclusive(env, monkeypatch):
    _session(env)
    _jsonl(env)
    now = datetime(2026, 4, 12, tzinfo=timezone.utc).timestamp()
    cutoff = now - 86400
    included = _write(env["insights"] / "_provisional/2026-04-10-included.md", _insight())
    excluded = _write(env["insights"] / "_provisional/2026-04-10-excluded.md", _insight())
    os.utime(included, (cutoff, cutoff))
    os.utime(excluded, (cutoff - 1, cutoff - 1))
    monkeypatch.setattr(sources.time, "time", lambda: now)
    issues = sources.scan(str(env["vault"]), "claude-sessions", env["folder"], days=1)
    assert [i.note_path for i in issues] == [str(included)]


def test_audit_provisional_backup_cutoff_is_inclusive(env, monkeypatch):
    now = datetime(2026, 4, 12, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(audit, "datetime", FrozenDateTime)
    notes = []
    for age in (86400, 86401):
        note = _write(env["insights"] / f"_provisional/2026-04-10-age-{age}.md", _insight())
        notes.append(note)
        run = (now - timedelta(seconds=age)).strftime("%Y-%m-%dT%H-%M-%S+00-00")
        _write(env["backups"] / run / "proj1" / note.relative_to(env["vault"]),
               _insight("sid-original", "2026-04-10-proj1-original"))
    issues = audit.scan(str(env["vault"]), "claude-sessions", env["folder"], days=1)
    assert [i.note_path for i in issues] == [str(notes[0])]


@pytest.mark.parametrize("check", [sources, coverage, audit], ids=lambda c: c.NAME)
def test_nested_scan_failure_exits_incomplete(env, monkeypatch, capsys, check):
    (env["insights"] / "_provisional").mkdir()
    original = Path.iterdir

    def iterdir(path):
        if path == env["insights"] / "_provisional":
            raise PermissionError("nested listing denied")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(sys, "argv", ["vault_doctor.py", "--check", check.NAME,
                        "--vault", str(env["vault"]), "--insights-folder", env["folder"], "--json"])
    assert vault_doctor.main() == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["crashed_checks"]
