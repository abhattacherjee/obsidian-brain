"""Migration #465 never uses a live vault, DB, ledger or capture directory."""

import json
import os
import shutil
import sqlite3
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import capture_state
import note_placement
import obsidian_utils
import vault_index
from scripts import capture_provisional_relocate as migration


def _note(path, status="provisional"):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    field = "" if status is None else f"status: {status}\n"
    path.write_text(
        f"---\ntype: claude-insight\n{field}date: 2026-10-28\n"
        "project: demo\n---\n\n# Migration\n\nPreserve history.\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _db_rows(db):
    # Snapshot without switching the DB into WAL mode or creating sidecars.
    conn = sqlite3.connect(Path(db).as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in ("notes", "access_log", "theme_members")
        }
    finally:
        conn.close()


def _files(root):
    return {
        p.relative_to(root).as_posix(): (p.stat().st_mtime_ns, p.stat().st_mode,
                                       p.read_bytes() if p.is_file() else None)
        for p in [root, *root.rglob("*")]
    }


@pytest.fixture(params=["claude-insights", "knowledge/insights"])
def env(tmp_path, monkeypatch, request):
    vault = tmp_path / "vault"
    folder = vault / request.param
    folder.mkdir(parents=True, mode=0o700)
    paths = {
        "down": folder / "down.md",
        "up": folder / "_provisional" / "up.md",
        "active": folder / "_provisional" / "active.md",
        "none": folder / "_provisional" / "none.md",
        "ready": folder / "_provisional" / "ready.md",
        "stay": folder / "stay.md",
        "rejected": folder / "_rejected" / "rejected.md",
        "nested": folder / "nested" / "nested.md",
    }
    for name, path in paths.items():
        _note(path, {"up": "promoted", "active": "active", "none": None,
                     "stay": "promoted"}.get(name, "provisional"))
    config = {"vault_path": str(vault), "insights_folder": request.param,
              "sessions_folder": "sessions", "wiki_folder": ""}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    # Deliberately readable: even a config permission "fix" breaks dry-run.
    config_path.chmod(0o644)
    monkeypatch.setattr(obsidian_utils, "_CONFIG_PATH", config_path)
    secure = tmp_path / "secure"
    monkeypatch.setattr(obsidian_utils, "_SECURE_DIR", str(secure))
    capture = secure / "capture"
    capture.mkdir(parents=True, mode=0o700)
    def capture_dir():
        capture.mkdir(parents=True, exist_ok=True, mode=0o700)
        return capture

    monkeypatch.setattr(capture_state, "capture_dir", capture_dir)
    # An explicit ledger in a DIFFERENT directory must not redirect claims.
    ledger = tmp_path / "ledger.jsonl"
    db = str(tmp_path / "index.db")
    vault_index.ensure_index(str(vault), [request.param], db_path=db)
    conn = vault_index._connect(db)
    try:
        conn.execute("INSERT INTO themes (id, name, note_count, created_date, updated_date) "
                     "VALUES (1, 'Theme', 4, '2026-10-28', '2026-10-28')")
        for name in ("down", "up", "active", "none", "stay"):
            conn.execute("INSERT INTO access_log (note_path, timestamp, context_type) "
                         "VALUES (?, 1, 'search')", (str(paths[name]),))
            conn.execute("INSERT INTO theme_members (theme_id, note_path, similarity, added_date) "
                         "VALUES (1, ?, 0.8, '2026-10-28')", (str(paths[name]),))
        conn.commit()
    finally:
        conn.close()
    rows = [
        {"id": name, "note_path": path.relative_to(vault).as_posix(),
         "kind": "new", "ts": 1793167199.0}
        for name, path in paths.items()
    ]
    rows.extend([
        {"id": "update", "note_path": paths["down"].relative_to(vault).as_posix(),
         "kind": "updated", "ts": 1793167201.0},
        {"id": "down", "score": 7, "ts": 1793253600.0},
    ])
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    ledger.chmod(0o600)
    args = ["--vault", str(vault), "--db", db, "--ledger", str(ledger)]
    return SimpleNamespace(vault=vault, folder=folder, paths=paths, config=config,
                           db=db, ledger=ledger, capture=capture, args=args,
                           config_path=config_path, tmp=tmp_path)


def _run(env, capsys, *extra):
    result = migration.main([*env.args, *extra])
    captured = capsys.readouterr()
    return result, json.loads(captured.out), captured.err


def test_dry_run_counts_and_writes_nothing(env, monkeypatch, capsys):
    # The regular config loader and DB connector both have write side effects.
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: pytest.fail("config writes"))
    monkeypatch.setattr(vault_index, "_connect", lambda *a: pytest.fail("DB writes"))
    before, rows = _files(env.tmp), _db_rows(env.db)
    code, report, _ = _run(env, capsys)
    assert code == 0
    assert report == {"provisional_in_insights": 1, "non_provisional_in_provisional": 3,
                      "access_log_rows": 4, "theme_members_rows": 4,
                      "ledger_rows": 5, "name_collisions": 0}
    assert _db_rows(env.db) == rows
    assert _files(env.tmp) == before


