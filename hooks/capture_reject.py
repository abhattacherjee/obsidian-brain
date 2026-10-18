"""capture_reject.py -- undoable rejection of an auto-captured note (#425).

The Captures pane's Reject button lands here. A ``new`` note is moved to
``<insights_folder>/_rejected/`` and dropped from the search index (the
indexer never scans ``_rejected/``). An ``updated`` row removes only the
``## Update`` section capture appended, saving it under ``_rejected/``; the
note it was added to, often a hand-written one, never moves.
"""

from __future__ import annotations

import os
from pathlib import Path

import capture_state
import obsidian_utils
import vault_index

REJECTED_DIR = "_rejected"


def _contained(vault: Path, rel_path: str) -> Path:
    target = (vault / rel_path).resolve()
    if not target.is_relative_to(vault.resolve()):
        raise ValueError(f"note path escapes the vault: {rel_path!r}")
    return target


def _remove_section(text: str, heading: str) -> tuple[str, str]:
    """Split ``text`` into (text without the section, the section)."""
    lines = text.split("\n")
    try:
        start = lines.index(heading)
    except ValueError:
        return text, ""
    end = start + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
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
        text = note.read_text(encoding="utf-8")
        kept, section = _remove_section(text, row.get("update_heading", ""))
        dest = rejected_dir / f"{note.stem}-update-{capture_id}.md"
        if section:
            err = obsidian_utils.write_vault_note(str(vault), f"{folder}/{REJECTED_DIR}", dest.name, section + "\n")
            if err:
                raise RuntimeError(err)
            rel_note = str(note.relative_to(vault.resolve()))
            err = obsidian_utils.write_vault_note(str(vault), str(Path(rel_note).parent), note.name, kept)
            if err:
                raise RuntimeError(err)
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
