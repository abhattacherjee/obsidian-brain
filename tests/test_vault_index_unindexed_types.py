"""Notes typed claude-wiki-index stay out of the index (#383)."""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import vault_index

NOTE = "---\ntype: {t}\ndate: 2026-10-03\nproject: demo\n---\n# {title}\n\nzebracorn body text\n"


def _write(p: Path, t: str, title: str = "T") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(NOTE.format(t=t, title=title), encoding="utf-8")


def _paths(db: str) -> set:
    conn = sqlite3.connect(db)
    try:
        return {Path(r[0]).name for r in conn.execute("SELECT path FROM notes")}
    finally:
        conn.close()


def test_constant():
    assert vault_index._UNINDEXED_TYPES == frozenset({"claude-wiki-index"})


def test_wiki_index_file_not_indexed_but_pages_are(tmp_path):
    vault = tmp_path / "v"
    _write(vault / "claude-wiki" / "index.md", "claude-wiki-index")
    _write(vault / "claude-wiki" / "log-2026.md", "claude-wiki-index")
    _write(vault / "claude-wiki" / "queries" / "2026" / "10-03-q.md", "claude-wiki")
    db = vault_index.ensure_index(str(vault), ["claude-wiki"], db_path=str(tmp_path / "i.db"))
    assert _paths(db) == {"10-03-q.md"}


def test_user_note_named_index_md_is_still_indexed(tmp_path):
    vault = tmp_path / "v"
    _write(vault / "claude-insights" / "index.md", "claude-insight")
    db = vault_index.ensure_index(str(vault), ["claude-insights"], db_path=str(tmp_path / "i.db"))
    assert _paths(db) == {"index.md"}


def test_excluded_count_in_stats(tmp_path):
    vault = tmp_path / "v"
    _write(vault / "claude-wiki" / "index.md", "claude-wiki-index")
    stats = vault_index.rebuild_index(str(vault), ["claude-wiki"], db_path=str(tmp_path / "i.db"), full=True)
    assert stats.get("excluded") == 1
    assert stats.get("malformed") == 0


def test_type_change_to_unindexed_removes_row(tmp_path):
    vault = tmp_path / "v"
    p = vault / "claude-wiki" / "index.md"
    _write(p, "claude-insight")
    db = str(tmp_path / "i.db")
    vault_index.ensure_index(str(vault), ["claude-wiki"], db_path=db)
    assert _paths(db) == {"index.md"}
    _write(p, "claude-wiki-index")
    later = time.time() + 5
    os.utime(p, (later, later))
    vault_index.ensure_index(str(vault), ["claude-wiki"], db_path=db)
    assert _paths(db) == set()
    assert vault_index.search_vault(db, "zebracorn", limit=5) == []


def test_missing_wiki_folder_is_skipped(tmp_path):
    vault = tmp_path / "v"
    _write(vault / "claude-sessions" / "s.md", "claude-session")
    db = vault_index.ensure_index(
        str(vault), ["claude-sessions", "claude-insights", "claude-wiki"], db_path=str(tmp_path / "i.db")
    )
    assert _paths(db) == {"s.md"}


def test_rebuild_with_wiki_folder_keeps_wiki_rows(tmp_path):
    vault = tmp_path / "v"
    _write(vault / "claude-sessions" / "s.md", "claude-session")
    _write(vault / "claude-wiki" / "queries" / "2026" / "10-03-q.md", "claude-wiki")
    db = str(tmp_path / "i.db")
    folders = ["claude-sessions", "claude-insights", "claude-wiki"]
    vault_index.ensure_index(str(vault), folders, db_path=db)
    vault_index.rebuild_index(str(vault), folders, db_path=db)             # preserve mode
    assert _paths(db) == {"s.md", "10-03-q.md"}
    vault_index.rebuild_index(str(vault), folders, db_path=db, full=True)  # full
    assert _paths(db) == {"s.md", "10-03-q.md"}
