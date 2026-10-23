"""One-off access_log purge (#448).

Rule chosen by the user 2026-10-23: delete every context_type='search' row
at or after 2026-10-18 00:00 local; keep recall and related rows. Dry run by
default; --apply backs the DB up first and a failed backup deletes nothing.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import vault_index

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "access_log_purge.py"


def _midnight(day: str) -> float:
    return datetime.datetime.strptime(day, "%Y-%m-%d").timestamp()


SINCE = _midnight("2026-10-18")


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "purge.db")
    conn = vault_index._connect(path)
    try:
        vault_index._init_schema(conn)
        rows = [
            ("/v/a.md", SINCE - 1, "search"),          # 10-17 23:59:59: kept
            ("/v/a.md", SINCE, "search"),              # 10-18 00:00:00: deleted
            ("/v/b.md", SINCE + 3600, "search"),       # deleted
            ("/v/b.md", SINCE + 86400 + 5, "search"),  # 10-19: deleted
            ("/v/a.md", SINCE + 10, "recall"),         # other context: kept
            ("/v/a.md", SINCE + 20, "related"),        # other context: kept
            ("/v/c.md", SINCE - 86400, "search"),      # older: kept
        ]
        conn.executemany(
            "INSERT INTO access_log (note_path, timestamp, context_type, project) "
            "VALUES (?, ?, ?, 'p')",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _rows(path: str) -> list[tuple]:
    conn = vault_index._connect(path)
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT note_path, timestamp, context_type FROM access_log ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()


def test_dry_run_deletes_nothing(db, tmp_path):
    before = _rows(db)
    result = vault_index.purge_access_log(db, "search", SINCE, backup_dir=str(tmp_path / "bk"))
    assert result["applied"] is False
    assert result["matched"] == 3
    assert result["by_day"] == {"2026-10-18": 2, "2026-10-19": 1}
    assert _rows(db) == before
    assert not (tmp_path / "bk").exists()


def test_apply_backs_up_then_deletes_only_matching_rows(db, tmp_path):
    before = _rows(db)
    bk = tmp_path / "bk"
    result = vault_index.purge_access_log(db, "search", SINCE, apply=True, backup_dir=str(bk))
    assert result["applied"] is True
    assert result["deleted"] == 3
    assert result["before_total"] == 7 and result["after_total"] == 4
    after = _rows(db)
    assert ("/v/a.md", SINCE - 1, "search") in after
    assert ("/v/c.md", SINCE - 86400, "search") in after
    assert {r[2] for r in after if r[1] >= SINCE} == {"recall", "related"}
    # The backup holds the pre-delete log, owner-only, in an owner-only dir.
    backup = result["backup_path"]
    assert Path(backup).parent == bk
    assert _rows(backup) == before
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600
    assert len(list(bk.glob("vault-db-backup-*"))) == 1
    assert stat.S_IMODE(os.stat(bk).st_mode) == 0o700


def test_date_boundary_is_local_midnight(db, tmp_path):
    vault_index.purge_access_log(db, "search", SINCE, apply=True, backup_dir=str(tmp_path / "bk"))
    stamps = {r[1] for r in _rows(db) if r[2] == "search"}
    assert SINCE - 1 in stamps
    assert SINCE not in stamps


def test_backup_failure_deletes_nothing(db, tmp_path, monkeypatch):
    before = _rows(db)

    def broken_backup(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(vault_index, "_backup_db", broken_backup)
    with pytest.raises(sqlite3.Error):
        vault_index.purge_access_log(db, "search", SINCE, apply=True,
                                     backup_dir=str(tmp_path / "bk"))
    assert _rows(db) == before
    assert not list((tmp_path / "bk").glob("vault-db-backup-*"))


def test_backup_count_mismatch_deletes_nothing(db, tmp_path, monkeypatch):
    """A backup that does not hold the full log is not a backup."""
    before = _rows(db)
    def partial_backup(src, dest_path):
        dest = vault_index._connect(dest_path)
        try:
            vault_index._init_schema(dest)  # schema, but no rows
        finally:
            dest.close()

    monkeypatch.setattr(vault_index, "_backup_db", partial_backup)
    with pytest.raises(RuntimeError, match="backup"):
        vault_index.purge_access_log(db, "search", SINCE, apply=True,
                                     backup_dir=str(tmp_path / "bk"))
    assert _rows(db) == before
    assert not list((tmp_path / "bk").glob("vault-db-backup-*"))


def test_row_inserted_after_backup_survives_and_does_not_abort(db, tmp_path, monkeypatch):
    """A search row that lands between the backup and the delete is not in the
    backup, so the purge must leave it alone and must not abort on the count."""
    real_backup = vault_index._backup_db

    def backup_then_insert(src, dest_path):
        real_backup(src, dest_path)
        late = vault_index._connect(db)
        try:
            late.execute(
                "INSERT INTO access_log (note_path, timestamp, context_type, project) "
                "VALUES ('/v/late.md', ?, 'search', 'p')", (SINCE + 99,))
            late.commit()
        finally:
            late.close()

    monkeypatch.setattr(vault_index, "_backup_db", backup_then_insert)
    result = vault_index.purge_access_log(db, "search", SINCE, apply=True,
                                          backup_dir=str(tmp_path / "bk"))
    assert result["deleted"] == 3
    after = _rows(db)
    assert ("/v/late.md", SINCE + 99, "search") in after
    assert ("/v/late.md", SINCE + 99, "search") not in _rows(result["backup_path"])


def test_missing_db_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        vault_index.purge_access_log(str(tmp_path / "absent.db"), "search", SINCE)


# --- script ---------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        env=os.environ.copy(),
    )


def test_script_dry_run_is_default_and_exits_0(db, tmp_path):
    before = _rows(db)
    proc = _run("--db", db, "--since", "2026-10-18", "--backup-dir", str(tmp_path / "bk"))
    assert proc.returncode == 0, proc.stderr
    assert "2026-10-18  2" in proc.stdout
    assert "dry run" in proc.stdout.lower()
    assert _rows(db) == before


def test_dry_run_lists_rows_on_request(db):
    result = vault_index.purge_access_log(db, "search", SINCE, list_rows=True)
    assert result["rows"] == [
        ("/v/a.md", SINCE), ("/v/b.md", SINCE + 3600), ("/v/b.md", SINCE + 86400 + 5),
    ]
    assert "rows" not in vault_index.purge_access_log(db, "search", SINCE)


def test_script_list_prints_rows(db):
    proc = _run("--db", db, "--since", "2026-10-18", "--list")
    assert proc.returncode == 0, proc.stderr
    assert "2026-10-18 00:00:00  /v/a.md" in proc.stdout
    assert "/v/c.md" not in proc.stdout


def test_script_apply_exits_0_and_reports_counts(db, tmp_path):
    proc = _run("--db", db, "--since", "2026-10-18", "--apply",
                "--backup-dir", str(tmp_path / "bk"))
    assert proc.returncode == 0, proc.stderr
    assert "deleted 3" in proc.stdout
    assert "before 7, after 4" in proc.stdout
    assert len(_rows(db)) == 4


def test_existing_loose_backup_dir_is_tightened_to_0700(db, tmp_path):
    bk = tmp_path / "bk"
    bk.mkdir()
    os.chmod(bk, 0o755)
    vault_index.purge_access_log(db, "search", SINCE, apply=True, backup_dir=str(bk))
    assert stat.S_IMODE(os.stat(bk).st_mode) == 0o700


def test_script_apply_with_context_recall_deletes_only_recall(db, tmp_path):
    proc = _run("--db", db, "--since", "2026-10-18", "--context", "recall", "--apply",
                "--backup-dir", str(tmp_path / "bk"))
    assert proc.returncode == 0, proc.stderr
    assert "deleted 1" in proc.stdout
    after = _rows(db)
    assert len(after) == 6
    assert "recall" not in {r[2] for r in after}
    assert sum(1 for r in after if r[2] == "search") == 5


def test_script_missing_db_exits_1(tmp_path):
    proc = _run("--db", str(tmp_path / "absent.db"), "--since", "2026-10-18")
    assert proc.returncode == 1
    assert "no index DB" in proc.stderr


def test_script_backup_failure_exits_1(db, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    before = _rows(db)
    proc = _run("--db", db, "--since", "2026-10-18", "--apply",
                "--backup-dir", str(blocker / "sub"))
    assert proc.returncode == 1
    assert "nothing deleted" in proc.stderr
    assert _rows(db) == before


@pytest.mark.parametrize("since", ["2026-13-01", "18-10-2026", "yesterday", ""],
                         ids=["bad-month", "wrong-order", "word", "empty"])
def test_script_bad_since_exits_2(db, since):
    proc = _run("--db", db, "--since", since)
    assert proc.returncode == 2
    # From the script's own parser, not Python failing to find the file.
    assert "usage: access_log_purge.py" in proc.stderr


def test_script_missing_since_exits_2(db):
    proc = _run("--db", db)
    assert proc.returncode == 2
    assert "usage: access_log_purge.py" in proc.stderr


def test_script_unknown_context_exits_2(db):
    proc = _run("--db", db, "--since", "2026-10-18", "--context", "bogus")
    assert proc.returncode == 2
    assert "usage: access_log_purge.py" in proc.stderr


def test_lock_at_begin_deletes_nothing_and_drops_backup(db, tmp_path, monkeypatch):
    """A failure at BEGIN IMMEDIATE (database is locked) is before the commit:
    nothing is deleted and the backup file must not be left behind."""
    before = _rows(db)
    real_connect = vault_index._connect

    class _Proxy:
        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, *a):
            if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
                raise sqlite3.OperationalError("database is locked")
            return self._c.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._c, name)

    def connect(path, *a, **k):
        conn = real_connect(path, *a, **k)
        return _Proxy(conn) if path == db else conn

    monkeypatch.setattr(vault_index, "_connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        vault_index.purge_access_log(db, "search", SINCE, apply=True,
                                     backup_dir=str(tmp_path / "bk"))
    monkeypatch.setattr(vault_index, "_connect", real_connect)
    assert _rows(db) == before
    assert not list((tmp_path / "bk").glob("vault-db-backup-*"))
