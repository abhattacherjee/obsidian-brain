"""CLI for deterministic vault note persistence.

Skills previously persisted vault notes via a Write tool call, which breaks
in environments that route writes through a context-blind helper sub-agent
(#269). This gives skills a deterministic command instead: reads note
content from stdin, delegates to obsidian_utils.write_vault_note for the
atomic mkdir + traversal-checked + chmod 0o600 + rename write, and prints
the absolute destination path on success so the skill can echo it back to
the user.

Stdin is capped at 1_000_000 bytes (project CLAUDE.md security pattern).
"""
from __future__ import annotations

import sys
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


def main():
    """CLI entrypoint. Usage: python3 note_writer.py write <vault_path> <folder> <filename>"""
    usage = "usage: note_writer.py write <vault_path> <folder> <filename>"
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

    print(f"unknown command: {cmd}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
