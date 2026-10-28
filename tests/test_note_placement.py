"""R1/R2 placement uses only tmp vaults, an explicit DB and a tmp ledger."""

import json
import os
import sqlite3
import stat
from types import SimpleNamespace

import pytest

import capture_state
import note_placement
import note_writer
import obsidian_utils
import vault_index


def _write_note(path, status="provisional"):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(
        f"---\ntype: claude-insight\nstatus: {status}\nproject: demo\n"
        "date: 2026-10-28\n---\n\n# Placement\n\nAtomic bookkeeping.\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _snapshot(db):
    conn = vault_index._connect(db)
    try:
        return {
            table: [dict(row) for row in conn.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid")]
            for table in ("notes", "access_log", "theme_members", "themes", "term_df")
        }
    finally:
        conn.close()


def _assert_rekeyed(before, after, old, new):
    expected = {
        table: [
            {key: str(new) if key in ("path", "note_path") and value == str(old) else value
             for key, value in row.items()}
            for row in rows
        ]
        for table, rows in before.items()
    }
    assert after == expected  # Includes rowids, row counts, vectors and theme state.


@pytest.fixture(params=["claude-insights", "knowledge/insights"])
def env(tmp_path, monkeypatch, request):
    vault = tmp_path / "vault"
    folder = vault / request.param
    folder.mkdir(parents=True, mode=0o700)
    config = {"vault_path": str(vault), "insights_folder": request.param}
    old, new = folder / "note.md", folder / "_provisional" / "note.md"
    _write_note(old)
    _write_note(folder / "unrelated.md", "promoted")
    db = vault_index.ensure_index(str(vault), [request.param], db_path=str(tmp_path / "index.db"))
    conn = vault_index._connect(db)
    try:
        for path in (old, old, folder / "unrelated.md"):
            conn.execute(
                "INSERT INTO access_log (note_path, timestamp, context_type) VALUES (?, ?, ?)",
                (str(path), 123.0, "search"),
            )
        for theme in (1, 2):
            conn.execute(
                "INSERT INTO themes (id, name, note_count, created_date, updated_date) "
                "VALUES (?, ?, 1, '2026-10-28', '2026-10-28')", (theme, f"Theme {theme}"),
            )
            conn.execute(
                "INSERT INTO theme_members (theme_id, note_path, similarity, added_date) "
                "VALUES (?, ?, 0.8, '2026-10-28')", (theme, str(old)),
            )
        conn.commit()
    finally:
        conn.close()
    ledger_dir = tmp_path / "capture"
    ledger_dir.mkdir(mode=0o700)
    monkeypatch.setattr(capture_state, "capture_dir", lambda: ledger_dir)
    rel = old.relative_to(vault).as_posix()
    # Either side of local midnight in Denver on 2026-10-28 (UTC-06:00).
    for cid, kind, ts in (("created", "new", 1793167199.0), ("update", "updated", 1793167201.0)):
        capture_state.ledger_append({"id": cid, "note_path": rel, "kind": kind, "ts": ts,
                                     "status": "active", "session_id": "demo"})
    # A superseding row has a later timestamp; placement must retain the first.
    capture_state.ledger_append({"id": "created", "ts": 1793253600.0, "score": 7})
    capture_state.ledger_append({"id": "other", "kind": "new", "ts": 1.0,
                                 "note_path": f"{request.param}/unrelated.md"})
    return SimpleNamespace(config=config, vault=vault, old=old, new=new, db=db,
                           ledger=ledger_dir / capture_state.LEDGER_NAME)


def _assert_no_locks(env):
    assert not list(env.vault.rglob("*.ob-lock"))


@pytest.mark.parametrize("status", ["provisional", "promoted", "active", None, "", "other"])
def test_home_for_two_homes_and_every_status(env, status):
    expected = env.new if status == "provisional" else env.old
    assert note_placement.home_for(env.config, env.old, status) == expected
    assert note_placement.home_for(env.config, env.new, status) == expected
    assert not env.new.parent.exists()  # Computing a home never creates it.


@pytest.mark.parametrize("relative", ["_rejected/n.md", "nested/n.md", "_provisional/nested/n.md"])
def test_place_leaves_notes_outside_r1_homes_alone(env, relative, monkeypatch):
    path = env.old.parent / relative
    _write_note(path)
    monkeypatch.setattr(note_placement, "relocate", lambda *a: pytest.fail("outside R1 homes"))
    assert note_placement.home_for(env.config, path, "provisional") is None
    assert note_placement.place(env.config, path, "provisional", env.db) == path
    assert path.is_file()


def test_place_leaves_notes_outside_insights_alone(env):
    path = env.vault / "sessions" / "note.md"
    _write_note(path)
    assert note_placement.home_for(env.config, path, "provisional") is None
    assert note_placement.place(env.config, path, "provisional", env.db) == path


def test_move_bookkeeping_and_roundtrip(env):
    before = _snapshot(env.db)
    ledger_before = env.ledger.read_bytes()
    merged_before = capture_state._merged_rows()
    content = env.old.read_bytes()
    assert note_placement.place(env.config, env.old, "provisional", env.db) == env.new
    assert not env.old.exists() and env.new.read_bytes() == content
    _assert_rekeyed(before, _snapshot(env.db), env.old, env.new)
    expected = [
        {**row, "note_path": env.new.relative_to(env.vault).as_posix()}
        if row["id"] in ("created", "update") else row
        for row in merged_before
    ]
    assert capture_state._merged_rows() == expected
    assert env.ledger.read_bytes().startswith(ledger_before)
    appended = [json.loads(line) for line in env.ledger.read_bytes()[len(ledger_before):].splitlines()]
    assert {row["id"] for row in appended} == {"created", "update"}
    assert len(appended) == 2
    assert stat.S_IMODE(env.new.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(env.new.stat().st_mode) == 0o600
    assert stat.S_IMODE(env.ledger.stat().st_mode) == 0o600
    _assert_no_locks(env)

    ledger_after = env.ledger.read_bytes()
    # Both an explicit repair rerun and an already-placed note are no-ops.
    assert note_placement.relocate(env.config, env.old, env.new, env.db) == env.new
    assert note_placement.place(env.config, env.new, "provisional", env.db) == env.new
    assert env.ledger.read_bytes() == ledger_after
    # The supplied frontmatter status wins over the stale provisional DB cache.
    assert note_placement.place(env.config, env.new, "promoted", env.db) == env.old
    assert _snapshot(env.db) == before
    assert capture_state._merged_rows() == merged_before
    _assert_no_locks(env)


def test_place_uses_frontmatter_status_despite_stale_db_cache(env):
    assert note_placement.relocate(env.config, env.old, env.new, env.db) == env.new
    note_writer.set_frontmatter_fields(str(env.vault), str(env.new), {"status": "promoted"})
    status = obsidian_utils._peek_frontmatter_fields(env.new, ("status",))["status"]
    conn = vault_index._connect(env.db)
    try:
        assert conn.execute("SELECT status FROM notes WHERE path = ?", (str(env.new),)).fetchone()[0] == "provisional"
    finally:
        conn.close()
    assert status == "promoted"
    assert note_placement.place(env.config, env.new, status, env.db) == env.old
    assert env.old.is_file() and not env.new.exists()
    assert capture_state.ledger_get("created")["note_path"] == env.old.relative_to(env.vault).as_posix()
    _assert_no_locks(env)


def test_collision_leaves_files_db_and_ledger_untouched(env, capsys):
    _write_note(env.new, "promoted")
    before, ledger = _snapshot(env.db), env.ledger.read_bytes()
    files = {path: path.read_bytes() for path in (env.old, env.new)}
    assert note_placement.relocate(env.config, env.old, env.new, env.db) == env.old
    assert {path: path.read_bytes() for path in files} == files
    assert _snapshot(env.db) == before and env.ledger.read_bytes() == ledger
    assert "collision" in capsys.readouterr().err.lower()
    _assert_no_locks(env)


@pytest.mark.parametrize("which", ["old", "new"])
def test_held_lock_moves_nothing_and_releases_only_own_locks(env, which):
    env.new.parent.mkdir(mode=0o700)
    lock, error = note_writer._acquire_lock(getattr(env, which))
    assert error is None
    before, ledger = _snapshot(env.db), env.ledger.read_bytes()
    try:
        assert note_placement.relocate(env.config, env.old, env.new, env.db) == env.old
        assert env.old.is_file() and not env.new.exists()
        assert _snapshot(env.db) == before and env.ledger.read_bytes() == ledger
        assert list(env.vault.rglob("*.ob-lock")) == [lock]
    finally:
        note_writer._release_lock(lock)
    assert note_placement.relocate(env.config, env.old, env.new, env.db) == env.new
    _assert_no_locks(env)


@pytest.mark.parametrize("bad", ["rejected", "nested", "outside", "escape", "renamed", "same_home"])
@pytest.mark.parametrize("bad_source", [False, True])
def test_relocate_rejects_non_r1_moves_before_writing(env, bad, bad_source):
    paths = {
        "rejected": env.old.parent / "_rejected" / "note.md",
        "nested": env.old.parent / "_provisional" / "nested" / "note.md",
        "outside": env.vault.parent / "elsewhere" / "note.md",
        "escape": env.old.parent / ".." / "escaped.md",
        "renamed": env.new.with_name("different.md"),
        "same_home": env.old.with_name("different.md"),
    }
    old, new = (paths[bad], env.new) if bad_source else (env.old, paths[bad])
    before, ledger = _snapshot(env.db), env.ledger.read_bytes()
    with pytest.raises(ValueError):
        note_placement.relocate(env.config, old, new, env.db)
    assert _snapshot(env.db) == before and env.ledger.read_bytes() == ledger
    assert env.old.is_file() and not env.new.parent.exists()
    _assert_no_locks(env)


@pytest.mark.parametrize("folder", ["../escape", "/absolute", ".obsidian", ".", ""])
def test_invalid_insights_config_is_rejected(env, folder):
    config = {**env.config, "insights_folder": folder}
    with pytest.raises(ValueError):
        note_placement.relocate(config, env.old, env.new, env.db)
    assert not env.new.parent.exists()


def test_symlink_escape_is_rejected(env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    env.new.parent.symlink_to(outside, target_is_directory=True)
    before = _snapshot(env.db)
    with pytest.raises(ValueError):
        note_placement.relocate(env.config, env.old, env.new, env.db)
    assert _snapshot(env.db) == before
    assert not list(outside.iterdir())
    _assert_no_locks(env)


def test_rename_failure_rolls_back_and_unlocks(env, monkeypatch):
    before, ledger = _snapshot(env.db), env.ledger.read_bytes()

    def fail(*args):
        raise OSError("injected rename failure")

    with monkeypatch.context() as patch:
        patch.setattr(note_placement.os, "rename", fail)
        with pytest.raises(OSError, match="injected rename failure"):
            note_placement.relocate(env.config, env.old, env.new, env.db)
    assert env.old.is_file() and not env.new.exists()
    assert _snapshot(env.db) == before and env.ledger.read_bytes() == ledger
    _assert_no_locks(env)
    assert note_placement.relocate(env.config, env.old, env.new, env.db) == env.new


def test_db_update_failure_rolls_back_before_rename(env):
    # An orphaned destination membership conflicts with the final UPDATE.
    # Earlier notes/access updates must roll back, and the file must stay put.
    conn = vault_index._connect(env.db)
    try:
        conn.execute(
            "INSERT INTO theme_members (theme_id, note_path, similarity, added_date) "
            "VALUES (1, ?, 0.2, '2026-10-28')", (str(env.new),),
        )
        conn.commit()
    finally:
        conn.close()
    before, ledger = _snapshot(env.db), env.ledger.read_bytes()
    with pytest.raises(sqlite3.IntegrityError):
        note_placement.relocate(env.config, env.old, env.new, env.db)
    assert env.old.is_file() and not env.new.exists()
    assert _snapshot(env.db) == before and env.ledger.read_bytes() == ledger
    _assert_no_locks(env)


def test_rerun_repairs_crash_after_file_move(env):
    before = _snapshot(env.db)
    env.new.parent.mkdir(mode=0o700)
    os.rename(env.old, env.new)  # Simulate a crash before DB commit and ledger append.
    assert note_placement.relocate(env.config, env.old, env.new, env.db) == env.new
    _assert_rekeyed(before, _snapshot(env.db), env.old, env.new)
    for row in capture_state._merged_rows():
        if row["id"] in ("created", "update"):
            assert row["note_path"] == env.new.relative_to(env.vault).as_posix()
    _assert_no_locks(env)


def test_rerun_repairs_partial_ledger_append(env, monkeypatch):
    before = _snapshot(env.db)
    real_append = capture_state.ledger_append
    calls = []

    def append(row):
        calls.append(row["id"])
        if len(calls) == 2:
            raise OSError("injected ledger failure")
        return real_append(row)

    with monkeypatch.context() as patch:
        patch.setattr(capture_state, "ledger_append", append)
        with pytest.raises(OSError, match="injected ledger failure"):
            note_placement.relocate(env.config, env.old, env.new, env.db)
    assert env.new.is_file() and not env.old.exists()
    _assert_rekeyed(before, _snapshot(env.db), env.old, env.new)
    _assert_no_locks(env)
    assert note_placement.relocate(env.config, env.old, env.new, env.db) == env.new
    rows = capture_state._merged_rows()
    assert {row["note_path"] for row in rows if row["id"] in ("created", "update")} == {
        env.new.relative_to(env.vault).as_posix()
    }
    assert len(env.ledger.read_text().splitlines()) == 6  # Exactly two superseding rows in total.


def test_write_transaction_and_both_note_locks_cover_rename(env, monkeypatch):
    before = _snapshot(env.db)
    real_rename, real_append = os.rename, capture_state.ledger_append
    events = []

    def assert_locks():
        assert all(note_writer._lock_path(path).is_file() for path in (env.old, env.new))
        assert all(stat.S_IMODE(note_writer._lock_path(path).stat().st_mode) == 0o600
                   for path in (env.old, env.new))

    def rename(old, new):
        assert_locks()
        assert _snapshot(env.db) == before  # Updates have not been committed yet.
        observer = vault_index._connect(env.db)
        try:
            observer.execute("PRAGMA busy_timeout=0")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                observer.execute("BEGIN IMMEDIATE")
        finally:
            observer.close()
        events.append("rename")
        return real_rename(old, new)

    def append(row):
        assert_locks()
        _assert_rekeyed(before, _snapshot(env.db), env.old, env.new)
        events.append("ledger")
        return real_append(row)

    monkeypatch.setattr(note_placement.os, "rename", rename)
    monkeypatch.setattr(capture_state, "ledger_append", append)
    assert note_placement.relocate(env.config, env.old, env.new, env.db) == env.new
    assert events == ["rename", "ledger", "ledger"]
    _assert_no_locks(env)


def test_missing_ledger_is_empty(env):
    env.ledger.unlink()
    before = _snapshot(env.db)
    assert note_placement.relocate(env.config, env.old, env.new, env.db) == env.new
    _assert_rekeyed(before, _snapshot(env.db), env.old, env.new)
    assert not env.ledger.exists()
    _assert_no_locks(env)


def test_both_files_missing_does_not_repoint_stores(env):
    env.old.unlink()
    before, ledger = _snapshot(env.db), env.ledger.read_bytes()
    with pytest.raises(FileNotFoundError):
        note_placement.relocate(env.config, env.old, env.new, env.db)
    assert _snapshot(env.db) == before and env.ledger.read_bytes() == ledger
    _assert_no_locks(env)


def test_already_home_does_not_lock_or_write(env, monkeypatch):
    before, ledger = _snapshot(env.db), env.ledger.read_bytes()
    monkeypatch.setattr(note_writer, "_acquire_lock", lambda *a: pytest.fail("already home"))
    assert note_placement.place(env.config, env.old, "active", env.db) == env.old
    assert note_placement.relocate(env.config, env.old, env.old, env.db) == env.old
    assert _snapshot(env.db) == before and env.ledger.read_bytes() == ledger
    assert not env.new.parent.exists()


def test_symlinked_vault_keeps_the_index_path_form(env, tmp_path):
    """#465: rows keep the configured vault path, so the next _sync keeps them."""
    link = tmp_path / "vault-link"
    link.symlink_to(env.vault, target_is_directory=True)
    folder = env.config["insights_folder"]
    config = {**env.config, "vault_path": str(link)}
    old = link / folder / "linked.md"
    _write_note(old)
    db = vault_index.ensure_index(str(link), [folder], db_path=env.db)
    conn = vault_index._connect(db)
    try:
        conn.execute("INSERT INTO access_log (note_path, timestamp, context_type) "
                     "VALUES (?, 1.0, 'search')", (str(old),))
        conn.commit()
    finally:
        conn.close()
    moved = note_placement.place(config, old, "provisional", db)
    expected = str(link / folder / "_provisional" / "linked.md")
    assert str(moved) == expected
    vault_index.ensure_index(str(link), [folder], db_path=db)
    conn = vault_index._connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM notes WHERE path = ?", (expected,)).fetchone()[0] == 1
        assert [r[0] for r in conn.execute(
            "SELECT note_path FROM access_log WHERE note_path LIKE '%linked.md'")] == [expected]
    finally:
        conn.close()
