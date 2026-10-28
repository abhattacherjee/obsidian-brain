#!/usr/bin/env python3
"""Move #465 insight notes to their frontmatter status's R1 home.

Dry run (the default) prints one JSON object and changes no input files,
permissions, caches or DB journals. --vault, --db and --ledger can point at
copies; omitted arguments use the configured vault, default index DB and
capture ledger. --apply backs up the DB beside it, pauses capture, drains
claims, places notes and verifies, always removing PAUSED afterwards.

Exit codes: 0 ok, 1 verification/operational failure, 2 usage error,
3 capture still active after --wait seconds (default 600).
"""

from __future__ import annotations

import sys

# A dry run must not create hook-module bytecode caches either.
sys.dont_write_bytecode = True

import argparse
import copy
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))

import capture_backlog  # noqa: E402
import capture_state  # noqa: E402
import note_placement  # noqa: E402
import note_writer  # noqa: E402
import obsidian_utils  # noqa: E402
import vault_index  # noqa: E402
from frontmatter import split_frontmatter  # noqa: E402
from obsidian_utils import indexed_folders  # noqa: E402


def _load_config() -> dict:
    """Use the normal defaults/config, without load_config's cache/chmod writes."""
    config = copy.deepcopy(obsidian_utils._DEFAULTS)
    try:
        with open(obsidian_utils._CONFIG_PATH, encoding="utf-8") as fh:
            user = json.load(fh)
    except FileNotFoundError:
        return config
    if not isinstance(user, dict):
        raise ValueError("config must be a JSON object")
    config.update(user)
    return config


@contextmanager
def _using_ledger(path: Path):
    """Redirect only ledger I/O; pause/claims must keep using capture_dir()."""
    original = capture_state._ledger_path
    capture_state._ledger_path = lambda: path
    try:
        yield
    finally:
        capture_state._ledger_path = original


@contextmanager
def _read_db(db: str):
    """Read without _connect's WAL conversion or new -wal/-shm files.

    SQLite mode=ro alone creates sidecars even for a checkpointed WAL DB.
    immutable avoids that when there is no WAL to read. With a live WAL,
    use its existing shared-memory index, so committed WAL rows count too.
    A WAL copy without its index must be checkpointed before inspection;
    silently ignoring that WAL would give incorrect counts.
    """
    wal, shm = Path(db + "-wal"), Path(db + "-shm")
    has_wal = wal.is_file() and wal.stat().st_size > 0
    if has_wal and not shm.is_file():
        raise ValueError("DB has a WAL without its shared-memory index; checkpoint the copy first")
    options = "mode=ro" if has_wal else "mode=ro&immutable=1"
    conn = sqlite3.connect(Path(db).as_uri() + "?" + options, uri=True, timeout=5.0)  # noqa: vault-db-connect -- read-only dry run; _connect would write WAL
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def _notes(config: dict) -> list[tuple[Path, str | None]]:
    """Only the two R1 homes; rejected and other nested notes stay untouched."""
    _, root = note_placement._insights_root(config)
    provisional = root / note_placement.PROVISIONAL_DIR
    if provisional.is_symlink():
        raise ValueError("_provisional must not be a symlink")
    notes = []
    for parent in (root, provisional):
        for path in sorted(parent.glob("*.md")):
            # Refuse aliases rather than letting resolve() move a different note.
            if path.is_symlink():
                raise ValueError(f"insight note must not be a symlink: {path.name}")
            if not path.is_file():
                continue
            home = note_placement.home_for(config, path, None)
            if home is None:
                raise ValueError(f"note is outside the R1 homes: {path.name}")
            # The scalar helper returns None both for an absent status and
            # for a failed parse. Do not promote an unreadable/malformed
            # provisional note into the synced folder on that fallback.
            lines, _, _ = obsidian_utils._read_frontmatter_region(path)
            if lines is None:
                raise OSError(f"cannot read frontmatter: {path.name}")
            if lines and lines[0].rstrip("\r\n") == "---" and split_frontmatter(lines)[-1]:
                raise ValueError(f"cannot determine frontmatter status: {path.name}")
            fields = obsidian_utils._peek_frontmatter_fields(path, ("status",))
            notes.append((path, obsidian_utils.normalize_status(fields["status"])))
    return notes


