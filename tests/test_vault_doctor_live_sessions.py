"""#438: the doctor skips sessions that are still running; snapshots escape
wikilinks and name their parent by the session's first-seen date."""

from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path

from obsidian_context_snapshot import _build_snapshot_body, _build_snapshot_note
from scripts.vault_doctor_checks import session_is_live, snapshot_integrity, snapshot_migration

SID = "66666666-6666-6666-6666-666666666666"


def _transcript(sid=SID, age_s=60):
    d = Path.home() / ".claude" / "projects" / "-Users-x-demo"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("{}\n", encoding="utf-8")
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


def _log_session_end(sid=SID, name="obsidian-brain-hook.log", ts=None):
    if ts is None:
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    p = Path.home() / ".claude" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(f"{ts} SessionEnd project=demo sid={sid[:8]} "
                 "outcome=OK_RAW_NOTE_ONLY msgs=9\n")


# --- session_is_live ------------------------------------------------------------


def test_recent_transcript_without_session_end_is_live():
    _transcript(age_s=60)
    assert session_is_live(SID) is True


def test_transcript_older_than_the_window_is_not_live():
    _transcript(age_s=3601)
    assert session_is_live(SID) is False


def test_transcript_just_inside_the_window_is_live():
    p = _transcript(age_s=0)
    assert session_is_live(SID, str(p), now=os.path.getmtime(p) + 3600) is True
    assert session_is_live(SID, str(p), now=os.path.getmtime(p) + 3601) is False


def test_session_end_in_current_log_ends_liveness():
    _transcript(age_s=60)
    _log_session_end()
    assert session_is_live(SID) is False


def test_session_end_in_rotated_log_ends_liveness():
    _transcript(age_s=60)
    _log_session_end(name="obsidian-brain-hook.log.1")
    assert session_is_live(SID) is False


def test_another_sessions_end_does_not_end_liveness():
    _transcript(age_s=60)
    _log_session_end(sid="77777777-7777-7777-7777-777777777777")
    assert session_is_live(SID) is True


def test_session_end_before_a_resume_does_not_end_liveness():
    # The session ended, was resumed and wrote again: the old SessionEnd
    # (logged an hour before the last write) belongs to the earlier run.
    _transcript(age_s=60)
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    _log_session_end(ts=old.isoformat(timespec="seconds"))
    assert session_is_live(SID) is True


def test_session_end_within_the_slack_ends_liveness():
    _transcript(age_s=60)
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=60 + 299)
    _log_session_end(ts=ts.isoformat(timespec="seconds"))
    assert session_is_live(SID) is False


def test_session_end_with_an_unparseable_time_ends_liveness():
    _transcript(age_s=60)
    _log_session_end(ts="garbage")
    assert session_is_live(SID) is False


def test_unknowns_are_not_live(tmp_path):
    assert session_is_live("") is False
    assert session_is_live(SID) is False  # no transcript anywhere
    assert session_is_live(SID, str(tmp_path / "gone.jsonl")) is False


# --- snapshot checks --------------------------------------------------------------


def _write_snapshot(path, sid, backlink_stem):
    path.write_text(
        f"---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: {sid}\n"
        f"project: demo\ntrigger: compact\nstatus: summarized\n"
        f'source_session_note: "[[{backlink_stem}]]"\n---\n\n# Snap\n\n## Summary\nbody\n',
        encoding="utf-8",
    )


def _write_session(path, sid):
    path.write_text(
        f"---\ntype: claude-session\ndate: 2026-04-18\nsession_id: {sid}\n"
        f"project: demo\nstatus: summarized\n---\n\n# S\n",
        encoding="utf-8",
    )


def _checks(mod, vault, name):
    return [i for i in mod.scan(str(vault), "claude-sessions", "claude-insights", 3650)
            if i.check == name]


def test_orphan_snapshot_of_a_live_session_is_skipped(tmp_path):
    sess = tmp_path / "claude-sessions"; sess.mkdir()
    _write_snapshot(sess / "2026-04-18-demo-6666-snapshot-120000.md", SID, "2026-04-18-demo-6666")
    assert len(_checks(snapshot_integrity, tmp_path, "snapshot-orphan")) == 1
    _transcript(age_s=60)
    assert _checks(snapshot_integrity, tmp_path, "snapshot-orphan") == []


def test_missing_snapshot_list_of_a_live_session_is_skipped(tmp_path):
    sess = tmp_path / "claude-sessions"; sess.mkdir()
    _write_session(sess / "2026-04-18-demo-6666.md", SID)
    _write_snapshot(sess / "2026-04-18-demo-6666-snapshot-120000.md", SID, "2026-04-18-demo-6666")
    for mod, name in ((snapshot_integrity, "session-snapshot-list-missing"),
                      (snapshot_migration, "session-missing-snapshots-list")):
        assert len(_checks(mod, tmp_path, name)) == 1, name
    _transcript(age_s=60)
    for mod, name in ((snapshot_integrity, "session-snapshot-list-missing"),
                      (snapshot_migration, "session-missing-snapshots-list")):
        assert _checks(mod, tmp_path, name) == [], name
    _log_session_end()
    for mod, name in ((snapshot_integrity, "session-snapshot-list-missing"),
                      (snapshot_migration, "session-missing-snapshots-list")):
        assert len(_checks(mod, tmp_path, name)) == 1, name


# --- snapshot hook ----------------------------------------------------------------

_META = {"project": "demo", "git_branch": "main", "duration_minutes": 5,
         "errors": ["if [[ $X == y ]]; then"], "files_touched": []}


def test_snapshot_body_escapes_wikilinks():
    user = ["check [[ $X == y ]] and [[2026-10-06-demo-47c0]]"]
    asst = ["see [[some-note]]"]
    body = _build_snapshot_body(user, _META, "compact", assistant_msgs=asst)
    assert "[[" not in body
    assert r"\[\[ $X == y ]]" in body
    assert r"\[\[some-note]]" in body


def test_snapshot_frontmatter_backlink_stays_a_real_link():
    note = _build_snapshot_note(SID, _META, "body", "compact", date_str="2026-10-20")
    assert 'source_session_note: "[[' in note


def test_snapshot_backlink_uses_the_first_seen_date():
    marker = Path.home() / ".claude" / "obsidian-brain" / "sessions" / f"{SID}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"first_seen_date": "2026-10-19"}), encoding="utf-8")
    os.chmod(marker, 0o600)
    note = _build_snapshot_note(SID, _META, "body", "compact", date_str="2026-10-20")
    assert 'source_session_note: "[[2026-10-19-demo-' in note
    assert "date: 2026-10-20" in note