def test_apply_places_every_note_preserves_stores_and_rerun_is_noop(env, capsys):
    before_rows = _db_rows(env.db)
    before_notes = {name: path.read_bytes() for name, path in env.paths.items()}
    ledger_prefix = env.ledger.read_bytes()
    code, report, _ = _run(env, capsys, "--apply")
    assert code == 0 and report["moved"] == 4
    assert report["verification"] == {"provisional_in_insights": 0,
                                      "access_log_rows": 0, "theme_members_rows": 0}
    backup = Path(report["db_backup"])
    assert _db_rows(backup) == before_rows
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700
    assert not (env.capture / "PAUSED").exists()
    assert not list(env.vault.rglob("*.ob-lock"))
    after_rows = _db_rows(env.db)
    assert {t: len(r) for t, r in before_rows.items()} == {
        t: len(r) for t, r in after_rows.items()}
    mapping = {}
    for name, old in env.paths.items():
        if name in ("down", "up", "active", "none"):
            status = "provisional" if name == "down" else "promoted"
            new = note_placement.home_for(env.config, old, status)
            mapping[str(old)] = str(new)
            assert not old.exists()
        else:
            new = old
        assert new.read_bytes() == before_notes[name]
    conn = vault_index._connect(env.db)
    try:
        for table, column in (("notes", "path"), ("access_log", "note_path"),
                              ("theme_members", "note_path")):
            paths = {row[0] for row in conn.execute(f"SELECT {column} FROM {table}")}
            assert not paths.intersection(mapping)
            assert set(mapping.values()) <= paths
    finally:
        conn.close()
    assert env.ledger.read_bytes().startswith(ledger_prefix)
    appended = [json.loads(line) for line in env.ledger.read_bytes()[len(ledger_prefix):].splitlines()]
    assert len(appended) == 5
    for row in appended:
        original = env.paths["down" if row["id"] == "update" else row["id"]]
        assert row["note_path"] == Path(mapping[str(original)]).relative_to(env.vault).as_posix()
        assert row["ts"] == (1793167201.0 if row["id"] == "update" else 1793167199.0)
    notes_after, ledger_after = _files(env.vault), env.ledger.read_bytes()
    code, again, _ = _run(env, capsys, "--apply")
    assert code == 0 and again["moved"] == 0 and again["repaired"] == 0
    assert env.ledger.read_bytes() == ledger_after
    assert _files(env.vault) == notes_after and _db_rows(env.db) == after_rows


def test_collision_is_counted_once_and_skipped(env, capsys):
    target = env.folder / "_provisional" / "down.md"
    _note(target, "promoted")
    before = {p: p.read_bytes() for p in (env.paths["down"], target)}
    code, report, _ = _run(env, capsys)
    assert code == 0 and report["name_collisions"] == 1
    code, report, err = _run(env, capsys, "--apply")
    assert code == 1 and report["name_collisions"] == 1
    assert report["verification"]["provisional_in_insights"] == 1
    assert {p: p.read_bytes() for p in before} == before
    assert "collision" in err.lower()


