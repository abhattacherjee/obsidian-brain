import sqlite3
from pathlib import Path

import pytest

from hooks import vault_index


def _seed(vault, sess):
    (sess).mkdir(parents=True, exist_ok=True)
    parent_path = sess / "2026-04-18-demo-aaaa.md"
    parent_path.write_text(
        "---\ntype: claude-session\ndate: 2026-04-18\nsession_id: s1\nproject: demo\n"
        "status: summarized\n---\n\n# Session\n",
        encoding="utf-8",
    )
    snap_path = sess / "2026-04-18-demo-aaaa-snapshot-140000.md"
    snap_path.write_text(
        "---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: s1\nproject: demo\n"
        "status: summarized\n"
        'source_session_note: "[[2026-04-18-demo-aaaa]]"\n'
        "---\n\n# Snap\n",
        encoding="utf-8",
    )
    return parent_path, snap_path


def test_log_access_on_snapshot_inserts_two_rows(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    parent_path, snap_path = _seed(vault, sess)
    db_path = vault_index.ensure_index(
        str(vault), ["claude-sessions"], db_path=str(tmp_path / "test.db")
    )
    # Clear cache so this test's snap_path is not stale from a previous run
    vault_index._PARENT_CACHE.clear()

    vault_index.log_access(db_path, str(snap_path), "search", "demo")

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT note_path FROM access_log").fetchall()
    conn.close()
    paths = {r[0] for r in rows}
    assert str(snap_path) in paths
    assert str(parent_path) in paths


def test_log_access_on_session_inserts_single_row(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    parent_path, _ = _seed(vault, sess)
    db_path = vault_index.ensure_index(
        str(vault), ["claude-sessions"], db_path=str(tmp_path / "test.db")
    )
    vault_index._PARENT_CACHE.clear()

    vault_index.log_access(db_path, str(parent_path), "recall", "demo")

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT note_path FROM access_log").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == str(parent_path)


def test_log_access_with_broken_snapshot_backlink_is_tolerant(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    snap = sess / "2026-04-18-demo-cccc-snapshot-090000.md"
    snap.write_text(
        "---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: no-such\n"
        'project: demo\nstatus: auto-logged\nsource_session_note: "[[does-not-exist]]"\n'
        "---\n\n# Orphan snap\n",
        encoding="utf-8",
    )
    db_path = vault_index.ensure_index(
        str(vault), ["claude-sessions"], db_path=str(tmp_path / "test.db")
    )
    vault_index._PARENT_CACHE.clear()

    # Must not raise
    vault_index.log_access(db_path, str(snap), "search", "demo")

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT note_path FROM access_log").fetchall()
    conn.close()
    # Only the snapshot is logged; parent cannot be resolved
    assert {r[0] for r in rows} == {str(snap)}
