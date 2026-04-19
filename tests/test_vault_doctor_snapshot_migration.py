import datetime
import os
from pathlib import Path

from scripts.vault_doctor_checks import snapshot_migration


def _write_legacy_snapshot(path, mtime_iso="2026-04-18T14:30:27", session_id="s1"):
    path.write_text(
        f"---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: {session_id}\n"
        "project: demo\ntrigger: compact\n---\n\n# Snap\n\n## What was happening\nx\n",
        encoding="utf-8",
    )
    dt = datetime.datetime.fromisoformat(mtime_iso)
    os.utime(path, (dt.timestamp(), dt.timestamp()))


def _write_session(path, session_id="s1"):
    path.write_text(
        f"---\ntype: claude-session\ndate: 2026-04-18\nsession_id: {session_id}\n"
        "project: demo\nstatus: summarized\n---\n\n# S\n",
        encoding="utf-8",
    )


def test_legacy_filename_gets_hhmmss_suffix(tmp_path):
    sess = tmp_path / "claude-sessions"; sess.mkdir()
    _write_legacy_snapshot(sess / "2026-04-18-demo-aa-snapshot.md")
    _write_session(sess / "2026-04-18-demo-aa.md")
    issues = snapshot_migration.scan(str(tmp_path), "claude-sessions", "claude-insights", 3650)
    legacy = [i for i in issues if i.check == "snapshot-legacy-filename"]
    assert len(legacy) == 1
    assert "snapshot-143027" in legacy[0].proposed_source

    results = snapshot_migration.apply(legacy, str(tmp_path / "backup"))
    assert all(r.status == "applied" for r in results)
    assert (sess / "2026-04-18-demo-aa-snapshot-143027.md").exists()
    assert not (sess / "2026-04-18-demo-aa-snapshot.md").exists()


def test_legacy_filename_collision_skips_with_warning(tmp_path):
    sess = tmp_path / "claude-sessions"; sess.mkdir()
    _write_legacy_snapshot(sess / "2026-04-18-demo-aa-snapshot.md")
    _write_legacy_snapshot(sess / "2026-04-18-demo-aa-snapshot-143027.md")
    issues = snapshot_migration.scan(str(tmp_path), "claude-sessions", "claude-insights", 3650)
    legacy = [i for i in issues if i.check == "snapshot-legacy-filename"]
    assert any(i.extra.get("unresolved") for i in legacy)


def test_missing_status_auto_logged_when_no_summary(tmp_path):
    sess = tmp_path / "claude-sessions"; sess.mkdir()
    _write_legacy_snapshot(sess / "2026-04-18-demo-bb-snapshot-120000.md")
    issues = snapshot_migration.scan(str(tmp_path), "claude-sessions", "claude-insights", 3650)
    miss = [i for i in issues if i.check == "snapshot-missing-status"]
    assert len(miss) == 1
    snapshot_migration.apply(miss, str(tmp_path / "backup"))
    text = (sess / "2026-04-18-demo-bb-snapshot-120000.md").read_text(encoding="utf-8")
    assert "status: auto-logged" in text


def test_missing_status_summarized_when_summary_exists(tmp_path):
    sess = tmp_path / "claude-sessions"; sess.mkdir()
    p = sess / "2026-04-18-demo-cc-snapshot-130000.md"
    p.write_text(
        "---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: s3\nproject: demo\n"
        "trigger: compact\n---\n\n# Snap\n\n## Summary\nreal summary body\n",
        encoding="utf-8",
    )
    issues = snapshot_migration.scan(str(tmp_path), "claude-sessions", "claude-insights", 3650)
    miss = [i for i in issues if i.check == "snapshot-missing-status"]
    snapshot_migration.apply(miss, str(tmp_path / "backup"))
    text = p.read_text(encoding="utf-8")
    assert "status: summarized" in text


