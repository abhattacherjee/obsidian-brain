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


def test_peek_frontmatter_field_handles_invalid_utf8(tmp_path):
    """Invalid UTF-8 bytes in one field must not take out the whole file.

    Before #283 this decoded strictly, raised UnicodeDecodeError, and the
    note was dropped from resolver filtering entirely. The shared reader
    decodes with errors="replace" (same as the index's parser), so the
    corrupt bytes become U+FFFD in *that* field and every other field still
    resolves. The unreadable-file diagnostic still exists — see
    test_peek_frontmatter_field_unreadable_file_logs — it just no longer
    fires for a byte-level decode problem, which is recoverable."""
    note = tmp_path / "n.md"
    note.write_bytes(b"---\ntype: claude-session\nbad: \xff\xfe\n---\n")
    assert obsidian_utils._peek_frontmatter_type(note) == "claude-session"


def test_peek_frontmatter_field_unreadable_file_logs(tmp_path, capsys):
    """An unreadable file returns None and says so on stderr, so a note
    silently dropped from filtering stays observable."""
    missing = tmp_path / "does-not-exist.md"
    assert obsidian_utils._peek_frontmatter_type(missing) is None
    captured = capsys.readouterr()
    assert "cannot read" in captured.err.lower()


# ---------------------------------------------------------------------------
# #283: _peek_frontmatter_field used to read 30 lines and return whatever
# `field:`-shaped line it found, even when no closing '---' was ever seen.
# ---------------------------------------------------------------------------


def test_peek_frontmatter_field_reads_field_past_line_30(tmp_path):
    """A field below the old 30-line bound must still be found.

    Restoring `if i >= 30: break` makes this fail."""
    note = tmp_path / "deep.md"
    filler = "\n".join(f"field_{i:02d}: v{i}" for i in range(60))
    note.write_text(
        f"---\n{filler}\nproject_path: \"/Users/a/dev/deep\"\n---\n\n# Body\n",
        encoding="utf-8",
    )
    assert obsidian_utils._peek_frontmatter_project_path(note) == "/Users/a/dev/deep"


def test_peek_frontmatter_field_unclosed_fence_returns_none(tmp_path):
    """The issue's fixture: no closing fence, so `status:` in the body is
    prose, not a field. Returning it would be reading a value out of the
    note's text."""
    note = tmp_path / "unclosed.md"
    note.write_text(
        "---\n"
        "type: session\n"
        "# My Note\n"
        "\n"
        "Note: this is body prose\n"
        "status: NOT REALLY A FIELD\n",
        encoding="utf-8",
    )
    assert obsidian_utils._peek_frontmatter_field(note, "status") is None
    # …and the fields *above* the break are refused too: the file has no
    # valid frontmatter region at all, so nothing in it can be trusted.
    assert obsidian_utils._peek_frontmatter_type(note) is None


def test_peek_frontmatter_field_bare_cr_is_not_a_line_terminator(tmp_path):
    """`newline=""` guarantee, in a form that changes the parse result:
    under universal-newline translation the bare \\r splits the `title:`
    line and a bogus `status` field appears."""
    note = tmp_path / "bare-cr.md"
    note.write_bytes(
        b'---\ntype: claude-session\ntitle: "before\rstatus: forged"\n---\n\nbody\n'
    )
    assert obsidian_utils._peek_frontmatter_field(note, "status") is None
    assert obsidian_utils._peek_frontmatter_type(note) == "claude-session"


def test_peek_frontmatter_field_oversized_frontmatter_returns_none(tmp_path):
    """Past MAX_FRONTMATTER_LINES the block is rejected rather than
    half-parsed."""
    note = tmp_path / "oversized.md"
    limit = obsidian_utils.MAX_FRONTMATTER_LINES
    bulk = "\n".join(f"field_{i:05d}: v{i}" for i in range(limit + 100))
    note.write_text(f"---\ntype: claude-session\n{bulk}\n---\n\n# Body\n", encoding="utf-8")
    assert obsidian_utils._peek_frontmatter_type(note) is None


# ---------------------------------------------------------------------------
# #283 follow-up: _build_existing_sid_set read every session note three times
# (type, project, session_id) on the SessionStart hook. _peek_frontmatter_fields
# is the one-read/one-parse variant; _peek_frontmatter_field wraps it, so there
# is exactly one parsing implementation.
# ---------------------------------------------------------------------------


def test_peek_frontmatter_fields_reads_the_file_once(tmp_path, monkeypatch):
    """N fields, one open. Three single-field peeks meant three reads."""
    import builtins

    note = tmp_path / "n.md"
    _write_note(note, {
        "type": "claude-session",
        "project": "obsidian-brain",
        "session_id": "SID-XYZ",
    })

    opens = []
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):
        if str(file) == str(note):
            opens.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)

    result = obsidian_utils._peek_frontmatter_fields(
        note, ("type", "project", "session_id")
    )

    assert result == {
        "type": "claude-session",
        "project": "obsidian-brain",
        "session_id": "SID-XYZ",
    }
    assert len(opens) == 1, f"expected one read, got {len(opens)}"


def test_peek_frontmatter_fields_missing_field_is_none(tmp_path):
    """A field the note does not have comes back None, not absent."""
    note = tmp_path / "n.md"
    _write_note(note, {"type": "claude-session"})

    result = obsidian_utils._peek_frontmatter_fields(note, ("type", "nope"))

    assert result == {"type": "claude-session", "nope": None}


def test_peek_frontmatter_fields_empty_value_is_none_and_warns(tmp_path, capsys):
    """Per-field behaviour is preserved exactly: an empty scalar warns and
    normalizes to None, and the OTHER fields still resolve."""
    note = tmp_path / "n.md"
    _write_note(note, {"type": "", "project": "obsidian-brain"})

    result = obsidian_utils._peek_frontmatter_fields(note, ("type", "project"))

    assert result == {"type": None, "project": "obsidian-brain"}
    err = capsys.readouterr().err
    assert "empty" in err.lower(), err
    assert "'type'" in err, err


def test_peek_frontmatter_fields_first_match_wins(tmp_path):
    """Duplicate keys: the first one inside the fence pair wins, as in the
    single-field scan (which returned on its first match)."""
    note = tmp_path / "dup.md"
    note.write_text(
        "---\ntype: claude-session\ntype: claude-snapshot\n---\n\nbody\n",
        encoding="utf-8",
    )

    assert obsidian_utils._peek_frontmatter_fields(note, ("type",)) == {
        "type": "claude-session"
    }


def test_peek_frontmatter_fields_unclosed_fence_returns_all_none(tmp_path):
    """No frontmatter region means no field may be harvested — for every
    requested field, not just the one that happened to be below the break."""
    note = tmp_path / "unclosed.md"
    note.write_text(
        "---\ntype: claude-session\nproject: real\n# My Note\n\nstatus: prose\n",
        encoding="utf-8",
    )

    assert obsidian_utils._peek_frontmatter_fields(
        note, ("type", "project", "status")
    ) == {"type": None, "project": None, "status": None}


def test_peek_frontmatter_fields_unreadable_names_the_cause(tmp_path, capsys):
    """The "cannot read" diagnostic must name the errno cause again — but via
    exc.strerror, so the absolute vault path never reaches stderr."""
    missing = tmp_path / "does-not-exist.md"

    result = obsidian_utils._peek_frontmatter_fields(missing, ("type", "project"))

    assert result == {"type": None, "project": None}
    err = capsys.readouterr().err
    assert "cannot read" in err.lower(), err
    assert "No such file" in err, err
    assert str(missing) not in err, f"leaked the absolute path: {err!r}"


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


# ─── Issue #105: _resolve_project_basename ───────────────────────────

def test_resolve_project_basename_happy_path(monkeypatch, tmp_path):
    """Happy path: os.getcwd works → returns its basename."""
    target = tmp_path / "some-project"
    target.mkdir()
    monkeypatch.chdir(target)
    assert obsidian_utils._resolve_project_basename() == "some-project"


