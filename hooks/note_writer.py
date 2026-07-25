"""CLI for deterministic vault note persistence.

Skills previously persisted vault notes via a Write tool call, which breaks
in environments that route writes through a context-blind helper sub-agent
(#269). This gives skills two deterministic commands instead:

- ``write``: create a new note. Reads note content from stdin, delegates to
  obsidian_utils.write_vault_note for the atomic mkdir + traversal-checked +
  chmod 0o600 + rename write, and prints the absolute destination path on
  success so the skill can echo it back to the user.
- ``append-update``: append a dated update section to an EXISTING note (used
  by /compress's update flow) and apply frontmatter mutations
  (``last_updated``, new tags) in the SAME atomic write. Never creates a
  file -- the target must already exist.

Stdin is capped at 1_000_000 bytes (project CLAUDE.md security pattern).
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

from obsidian_utils import write_vault_note

STDIN_CAP_BYTES = 1_000_000


def _read_stdin_capped() -> str:
    """Read stdin with the 1_000_000-byte cap (project security pattern)."""
    return sys.stdin.read(STDIN_CAP_BYTES)


def _validate_folder(folder: str):
    """Return an error message if ``folder`` is not a benign, vault-relative
    path, else None.

    write_vault_note()'s own containment check only blocks writes that
    escape the vault ROOT — it does not constrain *where inside* the vault
    a write lands. Obsidian executes JavaScript from
    ``.obsidian/plugins/<x>/main.js``, so a folder like
    ``.obsidian/plugins/evil`` passes containment cleanly and lands
    executable code. ``folder``/``filename`` are assembled by an LLM from
    session content, so they're not fully trusted inputs — validate here,
    before any filesystem side effect.

    Deliberately NOT an allowlist of known vault folder names: folder names
    are user-configurable (~/.claude/obsidian-brain-config.json) and several
    skills write to different folders. The rules below (no absolute/home
    path, no vault-root alias, no ``..`` segment, no dot-prefixed segment)
    block the actual vector without that coupling.

    Note: ``Path("").parts == Path(".").parts == ()`` — pathlib treats both
    as "no path at all" rather than as a single dot-segment, so the
    dot-prefix loop below never sees them. ``folder in ("", ".")`` is
    checked explicitly for that reason: unchecked, ``Path(vault)/""`` and
    ``Path(vault)/"."`` both collapse to the vault root itself (still fully
    contained, so write_vault_note()'s own guard would not catch it either),
    silently defeating the "write targets a named subfolder" contract.
    """
    if folder.startswith("/"):
        return f"invalid folder (must be relative to vault root, not absolute): {folder!r}"
    if folder.startswith("~"):
        return f"invalid folder (must not reference a home directory with '~'): {folder!r}"
    if folder in ("", "."):
        return f"invalid folder (must name a subfolder, not the vault root itself): {folder!r}"
    for part in Path(folder).parts:
        if part == "..":
            return f"invalid folder (contains a '..' traversal segment): {folder!r}"
        if part.startswith("."):
            return (
                f"invalid folder (contains a hidden/dot path segment, e.g. "
                f"'.obsidian'): {folder!r}"
            )
    return None


def _validate_filename(filename: str):
    """Return an error message if ``filename`` is not a bare ``*.md`` name,
    else None.

    Bare-name check (``Path(filename).name == filename``) rejects any path
    separator — including ``../`` traversal and a plain subdirectory like
    ``notes/x.md`` — so a filename segment can never place the write outside
    the single target folder write_vault_note() was given.

    Also rejects a filename with nothing before the ``.md`` suffix (e.g.
    ``".md"`` itself), which would otherwise create a hidden dotfile in the
    target folder. Checked via length, not ``Path(...).stem`` — pathlib
    treats a leading-dot name as an extension-less dotfile, so
    ``Path(".md").stem == ".md"`` (not empty) and would silently miss this.
    """
    if Path(filename).name != filename:
        return (
            f"invalid filename (must be a bare name, no path separators): "
            f"{filename!r}"
        )
    if not filename.endswith(".md"):
        return f"invalid filename (must end with .md): {filename!r}"
    if len(filename) <= len(".md"):
        return f"invalid filename (must have a non-empty name before .md): {filename!r}"
    return None


def run_write(vault_path: str, folder: str, filename: str, content: str) -> int:
    """Write ``content`` to ``<vault_path>/<folder>/<filename>``.

    Validates ``folder``/``filename`` (see _validate_folder/_validate_filename)
    before touching the filesystem, then delegates the actual write entirely
    to write_vault_note() for the atomic write, the path-traversal
    containment check, and the 0o600 permission — this function does not
    reimplement any of that.

    Prints ``OK: <abs path>`` to stdout and returns 0 on success.
    Prints ``ERROR: <msg>`` to stderr and returns 1 on failure (validation
    rejection or write_vault_note() error). Never partially writes:
    validation failures have no filesystem side effect at all, and
    write_vault_note() either lands the file atomically or returns an error
    before any rename.
    """
    validation_err = _validate_folder(folder) or _validate_filename(filename)
    if validation_err:
        print(f"ERROR: {validation_err}", file=sys.stderr)
        return 1

    err = write_vault_note(vault_path, folder, filename, content)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    # write_vault_note() returns only None/error, not the path, so the
    # destination is composed here from the same three inputs it was
    # given — kept in sync with its own dest_dir/dest construction.
    dest = Path(vault_path) / folder / filename
    print(f"OK: {dest.resolve()}")
    return 0


# ---------------------------------------------------------------------------
# append-update
# ---------------------------------------------------------------------------

# Trailing metadata sections that mark the end of the "real" note body.
# Reuses the exact list obsidian_utils.upgrade_note_with_summary() treats as
# audit-trail sections to preserve (see its `audit_sections` list, ~line
# 4300), plus the "_(Summary source: ...)_" line the /compress SKILL.md also
# names. An update section must land BEFORE the first of these, scanning
# top-down -- never after -- so it reads as part of the note's substantive
# content rather than getting buried under (or inside) the audit trail.
_TRAILING_MARKERS = (
    "## Tool Usage",
    "## Conversation (raw)",
    "## Session Metadata",
    "## Files Touched",
)
_SUMMARY_SOURCE_RE = re.compile(r"^_\(Summary source:.*\)_\s*$")

_DATE_LINE_RE = re.compile(r"^date:\s*.*$")
_LAST_UPDATED_LINE_RE = re.compile(r"^last_updated:\s*.*$")
_TAGS_KEY_RE = re.compile(r"^tags:\s*$")
_TAG_ITEM_RE = re.compile(r"^(?P<indent>\s*)-\s*(?P<tag>\S.*?)\s*$")


def _is_trailing_marker(line: str) -> bool:
    """True if ``line`` is one of the exact trailing-section headings, or
    matches the "_(Summary source: ...)_" line (its suffix varies)."""
    stripped = line.rstrip("\n")
    if stripped in _TRAILING_MARKERS:
        return True
    return bool(_SUMMARY_SOURCE_RE.match(stripped))


def _resolve_note_path(vault_path: str, note_path: str):
    """Validate and resolve ``note_path`` before any filesystem write.

    Returns ``(resolved Path, None)`` on success or ``(None, error message)``
    on failure. Checked, in order:

    1. Containment: ``resolve()`` + ``is_relative_to()`` against the vault
       root (same pattern as ``write_vault_note()`` / ``_validate_folder()``).
    2. Dot-segment reject on every directory segment between the vault root
       and the file. Containment alone does not stop a write that targets an
       *existing* file already sitting inside ``.obsidian/**`` -- that path
       is fully contained within the vault root, so only this check (mirrors
       ``_validate_folder()``'s dot-segment rule) blocks it.
    3. The target must already exist and be a regular file: unlike
       ``write``, this command only ever appends to an existing note -- it
       must never create one.

    Steps 1-3 are pure ``resolve()``/``exists()``/``is_file()`` checks --
    reads, not writes -- so this can run before the file is ever opened.
    """
    vault_real = Path(vault_path).resolve()
    resolved = Path(note_path).resolve()

    if not resolved.is_relative_to(vault_real):
        return None, f"path traversal blocked: {note_path!r} is not inside vault root"

    rel_parts = resolved.relative_to(vault_real).parts
    for part in rel_parts[:-1]:
        if part.startswith("."):
            return None, (
                "invalid note path (contains a hidden/dot path segment, "
                f"e.g. '.obsidian'): {note_path!r}"
            )

    if not resolved.exists():
        return None, f"note does not exist: {note_path!r}"
    if not resolved.is_file():
        return None, f"note path is not a regular file: {note_path!r}"

    return resolved, None


def _split_frontmatter(lines: list[str]):
    """Split ``lines`` (from ``str.splitlines(keepends=True)``) into the
    frontmatter fence pair and body.

    Returns ``(open_fence_line, frontmatter_lines, close_fence_line,
    body_lines)`` on success, or ``(None, None, None, None)`` if the file
    does not open with a well-formed ``---`` ... ``---`` frontmatter block
    (missing opening fence, or no closing fence found anywhere in the file).
    """
    if not lines or lines[0].rstrip("\n") != "---":
        return None, None, None, None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\n") == "---":
            return lines[0], lines[1:i], lines[i], lines[i + 1:]
    return None, None, None, None


def _apply_last_updated(fm_lines: list[str], last_updated: str):
    """Return ``(new_fm_lines, error)``.

    Replaces an existing top-level ``last_updated:`` line's value in place.
    If absent, inserts a new ``last_updated: <value>`` line immediately
    after the top-level ``date:`` line. Never touches ``date``,
    ``source_session``, ``source_session_note``, or ``type`` -- every other
    line is copied through unchanged.
    """
    out = list(fm_lines)
    for idx, line in enumerate(out):
        if _LAST_UPDATED_LINE_RE.match(line.rstrip("\n")):
            out[idx] = f"last_updated: {last_updated}\n"
            return out, None

    for idx, line in enumerate(out):
        if _DATE_LINE_RE.match(line.rstrip("\n")):
            out.insert(idx + 1, f"last_updated: {last_updated}\n")
            return out, None

    return None, "frontmatter missing 'date:' field (required to insert last_updated)"


def _apply_add_tags(fm_lines: list[str], add_tags: list[str]) -> list[str]:
    """Return frontmatter lines with ``add_tags`` merged into the ``tags:``
    block, appended at its end in original list-item indentation.

    No-op (returns ``fm_lines`` unchanged) if ``add_tags`` is empty, or if no
    ``tags:`` block is present at all -- a note with no tags block is
    "nothing to merge into", not an error. Tags already present (in the
    existing block, or repeated within ``add_tags`` itself) are skipped.
    """
    if not add_tags:
        return fm_lines

    tags_idx = None
    for idx, line in enumerate(fm_lines):
        if _TAGS_KEY_RE.match(line.rstrip("\n")):
            tags_idx = idx
            break
    if tags_idx is None:
        return fm_lines

    existing = []
    indent = "  "
    end_idx = tags_idx + 1
    for idx in range(tags_idx + 1, len(fm_lines)):
        m = _TAG_ITEM_RE.match(fm_lines[idx].rstrip("\n"))
        if not m:
            break
        indent = m.group("indent") or indent
        existing.append(m.group("tag"))
        end_idx = idx + 1

    seen = set(existing)
    new_lines = []
    for tag in add_tags:
        if not tag or tag in seen:
            continue
        seen.add(tag)
        new_lines.append(f"{indent}- {tag}\n")

    return fm_lines[:end_idx] + new_lines + fm_lines[end_idx:]


def _atomic_rewrite(dest: Path, content: str):
    """Rewrite an EXISTING file at ``dest`` with ``content``, atomically.

    Mirrors obsidian_utils.write_vault_note()'s temp-file + chmod 0o600 +
    rename idiom exactly, adapted to rewriting a known destination in place
    rather than creating one fresh under a vault/folder/filename triple: the
    temp file is created as a sibling of ``dest`` (same directory, so
    ``os.rename`` is an atomic same-filesystem move) and is only renamed
    over ``dest`` after both the write and the chmod succeed. On any
    failure the temp file is unlinked and ``dest`` is left byte-identical --
    the rename is the only step that can touch ``dest``, and it happens
    last.

    Returns None on success, a non-empty error string on failure.
    """
    dest_dir = dest.parent
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(dest_dir), prefix=".ob-", suffix=".md.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.chmod(tmp_path, 0o600)
            os.rename(tmp_path, str(dest))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        return f"write failed for {dest}: {exc}"
    return None


def _parse_append_update_flags(argv: list[str]):
    """Parse the optional ``--last-updated``/``--add-tags`` flags that
    follow ``<vault_path> <note_path>`` for the ``append-update`` command.

    Returns ``(last_updated, add_tags_csv, error)``. On success ``error`` is
    None and either value may be None if its flag was omitted (both are
    optional -- an omitted flag is a no-op for that mutation, not an
    error). On a parse error the first two values are None and ``error`` is
    a usage-appropriate message.
    """
    last_updated = None
    add_tags_csv = None
    i = 0
    while i < len(argv):
        flag = argv[i]
        if flag == "--last-updated":
            if i + 1 >= len(argv):
                return None, None, "--last-updated requires a value"
            last_updated = argv[i + 1]
            i += 2
        elif flag == "--add-tags":
            if i + 1 >= len(argv):
                return None, None, "--add-tags requires a value"
            add_tags_csv = argv[i + 1]
            i += 2
        else:
            return None, None, f"unknown flag: {flag!r}"
    return last_updated, add_tags_csv, None


def run_append_update(
    vault_path: str,
    note_path: str,
    update_text: str,
    last_updated: str | None = None,
    add_tags_csv: str | None = None,
) -> int:
    """Append ``update_text`` to the existing note at ``note_path`` and, in
    the SAME atomic write, apply frontmatter mutations.

    Insertion point: scans the note body (after the frontmatter's closing
    fence) top-down for the first line matching a trailing metadata marker
    (see ``_TRAILING_MARKERS``/``_SUMMARY_SOURCE_RE``) and inserts
    ``update_text`` immediately before it, adding a blank line on either
    side as needed. If no marker is found, appends at end of file.

    Frontmatter mutations (both optional, applied via ``_apply_last_updated``
    / ``_apply_add_tags``): replace-or-insert ``last_updated``, and merge new
    tags into the ``tags:`` block. ``date``, ``source_session``,
    ``source_session_note``, and ``type`` are never touched.

    Validates path containment (``_resolve_note_path``) and parses/mutates
    entirely in memory before calling ``_atomic_rewrite`` -- the single
    write call. On ANY validation, frontmatter-parse, or write failure,
    prints ``ERROR: <reason>`` to stderr, returns 1, and leaves the file on
    disk byte-identical (no write attempt is made until every check upstream
    of it has already succeeded).

    Prints ``OK: <resolved path>`` to stdout and returns 0 on success.
    """
    resolved, err = _resolve_note_path(vault_path, note_path)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            original_text = fh.read()
    except OSError as exc:
        print(f"ERROR: cannot read {note_path}: {exc}", file=sys.stderr)
        return 1

    lines = original_text.splitlines(keepends=True)
    open_fence, fm_lines, close_fence, body_lines = _split_frontmatter(lines)
    if fm_lines is None:
        print(
            f"ERROR: malformed or missing frontmatter (no closing '---'): {note_path}",
            file=sys.stderr,
        )
        return 1

    if last_updated:
        fm_lines, fm_err = _apply_last_updated(fm_lines, last_updated)
        if fm_err:
            print(f"ERROR: {fm_err}", file=sys.stderr)
            return 1

    add_tags = [t.strip() for t in (add_tags_csv or "").split(",") if t.strip()]
    fm_lines = _apply_add_tags(fm_lines, add_tags)

    insertion_idx = len(body_lines)
    for idx, line in enumerate(body_lines):
        if _is_trailing_marker(line):
            insertion_idx = idx
            break

    prefix = body_lines[:insertion_idx]
    if prefix and prefix[-1].strip() != "":
        prefix = prefix + ["\n"]

    block_text = update_text if update_text.endswith("\n") else update_text + "\n"
    new_body_lines = prefix + [block_text, "\n"] + body_lines[insertion_idx:]

    new_text = open_fence + "".join(fm_lines) + close_fence + "".join(new_body_lines)

    write_err = _atomic_rewrite(resolved, new_text)
    if write_err:
        print(f"ERROR: {write_err}", file=sys.stderr)
        return 1

    print(f"OK: {resolved}")
    return 0


def main():
    """CLI entrypoint.

    Usage:
      note_writer.py write <vault_path> <folder> <filename>
      note_writer.py append-update <vault_path> <note_path> \
[--last-updated YYYY-MM-DD] [--add-tags a,b,c]
    """
    usage = (
        "usage: note_writer.py write <vault_path> <folder> <filename>\n"
        "       note_writer.py append-update <vault_path> <note_path> "
        "[--last-updated YYYY-MM-DD] [--add-tags a,b,c]"
    )
    if len(sys.argv) < 2:
        print(usage, file=sys.stderr)
        sys.exit(2)

    cmd = sys.argv[1]
    if cmd == "write":
        if len(sys.argv) != 5:
            print(usage, file=sys.stderr)
            sys.exit(2)
        vault_path, folder, filename = sys.argv[2], sys.argv[3], sys.argv[4]
        content = _read_stdin_capped()
        sys.exit(run_write(vault_path, folder, filename, content))

    if cmd == "append-update":
        if len(sys.argv) < 4:
            print(usage, file=sys.stderr)
            sys.exit(2)
        vault_path, note_path = sys.argv[2], sys.argv[3]
        last_updated, add_tags_csv, flag_err = _parse_append_update_flags(sys.argv[4:])
        if flag_err:
            print(f"ERROR: {flag_err}", file=sys.stderr)
            sys.exit(2)
        update_text = _read_stdin_capped()
        sys.exit(
            run_append_update(vault_path, note_path, update_text, last_updated, add_tags_csv)
        )

    print(f"unknown command: {cmd}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
