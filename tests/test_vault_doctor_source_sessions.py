"""Tests for the source_sessions vault_doctor check module."""

import json
import os
import sys
import time
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))


@pytest.fixture
def doctor_vault(tmp_path):
    """Tmp vault layout with folders + JSONL home for session matching."""
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    (vault / "claude-insights").mkdir(parents=True)
    (vault / "claude-decisions").mkdir(parents=True)
    (vault / "claude-error-fixes").mkdir(parents=True)
    (vault / "claude-retros").mkdir(parents=True)
    claude_home = tmp_path / ".claude" / "projects" / "-Users-foo-proj1"
    claude_home.mkdir(parents=True)
    return {
        "vault": vault,
        "home": tmp_path,
        "jsonl_dir": claude_home,
        "project": "proj1",
    }


def _write_jsonl(path: Path, first_ts: str, last_ts_mtime: float) -> None:
    """Write a minimal JSONL with two entries and set mtime to last_ts_mtime."""
    payload = [
        {"type": "user", "timestamp": first_ts},
        {"type": "assistant", "timestamp": first_ts},
    ]
    path.write_text("\n".join(json.dumps(p) for p in payload) + "\n", encoding="utf-8")
    os.utime(path, (last_ts_mtime, last_ts_mtime))


def _write_session_note(dir_path: Path, date: str, project: str, sid: str, hash_: str) -> Path:
    note = dir_path / f"{date}-{project}-{hash_}.md"
    note.write_text(
        f"---\n"
        f"type: claude-session\n"
        f"date: {date}\n"
        f"session_id: {sid}\n"
        f"project: {project}\n"
        f"status: summarized\n"
        f"---\n"
        f"# Session\n## Summary\nstub\n",
        encoding="utf-8",
    )
    return note


def _write_insight(dir_path: Path, date: str, slug: str, project: str, src_sid: str,
                   src_note_basename: str, mtime: float) -> Path:
    note = dir_path / f"{date}-{slug}.md"
    note.write_text(
        f"---\n"
        f"type: claude-insight\n"
        f"date: {date}\n"
        f"source_session: {src_sid}\n"
        f'source_session_note: "[[{src_note_basename}]]"\n'
        f"project: {project}\n"
        f"tags:\n"
        f"  - claude/insight\n"
        f"  - claude/project/{project}\n"
        f"---\n"
        f"# Test insight\n",
        encoding="utf-8",
    )
    os.utime(note, (mtime, mtime))
    return note


def test_scan_flags_insight_stamped_to_wrong_session(doctor_vault, monkeypatch):
    """Insight mtime falls in session-B window but references session-A → stale."""
    import vault_doctor_checks.source_sessions as check

    v = doctor_vault["vault"]
    home = doctor_vault["home"]
    jsonl_dir = doctor_vault["jsonl_dir"]
    monkeypatch.setenv("HOME", str(home))

    # Session A: 2026-04-09 10:00–11:00
    a_start = time.mktime(time.strptime("2026-04-09 10:00", "%Y-%m-%d %H:%M"))
    a_end = a_start + 3600
    _write_jsonl(jsonl_dir / "sid-a.jsonl", "2026-04-09T10:00:00Z", a_end)
    _write_session_note(v / "claude-sessions", "2026-04-09", "proj1", "sid-a", "aaaa")

    # Session B: 2026-04-10 14:00–15:00
    b_start = time.mktime(time.strptime("2026-04-10 14:00", "%Y-%m-%d %H:%M"))
    b_end = b_start + 3600
    _write_jsonl(jsonl_dir / "sid-b.jsonl", "2026-04-10T14:00:00Z", b_end)
    _write_session_note(v / "claude-sessions", "2026-04-10", "proj1", "sid-b", "bbbb")

    # Insight captured 2026-04-10 14:30 but wrongly stamped with sid-a
    insight_mtime = b_start + 1800
    _write_insight(
        v / "claude-insights",
        "2026-04-10",
        "stale-insight-0001",
        "proj1",
        "sid-a",
        "2026-04-09-proj1-aaaa",
        insight_mtime,
    )

    issues = check.scan(
        str(v), "claude-sessions", "claude-insights", days=7, project="proj1"
    )
    assert len(issues) == 1
    iss = issues[0]
    assert "stale-insight-0001" in iss.note_path
    assert iss.current_source == "[[2026-04-09-proj1-aaaa]]"
    assert iss.proposed_source == "[[2026-04-10-proj1-bbbb]]"
    assert iss.project == "proj1"
    assert iss.extra.get("proposed_sid") == "sid-b"