def test_resolve_project_basename_falls_back_to_env(monkeypatch):
    """When os.getcwd raises, returns basename of CLAUDE_PROJECT_DIR."""
    def _raise(*a, **kw):
        raise FileNotFoundError("cwd deleted")
    monkeypatch.setattr(os, "getcwd", _raise)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp/fake-project-dir/my-proj")
    assert obsidian_utils._resolve_project_basename() == "my-proj"


def test_resolve_project_basename_returns_none_when_both_unavailable(monkeypatch):
    """When both cwd and CLAUDE_PROJECT_DIR fail, returns None for caller
    to treat as 'cannot determine project'."""
    def _raise(*a, **kw):
        raise FileNotFoundError("cwd deleted")
    monkeypatch.setattr(os, "getcwd", _raise)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert obsidian_utils._resolve_project_basename() is None


def test_resolve_project_basename_normalizes_root_cwd_to_none(monkeypatch):
    """cwd='/' has basename '' which would unsafely become a cross-project
    glob ('~/.claude/projects/*/*.jsonl') downstream. Normalize to None per
    Copilot R2 PR #113 so caller falls through to the strict layer-4 scan."""
    monkeypatch.setattr(os, "getcwd", lambda: "/")
    assert obsidian_utils._resolve_project_basename() is None


def test_resolve_project_basename_normalizes_env_trailing_slash(monkeypatch):
    """CLAUDE_PROJECT_DIR ending with '/' has basename '' which would unsafely
    become a cross-project glob downstream. Strip trailing slash before
    basename, then normalize empty to None per Copilot R2 PR #113."""
    def _raise(*a, **kw):
        raise FileNotFoundError("cwd deleted")
    monkeypatch.setattr(os, "getcwd", _raise)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/path/to/myproj/")
    assert obsidian_utils._resolve_project_basename() == "myproj"


def test_resolve_project_basename_env_root_normalizes_to_none(monkeypatch):
    """CLAUDE_PROJECT_DIR='/' normalizes to None (root path is not a project)."""
    def _raise(*a, **kw):
        raise FileNotFoundError("cwd deleted")
    monkeypatch.setattr(os, "getcwd", _raise)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/")
    assert obsidian_utils._resolve_project_basename() is None


# ─── Issue #105: _recent_bootstrap_sid ────────────────────────────────

def _seed_bootstrap(home: Path, project: str, sid: str, mtime_offset: float = 0.0) -> Path:
    """Helper: create a sid-<project> bootstrap file under home with given content
    and set mtime to (now + offset). Returns the file path."""
    import time
    bdir = home / ".claude" / "obsidian-brain"
    bdir.mkdir(parents=True, exist_ok=True)
    bdir.chmod(0o700)
    path = bdir / f"sid-{project}"
    path.write_text(sid)
    if mtime_offset != 0.0:
        ts = time.time() + mtime_offset
        os.utime(path, (ts, ts))
    return path


def test_recent_bootstrap_sid_zero_recent_returns_none(isolated_home):
    """Empty bootstrap dir → None."""
    assert obsidian_utils._recent_bootstrap_sid() is None


def test_recent_bootstrap_sid_exactly_one_recent_returns_sid(isolated_home):
    """Single recent bootstrap → returns its content."""
    sid = _unique_sid()
    _seed_bootstrap(isolated_home, "myproj", sid)
    assert obsidian_utils._recent_bootstrap_sid() == sid


def test_recent_bootstrap_sid_two_recent_returns_none(isolated_home):
    """Two recent bootstraps → None (strict; never silently mis-attributes)."""
    _seed_bootstrap(isolated_home, "proj-a", _unique_sid())
    _seed_bootstrap(isolated_home, "proj-b", _unique_sid())
    assert obsidian_utils._recent_bootstrap_sid() is None


def test_recent_bootstrap_sid_skips_tmp_partials(isolated_home):
    """sid-*.tmp atomic-write residue is not counted as a bootstrap."""
    sid = _unique_sid()
    # One real recent bootstrap + one .tmp partial → still exactly-one
    _seed_bootstrap(isolated_home, "myproj", sid)
    tmp = isolated_home / ".claude" / "obsidian-brain" / ".ob-sid-abc.tmp"
    tmp.write_text("garbage")
    assert obsidian_utils._recent_bootstrap_sid() == sid


def test_recent_bootstrap_sid_skips_stale(isolated_home):
    """Bootstrap file outside recency window → None."""
    # Set mtime 700s in the past (window default is 600s)
    _seed_bootstrap(isolated_home, "myproj", _unique_sid(), mtime_offset=-700.0)
    assert obsidian_utils._recent_bootstrap_sid() is None


def test_recent_bootstrap_sid_skips_empty_content(isolated_home):
    """Recent bootstrap with empty/whitespace content → None (corrupted write)."""
    bdir = isolated_home / ".claude" / "obsidian-brain"
    bdir.mkdir(parents=True, exist_ok=True)
    bdir.chmod(0o700)
    (bdir / "sid-myproj").write_text("   \n  ")
    assert obsidian_utils._recent_bootstrap_sid() is None


def test_recent_bootstrap_sid_rejects_unsafe_sid_format(isolated_home):
    """Bootstrap content failing _SID_FILENAME_SAFE regex → None.

    Without this validation, a corrupted or attacker-controlled bootstrap file
    with content like '../../../tmp/foo' could propagate path-traversal strings
    into cache_get/cache_set composition. Per Copilot R1 PR #113.
    """
    bdir = isolated_home / ".claude" / "obsidian-brain"
    bdir.mkdir(parents=True, exist_ok=True)
    bdir.chmod(0o700)
    # Attacker-controlled / corrupted SIDs that should all be rejected
    for unsafe in [
        "../../../tmp/escape",        # path traversal
        "/absolute/path",             # absolute path
        "sid with spaces",            # whitespace
        "sid\nwith-newline",          # newline
        "sid\twith-tab",              # tab
        "sid*glob",                   # glob char
        "sid/with-slash",             # path separator
        "sid\\with-backslash",        # backslash
        "x" * 200,                    # exceeds 128-char limit
    ]:
        # Reset dir for each iteration so this is exactly-one
        for f in bdir.glob("sid-*"):
            f.unlink()
        (bdir / "sid-myproj").write_text(unsafe)
        assert obsidian_utils._recent_bootstrap_sid() is None, \
            f"Unsafe SID format should be rejected: {unsafe!r}"


# ─── Issue #105: _resolve_session_id integration ──────────────────────

def test_resolve_session_id_cwd_gone_uses_recent_bootstrap(isolated_home, monkeypatch):
    """Headline regression: cwd-gone + valid recent bootstrap → returns the SID
    via layer 4. This is the scenario from 2026-04-24 retros that motivated #105."""
    sid = _unique_sid()
    _seed_bootstrap(isolated_home, "myworktree", sid)

    def _raise(*a, **kw):
        raise FileNotFoundError("cwd deleted")
    monkeypatch.setattr(os, "getcwd", _raise)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    assert obsidian_utils._resolve_session_id() == sid


def test_resolve_session_id_cwd_gone_no_bootstrap_returns_unknown(isolated_home, monkeypatch):
    """Cwd-gone + no recent bootstrap → 'unknown' sentinel (graceful, never raises)."""
    def _raise(*a, **kw):
        raise FileNotFoundError("cwd deleted")
    monkeypatch.setattr(os, "getcwd", _raise)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    assert obsidian_utils._resolve_session_id() == "unknown"


