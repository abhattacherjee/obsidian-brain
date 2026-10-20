"""capture_reject.py -- undoable rejection of an auto-captured note (#425).

The Captures pane's Reject button lands here. A ``new`` note is moved to
``<insights_folder>/_rejected/`` and dropped from the search index (the
indexer never scans ``_rejected/``). An ``updated`` row removes only the
``## Update`` section capture appended, saving it under ``_rejected/``; the
note it was added to, often a hand-written one, never moves.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import capture_save
import capture_state
import note_writer
import obsidian_utils
import vault_index

REJECTED_DIR = "_rejected"


def _contained(vault: Path, rel_path: str) -> Path:
    target = (vault / rel_path).resolve()
    if not target.is_relative_to(vault.resolve()):
        raise ValueError(f"note path escapes the vault: {rel_path!r}")
    return target


def _remove_section(text: str, heading: str, marker: str = "") -> tuple[str, str]:
    """Split ``text`` into (text without the section, the section)."""
    lines = text.split("\n")
    try:
        start = lines.index(heading)
    except ValueError:
        return text, ""
    end = capture_save.section_end(lines, start, marker)
    section = "\n".join(lines[start:end]).strip("\n")
    kept = lines[:start] + lines[end:]
    while start > 0 and start < len(kept) and kept[start - 1] == "" and kept[start] == "":
        del kept[start]  # collapse the blank line pair left behind
    return "\n".join(kept), section


def _cut_section(vault: Path, folder: str, note: Path, heading: str, capture_id: str, dest: Path,
                 held: bool = False) -> str:
    """Cut the section ``heading`` opens out of ``note`` and save it as ``dest``; return the section ('' if gone).

    Under the same lock append-update takes (``held``: the caller already
    holds it), plus its stat re-check, so a capture appending to this note
    right now is never overwritten. CRLF notes stay CRLF.
    """
    lock = None
    if not held:
        lock, err = note_writer._acquire_lock(note)
        if err:
            raise RuntimeError(err)
    try:
        st = os.stat(note)
        with open(note, encoding="utf-8", newline="") as fh:
            text = fh.read()
        crlf = "\r\n" in text
        kept, section = _remove_section(text.replace("\r\n", "\n") if crlf else text,
                                        heading, capture_save.end_marker(capture_id))
        if crlf:
            kept = kept.replace("\n", "\r\n")
        if section:
            err = obsidian_utils.write_vault_note(str(vault), f"{folder}/{REJECTED_DIR}", dest.name, section + "\n")
            if err:
                raise RuntimeError(err)
            err = note_writer._atomic_rewrite(note, kept, (st.st_mtime_ns, st.st_size))
            if err:
                raise RuntimeError(err)
        return section
    finally:
        note_writer._release_lock(lock)


def reject(capture_id: str, config: dict | None = None) -> dict:
    """Reject one capture; returns the updated ledger row. Idempotent."""
    row = capture_state.ledger_get(capture_id)
    if row is None:
        raise KeyError(capture_id)
    if row.get("status") == "rejected":
        return row
    config = config or obsidian_utils.load_config()
    vault = Path(config["vault_path"])
    folder = config.get("insights_folder", "claude-insights")
    rejected_dir = vault / folder / REJECTED_DIR
    rejected_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    if row.get("kind") == "updated":
        note = _contained(vault, row["note_path"])
        dest = rejected_dir / f"{note.stem}-update-{capture_id}.md"
        lock, err = note_writer._acquire_lock(note)
        if err:
            raise RuntimeError(err)
        try:
            # #427: re-read under the lock. A split-updates --apply that finished
            # since the read above turned this row into a new note of its own.
            fresh = capture_state.ledger_get(capture_id) or row
            if fresh.get("status") == "rejected":
                return fresh
            if fresh.get("kind") == "updated":
                _cut_section(vault, folder, note, fresh.get("update_heading", ""), capture_id, dest, held=True)
                return capture_state.ledger_update(capture_id, status="rejected",
                                                   rejected_path=str(dest.relative_to(vault)))
        finally:
            note_writer._release_lock(lock)
        row = fresh

    note = _contained(vault, row["note_path"])
    dest = rejected_dir / note.name
    if note.exists():
        os.replace(note, dest)
    db = vault_index._default_db_path()
    if os.path.isfile(db):
        vault_index.remove_note(db, str(note))
    if row.get("split_from") and isinstance(row.get("split_heading"), str):
        # #427: a split whose source rewrite never ran left the section in the
        # source note too; it goes with the rejection. When the split note was
        # never written, the section is the only copy, so it is what is kept.
        source = _contained(vault, row["split_from"])
        if source.is_file():
            leftover = rejected_dir / f"{source.stem}-update-{capture_id}.md"
            if _cut_section(vault, folder, source, row["split_heading"], capture_id, leftover) and not dest.exists():
                dest = leftover
    return capture_state.ledger_update(capture_id, status="rejected", rejected_path=str(dest.relative_to(vault)))


def recent_rejections(project: str, n: int = 10) -> list[str]:
    """Titles of the project's most recent rejected captures, newest first."""
    rows = [r for r in capture_state.ledger_list(limit=10_000)
            if r.get("status") == "rejected" and r.get("project") == project]
    return [r.get("title", "") for r in rows[:n]]
