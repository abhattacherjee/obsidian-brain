import json
from pathlib import Path
from unittest.mock import patch

from hooks.obsidian_utils import find_unsummarized_notes, upgrade_unsummarized_note


def _fixture(sess_dir: Path, name: str, type_: str, status: str, session_id: str, body: str = ""):
    p = sess_dir / name
    p.write_text(
        f"---\ntype: {type_}\ndate: 2026-04-18\nsession_id: {session_id}\n"
        f"project: demo\nstatus: {status}\n---\n\n# {name}\n\n{body}",
        encoding="utf-8",
    )


def _fixture_no_type(sess_dir: Path, name: str, status: str, session_id: str):
    p = sess_dir / name
    p.write_text(
        f"---\ndate: 2026-04-18\nsession_id: {session_id}\n"
        f"project: demo\nstatus: {status}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_find_unsummarized_picks_up_snapshots(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    _fixture(sess, "2026-04-18-demo-aaaa-snapshot-140000.md", "claude-snapshot", "auto-logged", "s1")
    _fixture(sess, "2026-04-18-demo-aaaa.md", "claude-session", "auto-logged", "s1")

    result = json.loads(find_unsummarized_notes(str(vault), "claude-sessions", "demo"))
    paths = result["unsummarized"]
    names = [Path(p).name for p in paths]
    assert any("snapshot-140000" in n for n in names)
    assert any(n == "2026-04-18-demo-aaaa.md" for n in names)


def test_find_unsummarized_skips_summarized_snapshots(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    _fixture(sess, "2026-04-18-demo-bbbb-snapshot-100000.md", "claude-snapshot", "summarized", "s2")

    result = json.loads(find_unsummarized_notes(str(vault), "claude-sessions", "demo"))
    assert result["unsummarized"] == []


def test_find_unsummarized_orders_snapshots_before_parent(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    _fixture(sess, "2026-04-18-demo-cccc.md", "claude-session", "auto-logged", "s3")
    _fixture(sess, "2026-04-18-demo-cccc-snapshot-120000.md", "claude-snapshot", "auto-logged", "s3")
    _fixture(sess, "2026-04-18-demo-cccc-snapshot-090000.md", "claude-snapshot", "auto-logged", "s3")

    result = json.loads(find_unsummarized_notes(str(vault), "claude-sessions", "demo"))
    names = [Path(p).name for p in result["unsummarized"]]
    # Snapshots before parent within the same session_id group
    assert names.index("2026-04-18-demo-cccc-snapshot-090000.md") < names.index("2026-04-18-demo-cccc.md")
    assert names.index("2026-04-18-demo-cccc-snapshot-120000.md") < names.index("2026-04-18-demo-cccc.md")


def test_find_unsummarized_rejects_non_session_non_snapshot_types(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    _fixture(sess, "2026-04-18-demo-dddd.md", "claude-session", "auto-logged", "s4")
    _fixture(sess, "2026-04-18-demo-dddd-insight.md", "claude-insight", "auto-logged", "s4")

    result = json.loads(find_unsummarized_notes(str(vault), "claude-sessions", "demo"))
    names = [Path(p).name for p in result["unsummarized"]]
    assert "2026-04-18-demo-dddd.md" in names
    assert "2026-04-18-demo-dddd-insight.md" not in names


def test_find_unsummarized_keeps_legacy_notes_without_type_field(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    _fixture_no_type(sess, "2026-04-18-demo-eeee-legacy.md", "auto-logged", "s5")

    result = json.loads(find_unsummarized_notes(str(vault), "claude-sessions", "demo"))
    names = [Path(p).name for p in result["unsummarized"]]
    assert "2026-04-18-demo-eeee-legacy.md" in names


def test_snapshot_routes_through_snapshot_prompt(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    snap_path = sess / "2026-04-18-demo-dddd-snapshot-140000.md"
    snap_path.write_text(
        "---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: s4\n"
        "project: demo\ntrigger: compact\nstatus: auto-logged\n---\n\n"
        "# Context Snapshot: demo\n\n## What was happening\nWorking on X.\n\n"
        "## Last messages (raw)\n**User:** hi\n**Assistant:** hello\n",
        encoding="utf-8",
    )

    calls = []

    def fake_snapshot_summary(*args, **kwargs):
        calls.append(("snapshot", args, kwargs))
        return (
            "## Summary\nIn-flight work on X interrupted by /compact.\n\n"
            "## Key context that may be lost (summary)\n- Open decision: approach A vs B\n"
        )

    def fake_session_summary(*args, **kwargs):
        calls.append(("session", args, kwargs))
        return "## Summary\nshould-not-be-used\n"

    with patch("hooks.obsidian_utils.generate_snapshot_summary", fake_snapshot_summary), \
         patch("hooks.obsidian_utils.generate_summary", fake_session_summary):
        result = upgrade_unsummarized_note(str(snap_path), str(vault), "claude-sessions", "demo")

    assert not result.startswith("Failed"), result
    types_called = [c[0] for c in calls]
    assert types_called == ["snapshot"]
    # File now contains the snapshot-shaped summary
    assert "## Key context that may be lost (summary)" in snap_path.read_text(encoding="utf-8")