def test_resolve_session_id_happy_path_uses_existing_layers(isolated_home, monkeypatch, tmp_path):
    """Cwd valid + bootstrap valid → resolves via layer 2 (no behavior change)."""
    sid = _unique_sid()
    project = "happypath-proj"

    # Seed the existing bootstrap fast path machinery: write sid-<project> AND
    # a JSONL the fast path can stat.
    _seed_bootstrap(isolated_home, project, sid)
    cc_dir = isolated_home / ".claude" / "projects" / f"-Users-test-{project}"
    cc_dir.mkdir(parents=True, exist_ok=True)
    (cc_dir / f"{sid}.jsonl").write_text("{}\n")

    target = tmp_path / project
    target.mkdir()
    monkeypatch.chdir(target)

    # _bootstrap_prefix() reads the module-level _BOOTSTRAP_PREFIX which is
    # frozen at import time to the real $HOME. Redirect it for this test so
    # the fast path actually finds the seeded bootstrap.
    bdir = isolated_home / ".claude" / "obsidian-brain"
    monkeypatch.setattr(
        obsidian_utils, "_BOOTSTRAP_PREFIX", str(bdir) + "/sid-"
    )

    assert obsidian_utils._resolve_session_id(allow_bootstrap=True) == sid


def test_resolve_session_id_slow_path_skips_layer_2(isolated_home, monkeypatch, tmp_path):
    """allow_bootstrap=False (used by _slow_path_newest_sid) skips layer 2
    even when bootstrap exists. Preserves the existing 'health-check is
    bootstrap-blind' contract."""
    project = "slowpath-proj"
    bootstrap_sid = _unique_sid()
    jsonl_sid = _unique_sid()

    # Seed bootstrap with one SID, JSONL with a different one — slow path must
    # return the JSONL's SID, ignoring the bootstrap file entirely.
    _seed_bootstrap(isolated_home, project, bootstrap_sid)
    cc_dir = isolated_home / ".claude" / "projects" / f"-Users-test-{project}"
    cc_dir.mkdir(parents=True, exist_ok=True)
    (cc_dir / f"{jsonl_sid}.jsonl").write_text("{}\n")

    target = tmp_path / project
    target.mkdir()
    monkeypatch.chdir(target)

    # Layer 2 is skipped here, so _BOOTSTRAP_PREFIX redirect is unnecessary;
    # but the slow-path uses _glob_project_jsonls which uses expanduser at
    # call time → HOME monkeypatch (already done by isolated_home) is enough.
    assert obsidian_utils._resolve_session_id(allow_bootstrap=False) == jsonl_sid


def test_try_bootstrap_fast_path_rejects_unsafe_cached_sid(isolated_home, monkeypatch):
    """_try_bootstrap_fast_path validates cached_sid against _SID_FILENAME_SAFE
    before trusting it. Symmetric to the validation in _recent_bootstrap_sid.
    Without this, a corrupted sid-<project> file with content like '../foo'
    could propagate path-traversal strings into cache_get/cache_set composition.
    Per Copilot R2 PR #113."""
    project = "fastpath-proj"
    bdir = isolated_home / ".claude" / "obsidian-brain"
    bdir.mkdir(parents=True, exist_ok=True)
    bdir.chmod(0o700)
    # Write an unsafe (path-traversal) value into the bootstrap.
    (bdir / f"sid-{project}").write_text("../../../tmp/escape")
    monkeypatch.setattr(obsidian_utils, "_BOOTSTRAP_PREFIX", str(bdir) + "/sid-")

    # No JSONL setup is needed here: this test asserts that the fast path
    # rejects unsafe bootstrap content before trusting or composing with it.
    assert obsidian_utils._try_bootstrap_fast_path(project) is None


def test_resolve_session_id_slow_path_skips_layer_4_recent_bootstrap(isolated_home, monkeypatch):
    """allow_bootstrap=False (used by _slow_path_newest_sid) skips BOTH layer 2
    AND layer 4 — does not trust the cross-project recent-bootstrap scan either.
    Preserves the bootstrap-blind health-check contract that check_hook_status
    relies on at obsidian_utils.py:827."""
    project = "healthcheck-proj"
    bootstrap_sid = _unique_sid()

    # Seed a recent bootstrap (would normally be picked up by layer 4)
    _seed_bootstrap(isolated_home, project, bootstrap_sid)

    # cwd valid, but NO matching JSONL exists for this project — slow path
    # returns "unknown" → without the fix, layer 4 would find bootstrap_sid
    # and return it. With the fix, layer 4 is gated off → returns "unknown".
    target = isolated_home / project
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(target)

    assert obsidian_utils._resolve_session_id(allow_bootstrap=False) == "unknown"


# ─── Issue #260: cross-project session-id / cached-context mis-resolution ──
#
# Reported twice, seven weeks apart: during a /retro in an active session,
# get_session_context() returned a completely different, prior session — wrong
# sid, wrong project, and that session's snapshots mined as first-class /retro
# evidence — with no discovery_errors entry and no collision WARN.
#
# Three defects, one class (silently guessing across projects):
#   1. _resolve_session_id fell through to the cross-project bootstrap scan
#      (layer 4, built for #105's cwd-gone case) whenever layers 1-3 failed for
#      ANY reason, including "cwd is fine, this project just has no JSONL yet".
#   2. get_session_context returned the sid-keyed cache before computing the
#      live project, so one wrong sid yielded another repo's whole context.
#   3. ~/.claude/projects/*<project>/ is a SUFFIX glob, so a bare basename
#      matched every project dir ending in that segment.


def _redirect_secure_paths(monkeypatch, home: Path) -> None:
    """Point the module-level secure-dir constants at a redirected HOME.

    _SECURE_DIR / _CACHE_PREFIX / _BOOTSTRAP_PREFIX are frozen at import time
    from the REAL $HOME, so a HOME-only redirect still reads and writes the
    user's live cache and bootstrap files. Tests that touch either must patch
    the constants too.
    """
    secure = home / ".claude" / "obsidian-brain"
    monkeypatch.setattr(obsidian_utils, "_SECURE_DIR", str(secure))
    monkeypatch.setattr(obsidian_utils, "_CACHE_PREFIX", str(secure / "cache-"))
    monkeypatch.setattr(obsidian_utils, "_BOOTSTRAP_PREFIX", str(secure / "sid-"))


def _encoded_project_dir(home: Path, path: str) -> Path:
    """~/.claude/projects/ dir name Claude Code would use for `path`."""
    return home / ".claude" / "projects" / path.replace("/", "-").replace("_", "-")


def test_get_session_context_readable_subdir_never_borrows_other_project_session(
    isolated_home, tmp_path, monkeypatch, capsys
):
    """Headline #260 repro, end to end.

    cwd is a readable, non-git subdirectory with no JSONLs of its own
    (`.../sonno-tiny-homes-pitch/docs`), and exactly ONE other project has a
    recent sid-* bootstrap plus a poisoned cache entry (`wealth-management`).
    get_session_context() must not return ANY of that other session's fields.
    """
    other_sid = "0524bab1-1111-2222-3333-444455556666"
    other_hash = "976b"
    other_note = "2026-06-28-wealth-management-976b"

    _redirect_secure_paths(monkeypatch, isolated_home)

    # The other project: recent bootstrap + a real JSONL + a cached context.
    _seed_bootstrap(isolated_home, "wealth-management", other_sid)
    other_cc = _encoded_project_dir(isolated_home, "/Users/x/dev/wealth-management")
    other_cc.mkdir(parents=True)
    (other_cc / f"{other_sid}.jsonl").write_text("{}\n", encoding="utf-8")

    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    cache_key = f"session_context:{vault}:claude-sessions"
    (isolated_home / ".claude" / "obsidian-brain" / f"cache-{other_sid}.json").write_text(
        json.dumps({cache_key: {
            "session_id": other_sid,
            "hash": other_hash,
            "project": "wealth-management",
            "session_note_name": other_note,
            "cwd": "/Users/x/dev/wealth-management",
        }}),
        encoding="utf-8",
    )

    # This session: a readable subdirectory of a project with no JSONLs.
    here = tmp_path / "sonno-tiny-homes-pitch" / "docs"
    here.mkdir(parents=True)
    monkeypatch.chdir(here)

    ctx = obsidian_utils.get_session_context(str(vault), "claude-sessions")

    assert ctx["session_id"] == "unknown", (
        f"resolved another project's session: {ctx['session_id']}"
    )
    assert ctx["hash"] == ""
    assert ctx["session_note_name"] == ""
    assert ctx["project"] == "docs"
    assert other_sid not in json.dumps(ctx)
    assert "wealth-management" not in json.dumps(ctx)


