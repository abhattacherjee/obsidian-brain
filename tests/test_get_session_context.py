# tests/test_get_session_context.py
"""Tests for first-seen-date marker, hash-resolver, and basename invariants
introduced for obsidian-brain#101 (subsumes #86)."""

from __future__ import annotations

import datetime
import json
import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

import obsidian_utils


def _unique_sid() -> str:
    return f"test-sid-{uuid.uuid4().hex}"


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect `~/.claude/obsidian-brain/sessions/` into tmp_path so marker
    writes do not pollute the real user directory across tests."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    yield tmp_path


def test_first_seen_date_lazy_writes_and_returns_today(isolated_home):
    sid = _unique_sid()
    today = datetime.date.today().isoformat()

    result = obsidian_utils._first_seen_date(sid)

    assert result == today
    marker = isolated_home / ".claude" / "obsidian-brain" / "sessions" / f"{sid}.json"
    assert marker.exists()
    assert oct(marker.stat().st_mode)[-3:] == "600"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["first_seen_date"] == today
    assert "first_seen_iso" in payload


def test_first_seen_date_idempotent_across_calls(isolated_home):
    sid = _unique_sid()
    first = obsidian_utils._first_seen_date(sid)
    second = obsidian_utils._first_seen_date(sid)
    third = obsidian_utils._first_seen_date(sid)
    assert first == second == third


def test_first_seen_date_survives_today_advance(isolated_home):
    """Cross-midnight invariant: once the marker exists, advancing
    date.today() must not change the returned value."""
    sid = _unique_sid()
    day_n = datetime.date(2026, 4, 25)
    day_n_plus_1 = datetime.date(2026, 4, 26)

    class _FrozenDate:
        @staticmethod
        def today():
            return _FrozenDate._now

    _FrozenDate._now = day_n
    with patch.object(obsidian_utils.datetime, "date", _FrozenDate):
        first = obsidian_utils._first_seen_date(sid)
        assert first == day_n.isoformat()

    _FrozenDate._now = day_n_plus_1
    with patch.object(obsidian_utils.datetime, "date", _FrozenDate):
        second = obsidian_utils._first_seen_date(sid)
        assert second == day_n.isoformat()  # still day-N, not day-N+1


def test_first_seen_date_corruption_self_heals(isolated_home):
    sid = _unique_sid()
    marker_dir = isolated_home / ".claude" / "obsidian-brain" / "sessions"
    marker_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = marker_dir / f"{sid}.json"
    marker.write_text("not valid json {", encoding="utf-8")

    today = datetime.date.today().isoformat()
    result = obsidian_utils._first_seen_date(sid)
    assert result == today
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["first_seen_date"] == today

    # Subsequent call returns the rewritten value, no further mutation
    result2 = obsidian_utils._first_seen_date(sid)
    assert result2 == today


def test_first_seen_date_rejects_path_traversal_sid(isolated_home, capsys):
    """A sid shaped like a path-traversal attempt must NOT escape the
    marker directory; helper falls back to today's date and warns."""
    today = datetime.date.today().isoformat()
    result = obsidian_utils._first_seen_date("../../../etc/passwd")
    assert result == today
    # No marker file should have been created anywhere outside sessions/
    sessions_dir = isolated_home / ".claude" / "obsidian-brain" / "sessions"
    if sessions_dir.exists():
        assert list(sessions_dir.glob("*passwd*")) == []
    captured = capsys.readouterr()
    assert "unsafe sid" in captured.err.lower() or "refusing" in captured.err.lower()


def test_first_seen_date_chmods_existing_loose_mode_dir(isolated_home):
    """mkdir(mode=0o700, exist_ok=True) is a no-op on a pre-existing dir;
    helper must explicitly chmod 0o700 if mode is too permissive."""
    sessions = isolated_home / ".claude" / "obsidian-brain" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    os.chmod(sessions, 0o755)  # simulate a previously-buggy permission
    sid = _unique_sid()
    obsidian_utils._first_seen_date(sid)
    assert oct(sessions.stat().st_mode)[-3:] == "700"


