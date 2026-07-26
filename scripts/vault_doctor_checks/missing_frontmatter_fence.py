"""vault_doctor check: repair notes whose frontmatter lost its opening ``---``.

Nine notes in the live vault (7 ``/retro`` notes, 2 insights — issue #276)
have no opening frontmatter fence: their first byte is the first frontmatter
key (``type: claude-retro``). To any YAML frontmatter parser those notes have
NO frontmatter at all — ``type``/``date``/``project``/``tags`` are body text,
so the notes are invisible to tag-based Dataview queries and to every vault
tool that filters on frontmatter.

Cause: the leading-fence-eaten failure mode. When note content was handed to
a sub-agent to transcribe verbatim, the opening ``---`` was consumed as a
prompt/content delimiter and never reached disk. PR #273 stops NEW
occurrences (``note_writer.py write`` rejects content that does not begin
with a ``---`` fence at column 0); this check repairs the existing damage.

Repair = insert ``---`` as a new first line. Nothing else changes: not one
byte of the rest of the file.

Detection is deliberately conservative — a false positive injects a stray
``---`` into a healthy note. All four preconditions must hold:

  1. The first line is NOT ``---`` (an opened note is not our business).
  2. The first line is frontmatter-shaped (``key:``).
  3. A closing ``---`` fence exists later in the file, within the line bound.
     This is what makes the intent unambiguous: without a closing fence there
     is no way to distinguish a fenceless-frontmatter note from an ordinary
     markdown file that happens to open with a colon line.
  4. Every line above that closing fence is frontmatter-shaped (``key:`` /
     ``- item`` / indented continuation / blank).

A note whose first line carries a UTF-8 BOM fails (2) and is left alone: the
BOM would sit above the inserted fence and the repaired note would still not
parse. Notes with invalid UTF-8 are skipped — that is
``encoding-corruption``'s job, and this check must not re-encode anything.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from . import Issue, Result

NAME = "missing-frontmatter-fence"
DESCRIPTION = (
    "Detect and repair notes whose frontmatter is missing its opening '---' fence"
)
DEFAULT_WINDOW_DAYS = 9999  # unbounded — the damage is historic; scan all notes

# Frontmatter line shapes, kept byte-identical to hooks/note_writer.py's
# _FM_KEY_RE / _FM_ITEM_RE / _FM_CONT_RE. note_writer is the consumer that
# has to parse the repaired note, so a shape this check accepts but
# note_writer rejects would produce a "repair" that still fails to load.
_FM_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*\s*:")
_FM_ITEM_RE = re.compile(r"^\s*-\s")
_FM_CONT_RE = re.compile(r"^\s+\S")

# Bound on how far to search for the closing fence. Same value and same
# reasoning as note_writer's ``_FM_MAX_LINES = 1000``: it is not a "notes are
# small" assumption but a guard against a pathological file whose body is all
# key-shaped lines. note_writer's own bound was raised from 200 to 1000 after
# 200 false-rejected 11 well-formed live notes (/emerge and /standup output
# with long ``projects:`` lists, closing fences as deep as line 460).
_FM_MAX_LINES = 1000


def _split_lines_lf_crlf(text: str) -> list[str]:
    """Split ``text`` into lines with terminators attached, recognizing ONLY
    ``\\r\\n`` and ``\\n``.

    Deliberately not ``str.splitlines(keepends=True)``, which also breaks on a
    bare ``\\r``, ``\\v``, ``\\f`` and several Unicode separators — a note body
    can legitimately contain a bare ``\\r`` (a pasted terminal progress-bar
    redraw inside a fenced code block). Mirrors the helper of the same name in
    hooks/note_writer.py; duplicated rather than imported because hooks/ is not
    importable from the scripts/ package.
    """
    lines: list[str] = []
    start = 0
    for i, ch in enumerate(text):
        if ch == "\n":
            lines.append(text[start:i + 1])
            start = i + 1
    if start < len(text):
        lines.append(text[start:])
    return lines


def _detect_line_ending(text: str) -> str:
    """Return ``text``'s dominant line ending, ``"\\r\\n"`` or ``"\\n"``.

    The inserted fence line is emitted with this ending, so a CRLF-authored
    note does not end up with one stray LF line at the top. Same approach as
    hooks/note_writer.py's ``_detect_line_ending``; ties and files with no
    line endings at all default to ``"\\n"``.
    """
    crlf_count = text.count("\r\n")
    lf_only_count = len(re.findall(r"(?<!\r)\n", text))
    return "\r\n" if crlf_count > lf_only_count else "\n"


def _is_frontmatter_shaped(stripped: str) -> bool:
    """True when a line could belong inside a frontmatter block."""
    if not stripped.strip():
        return True
    return bool(
        _FM_KEY_RE.match(stripped)
        or _FM_ITEM_RE.match(stripped)
        or _FM_CONT_RE.match(stripped)
    )


def _find_fenceless_frontmatter(text: str) -> int | None:
    """Return the 0-based index of the closing ``---`` line when ``text`` is a
    note whose frontmatter lost its opening fence; ``None`` otherwise.

    This function IS the detection rule — scan() and apply() both call it, so
    apply re-verifies rather than trusting a possibly stale Issue.
    """
    lines = _split_lines_lf_crlf(text)
    if not lines:
        return None

    # (1) An opening fence already present → healthy note, not ours.
    first = lines[0].rstrip("\r\n")
    if first == "---":
        return None

    # (2) First line must be frontmatter-shaped as a key.
    if not _FM_KEY_RE.match(first):
        return None

    # (3)/(4) Walk forward to the closing fence, requiring every line on the
    # way to be frontmatter-shaped.
    #
    # The bound is ``_FM_MAX_LINES`` MINUS ONE, not _FM_MAX_LINES: repair
    # inserts a line at the top, so a closing fence found at index i here
    # lands at index i+1 in the repaired note, and note_writer accepts a
    # closing fence only up to index _FM_MAX_LINES. Repairing a note whose
    # fence sits at index 1000 would produce a note note_writer still
    # refuses — a "fixed" file that is not fixed.
    for i in range(1, min(len(lines), _FM_MAX_LINES)):
        stripped = lines[i].rstrip("\r\n")
        if stripped == "---":
            return i
        if not _is_frontmatter_shaped(stripped):
            return None
    return None


def _parse_project(lines: list[str], close_idx: int) -> str:
    """Best-effort ``project:`` value from the fenceless frontmatter region."""
    for line in lines[:close_idx]:
        stripped = line.rstrip("\r\n")
        if stripped.startswith("project:"):
            return stripped.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


def _project_matches(note_project: str, filter_project: str | None) -> bool:
    """Filter on the frontmatter ``project:`` value, case-insensitively.

    Unlike checks that match a ``-<project>-`` filename substring, the value
    is right there in the region this check has already shape-validated, so
    use it. A note with no ``project:`` key never matches an explicit
    ``--project`` filter (it still appears in an unfiltered scan).
    """
    if not filter_project:
        return True
    return note_project.lower() == filter_project.lower()


def scan(
    vault_path: str,
    sessions_folder: str,
    insights_folder: str,
    days: int,
    project: str | None = None,
) -> list[Issue]:
    """Find notes whose frontmatter is missing its opening ``---`` fence.

    ``days`` is accepted for interface compatibility and ignored: the damage
    is historic (the oldest affected live note is from 2026-05-08), so a
    recency window would hide exactly the notes that need repairing.
    """
    vault_root = Path(vault_path)
    issues: list[Issue] = []

    for folder in [sessions_folder, insights_folder]:
        folder_path = vault_root / folder
        if not folder_path.is_dir():
            continue

        for md_file in sorted(folder_path.glob("*.md")):
            if not md_file.is_file():
                continue
            try:
                # Strict decode: a note with invalid UTF-8 belongs to the
                # encoding-corruption check. Repairing it here would mean
                # re-encoding the whole file, which is far more than
                # inserting a fence.
                text = md_file.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            close_idx = _find_fenceless_frontmatter(text)
            if close_idx is None:
                continue

            lines = _split_lines_lf_crlf(text)
            note_project = _parse_project(lines, close_idx)
            if not _project_matches(note_project, project):
                continue

            first_key = lines[0].rstrip("\r\n")
            issues.append(Issue(
                check=NAME,
                note_path=str(md_file),
                project=note_project,
                current_source=f"first line is {first_key[:60]!r} (no opening fence)",
                proposed_source="insert '---' as a new first line",
                reason=(
                    f"frontmatter has a closing '---' at line {close_idx + 1} but no "
                    f"opening fence; the note parses as having no frontmatter at all"
                ),
                # High but not 1.0: the four preconditions make the intent
                # unambiguous for machine-written notes, but a hand-authored
                # markdown file whose opening paragraph happens to be all
                # ``key: value`` lines followed by a ``---`` horizontal rule
                # would satisfy them too.
                confidence=0.99,
                extra={
                    "closing_fence_line": close_idx + 1,
                    "frontmatter_line_count": close_idx,
                },
            ))

    return issues


def apply(issues: list[Issue], backup_root: str) -> list[Result]:
    """Insert the missing ``---`` opening fence, atomically."""
    results: list[Result] = []

    for issue in issues:
        note_path = issue.note_path
        try:
            original = Path(note_path).read_bytes()
            text = original.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            results.append(Result(
                check=NAME,
                note_path=note_path,
                status="error",
                error=str(exc),
            ))
            continue

        # Defensive re-verification against the live file. An Issue can be
        # stale (already repaired by an earlier run, edited in Obsidian
        # between scan and apply); blindly prepending a fence then would add
        # a SECOND one and break the note the check exists to fix.
        if _find_fenceless_frontmatter(text) is None:
            results.append(Result(
                check=NAME,
                note_path=note_path,
                status="skipped",
                error=(
                    "note no longer matches the fenceless-frontmatter pattern "
                    "(already repaired, or edited since the scan)"
                ),
            ))
            continue

        backup_dir = Path(backup_root) / NAME
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / Path(note_path).name
            shutil.copy2(note_path, backup_path)
        except OSError as exc:
            results.append(Result(
                check=NAME,
                note_path=note_path,
                status="error",
                error=f"backup failed: {exc}",
            ))
            continue

        new_bytes = (
            ("---" + _detect_line_ending(text)).encode("utf-8") + original
        )

        dest = Path(note_path)
        tmp = None
        try:
            # Preserve the note's existing permission bits. This repair adds
            # one line; it is not the place to silently re-permission a user
            # file. Copying the source mode can only ever reproduce what the
            # user already has, never widen it.
            mode = os.stat(note_path).st_mode & 0o7777
            fd, tmp = tempfile.mkstemp(
                dir=str(dest.parent),
                prefix=".vd-fence-",
                suffix=".tmp",
            )
            try:
                os.write(fd, new_bytes)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(tmp, mode)
            os.replace(tmp, str(dest))
        except OSError as exc:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            results.append(Result(
                check=NAME,
                note_path=note_path,
                status="error",
                error=str(exc),
            ))
            continue

        results.append(Result(
            check=NAME,
            note_path=note_path,
            status="applied",
            backup_path=str(backup_path),
        ))

    return results