def test_resolve_session_id_readable_cwd_without_jsonl_refuses_bootstrap_scan(
    isolated_home, tmp_path, monkeypatch
):
    """Fix 1 in isolation: cwd readable + no JSONL for it + exactly one recent
    cross-project bootstrap → 'unknown', NOT the other project's sid."""
    _redirect_secure_paths(monkeypatch, isolated_home)
    other_sid = _unique_sid()
    _seed_bootstrap(isolated_home, "some-other-project", other_sid)

    here = tmp_path / "pitch" / "docs"
    here.mkdir(parents=True)
    monkeypatch.chdir(here)

    assert obsidian_utils._resolve_session_id() == "unknown"


def test_resolve_session_id_cwd_gone_still_uses_recent_bootstrap_after_260(
    isolated_home, monkeypatch
):
    """#105 regression guard restated for #260: the gate keys on 'cwd produced
    no basename at all', so the cwd-gone path layer 4 exists for still works."""
    _redirect_secure_paths(monkeypatch, isolated_home)
    sid = _unique_sid()
    _seed_bootstrap(isolated_home, "deleted-worktree", sid)

    def _raise(*a, **kw):
        raise FileNotFoundError("cwd deleted")

    monkeypatch.setattr(os, "getcwd", _raise)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    assert obsidian_utils._resolve_session_id() == sid


def test_resolve_session_id_cwd_gone_with_env_dir_resolves_via_jsonl(
    isolated_home, monkeypatch
):
    """The other half of the #105 path: cwd gone but CLAUDE_PROJECT_DIR names a
    project whose JSONL still exists → layer 3 resolves it. The #260 gate must
    not need layer 4 here (and must not consult it, since the env var IS a
    readable answer)."""
    _redirect_secure_paths(monkeypatch, isolated_home)
    real_sid = _unique_sid()
    decoy_sid = _unique_sid()
    _seed_bootstrap(isolated_home, "unrelated-project", decoy_sid)

    worktree = "/Users/x/dev/repo--feature-branch"
    cc_dir = _encoded_project_dir(isolated_home, worktree)
    cc_dir.mkdir(parents=True)
    (cc_dir / f"{real_sid}.jsonl").write_text("{}\n", encoding="utf-8")

    def _raise(*a, **kw):
        raise FileNotFoundError("cwd deleted")

    monkeypatch.setattr(os, "getcwd", _raise)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", worktree)

    assert obsidian_utils._resolve_session_id() == real_sid


def test_get_session_context_rejects_cached_context_from_a_different_cwd(
    isolated_home, tmp_path, monkeypatch, capsys
):
    """Fix 2: a cache-<sid>.json entry stamped with another cwd is discarded,
    recomputed, and the discard is announced on stderr (never silent)."""
    _redirect_secure_paths(monkeypatch, isolated_home)
    sid = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: sid)

    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    secure = isolated_home / ".claude" / "obsidian-brain"
    secure.mkdir(parents=True, exist_ok=True)
    cache_key = f"session_context:{vault}:claude-sessions"
    (secure / f"cache-{sid}.json").write_text(
        json.dumps({cache_key: {
            "session_id": sid,
            "hash": "dead",
            "project": "wealth-management",
            "session_note_name": "2026-06-28-wealth-management-976b",
            "cwd": "/Users/x/dev/wealth-management",
        }}),
        encoding="utf-8",
    )

    here = tmp_path / "sonno-tiny-homes-pitch"
    here.mkdir()
    monkeypatch.chdir(here)

    ctx = obsidian_utils.get_session_context(str(vault), "claude-sessions")

    assert ctx["project"] == "sonno-tiny-homes-pitch", (
        f"returned another cwd's cached context: {ctx}"
    )
    assert ctx["session_note_name"] != "2026-06-28-wealth-management-976b"
    assert ctx["hash"] != "dead"
    assert ctx["cwd"] == os.getcwd()
    err = capsys.readouterr().err
    assert "WARN" in err and "wealth-management" in err, err


def test_get_session_context_recomputes_pre_260_cache_entry_quietly(
    isolated_home, tmp_path, monkeypatch, capsys
):
    """A cache entry written before the cwd stamp existed has no `cwd` key.
    That is a format upgrade, not evidence of mis-attribution: recompute, but
    do not cry wolf on stderr."""
    _redirect_secure_paths(monkeypatch, isolated_home)
    sid = "99999999-8888-7777-6666-555555555555"
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: sid)

    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    secure = isolated_home / ".claude" / "obsidian-brain"
    secure.mkdir(parents=True, exist_ok=True)
    cache_key = f"session_context:{vault}:claude-sessions"
    (secure / f"cache-{sid}.json").write_text(
        json.dumps({cache_key: {
            "session_id": sid, "hash": "abcd", "project": "legacy-proj",
            "session_note_name": "2026-01-01-legacy-proj-abcd",
        }}),
        encoding="utf-8",
    )

    here = tmp_path / "current-proj"
    here.mkdir()
    monkeypatch.chdir(here)

    ctx = obsidian_utils.get_session_context(str(vault), "claude-sessions")
    assert ctx["project"] == "current-proj"
    assert "WARN" not in capsys.readouterr().err

    # The recomputed entry carries the stamp, so the next call is a clean hit.
    ctx2 = obsidian_utils.get_session_context(str(vault), "claude-sessions")
    assert ctx2 == ctx
    assert "WARN" not in capsys.readouterr().err


def test_get_session_context_unknown_is_not_cached_and_returns_live_project(
    isolated_home, tmp_path, monkeypatch
):
    """'unknown' round-trips: no cache file is written for it, and the project
    comes from the live canonical_project_name() rather than a stale entry."""
    _redirect_secure_paths(monkeypatch, isolated_home)
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: "unknown")

    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    here = tmp_path / "live-project"
    here.mkdir()
    monkeypatch.chdir(here)

    ctx = obsidian_utils.get_session_context(str(vault), "claude-sessions")
    assert ctx == {
        "session_id": "unknown",
        "hash": "",
        "project": "live-project",
        "session_note_name": "",
        "cwd": os.getcwd(),
    }
    secure = isolated_home / ".claude" / "obsidian-brain"
    assert list(secure.glob("cache-unknown*")) == []


def test_glob_project_jsonls_prefers_the_dir_encoding_this_cwd(
    isolated_home, tmp_path, monkeypatch
):
    """Fix 3: two project dirs end in '-docs'; only the one encoding this cwd
    counts, even though the unrelated one holds a strictly newer session."""
    import time

    here = tmp_path / "sonno-tiny-homes-pitch" / "docs"
    here.mkdir(parents=True)
    monkeypatch.chdir(here)

    mine = _encoded_project_dir(isolated_home, os.getcwd())
    theirs = isolated_home / ".claude" / "projects" / "-Users-x-dev-claude-workspace-docs"
    mine.mkdir(parents=True)
    theirs.mkdir(parents=True)

    my_sid = "aaaaaaaa-0000-0000-0000-000000000001"
    their_sid = "bbbbbbbb-0000-0000-0000-000000000002"
    (mine / f"{my_sid}.jsonl").write_text("{}\n", encoding="utf-8")
    (theirs / f"{their_sid}.jsonl").write_text("{}\n", encoding="utf-8")
    now = time.time()
    os.utime(mine / f"{my_sid}.jsonl", (now - 3600, now - 3600))
    os.utime(theirs / f"{their_sid}.jsonl", (now, now))  # newest overall

    matches = obsidian_utils._glob_project_jsonls("docs")
    assert [os.path.basename(m) for m in matches] == [f"{my_sid}.jsonl"]
    assert obsidian_utils._try_slow_jsonl_glob("docs") == my_sid


