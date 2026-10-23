"""Access-log GC: keep the newest N events per note (#448).

Nothing pruned access_log, and activation reads every row of every
candidate, so the read cost grew with the log. gc_access_log keeps the
newest ACCESS_LOG_KEEP_PER_NOTE rows per note (the Friston spec's 1,000).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

import vault_index

FOLDER = "claude-insights"


def _vault(tmp_path: Path, n: int = 4) -> tuple[Path, list[str]]:
    vault = tmp_path / "vault"
    ins = vault / FOLDER
    ins.mkdir(parents=True)
    paths = []
    for i in range(n):
        p = ins / f"2026-10-2{i}-gadget-{i}.md"
        p.write_text(
            "---\ntype: claude-insight\ndate: 2026-10-20\nproject: p\n---\n\n"
            f"# Gadget calibration note {i}\n\nGadget calibration detail {i} "
            + "sprocket " * i + "\n",
            encoding="utf-8",
        )
        paths.append(str(p))
    return vault, paths


def _insert(db: str, rows: list[tuple[str, float]]) -> None:
    conn = vault_index._connect(db)
    try:
        conn.executemany(
            "INSERT INTO access_log (note_path, timestamp, context_type, project) "
            "VALUES (?, ?, 'search', 'p')",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _rows(db: str, note: str | None = None) -> list[tuple[int, float]]:
    conn = vault_index._connect(db)
    try:
        if note is None:
            q, a = "SELECT id, timestamp FROM access_log ORDER BY id", ()
        else:
            q, a = "SELECT id, timestamp FROM access_log WHERE note_path = ? ORDER BY id", (note,)
        return [(r[0], r[1]) for r in conn.execute(q, a).fetchall()]
    finally:
        conn.close()


@pytest.fixture
def indexed(tmp_path):
    vault, paths = _vault(tmp_path)
    db = str(tmp_path / "gc.db")
    vault_index.ensure_index(str(vault), [FOLDER], db_path=db)
    return vault, paths, db


def test_keeps_newest_n_per_note(indexed):
    _vault_path, paths, db = indexed
    hot, cold = paths[0], paths[1]
    base = time.time() - 10_000
    # Inserted oldest-last, so id order and time order disagree: the GC must
    # rank by timestamp, not by id.
    _insert(db, [(hot, base + i) for i in reversed(range(12))])
    _insert(db, [(cold, base + i) for i in range(3)])
    conn = vault_index._connect(db)
    try:
        deleted = vault_index.gc_access_log(conn, keep=5)
    finally:
        conn.close()
    assert deleted == 7
    assert sorted(ts for _, ts in _rows(db, hot)) == [base + i for i in range(7, 12)]
    assert len(_rows(db, cold)) == 3


def test_timestamp_tie_breaks_by_newest_id(indexed):
    _vault_path, paths, db = indexed
    ts = time.time() - 100
    _insert(db, [(paths[0], ts)] * 4)
    ids = [i for i, _ in _rows(db, paths[0])]
    conn = vault_index._connect(db)
    try:
        assert vault_index.gc_access_log(conn, keep=2) == 2
    finally:
        conn.close()
    assert [i for i, _ in _rows(db, paths[0])] == ids[-2:]


def test_default_keep_is_the_spec_1000(indexed):
    _vault_path, paths, db = indexed
    assert vault_index.ACCESS_LOG_KEEP_PER_NOTE == 1000
    base = time.time() - 100_000
    _insert(db, [(paths[0], base + i) for i in range(1001)])
    conn = vault_index._connect(db)
    try:
        assert vault_index.gc_access_log(conn) == 1
    finally:
        conn.close()
    remaining = _rows(db, paths[0])
    assert len(remaining) == 1000
    assert min(ts for _, ts in remaining) == base + 1


@pytest.mark.parametrize("keep", [0, -1], ids=["zero", "negative"])
def test_keep_below_one_is_refused(indexed, keep):
    """keep=0 would delete every access row; refuse it before any DELETE."""
    _vault_path, paths, db = indexed
    _insert(db, [(paths[0], time.time())])
    conn = vault_index._connect(db)
    try:
        with pytest.raises(ValueError):
            vault_index.gc_access_log(conn, keep=keep)
    finally:
        conn.close()
    assert len(_rows(db)) == 1


def test_no_note_over_keep_deletes_nothing(indexed):
    _vault_path, paths, db = indexed
    base = time.time() - 1000
    _insert(db, [(p, base + i) for p in paths for i in range(5)])
    before = _rows(db)
    conn = vault_index._connect(db)
    try:
        assert vault_index.gc_access_log(conn, keep=5) == 0
    finally:
        conn.close()
    assert _rows(db) == before


def test_rerank_unchanged_when_no_note_exceeds_keep(indexed):
    _vault_path, paths, db = indexed
    base = time.time() - 5000
    # Uneven activation so the rerank actually depends on access_log.
    _insert(db, [(p, base + i * 10) for n, p in enumerate(paths) for i in range(n * 3)])
    queries = ["gadget calibration", "sprocket", "calibration detail"]

    def ranks():
        return [
            [r["path"] for r in vault_index.search_vault(db, q, log_access=False)]
            for q in queries
        ]

    before = ranks()
    assert any(before), "fixture must return hits"
    conn = vault_index._connect(db)
    try:
        assert vault_index.gc_access_log(conn, keep=1000) == 0
    finally:
        conn.close()
    assert ranks() == before


def test_ensure_index_trims_when_a_note_exceeds_keep(indexed, monkeypatch):
    vault, paths, db = indexed
    monkeypatch.setattr(vault_index, "ACCESS_LOG_KEEP_PER_NOTE", 3)
    base = time.time() - 1000
    _insert(db, [(paths[0], base + i) for i in range(6)])
    _insert(db, [(paths[1], base + i) for i in range(2)])
    vault_index.ensure_index(str(vault), [FOLDER], db_path=db)
    assert len(_rows(db, paths[0])) == 3
    assert len(_rows(db, paths[1])) == 2


def test_ensure_index_gc_threshold_is_exactly_keep(indexed, monkeypatch):
    """A note with exactly `keep` rows is not over the limit: no GC, no loss.
    One more row tips it over."""
    vault, paths, db = indexed
    monkeypatch.setattr(vault_index, "ACCESS_LOG_KEEP_PER_NOTE", 3)
    calls = []
    real = vault_index.gc_access_log

    def spy(conn, keep=None):
        calls.append(keep)
        return real(conn, keep)

    monkeypatch.setattr(vault_index, "gc_access_log", spy)
    base = time.time() - 1000
    _insert(db, [(paths[0], base + i) for i in range(3)])
    vault_index.ensure_index(str(vault), [FOLDER], db_path=db)
    assert calls == []
    assert len(_rows(db, paths[0])) == 3

    _insert(db, [(paths[0], base + 3)])
    vault_index.ensure_index(str(vault), [FOLDER], db_path=db)
    assert len(calls) == 1
    assert len(_rows(db, paths[0])) == 3


def test_ensure_index_skips_gc_when_no_note_exceeds_keep(indexed, monkeypatch):
    vault, paths, db = indexed
    calls = []
    real = vault_index.gc_access_log

    def spy(conn, keep=None):
        calls.append(keep)
        return real(conn, keep)

    monkeypatch.setattr(vault_index, "gc_access_log", spy)
    _insert(db, [(paths[0], time.time())])
    vault_index.ensure_index(str(vault), [FOLDER], db_path=db)
    assert calls == []


def test_ensure_index_gc_error_does_not_rebuild_the_db(indexed, monkeypatch, capsys):
    """ensure_index treats sqlite3.DatabaseError as a corrupt DB and deletes
    it. A GC failure (e.g. a lock) must not reach that branch, or a busy
    log would wipe the whole index and every access row."""
    vault, paths, db = indexed
    monkeypatch.setattr(vault_index, "ACCESS_LOG_KEEP_PER_NOTE", 1)
    _insert(db, [(paths[0], time.time() - 5), (paths[0], time.time())])

    def locked(conn, keep=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(vault_index, "gc_access_log", locked)
    vault_index.ensure_index(str(vault), [FOLDER], db_path=db)
    assert len(_rows(db, paths[0])) == 2
    err = capsys.readouterr().err
    assert "access_log GC skipped" in err
    assert "rebuilding" not in err


def test_rebuild_index_reports_gc_separately_from_orphans(indexed, monkeypatch):
    vault, paths, db = indexed
    monkeypatch.setattr(vault_index, "ACCESS_LOG_KEEP_PER_NOTE", 2)
    base = time.time() - 1000
    _insert(db, [(paths[0], base + i) for i in range(5)])
    _insert(db, [("/gone/orphan.md", base)])
    stats = vault_index.rebuild_index(str(vault), [FOLDER], db_path=db)
    assert stats["access_log_gc"] == 3
    assert stats["pruned_orphans"]["access_log"] == 1
    assert stats["preserved"]["access_log"] == 2


def test_full_rebuild_reports_zero_gc(tmp_path):
    vault, _paths = _vault(tmp_path)
    stats = vault_index.rebuild_index(str(vault), [FOLDER], db_path=str(tmp_path / "f.db"),
                                      full=True)
    assert stats["access_log_gc"] == 0
