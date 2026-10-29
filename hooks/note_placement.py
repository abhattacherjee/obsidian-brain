"""Move insight notes between the two R1 homes, preserving path-keyed stores.

The caller supplies the frontmatter status and an initialized index DB path.
The DB status cache does not determine placement. A failed ledger append is
reported to the caller; rerunning ``relocate`` repairs it after the file move.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import capture_state
import note_writer
import obsidian_utils
import vault_index

PROVISIONAL_DIR = "_provisional"


def _insights_root(config: dict) -> tuple[Path, Path]:
    vault = Path(config["vault_path"]).resolve()
    folder = config.get("insights_folder", "claude-insights")
    error = note_writer._validate_folder(folder)
    if error:
        raise ValueError(error)
    root = (vault / folder).resolve()
    if not root.is_relative_to(vault) or root == vault:
        raise ValueError("insights_folder must resolve inside the vault")
    if any(part.startswith(".") for part in root.relative_to(vault).parts):
        raise ValueError("insights_folder must not resolve to a hidden folder")
    return vault, root


def _db_form(config: dict, vault: Path, path: Path) -> str:
    """The path string vault_index stores: the configured vault path, unresolved."""
    return str(Path(config["vault_path"]) / path.relative_to(vault))


def _is_r1_home(path: Path, root: Path) -> bool:
    return (
        path.is_relative_to(root)
        and path.parent in (root, root / PROVISIONAL_DIR)
        and path != root / PROVISIONAL_DIR
        and not path.is_dir()
    )


def home_for(config: dict, abs_path: str | Path, status: str | None) -> Path | None:
    """Return the note's R1 home, or None for a note outside the two homes.

    This is read-only. Only ``provisional`` belongs in ``_provisional/``;
    every other status (including no status) belongs directly under insights.
    """
    _, root = _insights_root(config)
    path = Path(abs_path).resolve()
    if not _is_r1_home(path, root):
        return None
    parent = root / PROVISIONAL_DIR if status == "provisional" else root
    return parent / path.name


def relocate(config: dict, old_abs: str | Path, new_abs: str | Path, db: str | Path) -> Path:
    """Move one basename between R1 homes and repair DB/ledger paths.

    Locks or collisions return the old path without changing any stores.
    Other failures raise, releasing both note locks and rolling back the DB
    transaction. If the rename already happened, a rerun repairs the stores.
    """
    vault, root = _insights_root(config)
    old, new = Path(old_abs).resolve(), Path(new_abs).resolve()
    if not all(_is_r1_home(path, root) for path in (old, new)):
        raise ValueError("both note paths must resolve to R1 homes inside insights_folder")
    if old.name != new.name or (old != new and old.parent == new.parent):
        raise ValueError("placement only moves the same basename between R1 homes")
    if old == new:
        return old

    # The destination (or the old home during crash recovery) may not exist
    # yet. Its sibling note lock needs the directory before it can be taken.
    (root / PROVISIONAL_DIR).mkdir(mode=0o700, exist_ok=True)
    locks = []
    try:
        for path in (old, new):
            lock, error = note_writer._acquire_lock(path)
            if error:
                print(f"[note-placement] {error}", file=sys.stderr)
                return old
            locks.append(lock)

        old_exists, new_exists = old.exists(), new.exists()
        if old_exists and new_exists:
            print(f"[note-placement] collision: {old} -> {new}; nothing moved", file=sys.stderr)
            return old
        if not old_exists and not new_exists:
            raise FileNotFoundError(f"neither note exists: {old} or {new}")
        if old_exists:
            # The caller's status was read before it released its note lock;
            # a hand edit may have landed since. The file under both locks wins.
            current = obsidian_utils.normalize_status(
                obsidian_utils._peek_frontmatter_fields(old, ("status",))["status"])
            if home_for(config, old, current) != new:
                print(f"[note-placement] status changed under the lock: {old} stays; nothing moved",
                      file=sys.stderr)
                return old

        # vault_index._sync keys rows on the configured (unresolved) vault
        # path. Write that form, and match either form, so a symlinked vault
        # does not leave a row the next sync prunes along with its history.
        db_old, db_new = _db_form(config, vault, old), _db_form(config, vault, new)
        olds = (db_old, str(old))
        conn = vault_index._connect(str(db))
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                new_indexed = conn.execute(
                    "SELECT 1 FROM notes WHERE path = ?", (db_new,)).fetchone() is not None
                if not new_indexed:
                    conn.execute("UPDATE notes SET path = ? WHERE path IN (?, ?)", (db_new, *olds))
                conn.execute("UPDATE access_log SET note_path = ? WHERE note_path IN (?, ?)", (db_new, *olds))
                if new_indexed:
                    # The row at the new path is canonical. Memberships it
                    # already has win; leftovers are duplicates, deleted
                    # directly so no theme centroid is reversed.
                    conn.execute("UPDATE OR IGNORE theme_members SET note_path = ? "
                                 "WHERE note_path IN (?, ?)", (db_new, *olds))
                    conn.execute("DELETE FROM theme_members WHERE note_path IN (?, ?)", olds)
                    for form in dict.fromkeys(olds):
                        if conn.execute("SELECT 1 FROM notes WHERE path = ?", (form,)).fetchone():
                            vault_index._delete_note(conn, form)
                else:
                    conn.execute("UPDATE theme_members SET note_path = ? WHERE note_path IN (?, ?)", (db_new, *olds))
                if old_exists:
                    os.rename(old, new)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()

        old_rel, new_rel = old.relative_to(vault).as_posix(), new.relative_to(vault).as_posix()
        for row in capture_state._merged_rows():
            if row.get("note_path") == old_rel:
                capture_state.ledger_append({**row, "note_path": new_rel})
        return Path(db_new)
    finally:
        for lock in reversed(locks):
            note_writer._release_lock(lock)


def place(config: dict, abs_path: str | Path, status: str | None, db: str | Path) -> Path:
    """Place a note by frontmatter status and return the path it now has."""
    path = Path(abs_path)
    home = home_for(config, path, status)
    if home is None or home == path.resolve():
        return path
    return relocate(config, path, home, db)
