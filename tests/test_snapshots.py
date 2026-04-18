import datetime
import json
import re
from pathlib import Path

import hooks.obsidian_context_snapshot as snap
from hooks.obsidian_utils import read_note_metadata


def test_snapshot_frontmatter_has_status_and_source_session_note(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))

    now = datetime.datetime(2026, 4, 18, 14, 30, 27)
    session_id = "abc-def-ghi"
    metadata = {"project": "demo", "git_branch": "develop"}

    note = snap._build_snapshot_note(session_id, metadata, body="## What was happening\nsome body\n", trigger="compact")

    # Frontmatter assertions
    assert "\nstatus: auto-logged\n" in note
    # Parent filename is <YYYY-MM-DD>-<project>-<sid4>
    assert re.search(r"\nsource_session_note: \"\[\[\d{4}-\d{2}-\d{2}-demo-[a-f0-9]{4}\]\]\"\n", note)


def test_snapshot_filename_includes_hhmmss(monkeypatch):
    # Freeze datetime.datetime.now so _run() produces a deterministic HHMMSS
    class FrozenDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 18, 14, 30, 27)
    monkeypatch.setattr(snap.datetime, "datetime", FrozenDatetime)

    from hooks.obsidian_utils import make_filename
    fname = make_filename("2026-04-18", "demo", "abc-def-ghi", suffix=f"-snapshot-{FrozenDatetime.now():%H%M%S}")
    assert fname.endswith("-snapshot-143027.md")
    assert re.match(r"2026-04-18-demo-[a-f0-9]{4}-snapshot-143027\.md$", fname)