def test_missing_backlink_populates_source_session_note(tmp_path):
    sess = tmp_path / "claude-sessions"; sess.mkdir()
    # Parent filename must match the computed stem: <date>-<slug(project)>-<sha256(sid)[:4]>
    # For session_id="s4" the hash prefix is computed deterministically.
    import hashlib as _h
    sid = "s4"
    hash4 = _h.sha256(sid.encode()).hexdigest()[:4]
    parent_stem = f"2026-04-18-demo-{hash4}"
    _write_session(sess / f"{parent_stem}.md", session_id=sid)
    p = sess / f"{parent_stem}-snapshot-110000.md"
    p.write_text(
        f"---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: {sid}\nproject: demo\n"
        "trigger: compact\nstatus: auto-logged\n---\n\n# Snap\n",
        encoding="utf-8",
    )
    issues = snapshot_migration.scan(str(tmp_path), "claude-sessions", "claude-insights", 3650)
    miss = [i for i in issues if i.check == "snapshot-missing-backlink"]
    assert len(miss) == 1
    assert not miss[0].extra.get("unresolved")  # parent exists
    snapshot_migration.apply(miss, str(tmp_path / "backup"))
    text = p.read_text(encoding="utf-8")
    assert f'source_session_note: "[[{parent_stem}]]"' in text


def test_missing_backlink_unresolved_when_parent_absent(tmp_path):
    sess = tmp_path / "claude-sessions"; sess.mkdir()
    p = sess / "2026-04-18-demo-zz-snapshot-110000.md"
    p.write_text(
        "---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: nonexistent\n"
        "project: demo\ntrigger: compact\nstatus: auto-logged\n---\n\n# Snap\n",
        encoding="utf-8",
    )
    issues = snapshot_migration.scan(str(tmp_path), "claude-sessions", "claude-insights", 3650)
    miss = [i for i in issues if i.check == "snapshot-missing-backlink"]
    assert len(miss) == 1
    assert miss[0].extra.get("unresolved")


def test_session_missing_snapshots_list_is_backfilled(tmp_path):
    sess = tmp_path / "claude-sessions"; sess.mkdir()
    _write_session(sess / "2026-04-18-demo-dd.md", session_id="s5")
    _write_legacy_snapshot(sess / "2026-04-18-demo-dd-snapshot-100000.md", session_id="s5")
    _write_legacy_snapshot(sess / "2026-04-18-demo-dd-snapshot-140000.md", session_id="s5")
    issues = snapshot_migration.scan(str(tmp_path), "claude-sessions", "claude-insights", 3650)
    miss = [i for i in issues if i.check == "session-missing-snapshots-list"]
    assert len(miss) == 1
    snapshot_migration.apply(miss, str(tmp_path / "backup"))
    text = (sess / "2026-04-18-demo-dd.md").read_text(encoding="utf-8")
    assert "snapshots:" in text
    assert "2026-04-18-demo-dd-snapshot-100000" in text
    assert "2026-04-18-demo-dd-snapshot-140000" in text


def test_wikilink_rewrite_across_vault_on_rename(tmp_path):
    """Rename should rewrite [[old-stem]] references across the vault."""
    sess = tmp_path / "claude-sessions"; sess.mkdir()
    insights = tmp_path / "claude-insights"; insights.mkdir()
    _write_legacy_snapshot(sess / "2026-04-18-demo-xx-snapshot.md")
    _write_session(sess / "2026-04-18-demo-xx.md")
    # An insight note references the legacy stem
    (insights / "ref.md").write_text(
        "---\ntype: claude-insight\n---\n\nsee [[2026-04-18-demo-xx-snapshot]] for context\n",
        encoding="utf-8",
    )
    issues = snapshot_migration.scan(str(tmp_path), "claude-sessions", "claude-insights", 3650)
    legacy = [i for i in issues if i.check == "snapshot-legacy-filename"]
    snapshot_migration.apply(legacy, str(tmp_path / "backup"))
    ref_text = (insights / "ref.md").read_text(encoding="utf-8")
    assert "[[2026-04-18-demo-xx-snapshot-143027]]" in ref_text
    assert "[[2026-04-18-demo-xx-snapshot]]" not in ref_text


def test_migration_is_idempotent(tmp_path):
    sess = tmp_path / "claude-sessions"; sess.mkdir()
    _write_legacy_snapshot(sess / "2026-04-18-demo-ee-snapshot.md", session_id="eeee")
    # Parent stem must match hash of "eeee" since _write_legacy_snapshot hardcodes project="demo"
    import hashlib as _h
    hash4 = _h.sha256(b"eeee").hexdigest()[:4]
    _write_session(sess / f"2026-04-18-demo-{hash4}.md", session_id="eeee")
    # First pass
    issues1 = snapshot_migration.scan(str(tmp_path), "claude-sessions", "claude-insights", 3650)
    snapshot_migration.apply([i for i in issues1 if not i.extra.get("unresolved")],
                             str(tmp_path / "backup"))
    # Second pass — should be empty (or only unresolved issues)
    issues2 = snapshot_migration.scan(str(tmp_path), "claude-sessions", "claude-insights", 3650)
    # All remaining issues should be unresolved (nothing fixable left)
    assert all(i.extra.get("unresolved") for i in issues2)


