import datetime
import io
import json
import re

import hooks.obsidian_context_snapshot as snap


def test_snapshot_frontmatter_has_status_and_source_session_note():
    session_id = "abc-def-ghi"
    metadata = {"project": "demo", "git_branch": "develop"}
    note = snap._build_snapshot_note(
        session_id,
        metadata,
        body="## What was happening\nsome body\n",
        trigger="compact",
    )
    assert "\nstatus: auto-logged\n" in note
    assert re.search(
        r'\nsource_session_note: "\[\[\d{4}-\d{2}-\d{2}-demo-[a-f0-9]{4}\]\]"\n',
        note,
    )


def test_run_writes_file_with_hhmmss_suffix(tmp_path, monkeypatch):
    """_run() integrates datetime.now() into the snapshot filename."""

    # Freeze datetime so the suffix is deterministic
    class FrozenDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 18, 14, 30, 27)

    monkeypatch.setattr(snap.datetime, "datetime", FrozenDatetime)

    # Set up a vault
    vault = tmp_path / "vault"
    sessions = vault / "claude-sessions"
    sessions.mkdir(parents=True)

    # Stub load_config — the real one reads ~/.claude/obsidian-brain-config.json
    # via Path.home() bound at import time, so $HOME monkeypatching won't reach it.
    monkeypatch.setattr(
        snap,
        "load_config",
        lambda: {
            "vault_path": str(vault),
            "sessions_folder": "claude-sessions",
            "snapshot_on_compact": True,
        },
    )

    # transcript_path containment check uses os.path.expanduser("~/.claude/projects")
    # which still reads $HOME at call time, so point it at tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))

    # Stub transcript-parsing helpers so _run() doesn't need a real JSONL
    monkeypatch.setattr(
        snap,
        "read_transcript",
        lambda path: [{"type": "user", "message": {"content": "hello"}}],
    )
    monkeypatch.setattr(snap, "extract_user_messages", lambda msgs: ["hello"])
    monkeypatch.setattr(
        snap,
        "extract_session_metadata",
        lambda msgs, cwd: {
            "project": "demo",
            "git_branch": "develop",
            "duration_minutes": 1,
        },
    )

    # transcript_path must sit inside ~/.claude/projects/ to pass the containment check
    projects_dir = tmp_path / ".claude" / "projects" / "demo"
    projects_dir.mkdir(parents=True)
    transcript = projects_dir / "sess.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")

    stdin_json = json.dumps(
        {
            "session_id": "abc-def-ghi",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
            "source": "compact",
        }
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_json))

    snap._run()

    # Exactly one snapshot note should have been written with the HHMMSS suffix
    written = list(sessions.glob("*.md"))
    assert len(written) == 1
    assert written[0].name.endswith("-snapshot-143027.md")
    assert re.match(
        r"2026-04-18-demo-[a-f0-9]{4}-snapshot-143027\.md$",
        written[0].name,
    )
