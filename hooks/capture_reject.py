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


def _section_end(lines: list[str], start: int, marker: str) -> int:
    """Index just past the section that opens at ``start``.

    Through the capture's own end marker when it has one; otherwise (an
    update written before the marker existed) up to the next ``## `` heading
    outside a fenced code block.
    """
    if marker:
        for i in range(start + 1, len(lines)):
            if lines[i].strip() == marker:
                return i + 1
    fence = None
    for i in range(start + 1, len(lines)):
        fence, is_fence = capture_save.fence_step(fence, lines[i])
        if not is_fence and fence is None and lines[i].startswith("## "):
            return i
    return len(lines)


def _remove_section(text: str, heading: str, marker: str = "") -> tuple[str, str]:
    """Split ``text`` into (text without the section, the section)."""
    lines = text.split("\n")
    try:
        start = lines.index(heading)
    except ValueError:
        return text, ""
    end = _section_end(lines, start, marker)
    section = "\n".join(lines[start:end]).strip("\n")
    kept = lines[:start] + lines[end:]
    while start > 0 and start < len(kept) and kept[start - 1] == "" and kept[start] == "":
        del kept[start]  # collapse the blank line pair left behind
    return "\n".join(kept), section


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
    note = _contained(vault, row["note_path"])
    rejected_dir = vault / folder / REJECTED_DIR
    rejected_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    if row.get("kind") == "updated":
        dest = rejected_dir / f"{note.stem}-update-{capture_id}.md"
        # The same lock append-update takes, plus its stat re-check, so a capture
        # appending to this note right now is never overwritten.
        lock, err = note_writer._acquire_lock(note)
        if err:
            raise RuntimeError(err)
        try:
            st = os.stat(note)
            with open(note, encoding="utf-8", newline="") as fh:
                text = fh.read()
            crlf = "\r\n" in text
            kept, section = _remove_section(text.replace("\r\n", "\n") if crlf else text,
                                            row.get("update_heading", ""), capture_save.end_marker(capture_id))
            if crlf:
                kept = kept.replace("\n", "\r\n")
            if section:
                err = obsidian_utils.write_vault_note(str(vault), f"{folder}/{REJECTED_DIR}", dest.name, section + "\n")
                if err:
                    raise RuntimeError(err)
                err = note_writer._atomic_rewrite(note, kept, (st.st_mtime_ns, st.st_size))
                if err:
                    raise RuntimeError(err)
        finally:
            note_writer._release_lock(lock)
    else:
        dest = rejected_dir / note.name
        if note.exists():
            os.replace(note, dest)
        db = vault_index._default_db_path()
        if os.path.isfile(db):
            vault_index.remove_note(db, str(note))
    rel_dest = str(dest.relative_to(vault))
    return capture_state.ledger_update(capture_id, status="rejected", rejected_path=rel_dest)


def recent_rejections(project: str, n: int = 10) -> list[str]:
    """Titles of the project's most recent rejected captures, newest first."""
    rows = [r for r in capture_state.ledger_list(limit=10_000)
            if r.get("status") == "rejected" and r.get("project") == project]
    return [r.get("title", "") for r in rows[:n]]
