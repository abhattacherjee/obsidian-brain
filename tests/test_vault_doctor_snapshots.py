from pathlib import Path
from scripts.vault_doctor_checks import snapshot_integrity


def _write_snapshot(path, session_id, backlink_stem, status="summarized", has_summary=True):
    summary_body = "## Summary\nbody\n" if has_summary else ""
    path.write_text(
        f"---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: {session_id}\n"
        f"project: demo\ntrigger: compact\nstatus: {status}\n"
        f'source_session_note: "[[{backlink_stem}]]"\n---\n\n# Snap\n\n{summary_body}',
        encoding="utf-8",
    )


def _write_session(path, session_id, snapshots: list[str] | None = None):
    snap_block = ""
    if snapshots:
        snap_block = "snapshots:\n" + "\n".join(f'  - "{s}"' for s in snapshots) + "\n"
    path.write_text(
        f"---\ntype: claude-session\ndate: 2026-04-18\nsession_id: {session_id}\n"
        f"project: demo\n{snap_block}status: summarized\n---\n\n# S\n",
        encoding="utf-8",
    )


def test_orphan_snapshot_detected(tmp_path):
    vault = tmp_path; sess = vault / "claude-sessions"; sess.mkdir()
    _write_snapshot(sess / "2026-04-18-demo-aa-snapshot-120000.md", "missing", "nothing")
    issues = snapshot_integrity.scan(str(vault), "claude-sessions", "claude-insights", 30)
    orphans = [i for i in issues if i.check == "snapshot-orphan"]
    assert len(orphans) == 1


def test_broken_backlink_is_rebuilt_when_session_findable(tmp_path):
    vault = tmp_path; sess = vault / "claude-sessions"; sess.mkdir()
    _write_session(sess / "2026-04-18-demo-aa.md", "s1")
    _write_snapshot(sess / "2026-04-18-demo-aa-snapshot-120000.md", "s1", "wrong-stem")
    issues = snapshot_integrity.scan(str(vault), "claude-sessions", "claude-insights", 30)
    broken = [i for i in issues if i.check == "snapshot-broken-backlink"]
    assert len(broken) == 1
    assert "2026-04-18-demo-aa" in broken[0].proposed_source


def test_session_missing_snapshots_list_is_fixed(tmp_path):
    vault = tmp_path; sess = vault / "claude-sessions"; sess.mkdir()
    _write_session(sess / "2026-04-18-demo-bb.md", "s2")
    _write_snapshot(sess / "2026-04-18-demo-bb-snapshot-100000.md", "s2", "2026-04-18-demo-bb")
    _write_snapshot(sess / "2026-04-18-demo-bb-snapshot-140000.md", "s2", "2026-04-18-demo-bb")
    issues = snapshot_integrity.scan(str(vault), "claude-sessions", "claude-insights", 30)
    missing = [i for i in issues if i.check == "session-snapshot-list-missing"]
    assert len(missing) == 1

    import os
    backup = tmp_path / "backup"
    snapshot_integrity.apply(missing, str(backup))

    text = (sess / "2026-04-18-demo-bb.md").read_text(encoding="utf-8")
    assert 'snapshots:' in text
    assert '2026-04-18-demo-bb-snapshot-100000' in text
    assert '2026-04-18-demo-bb-snapshot-140000' in text


def test_summary_status_mismatch_auto_fixes(tmp_path):
    vault = tmp_path; sess = vault / "claude-sessions"; sess.mkdir()
    _write_session(sess / "2026-04-18-demo-cc.md", "s3")
    # Status says auto-logged but ## Summary is already populated
    _write_snapshot(sess / "2026-04-18-demo-cc-snapshot-090000.md", "s3",
                    "2026-04-18-demo-cc", status="auto-logged", has_summary=True)
    issues = snapshot_integrity.scan(str(vault), "claude-sessions", "claude-insights", 30)
    mismatches = [i for i in issues if i.check == "snapshot-summary-status-mismatch"]
    assert len(mismatches) == 1

    snapshot_integrity.apply(mismatches, str(tmp_path / "backup"))
    text = (sess / "2026-04-18-demo-cc-snapshot-090000.md").read_text(encoding="utf-8")
    assert "status: summarized" in text
    assert "status: auto-logged" not in text


def test_idempotent_scan_after_apply(tmp_path):
    """Running scan again after apply returns no issues."""
    vault = tmp_path; sess = vault / "claude-sessions"; sess.mkdir()
    _write_session(sess / "2026-04-18-demo-bb.md", "s2")
    _write_snapshot(sess / "2026-04-18-demo-bb-snapshot-100000.md", "s2", "2026-04-18-demo-bb")

    issues = snapshot_integrity.scan(str(vault), "claude-sessions", "claude-insights", 30)
    fixable = [i for i in issues if not i.extra.get("unresolved")]
    snapshot_integrity.apply(fixable, str(tmp_path / "backup"))
    reissues = snapshot_integrity.scan(str(vault), "claude-sessions", "claude-insights", 30)
    assert len(reissues) == 0