def test_first_seen_date_chmods_existing_loose_mode_marker(isolated_home):
    """If a marker file exists with overly-permissive mode (e.g., from a
    previous bug or manual edit), the helper must self-heal it to 0o600."""
    sessions = isolated_home / ".claude" / "obsidian-brain" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True, mode=0o700)
    sid = _unique_sid()
    marker = sessions / f"{sid}.json"
    marker.write_text(
        json.dumps({"first_seen_date": "2026-04-20", "first_seen_iso": "x"}),
        encoding="utf-8",
    )
    os.chmod(marker, 0o644)  # simulate a previously-buggy permission

    obsidian_utils._first_seen_date(sid)
    assert oct(marker.stat().st_mode)[-3:] == "600"


def test_get_session_context_fallback_uses_marker_date(isolated_home, tmp_path, monkeypatch):
    """get_session_context() fallback must compose its basename from
    _first_seen_date(sid), not date.today() — so cross-midnight insights
    and SessionEnd writes agree on the filename. Mock date.today() to a
    different day than the marker so the test actually exercises the
    divergence the helper prevents."""
    sid = _unique_sid()
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: sid)
    monkeypatch.setattr(obsidian_utils, "canonical_project_name", lambda *a, **kw: "obsidian-brain")

    marker_date = "2026-04-20"  # day-N
    other_day = datetime.date(2026, 4, 22)  # day-N+2 — different from marker

    # Pre-write a marker pointing at day-N
    marker_dir = isolated_home / ".claude" / "obsidian-brain" / "sessions"
    marker_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (marker_dir / f"{sid}.json").write_text(
        json.dumps({"first_seen_date": marker_date, "first_seen_iso": "x"}),
        encoding="utf-8",
    )

    vault = tmp_path / "vault"
    sessions = vault / "claude-sessions"
    sessions.mkdir(parents=True)

    class _FrozenDate:
        @staticmethod
        def today():
            return other_day

    # With date.today() mocked to day-N+2, the fallback must STILL produce
    # the day-N basename via the marker. If the fallback ignored the marker
    # and used date.today(), the basename would start with 2026-04-22.
    with patch.object(obsidian_utils.datetime, "date", _FrozenDate):
        ctx = obsidian_utils.get_session_context(str(vault), "claude-sessions")

    assert ctx["session_note_name"].startswith(f"{marker_date}-obsidian-brain-"), (
        f"expected basename pinned to marker date {marker_date}, got {ctx['session_note_name']}"
    )
    # Must be byte-equal to make_filename(marker_date, ...)
    expected = obsidian_utils.make_filename(marker_date, "obsidian-brain", sid)[:-3]
    assert ctx["session_note_name"] == expected


def test_helper_and_session_end_produce_byte_identical_basename(isolated_home, monkeypatch):
    """Project-slug invariant: across many (project, sid) combinations,
    get_session_context()'s fallback basename and the basename SessionEnd
    would build via make_filename(_first_seen_date(sid), slugify(project), sid)
    are byte-for-byte identical. Catches any future regression that
    reintroduces a hand-composed slug or a different date source."""
    projects = [
        "obsidian-brain",
        "tiny-vacation-agent",
        "personal-ws",
        "claude-code-skills",
        "very-long-project-name-that-might-trip-truncation-logic",
        "abc",
        "name with spaces",
        "name_with_underscores",
        "obsidian-brain--issue-101-source-session-basename-stability",
        "Mixed-Case-Project",
    ]
    for project in projects:
        for _ in range(3):
            sid = _unique_sid()
            monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda s=sid: s)
            monkeypatch.setattr(obsidian_utils, "canonical_project_name",
                                lambda *a, project=project, **kw: project)

            # Helper side
            ctx = obsidian_utils.get_session_context()
            helper_basename = ctx["session_note_name"]

            # SessionEnd side — replicate the exact call shape
            date_str = obsidian_utils._first_seen_date(sid)
            session_end_filename = obsidian_utils.make_filename(
                date_str,
                obsidian_utils.slugify(project),
                sid,
            )
            session_end_basename = session_end_filename[:-3]  # strip .md

            assert helper_basename == session_end_basename, (
                f"divergence for project={project!r}, sid={sid}:\n"
                f"  helper:      {helper_basename}\n"
                f"  session_end: {session_end_basename}"
            )