def test_missing_backlink_unresolved_when_sid_missing(tmp_path):
    """No session_id in frontmatter -> unresolved (cannot compute parent)."""
    sess = tmp_path / "claude-sessions"; sess.mkdir()
    p = sess / "2026-04-18-demo-ff-snapshot-100000.md"
    p.write_text(
        "---\ntype: claude-snapshot\ndate: 2026-04-18\n"
        "project: demo\ntrigger: compact\nstatus: auto-logged\n---\n\n# Snap\n",
        encoding="utf-8",
    )
    issues = snapshot_migration.scan(str(tmp_path), "claude-sessions", "claude-insights", 3650)
    miss = [i for i in issues if i.check == "snapshot-missing-backlink"]
    assert len(miss) == 1
    assert miss[0].extra.get("unresolved")
    # Apply should record unresolved status
    results = snapshot_migration.apply(miss, str(tmp_path / "backup"))
    assert results[0].status == "unresolved"


def test_scan_returns_empty_when_sessions_folder_missing(tmp_path):
    """scan() must return [] when the sessions folder does not exist."""
    # No claude-sessions dir created at tmp_path
    issues = snapshot_migration.scan(str(tmp_path), "claude-sessions", "claude-insights", 3650)
    assert issues == []


def test_scan_project_filter_excludes_other_projects(tmp_path):
    """Project filter should skip snapshots whose project differs."""
    sess = tmp_path / "claude-sessions"; sess.mkdir()
    _write_legacy_snapshot(sess / "2026-04-18-demo-gg-snapshot.md")
    # Keep project=demo but filter by 'other' — should exclude.
    issues = snapshot_migration.scan(
        str(tmp_path), "claude-sessions", "claude-insights", 3650, project="other"
    )
    assert issues == []


def test_scan_skips_notes_without_frontmatter(tmp_path):
    """A .md file without frontmatter should be silently skipped by scan()."""
    sess = tmp_path / "claude-sessions"; sess.mkdir()
    (sess / "no-frontmatter.md").write_text("just a body, no yaml\n", encoding="utf-8")
    # And still detect a real legacy snapshot alongside.
    _write_legacy_snapshot(sess / "2026-04-18-demo-hh-snapshot.md")
    issues = snapshot_migration.scan(str(tmp_path), "claude-sessions", "claude-insights", 3650)
    # Only the valid snapshot produces issues; the orphan file is ignored.
    assert any(i.note_path.endswith("2026-04-18-demo-hh-snapshot.md") for i in issues)


def test_apply_unknown_check_is_skipped(tmp_path):
    """apply() should mark unrecognized check names as 'skipped'."""
    from scripts.vault_doctor_checks import Issue as _Issue
    fake = _Issue(
        check="not-a-real-check",
        note_path=str(tmp_path / "x.md"),
        project="demo",
        current_source="",
        proposed_source="",
        reason="",
        confidence=1.0,
    )
    results = snapshot_migration.apply([fake], str(tmp_path / "backup"))
    assert len(results) == 1
    assert results[0].status == "skipped"


def test_apply_status_raises_runtime_error_when_no_frontmatter(tmp_path):
    """apply() should record an error when the snapshot has no frontmatter."""
    from scripts.vault_doctor_checks import Issue as _Issue
    sess = tmp_path / "claude-sessions"; sess.mkdir()
    p = sess / "broken.md"
    p.write_text("no frontmatter at all\n", encoding="utf-8")
    issue = _Issue(
        check="snapshot-missing-status",
        note_path=str(p),
        project="demo",
        current_source="(no status field)",
        proposed_source="status: auto-logged",
        reason="test",
        confidence=0.98,
    )
    results = snapshot_migration.apply([issue], str(tmp_path / "backup"))
    assert len(results) == 1
    assert results[0].status == "error"
    assert "frontmatter" in (results[0].error or "")
