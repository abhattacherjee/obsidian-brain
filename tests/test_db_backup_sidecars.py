"""DB backups stay self-contained, and read-only inspection creates no sidecars."""

import datetime
import sqlite3
from pathlib import Path

import pytest

import capture_backlog
import vault_index


SINCE = datetime.datetime(2026, 10, 18).timestamp()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "index.db"
    conn = vault_index._connect(str(path))
    try:
        vault_index._init_schema(conn)
        conn.execute(
            "INSERT INTO notes (path, type, title, mtime) "
            "VALUES ('/v/a.md', 'claude-insight', 'A note', 1)"
        )
        conn.executemany(
            "INSERT INTO access_log (note_path, timestamp, context_type, project) "
            "VALUES (?, ?, ?, 'p')",
            [
                ("/v/a.md", SINCE - 1, "search"),
                ("/v/a.md", SINCE, "search"),
                ("/v/b.md", SINCE + 3600, "search"),
                ("/v/b.md", SINCE + 86400 + 5, "search"),
                ("/v/a.md", SINCE + 10, "recall"),
                ("/v/a.md", SINCE + 20, "related"),
                ("/v/c.md", SINCE - 86400, "search"),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return str(path)


@pytest.fixture
def wal_copy(tmp_path):
    path = tmp_path / "source.db"
    conn = vault_index._connect(str(path))
    try:
        conn.execute("CREATE TABLE items (value TEXT)")
        conn.executemany("INSERT INTO items VALUES (?)", [("first",), ("second",)])
        conn.commit()
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    copy = tmp_path / "copy ?#.db"
    copy.write_bytes(path.read_bytes())
    assert copy.read_bytes()[18:20] == b"\x02\x02"
    return copy


def test_purge_backup_is_single_rollback_journal_file(db, tmp_path):
    bk = tmp_path / "bk"
    result = vault_index.purge_access_log(
        db, "search", SINCE, apply=True, backup_dir=str(bk)
    )
    backup_path = result["backup_path"]
    assert Path(backup_path).read_bytes()[18:20] == b"\x01\x01"
    assert sorted(p.name for p in bk.iterdir()) == [Path(backup_path).name]


def test_backlog_backup_is_single_rollback_journal_file(db, tmp_path):
    bk = tmp_path / "bk"
    backup_path = capture_backlog.backup_db(db, bk)
    assert Path(backup_path).read_bytes()[18:20] == b"\x01\x01"
    assert sorted(p.name for p in bk.iterdir()) == [Path(backup_path).name]


def test_read_only_open_of_wal_copy_creates_no_side_files(wal_copy):
    conn = vault_index.connect_read_only(str(wal_copy))
    try:
        assert conn.execute("SELECT value FROM items ORDER BY rowid").fetchall() == [
            ("first",), ("second",)
        ]
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO items VALUES ('third')")
    finally:
        conn.close()
    assert not Path(str(wal_copy) + "-wal").exists()
    assert not Path(str(wal_copy) + "-shm").exists()


def test_read_only_open_refuses_wal_without_shm(wal_copy):
    Path(str(wal_copy) + "-wal").write_bytes(b"non-empty WAL")
    with pytest.raises(ValueError, match="WAL without its shared-memory index"):
        vault_index.connect_read_only(str(wal_copy))


def test_read_only_open_never_creates_a_missing_db(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        vault_index.connect_read_only(str(path))
    assert not path.exists()