def test_scan_ignores_correct_insight(doctor_vault, monkeypatch):
    """Insight mtime matches its referenced session window → not flagged."""
    import vault_doctor_checks.source_sessions as check

    v = doctor_vault["vault"]
    home = doctor_vault["home"]
    jsonl_dir = doctor_vault["jsonl_dir"]
    monkeypatch.setenv("HOME", str(home))

    a_start = time.mktime(time.strptime("2026-04-10 14:00", "%Y-%m-%d %H:%M"))
    a_end = a_start + 3600
    _write_jsonl(jsonl_dir / "sid-a.jsonl", "2026-04-10T14:00:00Z", a_end)
    _write_session_note(v / "claude-sessions", "2026-04-10", "proj1", "sid-a", "aaaa")

    _write_insight(
        v / "claude-insights",
        "2026-04-10",
        "good-insight",
        "proj1",
        "sid-a",
        "2026-04-10-proj1-aaaa",
        a_start + 1800,
    )

    issues = check.scan(str(v), "claude-sessions", "claude-insights", days=7, project="proj1")
    assert issues == []


def test_scan_honors_days_window(doctor_vault, monkeypatch):
    """Notes older than `days` are not scanned."""
    import vault_doctor_checks.source_sessions as check

    v = doctor_vault["vault"]
    home = doctor_vault["home"]
    jsonl_dir = doctor_vault["jsonl_dir"]
    monkeypatch.setenv("HOME", str(home))

    old_ts = time.time() - 30 * 86400
    _write_jsonl(jsonl_dir / "sid-old.jsonl", "2026-03-12T10:00:00Z", old_ts + 3600)
    _write_session_note(v / "claude-sessions", "2026-03-12", "proj1", "sid-old", "0aaa")
    _write_insight(
        v / "claude-insights",
        "2026-03-12",
        "ancient-insight",
        "proj1",
        "wrong-sid",
        "2026-03-12-proj1-0aaa",
        old_ts + 1800,
    )

    issues = check.scan(str(v), "claude-sessions", "claude-insights", days=7, project="proj1")
    assert issues == []  # outside 7-day window


def test_scan_marks_unresolved_when_no_window_matches(doctor_vault, monkeypatch):
    """Insight whose mtime does not fall inside any session window → UNRESOLVED (or not flagged)."""
    import vault_doctor_checks.source_sessions as check

    v = doctor_vault["vault"]
    home = doctor_vault["home"]
    jsonl_dir = doctor_vault["jsonl_dir"]
    monkeypatch.setenv("HOME", str(home))

    a_start = time.mktime(time.strptime("2026-04-10 10:00", "%Y-%m-%d %H:%M"))
    _write_jsonl(jsonl_dir / "sid-a.jsonl", "2026-04-10T10:00:00Z", a_start + 3600)
    _write_session_note(v / "claude-sessions", "2026-04-10", "proj1", "sid-a", "aaaa")

    # Insight captured at 20:00 — no session window covers it, and current source 'wrong-sid' isn't a real session
    gap_mtime = time.mktime(time.strptime("2026-04-10 20:00", "%Y-%m-%d %H:%M"))
    _write_insight(
        v / "claude-insights",
        "2026-04-10",
        "gap-insight",
        "proj1",
        "wrong-sid",  # doesn't match any session
        "2026-04-10-proj1-wrong",
        gap_mtime,
    )

    issues = check.scan(str(v), "claude-sessions", "claude-insights", days=7, project="proj1")
    assert len(issues) == 1, f"expected exactly 1 unresolved issue, got {len(issues)}"
    iss = issues[0]
    assert iss.extra.get("unresolved") is True
    assert iss.confidence == 0.0
    assert iss.proposed_source == ""
    assert "gap-insight" in iss.note_path