def test_session_end_filename_uses_marker_date(isolated_home, monkeypatch):
    """SessionEnd reads _first_seen_date(sid), not date.today()."""
    sid = _unique_sid()
    # Pre-write a marker pointing at day-N (yesterday relative to "today")
    marker_dir = isolated_home / ".claude" / "obsidian-brain" / "sessions"
    marker_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (marker_dir / f"{sid}.json").write_text(
        json.dumps({"first_seen_date": "2026-04-25", "first_seen_iso": "x"}),
        encoding="utf-8",
    )

    # Direct exercise of the helper SessionEnd uses
    date_str = obsidian_utils._first_seen_date(sid)
    assert date_str == "2026-04-25"

    project_slug = obsidian_utils.slugify("obsidian-brain")
    filename = obsidian_utils.make_filename(date_str, project_slug, sid)
    assert filename.startswith("2026-04-25-obsidian-brain-")
    assert filename.endswith(".md")


def _write_note(path: Path, frontmatter: dict, body: str = "body\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for k, v in frontmatter.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append(body)
    path.write_text("\n".join(lines), encoding="utf-8")


def test_peek_frontmatter_type_reads_session(tmp_path):
    note = tmp_path / "n.md"
    _write_note(note, {"type": "claude-session", "session_id": "abc"})
    assert obsidian_utils._peek_frontmatter_type(note) == "claude-session"


def test_peek_frontmatter_type_reads_snapshot(tmp_path):
    note = tmp_path / "n.md"
    _write_note(note, {"type": "claude-snapshot", "session_id": "abc"})
    assert obsidian_utils._peek_frontmatter_type(note) == "claude-snapshot"


def test_peek_frontmatter_type_returns_none_when_missing(tmp_path):
    note = tmp_path / "n.md"
    _write_note(note, {"session_id": "abc"})
    assert obsidian_utils._peek_frontmatter_type(note) is None


def test_peek_frontmatter_project_path_strips_quotes(tmp_path):
    note = tmp_path / "n.md"
    _write_note(note, {
        "type": "claude-session",
        "project_path": '"/Users/a/dev/obsidian-brain"',
    })
    assert obsidian_utils._peek_frontmatter_project_path(note) == "/Users/a/dev/obsidian-brain"


def test_peek_frontmatter_field_empty_value_returns_none(tmp_path):
    """An empty scalar (`field:` with no value) returns None, not ''.
    Lets resolver call sites use truthy checks safely."""
    note = tmp_path / "n.md"
    _write_note(note, {"type": "", "session_id": "abc"})
    assert obsidian_utils._peek_frontmatter_type(note) is None


def test_resolve_filters_snapshot_type(tmp_path):
    """Defense-in-depth: even if a snapshot ever ends up with a session-shaped
    filename (matching the resolver glob ``*-{h}.md``), the type filter must
    exclude it. We deliberately give the snapshot a session-shaped name here
    so the glob matches and the type filter is the only thing keeping it out."""
    sessions_dir = tmp_path
    h = "abcd"
    _write_note(sessions_dir / f"2026-04-20-foo-{h}.md",
                {"type": "claude-session", "session_id": "real",
                 "project_path": '"/cwd/foo"'})
    _write_note(sessions_dir / f"2026-04-20-snap-{h}.md",
                {"type": "claude-snapshot", "session_id": "real"})

    basename, collisions = obsidian_utils._resolve_session_note_by_hash(
        sessions_dir, h, cwd="/cwd/foo"
    )
    assert basename == f"2026-04-20-foo-{h}"
    assert collisions == []


def test_resolve_disambiguates_by_project_path(tmp_path):
    sessions_dir = tmp_path
    h = "abcd"
    _write_note(sessions_dir / f"2026-04-20-proj-a-{h}.md",
                {"type": "claude-session", "session_id": "a",
                 "project_path": '"/cwd/a"'})
    _write_note(sessions_dir / f"2026-04-20-proj-b-{h}.md",
                {"type": "claude-session", "session_id": "b",
                 "project_path": '"/cwd/b"'})

    basename, collisions = obsidian_utils._resolve_session_note_by_hash(
        sessions_dir, h, cwd="/cwd/a"
    )
    assert basename == f"2026-04-20-proj-a-{h}"
    assert collisions == [f"2026-04-20-proj-b-{h}.md"]


def test_resolve_double_collision_returns_none(tmp_path):
    """Two session-type notes with same hash AND same project_path → ambiguous,
    caller falls back to composed name."""
    sessions_dir = tmp_path
    h = "abcd"
    _write_note(sessions_dir / f"2026-04-20-proj-a-{h}.md",
                {"type": "claude-session", "session_id": "a1",
                 "project_path": '"/cwd/a"'})
    _write_note(sessions_dir / f"2026-04-21-proj-a-{h}.md",
                {"type": "claude-session", "session_id": "a2",
                 "project_path": '"/cwd/a"'})

    basename, collisions = obsidian_utils._resolve_session_note_by_hash(
        sessions_dir, h, cwd="/cwd/a"
    )
    assert basename is None
    assert sorted(collisions) == sorted([
        f"2026-04-20-proj-a-{h}.md",
        f"2026-04-21-proj-a-{h}.md",
    ])


def test_resolve_no_match_returns_empty(tmp_path):
    """Sanity: empty directory → (None, [])."""
    basename, collisions = obsidian_utils._resolve_session_note_by_hash(
        tmp_path, "abcd", cwd="/cwd/x"
    )
    assert basename is None
    assert collisions == []


def test_get_session_context_uses_type_aware_resolver(isolated_home, tmp_path, monkeypatch, capsys):
    """get_session_context with a snapshot+session sharing the hash returns
    the session, not the snapshot (#101 Fix C)."""
    sid = "real-session-id"
    h = obsidian_utils.hashlib.sha256(sid.encode()).hexdigest()[:4]
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: sid)
    monkeypatch.setattr(obsidian_utils, "canonical_project_name",
                        lambda *a, **kw: "obsidian-brain")

    vault = tmp_path / "vault"
    sessions = vault / "claude-sessions"
    sessions.mkdir(parents=True)
    cwd = str(tmp_path / "obsidian-brain")
    (tmp_path / "obsidian-brain").mkdir()
    monkeypatch.chdir(tmp_path / "obsidian-brain")

    _write_note(sessions / f"2026-04-20-obsidian-brain-{h}.md",
                {"type": "claude-session", "session_id": sid,
                 "project_path": f'"{cwd}"'})
    _write_note(sessions / f"2026-04-20-obsidian-brain-{h}-snapshot-101010.md",
                {"type": "claude-snapshot", "session_id": sid})

    ctx = obsidian_utils.get_session_context(str(vault), "claude-sessions")
    assert ctx["session_note_name"] == f"2026-04-20-obsidian-brain-{h}"
    # Should NOT be the snapshot
    assert "snapshot" not in ctx["session_note_name"]


