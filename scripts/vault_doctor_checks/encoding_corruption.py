"""vault_doctor check: detect notes with invalid UTF-8 encoding.

Notes with non-UTF8 bytes (from pasted terminal output, emoji sequences,
or AI-generated content with special encoding) cause grep to return
"Binary file matches" and can corrupt downstream processing. This check
finds and optionally repairs such notes by re-encoding with lossy
replacement (U+FFFD for invalid bytes).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

from . import Issue, Result
from .source_sessions import _backup_note, _iter_note_paths

NAME = "encoding-corruption"
DESCRIPTION = "Detect vault notes with invalid UTF-8 bytes that cause binary file handling"
DEFAULT_WINDOW_DAYS = 9999  # unbounded — scan all notes


def _iter_md_files(vault_root: Path, folder: str, *, recursive: bool = False) -> Iterator[Path]:
    """Insights recursively (skipping _rejected), sessions flat; see _iter_note_paths."""
    folder_path = vault_root / folder
    if not folder_path.resolve().is_relative_to(vault_root.resolve()):
        raise ValueError(f"notes folder {folder_path} escapes vault {vault_root}")
    if not folder_path.is_dir():
        return
    yield from _iter_note_paths(folder_path, recursive=recursive, root=vault_root)




def scan(
    vault_path: str,
    sessions_folder: str,
    insights_folder: str,
    days: int,
    project: str | None = None,
) -> list[Issue]:
    """Find vault notes containing invalid UTF-8 bytes."""
    vault_root = Path(vault_path)
    issues: list[Issue] = []

    for folder in [sessions_folder, insights_folder]:
        for md_file in _iter_md_files(vault_root, folder, recursive=folder == insights_folder):
            # Optional project filter
            if project and f"-{project}-" not in md_file.name:
                continue

            try:
                raw = md_file.read_bytes()
            except OSError as exc:
                issues.append(Issue(
                    check=NAME,
                    note_path=str(md_file),
                    project=project or "",
                    current_source="Unreadable file",
                    proposed_source="Check file permissions and disk health",
                    reason=f"OSError reading file: {exc}",
                    extra={"error": str(exc), "vault_path": str(vault_root.resolve())},
                ))
                continue

            try:
                raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                # Count bad bytes
                clean = raw.decode("utf-8", errors="replace")
                bad_count = clean.count("\ufffd")
                issues.append(Issue(
                    check=NAME,
                    note_path=str(md_file),
                    project=project or "",
                    current_source=f"{bad_count} invalid byte(s)",
                    proposed_source="Re-encode with U+FFFD replacement",
                    reason=f"Invalid UTF-8 at byte {exc.start}: {exc.reason}",
                    extra={"bad_byte_count": bad_count, "first_error_offset": exc.start,
                           "vault_path": str(vault_root.resolve())},
                ))

    return issues


def apply(issues: list[Issue], backup_root: str) -> list[Result]:
    """Re-encode notes with errors='replace' to fix invalid bytes."""
    results: list[Result] = []

    for issue in issues:
        note_path = issue.note_path
        try:
            raw = Path(note_path).read_bytes()
            clean = raw.decode("utf-8", errors="replace")

            # Backup
            backup_path = str(_backup_note(
                issue, backup_root, legacy_relative=Path(note_path).name,
            ))

            # Atomic write
            parent = os.path.dirname(note_path)
            fd, tmp = tempfile.mkstemp(dir=parent, suffix=".md")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(clean)
                os.chmod(tmp, 0o600)
                os.replace(tmp, note_path)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

            results.append(Result(
                check=NAME,
                note_path=note_path,
                status="applied",
                backup_path=backup_path,
            ))
        except Exception as exc:
            results.append(Result(
                check=NAME,
                note_path=note_path,
                status="error",
                error=str(exc),
            ))

    return results
