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


def run_write(vault_path: str, folder: str, filename: str, content: str) -> int:
    """Write ``content`` to ``<vault_path>/<folder>/<filename>``.

    Delegates entirely to write_vault_note() for the atomic write, the
    path-traversal containment check, and the 0o600 permission — this
    function does not reimplement any of that.

    Prints ``OK: <abs path>`` to stdout and returns 0 on success.
    Prints ``ERROR: <msg>`` to stderr and returns 1 on failure. Never
    partially writes: write_vault_note() either lands the file atomically
    or returns an error before any rename.
    """
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