def test_get_session_context_disambiguates_cross_project_hash_collision(
    isolated_home, tmp_path, monkeypatch, capsys
):
    """When two session-type notes share the 4-char hash across projects,
    get_session_context returns the cwd-matching one and emits a WARN
    listing the other (#101 Fix C)."""
    sid = "real-session-id"
    h = obsidian_utils.hashlib.sha256(sid.encode()).hexdigest()[:4]
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: sid)
    monkeypatch.setattr(obsidian_utils, "canonical_project_name",
                        lambda *a, **kw: "obsidian-brain")

    vault = tmp_path / "vault"
    sessions = vault / "claude-sessions"
    sessions.mkdir(parents=True)

    cwd_a = str(tmp_path / "obsidian-brain")
    (tmp_path / "obsidian-brain").mkdir()
    monkeypatch.chdir(tmp_path / "obsidian-brain")

    # Two session-type notes with the SAME hash but DIFFERENT project_path —
    # this is the cross-project hash collision the resolver disambiguates.
    _write_note(sessions / f"2026-04-20-obsidian-brain-{h}.md",
                {"type": "claude-session", "session_id": "sid-a",
                 "project_path": f'"{cwd_a}"'})
    _write_note(sessions / f"2026-04-21-other-project-{h}.md",
                {"type": "claude-session", "session_id": "sid-b",
                 "project_path": '"/some/other/project"'})

    ctx = obsidian_utils.get_session_context(str(vault), "claude-sessions")
    assert ctx["session_note_name"] == f"2026-04-20-obsidian-brain-{h}", (
        f"expected cwd-matching basename, got {ctx['session_note_name']}"
    )
    captured = capsys.readouterr()
    assert "WARN" in captured.err
    assert f"hash {h}" in captured.err
    assert "other-project" in captured.err  # the OTHER session is named in the warning