def _path_forms(config: dict, paths) -> set[str]:
    vault = Path(config["vault_path"]).resolve()
    forms = set()
    for path in paths:
        resolved = Path(path).resolve()
        forms.update((str(resolved), note_placement._db_form(config, vault, resolved)))
        # A copied DB still contains the configured source vault's absolute
        # paths. Count those too, without opening anything in the source vault.
        if config.get("_migration_source_vault"):
            source = Path(config["_migration_source_vault"])
            relative = resolved.relative_to(vault)
            forms.update((str(source / relative), str(source.resolve() / relative)))
    return forms


def _path_counts(conn, paths: set[str]) -> dict[str, int]:
    # Scan once per table instead of an IN clause exceeding SQLite's bind limit
    # on a vault with thousands of misplaced notes.
    return {f"{table}_rows": sum(row[0] in paths for row in conn.execute(
        f"SELECT note_path FROM {table}")) for table in ("access_log", "theme_members")}


def _plan(config: dict, db: str) -> dict[str, int]:
    notes = _notes(config)
    misplaced = [(path, status) for path, status in notes
                 if note_placement.home_for(config, path, status) != path.resolve()]
    vault, root = note_placement._insights_root(config)
    relative = {path.resolve().relative_to(vault).as_posix() for path, _ in misplaced}
    collisions = {path.name for path, status in misplaced
                  if note_placement.home_for(config, path, status).exists()}
    with _read_db(db) as conn:
        counts = _path_counts(conn, _path_forms(config, [path for path, _ in misplaced]))
    return {
        "provisional_in_insights": sum(path.resolve().parent == root and status == "provisional"
                                       for path, status in notes),
        "non_provisional_in_provisional": sum(path.resolve().parent != root and status != "provisional"
                                             for path, status in notes),
        **counts,
        "ledger_rows": sum(row.get("note_path") in relative for row in capture_state._merged_rows()),
        "name_collisions": len(collisions),
    }


def _repair_before_sync(config: dict, db: str) -> set[Path]:
    """Finish interrupted moves before ensure_index can prune their history.

    Calling place on the already-moved file is a no-op. relocate's missing-old
    recovery is needed for DB paths and for a partially appended ledger alike.
    """
    with _read_db(db) as conn:
        stored = {row[0] for table, column in (("notes", "path"), ("access_log", "note_path"),
                                              ("theme_members", "note_path"))
                  for row in conn.execute(f"SELECT {column} FROM {table}")}
    ledger_paths = {row.get("note_path") for row in capture_state._merged_rows()}
    vault, root = note_placement._insights_root(config)
    repaired = set()
    for path, status in _notes(config):
        if note_placement.home_for(config, path, status) != path.resolve():
            continue
        other = (root if path.resolve().parent != root else root / note_placement.PROVISIONAL_DIR) / path.name
        if other.exists():
            continue
        if (_path_forms(config, [other]) & stored
                or other.relative_to(vault).as_posix() in ledger_paths):
            new = note_placement.relocate(config, other, path, db)
            if new.resolve() == path.resolve():
                repaired.add(other)
            else:
                # A held lock must not allow ensure_index to delete old rows.
                raise RuntimeError(f"interrupted move is still locked: {path.name}; retry migration")
    return repaired