def test_glob_project_jsonls_refuses_when_no_dir_encodes_cwd(
    isolated_home, tmp_path, monkeypatch, capsys
):
    """Ambiguous → refuse: several dirs match the suffix, none is ours. Return
    nothing (caller reports 'unknown') and say so on stderr."""
    here = tmp_path / "elsewhere" / "docs"
    here.mkdir(parents=True)
    monkeypatch.chdir(here)

    projects = isolated_home / ".claude" / "projects"
    for name, sid in (
        ("-Users-x-dev-claude_workspace-docs", "cccccccc-0000-0000-0000-000000000003"),
        ("-Users-x-dev-other-workspace-docs", "dddddddd-0000-0000-0000-000000000004"),
    ):
        d = projects / name
        d.mkdir(parents=True)
        (d / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")

    assert obsidian_utils._glob_project_jsonls("docs") == []
    assert obsidian_utils._try_slow_jsonl_glob("docs") == "unknown"
    err = capsys.readouterr().err
    assert "WARN" in err and "refusing to guess" in err, err


def test_glob_project_jsonls_keeps_every_encoding_variant_of_this_cwd(
    isolated_home, tmp_path, monkeypatch
):
    """A single checkout legitimately owns more than one project dir: CC has
    changed how it folds '_' in the encoded name, and both survive on disk
    (verified on this machine for claude_workspace/obsidian-brain). Both must
    be kept — refusing them would strand the live session."""
    here = tmp_path / "claude_workspace" / "obsidian-brain"
    here.mkdir(parents=True)
    monkeypatch.chdir(here)
    cwd = os.getcwd()

    projects = isolated_home / ".claude" / "projects"
    kept_underscore = projects / cwd.replace("/", "-")
    folded = projects / cwd.replace("/", "-").replace("_", "-")
    assert kept_underscore != folded
    kept_underscore.mkdir(parents=True)
    folded.mkdir(parents=True)
    (kept_underscore / "sid-old.jsonl").write_text("{}\n", encoding="utf-8")
    (folded / "sid-new.jsonl").write_text("{}\n", encoding="utf-8")

    matches = obsidian_utils._glob_project_jsonls("obsidian-brain")
    assert sorted(os.path.basename(m) for m in matches) == ["sid-new.jsonl", "sid-old.jsonl"]


# ─── #260 review round 2: the mis-resolution was still reachable ──────────
#
# The first pass fixed the layer-3 glob but left three ways back to a
# cross-project answer, all verified as live repros before these tests existed:
#   C1  the bootstrap fast path globbed the cached sid SEPARATELY, so the
#       "refusing to guess" empty list came back indistinguishable from "this
#       project has no other JSONLs" and was read as "trust the cache".
#   C2  the encoding fallback folded '_' but not '.', so a dot-named directory
#       (/Users/me/.openclaw, 17 transcripts on disk) resolved to 'unknown'.
#   S9  the folded glob only ran when the literal one came up EMPTY, so a
#       literal glob matching one WRONG directory suppressed the retry.


def _jsonl_line(cwd: str) -> str:
    """One CC transcript line, carrying the `cwd` field CC actually records."""
    return json.dumps({"type": "user", "cwd": cwd, "message": {"role": "user"}}) + "\n"


def test_bootstrap_under_current_basename_never_borrows_another_project(
    isolated_home, tmp_path, monkeypatch
):
    """C1: SessionStart writes sid-<cwd basename>, so the bootstrap file collides
    on a generic basename exactly as easily as the suffix glob does.

    cwd is `.../sonno-tiny-homes-pitch/docs`; two unrelated project dirs end in
    `-docs`; `sid-docs` holds the sid of one of them. The cached-sid glob lands
    in ONE directory and takes the sole-match leniency, while the all-JSONLs
    glob sees both and refuses — and the fast path used to resolve that
    disagreement in favour of the stale bootstrap, printing "refusing to guess"
    and then guessing through the other branch.
    """
    _redirect_secure_paths(monkeypatch, isolated_home)
    decoy_sid = "aaaaaaa-1111-2222-3333-444444444444"
    other_sid = "bbbbbbb-2222-3333-4444-555555555555"

    projects = isolated_home / ".claude" / "projects"
    decoy = projects / "-Users-x-dev-vendor-pitch-docs"
    other = projects / "-Users-x-dev-other-workspace-docs"
    decoy.mkdir(parents=True)
    other.mkdir(parents=True)
    (decoy / f"{decoy_sid}.jsonl").write_text("{}\n", encoding="utf-8")
    (other / f"{other_sid}.jsonl").write_text("{}\n", encoding="utf-8")

    # SessionStart's bootstrap for THIS cwd basename names the decoy's session.
    _seed_bootstrap(isolated_home, "docs", decoy_sid)

    here = tmp_path / "sonno-tiny-homes-pitch" / "docs"
    here.mkdir(parents=True)
    monkeypatch.chdir(here)

    assert obsidian_utils._resolve_session_id() == "unknown", (
        "the refusal was read as 'no other JSONLs exist' and the stale "
        "cross-project bootstrap was returned"
    )


def test_bootstrap_fast_path_returns_none_when_the_glob_refused(
    isolated_home, tmp_path, monkeypatch
):
    """C1 in isolation, at the function that owned the defect."""
    _redirect_secure_paths(monkeypatch, isolated_home)
    decoy_sid = "ccccccc-1111-2222-3333-444444444444"

    projects = isolated_home / ".claude" / "projects"
    for name, sid in (
        ("-Users-x-dev-vendor-pitch-docs", decoy_sid),
        ("-Users-x-dev-other-workspace-docs", "ddddddd-2222-3333-4444-555555555555"),
    ):
        d = projects / name
        d.mkdir(parents=True)
        (d / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")

    _seed_bootstrap(isolated_home, "docs", decoy_sid)
    here = tmp_path / "pitch" / "docs"
    here.mkdir(parents=True)
    monkeypatch.chdir(here)

    assert obsidian_utils._try_bootstrap_fast_path("docs") is None


def test_glob_project_jsonls_folds_a_dot_in_the_cwd_basename(
    isolated_home, tmp_path, monkeypatch, capsys
):
    """C2: Claude Code folds '.' to '-' when it encodes a cwd, so a dot-named
    directory's transcripts are on disk under a name the literal glob cannot
    see. Verified live from /Users/<me>/.openclaw: 17 JSONLs exist, the literal
    glob found 0, and the resolver answered 'unknown' for an active session.
    """
    here = tmp_path / ".openclaw"
    here.mkdir()
    monkeypatch.chdir(here)
    cwd = os.getcwd()

    sid = "eeeeeee-1111-2222-3333-444444444444"
    encoded = cwd.replace("/", "-").replace(".", "-").replace("_", "-")
    d = isolated_home / ".claude" / "projects" / encoded
    d.mkdir(parents=True)
    (d / f"{sid}.jsonl").write_text(_jsonl_line(cwd), encoding="utf-8")

    assert obsidian_utils._resolve_session_id() == sid
    # The directory IS one of this cwd's encodings, so nothing is announced.
    assert "WARN" not in capsys.readouterr().err


def test_glob_project_jsonls_unions_the_folded_variant_instead_of_falling_back(
    isolated_home, tmp_path, monkeypatch
):
    """S9: the folded glob used to run only when the literal one came up EMPTY.

    Here the literal glob matches exactly ONE directory — an unrelated repo —
    so the fallback never fired and the sole-match leniency returned that
    stranger as fact. Both globs must run and their results be UNIONed.
    """
    here = tmp_path / "ws" / "a_b"
    here.mkdir(parents=True)
    monkeypatch.chdir(here)
    cwd = os.getcwd()

    projects = isolated_home / ".claude" / "projects"
    stranger = projects / "-Users-x-dev-vendor-a_b"       # literal glob only
    mine = projects / cwd.replace("/", "-").replace("_", "-")  # folded glob only
    stranger.mkdir(parents=True)
    mine.mkdir(parents=True)
    stranger_sid = "fffffff-1111-2222-3333-444444444444"
    my_sid = "9999999-1111-2222-3333-444444444444"
    (stranger / f"{stranger_sid}.jsonl").write_text("{}\n", encoding="utf-8")
    (mine / f"{my_sid}.jsonl").write_text("{}\n", encoding="utf-8")

    assert obsidian_utils._try_slow_jsonl_glob("a_b") == my_sid


def test_transcript_cwd_wins_over_the_encoding_guess_on_a_fold_collision(
    isolated_home, monkeypatch
):
    """S7: over-generating encodings is NOT free.

    cwd `/Users/x/dev/a_b` and an unrelated repo at `/Users/x/dev/a-b` collide:
    the stranger's real project-dir name is byte-identical to one of our
    generated fold variants, so the encoding pre-filter keeps it and its newer
    session wins the mtime max. The transcripts' own `cwd` field — which Claude
    Code records on every line — settles it as fact rather than inference.

    cwd is synthesized rather than taken from tmp_path because pytest's tmp
    directories carry the test name, underscores and all, and folding those
    would destroy the very collision under test.
    """
    import time

    our_cwd = "/Users/x/dev/a_b"
    their_cwd = "/Users/x/dev/a-b"
    monkeypatch.setattr(os, "getcwd", lambda: our_cwd)

    projects = isolated_home / ".claude" / "projects"
    dir_ours = projects / our_cwd.replace("/", "-")
    dir_theirs = projects / their_cwd.replace("/", "-")
    assert dir_theirs.name == our_cwd.replace("/", "-").replace("_", "-"), (
        "fixture premise: their real dir name equals one of our fold variants"
    )
    dir_ours.mkdir(parents=True)
    dir_theirs.mkdir(parents=True)

    my_sid = "1212121-1111-2222-3333-444444444444"
    their_sid = "3434343-1111-2222-3333-444444444444"
    mine = dir_ours / f"{my_sid}.jsonl"
    stranger = dir_theirs / f"{their_sid}.jsonl"
    mine.write_text(_jsonl_line(our_cwd), encoding="utf-8")
    stranger.write_text(_jsonl_line(their_cwd), encoding="utf-8")
    now = time.time()
    os.utime(mine, (now - 3600, now - 3600))
    os.utime(stranger, (now, now))  # strictly newer, and kept by the pre-filter

    assert obsidian_utils._try_slow_jsonl_glob("a_b") == my_sid


def test_transcript_arbitration_ignores_a_transcript_with_no_cwd_field(
    isolated_home, tmp_path, monkeypatch
):
    """"Cannot tell" must not read as "not ours": a transcript with no usable
    cwd leaves its directory to the encoding pre-filter, which still keeps it.
    """
    here = tmp_path / "ws" / "docs"
    here.mkdir(parents=True)
    monkeypatch.chdir(here)
    cwd = os.getcwd()

    projects = isolated_home / ".claude" / "projects"
    mine = projects / cwd.replace("/", "-")
    stranger = projects / "-Users-x-dev-other-docs"
    mine.mkdir(parents=True)
    stranger.mkdir(parents=True)
    my_sid = "5656565-1111-2222-3333-444444444444"
    (mine / f"{my_sid}.jsonl").write_text("not json at all\n", encoding="utf-8")
    (stranger / "7878787-1111-2222-3333-444444444444.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )

    assert obsidian_utils._try_slow_jsonl_glob("docs") == my_sid


def test_transcript_cwd_reads_only_the_head_of_a_huge_transcript(tmp_path):
    """The S7 read is bounded: a transcript larger than the window still yields
    its cwd from the first line, and the window never grows with the file."""
    big = tmp_path / "huge.jsonl"
    with open(big, "w", encoding="utf-8") as f:
        f.write(_jsonl_line("/Users/x/dev/repo"))
        f.write(json.dumps({"pad": "z" * 500_000}) + "\n")
    assert obsidian_utils._transcript_cwd(str(big)) == "/Users/x/dev/repo"
    # A cwd that only appears past the window is deliberately not found.
    late = tmp_path / "late.jsonl"
    with open(late, "w", encoding="utf-8") as f:
        f.write(json.dumps({"pad": "z" * 200_000}) + "\n")
        f.write(_jsonl_line("/Users/x/dev/repo"))
    assert obsidian_utils._transcript_cwd(str(late)) is None


def test_ambiguous_glob_warns_once_not_once_per_note(
    isolated_home, tmp_path, monkeypatch, capsys
):
    """I3: the refusal WARN sits on a path _get_session_id_fast() re-runs once
    per NOTE (read_note_metadata calls it before its own cache lookup), so a
    /check-items sweep over a few hundred notes emitted a few hundred copies,
    all of them into the model's context. One per distinct ambiguity.
    """
    here = tmp_path / "elsewhere" / "docs"
    here.mkdir(parents=True)
    monkeypatch.chdir(here)

    projects = isolated_home / ".claude" / "projects"
    for name, sid in (
        ("-Users-x-dev-one-workspace-docs", "1111111-aaaa-bbbb-cccc-dddddddddddd"),
        ("-Users-x-dev-two-workspace-docs", "2222222-aaaa-bbbb-cccc-dddddddddddd"),
    ):
        d = projects / name
        d.mkdir(parents=True)
        (d / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")

    for _ in range(200):
        assert obsidian_utils._resolve_session_id() == "unknown"

    err = capsys.readouterr().err
    assert err.count("refusing to guess") == 1, (
        f"expected exactly one refusal WARN, got {err.count('refusing to guess')}"
    )


def test_sole_matching_dir_that_is_not_this_cwd_is_used_but_announced(
    isolated_home, tmp_path, monkeypatch, capsys
):
    """S6: the sole-match leniency stays (restricting it broke 11 pre-existing
    tests, and an unobserved encoding would strand a correct match) — but with
    a known, encodable cwd that the sole directory does not match, the code has
    positive evidence of a probable mis-match and must not stay silent.
    """
    here = tmp_path / "cc-token-router"
    here.mkdir()
    monkeypatch.chdir(here)

    sid = "8888888-aaaa-bbbb-cccc-dddddddddddd"
    d = (isolated_home / ".claude" / "projects"
         / "-Users-x-dev-claude-workspace-cc-token-router")
    d.mkdir(parents=True)
    (d / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")

    assert obsidian_utils._try_slow_jsonl_glob("cc-token-router") == sid
    assert obsidian_utils._try_slow_jsonl_glob("cc-token-router") == sid
    err = capsys.readouterr().err
    assert err.count("does not encode the current cwd") == 1, err


def test_sole_matching_dir_contradicted_by_its_transcripts_is_refused(
    isolated_home, tmp_path, monkeypatch, capsys
):
    """The live ~/.openclaw case, and the reason the '.' fold needs a backstop.

    Folding '.' is correct — Claude Code really does fold it — but it turns the
    basename of `/Users/me/.openclaw` into the suffix `-openclaw`, which the
    glob then matches against `-Users-me-dev-claude-workspace-openclaw`: a
    DIFFERENT repo that happens to end in the same segment, and the only match,
    so the sole-match leniency would hand back its session as fact. Verified on
    this machine: the dot-encoded dir for ~/.openclaw exists with 0 transcripts,
    while those 17 transcripts say `cwd: /Users/me/dev/claude_workspace/openclaw`
    on every line. Proof beats leniency — refuse, and say why.
    """
    here = tmp_path / ".openclaw"
    here.mkdir()
    monkeypatch.chdir(here)

    stranger = (isolated_home / ".claude" / "projects"
                / "-Users-x-dev-claude-workspace-openclaw")
    stranger.mkdir(parents=True)
    (stranger / "7171717-1111-2222-3333-444444444444.jsonl").write_text(
        _jsonl_line("/Users/x/dev/claude_workspace/openclaw"), encoding="utf-8"
    )

    assert obsidian_utils._resolve_session_id() == "unknown"
    err = capsys.readouterr().err
    assert "refusing to use it" in err, err
    assert "/Users/x/dev/claude_workspace/openclaw" in err


def test_sole_matching_dir_confirmed_by_its_transcripts_is_kept_silently(
    isolated_home, tmp_path, monkeypatch, capsys
):
    """The mirror image: a directory whose name is none of the encodings we
    generate (a symlinked checkout, a future CC scheme) but whose transcripts
    name this exact cwd. Proof keeps it, and there is nothing to warn about.
    """
    here = tmp_path / "weird-project"
    here.mkdir()
    monkeypatch.chdir(here)
    cwd = os.getcwd()

    sid = "6262626-1111-2222-3333-444444444444"
    d = (isolated_home / ".claude" / "projects"
         / "-some-entirely-unguessable-encoding-of-weird-project")
    d.mkdir(parents=True)
    (d / f"{sid}.jsonl").write_text(_jsonl_line(cwd), encoding="utf-8")

    assert obsidian_utils._try_slow_jsonl_glob("weird-project") == sid
    assert "WARN" not in capsys.readouterr().err


def test_resolve_session_id_cwd_gone_with_env_dir_still_reaches_recent_bootstrap(
    isolated_home, monkeypatch
):
    """I5: gating layer 4 on 'basename is None' narrowed #105 more than the
    docs claimed. CLAUDE_PROJECT_DIR is only consulted AFTER os.getcwd() raised
    — so a basename from it already means cwd is gone, which is #105's case —
    and hooks are exactly where Claude Code sets that variable. With layer 3
    missing (a dot-named worktree with no transcripts of its own), develop
    recovered the sid here and the first fix returned 'unknown'.
    """
    _redirect_secure_paths(monkeypatch, isolated_home)
    sid = _unique_sid()
    _seed_bootstrap(isolated_home, "deleted-worktree", sid)

    def _raise(*a, **kw):
        raise FileNotFoundError("cwd deleted")

    monkeypatch.setattr(os, "getcwd", _raise)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/Users/x/dev/.hidden-worktree")

    assert obsidian_utils._resolve_session_id() == sid


def test_resolve_session_id_readable_cwd_still_refuses_the_recent_bootstrap(
    isolated_home, tmp_path, monkeypatch
):
    """The other side of I5's gate: a READABLE cwd (the actual #260 bug) must
    still never reach the cross-project scan, however the basename was spelled.
    """
    _redirect_secure_paths(monkeypatch, isolated_home)
    _seed_bootstrap(isolated_home, "some-other-project", _unique_sid())
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/Users/x/dev/whatever")

    here = tmp_path / "pitch" / "docs"
    here.mkdir(parents=True)
    monkeypatch.chdir(here)

    assert obsidian_utils._resolve_session_id() == "unknown"


def test_get_session_context_announces_an_unresolvable_session_once(
    isolated_home, tmp_path, monkeypatch, capsys
):
    """I4: 'unknown' is the most common exit from the resolver and was the only
    one with no diagnostic. Downstream, /retro prints "no prior-session evidence
    found", asserting a fact about the VAULT when the truth is a fact about the
    RESOLVER — the user cannot tell a fresh session from a resolution failure.
    """
    _redirect_secure_paths(monkeypatch, isolated_home)
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: "unknown")

    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    here = tmp_path / "live-project"
    here.mkdir()
    monkeypatch.chdir(here)

    for _ in range(5):
        ctx = obsidian_utils.get_session_context(str(vault), "claude-sessions")
        assert ctx["session_id"] == "unknown"

    err = capsys.readouterr().err
    assert err.count("could not identify the current session") == 1, err
    assert os.getcwd() in err
    assert "session-scoped evidence" in err


# ─── #330 task 1: allow_env plumbing (no behavior yet) ─────────────────

def test_isolate_harness_session_id_globally_clears_the_real_value():
    """Proves the autouse fixture in conftest.py works: the suite runs inside
    a live Claude Code session, so CLAUDE_CODE_SESSION_ID is set in pytest's
    own environment unless something clears it per test (#330)."""
    assert "CLAUDE_CODE_SESSION_ID" not in os.environ


def test_resolve_session_id_allow_env_false_ignores_env_allow_env_true_uses_it(
    isolated_home, monkeypatch, tmp_path
):
    """#330 task 1 landed this as an inertness test ("allow_env has no effect
    yet"). Task 2 wires the actual read, which makes that premise false by
    design — updated here to assert the now-live split instead: allow_env=False
    must still ignore a well-formed env var and resolve via the scan (used by
    _slow_path_newest_sid / check_hook_status, which must not validate the env
    var against itself), while allow_env=True must now return it."""
    sid = _unique_sid()
    project = "allow-env-inert-proj"

    _seed_bootstrap(isolated_home, project, sid)
    cc_dir = isolated_home / ".claude" / "projects" / f"-Users-test-{project}"
    cc_dir.mkdir(parents=True, exist_ok=True)
    (cc_dir / f"{sid}.jsonl").write_text("{}\n")

    target = tmp_path / project
    target.mkdir()
    monkeypatch.chdir(target)

    bdir = isolated_home / ".claude" / "obsidian-brain"
    monkeypatch.setattr(
        obsidian_utils, "_BOOTSTRAP_PREFIX", str(bdir) + "/sid-"
    )

    # The env var must hold a DIFFERENT, well-formed sid than the one the
    # scan layers resolve — otherwise neither assertion below could tell a
    # live layer from a dead one. Verified by mutation (see #330 task 2
    # verification): deleting the layer-0 block turns result_true == sid
    # (RED against the assertion below), proving this isn't vacuous.
    env_sid = _unique_sid()
    assert env_sid != sid
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", env_sid)

    result_false = obsidian_utils._resolve_session_id(allow_env=False)
    result_true = obsidian_utils._resolve_session_id(allow_env=True)

    assert result_false == sid
    assert result_true == env_sid


def test_slow_path_newest_sid_passes_allow_env_false(isolated_home, monkeypatch):
    """_slow_path_newest_sid must call _resolve_session_id with
    allow_env=False, mirroring allow_bootstrap=False — otherwise
    check_hook_status becomes circular once the env layer is wired (#330)."""
    captured = {}

    def _fake_resolve(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "unknown"

    monkeypatch.setattr(obsidian_utils, "_resolve_session_id", _fake_resolve)

    obsidian_utils._slow_path_newest_sid()

    assert captured["kwargs"].get("allow_bootstrap") is False
    assert captured["kwargs"].get("allow_env") is False


# ─── #330 task 2: env-var layer 0 is live ──────────────────────────────
#
# Every test below sets CLAUDE_CODE_SESSION_ID to a value that DIFFERS from
# whatever the scan layers would resolve (never the same value) — see the
# plan's "CRITICAL — tests must be non-vacuous" note. The task-1 inertness
# test (now renamed above) was vacuous for exactly the opposite reason: an
# env value equal to the scan's answer cannot distinguish "the layer ran" from
# "the layer is dead and the scan happened to agree".

def test_resolve_session_id_env_layer_wins_over_a_real_resolvable_transcript(
    isolated_home, monkeypatch, tmp_path
):
    """Layer 0: a well-formed env var wins over the mtime-scan layers, even
    when a real, resolvable transcript exists on disk for a DIFFERENT sid."""
    project = "env-layer-wins-proj"
    scan_sid = _unique_sid()
    cc_dir = isolated_home / ".claude" / "projects" / f"-Users-test-{project}"
    cc_dir.mkdir(parents=True, exist_ok=True)
    (cc_dir / f"{scan_sid}.jsonl").write_text("{}\n")

    target = tmp_path / project
    target.mkdir()
    monkeypatch.chdir(target)

    env_sid = _unique_sid()
    assert env_sid != scan_sid
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", env_sid)

    assert obsidian_utils._resolve_session_id() == env_sid


def test_resolve_session_id_env_layer_short_circuits_before_any_scan(
    isolated_home, monkeypatch
):
    """No mtime scan runs when the env layer resolves: both scan entry
    points are monkeypatched to raise, and resolution must still succeed by
    returning the env value untouched."""
    def _boom(*a, **kw):
        raise AssertionError("scan layer ran despite a valid env var")

    monkeypatch.setattr(obsidian_utils, "_try_bootstrap_fast_path", _boom)
    monkeypatch.setattr(obsidian_utils, "_try_slow_jsonl_glob", _boom)

    env_sid = _unique_sid()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", env_sid)

    assert obsidian_utils._resolve_session_id() == env_sid


@pytest.mark.parametrize(
    "bad_env_sid", ["../../etc/passwd", "has space", "", "   "]
)
def test_resolve_session_id_malformed_env_falls_through_to_scan(
    isolated_home, monkeypatch, tmp_path, bad_env_sid
):
    """A malformed CLAUDE_CODE_SESSION_ID is never trusted — resolution falls
    through to the existing scan layers and returns their answer, never
    raising."""
    project = "malformed-env-proj"
    scan_sid = _unique_sid()
    cc_dir = isolated_home / ".claude" / "projects" / f"-Users-test-{project}"
    cc_dir.mkdir(parents=True, exist_ok=True)
    (cc_dir / f"{scan_sid}.jsonl").write_text("{}\n")

    target = tmp_path / project
    target.mkdir()
    monkeypatch.chdir(target)

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", bad_env_sid)

    assert obsidian_utils._resolve_session_id() == scan_sid


def test_resolve_session_id_env_absent_matches_pre_existing_scan_behavior(
    isolated_home, monkeypatch, tmp_path
):
    """No env var at all → identical to pre-#330 behavior: resolve via the
    scan layers. The autouse fixture already deletes the var for every test;
    this makes the contract explicit rather than relying on that side effect
    alone."""
    project = "env-absent-proj"
    scan_sid = _unique_sid()
    cc_dir = isolated_home / ".claude" / "projects" / f"-Users-test-{project}"
    cc_dir.mkdir(parents=True, exist_ok=True)
    (cc_dir / f"{scan_sid}.jsonl").write_text("{}\n")

    target = tmp_path / project
    target.mkdir()
    monkeypatch.chdir(target)

    assert "CLAUDE_CODE_SESSION_ID" not in os.environ
    assert obsidian_utils._resolve_session_id() == scan_sid


def test_get_session_context_stable_under_env_unstable_without_it(
    isolated_home, monkeypatch, tmp_path
):
    """Core #330 regression, reproduced directly: two consecutive
    get_session_context() calls must resolve to the SAME session while a
    competing transcript becomes the newest-mtime winner in between — this
    is the exact shape of the crossed retro note (source_session and
    source_session_note disagreeing because two calls in one note write
    resolved via two different newest-mtime winners).

    The negative control (same scenario, no env var) must resolve
    DIFFERENTLY on the second call — proving the stability comes from the
    env layer actually being consulted, not from get_session_context()'s own
    cache or from the test being trivially true either way."""
    import time

    # --- with the env var: stable across both calls -----------------
    project = "stability-env-proj"
    cc_dir = isolated_home / ".claude" / "projects" / f"-Users-test-{project}"
    cc_dir.mkdir(parents=True, exist_ok=True)
    (cc_dir / f"{_unique_sid()}.jsonl").write_text("{}\n")

    target = tmp_path / project
    target.mkdir()
    monkeypatch.chdir(target)

    env_sid = _unique_sid()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", env_sid)

    ctx1 = obsidian_utils.get_session_context()

    # Touch a competing transcript so it becomes the newest-mtime winner.
    later_sid = _unique_sid()
    later_path = cc_dir / f"{later_sid}.jsonl"
    later_path.write_text("{}\n")
    later_ts = time.time() + 3600
    os.utime(later_path, (later_ts, later_ts))

    ctx2 = obsidian_utils.get_session_context()

    assert ctx1["session_id"] == env_sid
    assert ctx2["session_id"] == env_sid
    assert ctx1["session_note_name"] == ctx2["session_note_name"]

    # --- negative control: no env var → the winner changes -----------
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    project2 = "stability-noenv-proj"
    cc_dir2 = isolated_home / ".claude" / "projects" / f"-Users-test-{project2}"
    cc_dir2.mkdir(parents=True, exist_ok=True)
    first_sid = _unique_sid()
    (cc_dir2 / f"{first_sid}.jsonl").write_text("{}\n")

    target2 = tmp_path / project2
    target2.mkdir()
    monkeypatch.chdir(target2)

    ctx3 = obsidian_utils.get_session_context()
    assert ctx3["session_id"] == first_sid

    second_sid = _unique_sid()
    second_path = cc_dir2 / f"{second_sid}.jsonl"
    second_path.write_text("{}\n")
    later_ts2 = time.time() + 3600
    os.utime(second_path, (later_ts2, later_ts2))

    ctx4 = obsidian_utils.get_session_context()
    assert ctx4["session_id"] == second_sid
    assert ctx3["session_id"] != ctx4["session_id"], (
        "negative control did not reproduce instability — the test setup "
        "cannot distinguish env-layer stability from an unrelated cache hit"
    )


def test_check_hook_status_ignores_env_var_uses_jsonl_scan(
    isolated_home, monkeypatch, tmp_path
):
    """check_hook_status() must stay allow_env=False end-to-end: a valid,
    well-formed env var pointing at a sid with NO transcript must not leak
    into the health check via _slow_path_newest_sid — it must keep reporting
    based on the JSONL scan, exactly as if the env var were unset."""
    project = "check-hook-status-proj"
    bootstrap_sid = _unique_sid()
    _seed_bootstrap(isolated_home, project, bootstrap_sid)

    target = tmp_path / project
    target.mkdir()
    monkeypatch.chdir(target)

    bdir = isolated_home / ".claude" / "obsidian-brain"
    monkeypatch.setattr(obsidian_utils, "_BOOTSTRAP_PREFIX", str(bdir) + "/sid-")

    # No JSONL transcripts exist anywhere for this project — the scan must
    # return 'unknown'. env_sid deliberately differs from bootstrap_sid so an
    # env-var leak would flip the reported "ok" state and message, rather
    # than accidentally reproducing the correct answer.
    env_sid = _unique_sid()
    assert env_sid != bootstrap_sid
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", env_sid)

    result = obsidian_utils.check_hook_status()

    assert result["current_sid"] == "unknown"
    assert result["ok"] is False
    assert "No session files found" in result["message"]


def test_resolve_session_id_env_no_transcript_warns_once_but_still_returns_it(
    isolated_home, monkeypatch, tmp_path, capsys
):
    """Well-formed env var with no matching transcript on disk yet is still
    trusted (format-only gate — a brand-new session has no transcript yet),
    but a one-time WARN is emitted so a genuinely stale/misdirected env var
    is visible without being fatal. Called twice to prove the WARN fires
    once, not once per call."""
    project = "env-no-transcript-proj"
    target = tmp_path / project
    target.mkdir()
    monkeypatch.chdir(target)

    env_sid = _unique_sid()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", env_sid)

    # No ~/.claude/projects/*<project>/ directory exists at all yet.
    result1 = obsidian_utils._resolve_session_id()
    result2 = obsidian_utils._resolve_session_id()

    assert result1 == env_sid
    assert result2 == env_sid

    err = capsys.readouterr().err
    assert err.count("no Claude Code transcript") == 1, err
    assert env_sid in err


def test_resolve_session_id_env_with_transcript_present_no_warn(
    isolated_home, monkeypatch, tmp_path, capsys
):
    """No WARN when the env sid's own transcript already exists — only the
    'no transcript yet' case is diagnostic-worthy."""
    project = "env-with-transcript-proj"
    env_sid = _unique_sid()
    cc_dir = isolated_home / ".claude" / "projects" / f"-Users-test-{project}"
    cc_dir.mkdir(parents=True, exist_ok=True)
    (cc_dir / f"{env_sid}.jsonl").write_text("{}\n")

    target = tmp_path / project
    target.mkdir()
    monkeypatch.chdir(target)

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", env_sid)

    assert obsidian_utils._resolve_session_id() == env_sid
    err = capsys.readouterr().err
    assert "no Claude Code transcript" not in err