def test_is_resumed_session_filters_snapshot_type(tmp_path, monkeypatch):
    """is_resumed_session must NOT return True when only a snapshot
    exists with this hash (subsumes #86). Snapshot is given a session-shaped
    filename so the resolver glob matches and the type filter is exercised."""
    sid = "fresh-session-id"
    h = obsidian_utils.hashlib.sha256(sid.encode()).hexdigest()[:4]
    vault = tmp_path / "vault"
    sessions = vault / "claude-sessions"
    sessions.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    # Snapshot with a session-shaped filename — only the type filter excludes it
    _write_note(sessions / f"2026-04-20-foo-{h}.md",
                {"type": "claude-snapshot", "session_id": "different"})

    assert obsidian_utils.is_resumed_session(str(vault), "claude-sessions", sid) is False


def test_is_resumed_session_returns_true_for_real_session(tmp_path, monkeypatch):
    sid = "fresh-session-id"
    h = obsidian_utils.hashlib.sha256(sid.encode()).hexdigest()[:4]
    vault = tmp_path / "vault"
    sessions = vault / "claude-sessions"
    sessions.mkdir(parents=True)
    cwd = str(tmp_path)
    monkeypatch.chdir(tmp_path)

    _write_note(sessions / f"2026-04-20-foo-{h}.md",
                {"type": "claude-session", "session_id": sid,
                 "project_path": f'"{cwd}"'})

    assert obsidian_utils.is_resumed_session(str(vault), "claude-sessions", sid) is True


def test_is_resumed_session_handles_collision_pair(tmp_path, monkeypatch, capsys):
    """Same-project two-session ambiguity (the original #86 scope):
    is_resumed_session returns False (no unambiguous prior session for
    THIS sid in THIS project), warns, and does not crash. Operator should
    investigate the duplicates manually."""
    sid = "fresh-session-id"
    h = obsidian_utils.hashlib.sha256(sid.encode()).hexdigest()[:4]
    vault = tmp_path / "vault"
    sessions = vault / "claude-sessions"
    sessions.mkdir(parents=True)
    cwd = str(tmp_path)
    monkeypatch.chdir(tmp_path)

    _write_note(sessions / f"2026-04-20-foo-{h}.md",
                {"type": "claude-session", "session_id": "old",
                 "project_path": f'"{cwd}"'})
    _write_note(sessions / f"2026-04-21-foo-{h}.md",
                {"type": "claude-session", "session_id": "newer",
                 "project_path": f'"{cwd}"'})

    result = obsidian_utils.is_resumed_session(str(vault), "claude-sessions", sid)
    assert result is False  # ← changed from True
    captured = capsys.readouterr()
    assert "WARN" in captured.err or "collide" in captured.err.lower()  # 'collide' (singular)


def test_peek_frontmatter_field_handles_invalid_utf8(tmp_path, capsys):
    """A file with invalid UTF-8 bytes returns None and logs to stderr,
    rather than raising and breaking the resolver chain."""
    note = tmp_path / "n.md"
    note.write_bytes(b"---\ntype: claude-session\nbad: \xff\xfe\n---\n")
    result = obsidian_utils._peek_frontmatter_type(note)
    assert result is None
    captured = capsys.readouterr()
    assert "cannot read" in captured.err.lower() or "decode" in captured.err.lower()


def test_peek_frontmatter_field_logs_empty_value(tmp_path, capsys):
    """Empty-but-present field is logged as a possible corruption signal."""
    note = tmp_path / "n.md"
    _write_note(note, {"type": "", "session_id": "abc"})
    result = obsidian_utils._peek_frontmatter_type(note)
    assert result is None
    captured = capsys.readouterr()
    assert "empty" in captured.err.lower()


def test_resolve_logs_when_sessions_dir_missing(tmp_path, capsys):
    """When sessions_dir doesn't exist, resolver logs to stderr (so a
    misconfigured vault path is observable), then returns no-match."""
    missing = tmp_path / "nonexistent"
    basename, collisions = obsidian_utils._resolve_session_note_by_hash(
        missing, "abcd", cwd="/cwd/x"
    )
    assert basename is None
    assert collisions == []
    captured = capsys.readouterr()
    assert "does not exist" in captured.err.lower() or "no-match" in captured.err.lower()