def test_pause_wait_timeout_resumes_and_does_not_index(env, monkeypatch, capsys):
    token = capture_state.claim("running")
    before, rows = env.ledger.read_bytes(), _db_rows(env.db)
    real_claims = capture_state.active_claims

    def claims():
        assert capture_state.is_paused()
        assert json.loads((env.capture / "PAUSED").read_text())["reason"] == "migration #465"
        return real_claims()

    monkeypatch.setattr(capture_state, "active_claims", claims)
    monkeypatch.setattr(vault_index, "ensure_index", lambda *a, **kw: pytest.fail("still active"))
    code, report, _ = _run(env, capsys, "--apply", "--wait", "0")
    assert code == 3 and report["active_claims"] == ["running"]
    assert not (env.capture / "PAUSED").exists()
    assert (env.capture / "running.claim").read_text() == token
    assert env.ledger.read_bytes() == before and _db_rows(env.db) == rows


def test_wait_drains_before_indexing(env, monkeypatch, capsys):
    token = capture_state.claim("finishing")
    events = []
    real_backup, real_index = migration.capture_backlog.backup_db, vault_index.ensure_index

    def backup(*args):
        assert not capture_state.is_paused()
        events.append("backup")
        return real_backup(*args)

    def sleep(seconds):
        assert 0 < seconds <= 1 and capture_state.is_paused()
        events.append("wait")
        capture_state.release("finishing", token)

    def index(*args, **kwargs):
        assert capture_state.is_paused() and capture_state.active_claims() == []
        events.append("index")
        return real_index(*args, **kwargs)

    monkeypatch.setattr(migration.capture_backlog, "backup_db", backup)
    monkeypatch.setattr(migration.time, "sleep", sleep)
    monkeypatch.setattr(vault_index, "ensure_index", index)
    code, _, _ = _run(env, capsys, "--apply", "--wait", "1")
    assert code == 0 and events == ["backup", "wait", "index"]
    assert not (env.capture / "PAUSED").exists()


def test_verification_failure_when_place_does_nothing(env, monkeypatch, capsys):
    monkeypatch.setattr(note_placement, "place", lambda config, path, status, db: path)
    code, report, _ = _run(env, capsys, "--apply")
    assert code == 1 and report["verification"]["provisional_in_insights"] == 1
    assert not (env.capture / "PAUSED").exists()


@pytest.mark.parametrize("table", ["access_log", "theme_members"])
def test_verification_checks_old_paths_even_if_place_returns_success(env, monkeypatch, capsys, table):
    real_place = note_placement.place

    def place(config, path, status, db):
        moved = real_place(config, path, status, db)
        if path == env.paths["down"]:
            conn = vault_index._connect(db)
            try:
                conn.execute(f"UPDATE {table} SET note_path = ? WHERE note_path = ?",
                             (str(path), str(moved)))
                conn.commit()
            finally:
                conn.close()
        return moved

    monkeypatch.setattr(note_placement, "place", place)
    code, report, _ = _run(env, capsys, "--apply")
    assert code == 1 and report["verification"][f"{table}_rows"] == 1
    assert not (env.capture / "PAUSED").exists()


def test_frontmatter_wins_over_stale_db_status(env, capsys):
    conn = vault_index._connect(env.db)
    try:
        conn.execute("UPDATE notes SET status = 'promoted' WHERE path = ?",
                     (str(env.paths["down"]),))
        conn.commit()
    finally:
        conn.close()
    code, report, _ = _run(env, capsys)
    assert code == 0 and report["provisional_in_insights"] == 1
    code, report, _ = _run(env, capsys, "--apply")
    assert code == 0 and report["moved"] == 4
    assert (env.folder / "_provisional" / "down.md").is_file()


def test_missing_ledger_is_empty_and_dry_run_does_not_create_capture_dir(env, capsys):
    env.ledger.unlink()
    env.capture.rmdir()
    before = _files(env.tmp)
    code, report, _ = _run(env, capsys)
    assert code == 0 and report["ledger_rows"] == 0
    assert _files(env.tmp) == before
    code, _, _ = _run(env, capsys, "--apply")
    assert code == 0 and not env.ledger.exists()