def _rebase_copy(config: dict, db: str) -> None:
    """Align a copied DB's absolute keys with --vault before syncing that copy.

    Ledger paths are already vault-relative. Only the explicitly selected DB
    changes; no files in the configured source vault are opened or moved.
    """
    if not config.get("_migration_source_vault"):
        return
    source = Path(config["_migration_source_vault"])
    sources = {str(source) + "/", str(source.resolve()) + "/"}
    target = config["vault_path"].rstrip("/") + "/"
    conn = vault_index._connect(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table, column in (("notes", "path"), ("access_log", "note_path"),
                              ("theme_members", "note_path")):
            for prefix in sorted(sources, key=len, reverse=True):
                conn.execute(
                    f"UPDATE {table} SET {column} = ? || substr({column}, ?) "
                    f"WHERE substr({column}, 1, ?) = ? AND substr({column}, 1, ?) != ?",
                    (target, len(prefix) + 1, len(prefix), prefix, len(target), target),
                )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _verify(config: dict, db: str, moved: set[Path]) -> dict[str, int]:
    _, root = note_placement._insights_root(config)
    remaining = sum(path.resolve().parent == root and status == "provisional"
                    for path, status in _notes(config))
    with _read_db(db) as conn:
        return {"provisional_in_insights": remaining,
                **_path_counts(conn, _path_forms(config, moved))}


def _apply(config: dict, db: str, wait: int, report: dict) -> int:
    # As in capture_provisional_backfill: verified SQLite online backup using
    # vault_index._connect/_backup_db, with 0o600 files in a 0o700 directory.
    backup_dir = Path(db).parent / "capture-provisional-relocate-backups"
    if not backup_dir.resolve().is_relative_to(Path(db).parent):
        raise ValueError("DB backup directory resolves outside the DB directory")
    report["db_backup"] = capture_backlog.backup_db(db, backup_dir)
    report.update(moved=0, repaired=0)
    try:
        capture_state.pause("migration #465")
        deadline = time.monotonic() + wait
        while True:
            claims = capture_state.active_claims()
            if not claims:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                report["active_claims"] = claims
                print("capture still active; migration skipped", file=sys.stderr)
                return 3
            time.sleep(min(1.0, remaining))

        report.update(_plan(config, db))  # Include any work finished during the drain.
        # A crash between rename and DB commit/ledger append must be repaired
        # BEFORE the sync, which otherwise deletes the missing old path's rows.
        _rebase_copy(config, db)
        moved = _repair_before_sync(config, db)
        report["repaired"] = len(moved)
        vault_index.ensure_index(config["vault_path"], indexed_folders(config), db_path=db)
        for path, status in _notes(config):
            new = note_placement.place(config, path, status, db)
            if new.resolve() != path.resolve():
                moved.add(path)
                report["moved"] += 1
        report["verification"] = _verify(config, db, moved)
        return 1 if any(report["verification"].values()) else 0
    finally:
        capture_state.resume()


def _wait_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--wait must be a non-negative integer")
    if seconds < 0:
        raise argparse.ArgumentTypeError("--wait must be a non-negative integer")
    return seconds


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, help="vault copy (default: configured vault)")
    parser.add_argument("--db", type=Path, help="DB copy (default: OBSIDIAN_BRAIN_DB or live DB)")
    parser.add_argument("--ledger", type=Path, help="ledger copy (default: capture ledger)")
    parser.add_argument("--apply", action="store_true", help="back up, pause capture, then migrate")
    parser.add_argument("--wait", type=_wait_seconds, default=600, help="claim drain timeout in seconds")
    args = parser.parse_args(argv)
    try:
        config = _load_config()
        source_vault = config.get("vault_path")
        vault = args.vault or config.get("vault_path")
        if not vault or not Path(vault).expanduser().is_dir():
            raise ValueError("vault not found; use --vault or run /obsidian-setup")
        # Preserve a configured symlink spelling: the index uses that path form.
        config["vault_path"] = str(Path(vault).expanduser().absolute())
        if (args.vault and source_vault
                and Path(source_vault).expanduser().resolve() != Path(config["vault_path"]).resolve()):
            config["_migration_source_vault"] = str(Path(source_vault).expanduser().absolute())
        db = str(Path(args.db or vault_index._default_db_path()).expanduser().resolve())
        if not Path(db).is_file():
            raise ValueError(f"index DB not found: {db}")
        # capture_dir() creates its directory. Derive its default without I/O.
        ledger = (args.ledger or Path(obsidian_utils._SECURE_DIR) / "capture" / capture_state.LEDGER_NAME)
        ledger = ledger.expanduser().resolve()
        if (ledger.is_dir() or ledger == Path(db)
                or (ledger.exists() and ledger.samefile(db))):
            raise ValueError("--ledger must be a ledger file, distinct from --db")
        root = Path(config["vault_path"]).resolve()
        for folder in indexed_folders(config, strict=True):
            error = note_writer._validate_folder(folder)
            if error or not (root / folder).resolve().is_relative_to(root):
                raise ValueError(error or "indexed folder resolves outside the vault")
        _notes(config)  # Validate containment before any backup or pause.
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))

    try:
        with _using_ledger(ledger):
            report = _plan(config, db)
            result = _apply(config, db, args.wait, report) if args.apply else 0
        print(json.dumps(report, sort_keys=True))
        return result
    except Exception as exc:  # Report failures and retain --apply's finally cleanup.
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
