"""Read-only vault scanner for /vault-ask and /vault-search (#375, #312).

Two commands:

- ``grep``: list the ``*.md`` files under one or more vault folders that
  match a regex. It is the fallback for sessions with no Grep tool (#375),
  and the only way to restrict a search to frontmatter (``--frontmatter-only``),
  which the Grep tool cannot do (#312).
- ``meta``: print the frontmatter fields the skills rank and display by, as
  JSON Lines. It replaces fixed ``Read(limit=40)`` reads, which silently drop
  fields in notes whose frontmatter runs past line 400 (/emerge and /standup
  notes close their fence as deep as line 460).

Usage::

    python3 vault_scan.py grep <vault> <folder> [<folder> ...] --pattern <regex>
                          [--ignore-case] [--frontmatter-only]
    python3 vault_scan.py meta <vault> <file> [<file> ...]

``grep`` matches one line at a time, like ripgrep behind the Grep tool, so
``^key:.*value`` anchors per line and a pattern never spans two lines. Stdout
is the sorted list of matching paths. Stderr always carries one summary line
(``vault_scan: N match(es), M file(s) scanned, K skipped``), so an empty
stdout is never ambiguous.

Exit codes: 0 on success (with or without matches), 2 for bad arguments
(``ERROR: <reason>`` on stderr), 1 for an unexpected error.

Read-only. Every file is checked with ``resolve()`` + ``is_relative_to()``
against the resolved vault root, so a symlink cannot pull in a file from
outside the vault.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from frontmatter import split_frontmatter, split_lines_lf_crlf
from note_writer import _validate_folder, _validate_vault_path

MAX_FILE_BYTES = 5 * 1024 * 1024
SNIPPET_CHARS = 200
META_FIELDS = ("date", "type", "project", "session_id", "source_session_note")


class _UsageError(Exception):
    """Bad arguments: reported as ``ERROR: <reason>`` with exit code 2."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _UsageError(message)


def _build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="vault_scan.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command")

    g = sub.add_parser("grep", help="list *.md files under vault folders that match a regex")
    g.add_argument("vault")
    g.add_argument("folders", nargs="+")
    g.add_argument("--pattern", required=True)
    g.add_argument("--ignore-case", action="store_true")
    g.add_argument("--frontmatter-only", action="store_true")

    m = sub.add_parser("meta", help="print frontmatter fields of notes as JSON Lines")
    m.add_argument("vault")
    m.add_argument("files", nargs="+")
    return p


def _read_text(path: Path) -> str:
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read()


def _is_inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root)
    except (OSError, RuntimeError):
        return False


def _check_vault(vault: str) -> Path:
    err = _validate_vault_path(vault)
    if err:
        raise _UsageError(err)
    return Path(vault).resolve()


def _folder_dirs(vault: str, root: Path, folders: list[str]) -> list[Path]:
    dirs = []
    for folder in folders:
        err = _validate_folder(folder)
        if err:
            raise _UsageError(err)
        d = Path(vault) / folder
        if not _is_inside(d, root):
            raise _UsageError(f"folder resolves outside the vault: {folder!r}")
        if not d.is_dir():
            raise _UsageError(f"folder not found in vault: {folder!r}")
        dirs.append(d)
    return dirs


def _iter_md(folder: Path):
    # followlinks=False: a symlinked subdirectory is never descended.
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(".md"):
                yield Path(dirpath) / name


def _frontmatter_lines(text: str):
    _open, fm, _close, _body, err = split_frontmatter(split_lines_lf_crlf(text))
    return None if err else fm


def _line_matches(regex: re.Pattern, lines) -> bool:
    return any(regex.search(line.rstrip("\r\n")) for line in lines)


def grep_files(vault: str, folders: list[str], regex: re.Pattern,
               frontmatter_only: bool = False):
    """Return ``(matches, scanned, skipped)``; ``matches`` is sorted."""
    root = _check_vault(vault)
    dirs = _folder_dirs(vault, root, folders)
    seen: set[str] = set()
    matches: list[str] = []
    scanned = skipped = 0
    for d in dirs:
        for path in _iter_md(d):
            key = os.path.normpath(str(path))
            if key in seen:
                continue
            seen.add(key)
            try:
                if not _is_inside(path, root) or path.stat().st_size > MAX_FILE_BYTES:
                    skipped += 1
                    continue
                text = _read_text(path)
            except OSError:
                skipped += 1
                continue
            if frontmatter_only:
                lines = _frontmatter_lines(text)
                if lines is None:
                    skipped += 1
                    continue
            else:
                lines = split_lines_lf_crlf(text)
            scanned += 1
            if _line_matches(regex, lines):
                matches.append(key)
    return sorted(matches), scanned, skipped