def test_apply_rewrites_only_source_session_fields(doctor_vault, tmp_path, monkeypatch):
    """After apply, only source_session/source_session_note change; body+tags preserved."""
    import vault_doctor_checks.source_sessions as check

    v = doctor_vault["vault"]
    home = doctor_vault["home"]
    jsonl_dir = doctor_vault["jsonl_dir"]
    monkeypatch.setenv("HOME", str(home))

    a_start = time.mktime(time.strptime("2026-04-09 10:00", "%Y-%m-%d %H:%M"))
    b_start = time.mktime(time.strptime("2026-04-10 14:00", "%Y-%m-%d %H:%M"))
    _write_jsonl(jsonl_dir / "sid-a.jsonl", "2026-04-09T10:00:00Z", a_start + 3600)
    _write_jsonl(jsonl_dir / "sid-b.jsonl", "2026-04-10T14:00:00Z", b_start + 3600)
    _write_session_note(v / "claude-sessions", "2026-04-09", "proj1", "sid-a", "aaaa")
    _write_session_note(v / "claude-sessions", "2026-04-10", "proj1", "sid-b", "bbbb")

    # Build an insight with extra tags and body content we want preserved
    note = v / "claude-insights" / "2026-04-10-rewrite-me.md"
    note.write_text(
        "---\n"
        "type: claude-insight\n"
        "date: 2026-04-10\n"
        "source_session: sid-a\n"
        'source_session_note: "[[2026-04-09-proj1-aaaa]]"\n'
        "project: proj1\n"
        "tags:\n"
        "  - claude/insight\n"
        "  - claude/project/proj1\n"
        "  - claude/topic/foo\n"
        "---\n"
        "\n"
        "# My insight\n"
        "\n"
        "Body line 1\n"
        "Body line 2 with [[2026-04-09-proj1-aaaa]] reference in body\n",
        encoding="utf-8",
    )
    os.utime(note, (b_start + 1800, b_start + 1800))

    # Capture the original body for byte-identity comparison
    original_text = note.read_text(encoding="utf-8")
    original_body = original_text.split("---\n", 2)[-1]

    issues = check.scan(
        str(v), "claude-sessions", "claude-insights", days=7, project="proj1"
    )
    assert len(issues) == 1, f"expected 1 issue, got {len(issues)}"

    backup_root = tmp_path / "backups"
    results = check.apply(issues, str(backup_root))

    assert len(results) == 1
    assert results[0].status == "applied"
    assert results[0].backup_path is not None
    assert Path(results[0].backup_path).exists()

    patched = note.read_text(encoding="utf-8")
    # Frontmatter targets rewritten
    assert "source_session: sid-b" in patched
    assert 'source_session_note: "[[2026-04-10-proj1-bbbb]]"' in patched
    # Untouched frontmatter preserved
    assert "claude/topic/foo" in patched
    assert "type: claude-insight" in patched
    assert "tags:\n  - claude/insight" in patched
    # Body byte-identical
    patched_body = patched.split("---\n", 2)[-1]
    assert patched_body == original_body, "body must be byte-identical after frontmatter rewrite"
    # Backup file matches the pre-patch content
    assert Path(results[0].backup_path).read_text(encoding="utf-8") == original_text


def test_apply_skips_unresolved(doctor_vault, tmp_path):
    """Issues marked unresolved are skipped, never rewritten."""
    import vault_doctor_checks.source_sessions as check
    from vault_doctor_checks import Issue

    v = doctor_vault["vault"]
    note = v / "claude-insights" / "2026-04-10-unresolved.md"
    original = (
        "---\n"
        "type: claude-insight\n"
        "date: 2026-04-10\n"
        "source_session: gone-sid\n"
        'source_session_note: "[[does-not-exist]]"\n'
        "project: proj1\n"
        "---\n"
        "# unresolved\n"
    )
    note.write_text(original, encoding="utf-8")

    issue = Issue(
        check="source-sessions",
        note_path=str(note),
        project="proj1",
        current_source="[[does-not-exist]]",
        proposed_source="",
        reason="no window",
        confidence=0.0,
        extra={"unresolved": True},
    )

    backup_root = tmp_path / "backups"
    results = check.apply([issue], str(backup_root))
    assert len(results) == 1
    assert results[0].status == "unresolved"
    assert results[0].backup_path is None
    # File must be byte-identical
    assert note.read_text(encoding="utf-8") == original


def test_apply_errors_on_missing_proposed_sid(doctor_vault, tmp_path):
    """Issue with no proposed_sid returns status=error, file untouched."""
    import vault_doctor_checks.source_sessions as check
    from vault_doctor_checks import Issue

    v = doctor_vault["vault"]
    note = v / "claude-insights" / "2026-04-10-noprop.md"
    original = "---\ntype: claude-insight\nproject: proj1\nsource_session: x\n---\n# x\n"
    note.write_text(original, encoding="utf-8")

    issue = Issue(
        check="source-sessions",
        note_path=str(note),
        project="proj1",
        current_source="[[foo]]",
        proposed_source="[[bar]]",  # proposed basename exists but extra['proposed_sid'] is missing
        reason="test",
        confidence=0.95,
        extra={},  # no proposed_sid
    )

    backup_root = tmp_path / "backups"
    results = check.apply([issue], str(backup_root))
    assert results[0].status == "error"
    assert "proposed_sid" in (results[0].error or "")
    assert note.read_text(encoding="utf-8") == original