def test_defaults_read_config_db_and_capture_ledger_without_writing(env, monkeypatch, capsys):
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", env.db)
    default = env.capture / capture_state.LEDGER_NAME
    env.ledger.rename(default)
    before = _files(env.tmp)
    assert migration.main([]) == 0
    assert json.loads(capsys.readouterr().out)["ledger_rows"] == 5
    assert _files(env.tmp) == before
    assert migration.main(["--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["moved"] == 4
    assert not (env.capture / "PAUSED").exists()


def test_rerun_repairs_crash_before_sync_can_prune_history(env, capsys):
    old = env.paths["down"]
    new = env.folder / "_provisional" / old.name
    os.rename(old, new)  # File moved, but DB and ledger still use the old home.
    code, report, _ = _run(env, capsys, "--apply")
    assert code == 0 and report["repaired"] == 1
    conn = vault_index._connect(env.db)
    try:
        for table in ("access_log", "theme_members"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE note_path = ?",
                                (str(new),)).fetchone()[0] == 1
    finally:
        conn.close()
    rows = [json.loads(line) for line in env.ledger.read_text().splitlines()]
    assert [row for row in rows if row["id"] == "down"][-1]["note_path"] == new.relative_to(env.vault).as_posix()


def test_rerun_repairs_ledger_even_when_file_and_db_already_moved(env, capsys):
    old = env.paths["down"]
    with migration._using_ledger(env.ledger):
        new = note_placement.place(env.config, old, "provisional", env.db)
        # Only one of the two IDs was rewritten before the interrupted append.
        capture_state.ledger_append({"id": "update", "note_path": old.relative_to(env.vault).as_posix()})
    code, report, _ = _run(env, capsys, "--apply")
    assert code == 0 and report["repaired"] == 1
    with migration._using_ledger(env.ledger):
        row = capture_state.ledger_get("update")
        assert row["note_path"] == new.relative_to(env.vault).as_posix()
        assert row["ts"] == 1793167201.0


def test_locked_crash_recovery_does_not_allow_sync_to_prune(env, monkeypatch, capsys):
    old = env.paths["down"]
    new = env.folder / "_provisional" / old.name
    os.rename(old, new)
    lock, error = note_placement.note_writer._acquire_lock(new)
    assert error is None
    before, ledger_before = _db_rows(env.db), env.ledger.read_bytes()
    monkeypatch.setattr(vault_index, "ensure_index", lambda *a, **kw: pytest.fail("would prune history"))
    try:
        assert migration.main([*env.args, "--apply"]) == 1
        assert "still locked" in capsys.readouterr().err
        assert not (env.capture / "PAUSED").exists()
        assert _db_rows(env.db) == before and env.ledger.read_bytes() == ledger_before
    finally:
        note_placement.note_writer._release_lock(lock)


def test_exception_after_pause_always_resumes(env, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise OSError("injected indexing failure")

    monkeypatch.setattr(vault_index, "ensure_index", fail)
    assert migration.main([*env.args, "--apply"]) == 1
    assert "injected indexing failure" in capsys.readouterr().err
    assert not (env.capture / "PAUSED").exists()


def test_backup_failure_does_not_pause_or_move(env, monkeypatch, capsys):
    def fail(*args):
        raise OSError("injected backup failure")

    before = _files(env.tmp)
    monkeypatch.setattr(migration.capture_backlog, "backup_db", fail)
    assert migration.main([*env.args, "--apply"]) == 1
    assert "injected backup failure" in capsys.readouterr().err
    assert _files(env.tmp) == before


@pytest.mark.parametrize("args", [["--wait", "-1"], ["--wait", "nan"], ["--wait"], ["--unknown"]])
def test_argparse_usage_errors_exit_two(env, args):
    with pytest.raises(SystemExit) as exc:
        migration.main([*env.args, *args])
    assert exc.value.code == 2


@pytest.mark.parametrize("bad", ["vault", "db", "folder", "symlink", "ledger"])
def test_invalid_inputs_are_usage_errors_before_writes(env, bad):
    args = list(env.args)
    if bad in ("vault", "db"):
        args[args.index(f"--{bad}") + 1] = str(env.tmp / "missing")
    elif bad == "folder":
        env.config_path.write_text(json.dumps({**env.config, "insights_folder": "../escape"}))
    elif bad == "ledger":
        args[args.index("--ledger") + 1] = str(env.tmp)
    else:
        outside = env.tmp / "outside.md"
        _note(outside)
        (env.folder / "escape.md").symlink_to(outside)
    before = _files(env.tmp)
    with pytest.raises(SystemExit) as exc:
        migration.main([*args, "--apply"])
    assert exc.value.code == 2 and _files(env.tmp) == before


def test_frontmatter_quotes_comments_and_body_forgery(env, capsys):
    _note(env.paths["down"], "'Provisional' # waiting")
    _note(env.paths["none"], None)
    with env.paths["none"].open("a", encoding="utf-8") as fh:
        fh.write("\nstatus: provisional\n")
    code, report, _ = _run(env, capsys, "--apply")
    assert code == 0 and report["moved"] == 4
    assert (env.folder / "_provisional" / "down.md").is_file()
    assert (env.folder / "none.md").is_file()


def test_expired_claim_does_not_block(env, capsys):
    capture_state.claim("expired")
    old = time.time() - capture_state.CLAIM_TTL_SECONDS - 1
    os.utime(env.capture / "expired.claim", (old, old))
    code, _, _ = _run(env, capsys, "--apply", "--wait", "0")
    assert code == 0 and (env.capture / "expired.claim").exists()


def test_malformed_frontmatter_does_not_fail_open_to_promoted(env):
    env.paths["ready"].write_text("---\nstatus: provisional\n\n# Missing fence\n")
    before = _files(env.tmp)
    with pytest.raises(SystemExit) as exc:
        migration.main([*env.args, "--apply"])
    assert exc.value.code == 2 and _files(env.tmp) == before


def test_plain_markdown_without_frontmatter_has_no_status(env, capsys):
    env.paths["none"].write_text("# Plain note\n\nstatus: provisional\n")
    code, report, _ = _run(env, capsys, "--apply")
    assert code == 0 and report["moved"] == 4
    assert (env.folder / "none.md").is_file()


def test_dry_run_sees_uncheckpointed_wal_rows_without_writes(env, capsys):
    conn = vault_index._connect(env.db)
    try:
        conn.execute("INSERT INTO access_log (note_path, timestamp, context_type) "
                     "VALUES (?, 2, 'search')", (str(env.paths["down"]),))
        conn.commit()
        # SQLite read locks use transient shm reader marks. Its mtime, the
        # WAL, main DB, rows and every other input must remain unchanged.
        before = _files(env.tmp)
        code, report, _ = _run(env, capsys)
        assert code == 0 and report["access_log_rows"] == 5
        after = _files(env.tmp)
        assert after["index.db-shm"][:2] == before["index.db-shm"][:2]
        del before["index.db-shm"], after["index.db-shm"]
        assert after == before
        assert conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0] == 6
    finally:
        conn.close()


def test_copies_with_original_absolute_db_paths(env, capsys):
    copied_vault, copied_db = env.tmp / "vault-copy", env.tmp / "copy.db"
    copied_ledger = env.tmp / "copy.jsonl"
    shutil.copytree(env.vault, copied_vault)
    shutil.copy2(env.db, copied_db)
    shutil.copy2(env.ledger, copied_ledger)
    sources = (_files(env.vault), _db_rows(env.db), env.ledger.read_bytes())
    args = ["--vault", str(copied_vault), "--db", str(copied_db), "--ledger", str(copied_ledger)]
    before = _files(env.tmp)
    assert migration.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["access_log_rows"] == 4 and report["theme_members_rows"] == 4
    assert _files(env.tmp) == before
    assert migration.main([*args, "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["moved"] == 4
    assert (copied_vault / env.config["insights_folder"] / "_provisional" / "down.md").is_file()
    for rows in _db_rows(copied_db).values():
        assert rows
    conn = vault_index._connect(str(copied_db))
    try:
        for table, column in (("notes", "path"), ("access_log", "note_path"), ("theme_members", "note_path")):
            assert all(Path(row[0]).is_relative_to(copied_vault)
                       for row in conn.execute(f"SELECT {column} FROM {table}"))
    finally:
        conn.close()
    assert (_files(env.vault), _db_rows(env.db), env.ledger.read_bytes()) == sources