def _parse_fields(fm_lines: list[str]) -> dict:
    """Top-level ``key: value`` scalars plus ``tags`` (block or flow list)."""
    meta: dict = {}
    tags: list[str] = []
    in_tags = False
    for raw in fm_lines:
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if in_tags and stripped.startswith("- "):
            tags.append(stripped[2:].strip().strip("\"'"))
            continue
        if in_tags and not stripped:
            continue
        in_tags = False
        if line[:1].isspace() or ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key, val = key.strip(), val.strip()
        if key == "tags":
            if val.startswith("[") and val.endswith("]"):
                tags.extend(
                    t.strip().strip("\"'") for t in val[1:-1].split(",") if t.strip()
                )
            elif val:
                tags.append(val.strip("\"'"))
            else:
                in_tags = True
            continue
        meta.setdefault(key, val.strip("\"'"))
    meta["tags"] = tags
    return meta


def _empty_row(path: str, error: str | None) -> dict:
    row = {"path": path}
    row.update({f: None for f in META_FIELDS})
    row.update({"tags": None, "title": None, "snippet": None, "error": error})
    return row


def _describe_parse_failure(reason: str) -> str:
    # Content-free category only: the raw reason can quote note text.
    try:
        from obsidian_utils import _describe_note_parse_failure
        return _describe_note_parse_failure(reason)
    except Exception:  # pragma: no cover - degraded import
        return "unknown"


def note_meta(vault_root: Path, vault: str, file_arg: str) -> dict:
    path = Path(file_arg)
    if not path.is_absolute():
        path = Path(vault) / path
    if not _is_inside(path, vault_root):
        return _empty_row(file_arg, "outside the vault")
    if path.suffix != ".md":
        return _empty_row(file_arg, "not a .md file")
    try:
        if not path.is_file():
            return _empty_row(file_arg, "not found")
        if path.stat().st_size > MAX_FILE_BYTES:
            return _empty_row(file_arg, "file too large")
        text = _read_text(path)
    except OSError as exc:
        return _empty_row(file_arg, f"unreadable: {exc.strerror or type(exc).__name__}")
    _open, fm, _close, body, err = split_frontmatter(split_lines_lf_crlf(text))
    if err:
        return _empty_row(file_arg, f"unparsable frontmatter: {_describe_parse_failure(err)}")
    fields = _parse_fields(fm)
    body_text = "".join(body)
    title = next(
        (ln[2:].strip() for ln in body_text.splitlines() if ln.startswith("# ")),
        path.stem,
    )
    row = {"path": file_arg}
    row.update({f: (fields.get(f) or None) for f in META_FIELDS})
    row.update({
        "tags": fields["tags"],
        "title": title,
        "snippet": " ".join(body_text.split())[:SNIPPET_CHARS],
        "error": None,
    })
    return row


def _run_grep(args) -> int:
    flags = re.IGNORECASE if args.ignore_case else 0
    try:
        regex = re.compile(args.pattern, flags)
    except re.error as exc:
        raise _UsageError(f"invalid regex {args.pattern!r}: {exc}")
    matches, scanned, skipped = grep_files(
        args.vault, args.folders, regex, args.frontmatter_only,
    )
    for m in matches:
        print(m)
    print(
        f"vault_scan: {len(matches)} match(es), {scanned} file(s) scanned, {skipped} skipped",
        file=sys.stderr,
    )
    return 0


def _run_meta(args) -> int:
    root = _check_vault(args.vault)
    for f in args.files:
        print(json.dumps(note_meta(root, args.vault, f), ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "grep":
            return _run_grep(args)
        if args.command == "meta":
            return _run_meta(args)
        raise _UsageError("a command is required: grep or meta")
    except _UsageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except SystemExit as exc:  # --help
        return int(exc.code or 0)
    except Exception as exc:
        print(f"ERROR: unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