def test_is_resumed_session_returns_false_on_cross_project_collision(tmp_path, monkeypatch, capsys):
    """Cross-project hash collision: a session-type note exists with the
    matching hash but project_path != cwd. Function returns False (this
    is NOT our resumed session) and warns."""
    sid = "fresh-session-id"
    h = obsidian_utils.hashlib.sha256(sid.encode()).hexdigest()[:4]
    vault = tmp_path / "vault"
    sessions = vault / "claude-sessions"
    sessions.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    # Single session-type note belonging to a DIFFERENT project
    _write_note(sessions / f"2026-04-20-foo-{h}.md",
                {"type": "claude-session", "session_id": "other-project-sid",
                 "project_path": '"/some/other/project"'})

    result = obsidian_utils.is_resumed_session(str(vault), "claude-sessions", sid)
    assert result is False, (
        "Cross-project hash collision should NOT mark this session as resumed; "
        "the colliding note belongs to a different project."
    )


def test_safe_getcwd_returns_empty_on_cwd_gone(monkeypatch):
    """When os.getcwd() raises (cwd deleted/unmounted — issue #105 territory),
    _safe_getcwd returns empty string so callers fall back gracefully instead
    of crashing SessionEnd."""
    def _raise(*a, **kw):
        raise FileNotFoundError("cwd deleted")
    monkeypatch.setattr(os, "getcwd", _raise)
    assert obsidian_utils._safe_getcwd() == ""


def test_safe_getcwd_returns_empty_on_oserror(monkeypatch):
    """OSError (permission, EIO) on os.getcwd() must also degrade gracefully."""
    def _raise(*a, **kw):
        raise OSError("EIO on cwd")
    monkeypatch.setattr(os, "getcwd", _raise)
    assert obsidian_utils._safe_getcwd() == ""


def test_resolver_glob_oserror_returns_none(tmp_path, monkeypatch, capsys):
    """If glob raises OSError (transient I/O, permission), resolver returns
    (None, []) and logs to stderr — does not propagate.

    Patches Path.glob globally because the resolver does ``Path(sessions_dir)``
    internally, which produces a fresh Path object whose `glob` method is
    bound at call time.
    """
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    def _raising_glob(self, pattern):
        raise OSError("simulated I/O error")
    monkeypatch.setattr(Path, "glob", _raising_glob)

    basename, collisions = obsidian_utils._resolve_session_note_by_hash(
        sessions, "abcd", cwd="/cwd/x"
    )
    assert basename is None
    assert collisions == []
    captured = capsys.readouterr()
    assert "glob failed" in captured.err.lower()


def test_resolve_treats_type_missing_as_session(tmp_path):
    """Legacy notes without an explicit `type:` frontmatter field still count
    as session notes so resumed-session detection doesn't regress on
    pre-existing vaults — matches the convention used by collect_open_items()
    in hooks/open_item_dedup.py.
    """
    sessions = tmp_path
    h = "abcd"
    _write_note(sessions / f"2026-04-20-foo-{h}.md",
                {"session_id": "abc", "project_path": '"/cwd/foo"'})  # NO type
    basename, collisions = obsidian_utils._resolve_session_note_by_hash(
        sessions, h, cwd="/cwd/foo"
    )
    assert basename == f"2026-04-20-foo-{h}"
    assert collisions == []


def test_is_resumed_session_uses_provided_cwd_over_getcwd(tmp_path, monkeypatch):
    """When ``cwd`` is passed explicitly, is_resumed_session uses it instead
    of os.getcwd(). SessionEnd passes hook_input["cwd"] (Claude Code's
    authoritative project path) so a hook process that chdir'd elsewhere
    still classifies the session against the right project.
    """
    sid = "real-session-id"
    h = obsidian_utils.hashlib.sha256(sid.encode()).hexdigest()[:4]
    vault = tmp_path / "vault"
    sessions = vault / "claude-sessions"
    sessions.mkdir(parents=True)

    project_a = tmp_path / "real-project"
    project_a.mkdir()
    cwd_a = str(project_a)
    _write_note(sessions / f"2026-04-20-foo-{h}.md",
                {"type": "claude-session", "session_id": sid,
                 "project_path": f'"{cwd_a}"'})

    # Force os.getcwd() into a DIFFERENT directory; the provided cwd must win.
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)

    # Without cwd param: returns False (os.getcwd() doesn't match note).
    assert obsidian_utils.is_resumed_session(str(vault), "claude-sessions", sid) is False

    # With cwd param: returns True (provided cwd matches note).
    assert obsidian_utils.is_resumed_session(
        str(vault), "claude-sessions", sid, cwd=cwd_a
    ) is True
