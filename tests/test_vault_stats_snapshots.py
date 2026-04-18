import json
from pathlib import Path

from hooks import vault_index, vault_stats


def _setup_vault(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    # 1 session, 2 snapshots (compact + clear) backed by it, 1 orphan
    (sess / "2026-04-18-demo-aa.md").write_text(
        "---\ntype: claude-session\ndate: 2026-04-18\nsession_id: s1\nproject: demo\n"
        "status: summarized\n---\n\n# S\n",
        encoding="utf-8",
    )
    (sess / "2026-04-18-demo-aa-snapshot-120000.md").write_text(
        "---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: s1\nproject: demo\n"
        'trigger: compact\nstatus: summarized\nsource_session_note: "[[2026-04-18-demo-aa]]"\n'
        "---\n\n# Snap\n",
        encoding="utf-8",
    )
    (sess / "2026-04-18-demo-aa-snapshot-150000.md").write_text(
        "---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: s1\nproject: demo\n"
        'trigger: clear\nstatus: auto-logged\nsource_session_note: "[[2026-04-18-demo-aa]]"\n'
        "---\n\n# Snap\n",
        encoding="utf-8",
    )
    (sess / "2026-04-18-demo-zz-snapshot-180000.md").write_text(
        "---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: missing\nproject: demo\n"
        'trigger: compact\nstatus: auto-logged\nsource_session_note: "[[does-not-exist]]"\n'
        "---\n\n# Orphan\n",
        encoding="utf-8",
    )
    return vault


def test_snapshot_stats_counts_by_trigger_and_integrity(tmp_path):
    vault = _setup_vault(tmp_path)
    db = vault_index.ensure_index(str(vault), ["claude-sessions"],
                                  db_path=str(tmp_path / "test.db"))
    payload = json.loads(vault_stats.compute_stats(db, "demo"))
    snap = payload["vault_wide"]["snapshots"]
    assert snap["total_snapshots"] == 3
    assert snap["by_trigger"] == {"compact": 2, "clear": 1, "auto": 0}
    assert snap["sessions_with_snapshots"] == 2   # s1 and "missing"
    assert snap["max_snapshots_per_session"] == 2
    assert snap["orphaned_snapshots"] == 1
    assert snap["broken_backlinks"] >= 1
    assert 0.0 <= snap["summarized_fraction"] <= 1.0


def test_snapshot_stats_zero_state(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    (sess / "2026-04-18-demo-aa.md").write_text(
        "---\ntype: claude-session\ndate: 2026-04-18\nsession_id: s1\nproject: demo\n---\n\n# S\n",
        encoding="utf-8",
    )
    db = vault_index.ensure_index(str(vault), ["claude-sessions"],
                                  db_path=str(tmp_path / "test2.db"))
    payload = json.loads(vault_stats.compute_stats(db, "demo"))
    snap = payload["vault_wide"]["snapshots"]
    assert snap["total_snapshots"] == 0
    assert snap["summarized_fraction"] == 1.0
    assert snap["by_trigger"] == {"compact": 0, "clear": 0, "auto": 0}


def test_snapshot_stats_unknown_trigger_folds_into_auto(tmp_path):
    """Trigger values outside {compact, clear, auto} are counted as 'auto'."""
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    (sess / "2026-04-18-demo-aa.md").write_text(
        "---\ntype: claude-session\ndate: 2026-04-18\nsession_id: s1\nproject: demo\n---\n\n# S\n",
        encoding="utf-8",
    )
    (sess / "2026-04-18-demo-aa-snapshot-120000.md").write_text(
        "---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: s1\nproject: demo\n"
        'trigger: weird_value\nstatus: auto-logged\nsource_session_note: "[[2026-04-18-demo-aa]]"\n'
        "---\n\n# Snap\n",
        encoding="utf-8",
    )
    db = vault_index.ensure_index(str(vault), ["claude-sessions"],
                                  db_path=str(tmp_path / "test3.db"))
    payload = json.loads(vault_stats.compute_stats(db, "demo"))
    snap = payload["vault_wide"]["snapshots"]
    assert snap["total_snapshots"] == 1
    assert snap["by_trigger"] == {"compact": 0, "clear": 0, "auto": 1}


def test_compute_stats_missing_db_returns_error(tmp_path):
    """compute_stats on a nonexistent DB returns {'error': ...} instead of raising."""
    payload = json.loads(vault_stats.compute_stats(str(tmp_path / "does-not-exist.db"), "demo"))
    assert "error" in payload
    assert "DB not found" in payload["error"]


def test_compute_stats_corrupt_db_returns_error(tmp_path):
    """compute_stats on a file that isn't a SQLite DB surfaces the inner exception."""
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_text("not a sqlite file", encoding="utf-8")
    payload = json.loads(vault_stats.compute_stats(str(corrupt), "demo"))
    assert "error" in payload
