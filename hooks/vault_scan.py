"""Read-only vault scanner for /vault-ask and /vault-search (#375, #312).

Two commands:

- ``grep``: list the ``*.md`` files under one or more vault folders that
  match a regex. It is the fallback for sessions with no native search tool (#375),
  and the only way to restrict a search to frontmatter (``--frontmatter-only``),
  which a native search tool cannot do (#312).
- ``meta``: print the frontmatter fields the skills rank and display by, as
  JSON Lines. It replaces fixed 40-line reads, which silently drop
  fields: frontmatter can run past line 40, and /emerge notes close their
  fence as deep as line 461 (/standup notes as deep as line 272).

Usage::

    python3 vault_scan.py grep <vault> <folder> [<folder> ...] --pattern=<regex>
                          [--ignore-case] [--frontmatter-only]
    python3 vault_scan.py meta <vault> <file> [<file> ...]

``grep`` matches one line at a time, like ripgrep behind a native search tool, so
``^key:.*value`` anchors per line and a pattern never spans two lines. Use the
``--pattern=<regex>`` form: with a space, a pattern that starts with ``-`` is
read as a flag. Stdout is the sorted list of matching paths. On success
(exit 0) stderr carries one summary line, ``vault_scan: N match(es), M file(s)
scanned, K skipped (too_large=a, unreadable=b, outside_vault=c,
bad_frontmatter=d, symlinked_dirs=e, unreadable_dirs=f)``, so an empty stdout
is never ambiguous. K is the sum of the six counters. ``unreadable_dirs``
counts directories that could not be listed (permissions), including a folder
named on the command line; their notes are not scanned. Under ``--frontmatter-only`` a
note with no frontmatter at all is scanned (it cannot match), not skipped;
only a fence that does not close counts as ``bad_frontmatter``.

Exit codes: 0 on success (with or without matches); 2 for bad arguments and
1 for an unexpected error or a missing dependency module. On exit 2 or 1,
stderr carries only an ``ERROR: <reason>`` line and there is no summary line.

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

try:
    from frontmatter import NO_OPENING_FENCE_REASON, split_frontmatter, split_lines_lf_crlf
    from note_writer import _validate_folder, _validate_vault_path
except ImportError as _exc:  # reported by main() as ERROR:, exit 1
    _IMPORT_ERROR: ImportError | None = _exc
else:
    _IMPORT_ERROR = None

MAX_FILE_BYTES = 5 * 1024 * 1024
SNIPPET_CHARS = 200
META_FIELDS = ("date", "type", "project", "session_id", "source_session_note")
SKIP_KINDS = ("too_large", "unreadable", "outside_vault", "bad_frontmatter",
              "symlinked_dirs", "unreadable_dirs")
_UTILS_WARNED = False


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


def _iter_md(folder: Path, symlinked_dirs: set[str], unreadable_dirs: set[str]):
    # followlinks=False: a symlinked subdirectory is never descended. Each one
    # is recorded in ``symlinked_dirs`` so the summary can count it. A directory
    # that cannot be listed is recorded in ``unreadable_dirs``: os.walk drops it
    # silently unless ``onerror`` is given.
    def _on_error(exc: OSError) -> None:
        unreadable_dirs.add(os.path.normpath(str(exc.filename or folder)))

    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False,
                                                onerror=_on_error):
        dirnames.sort()
        for name in dirnames:
            sub = os.path.join(dirpath, name)
            if os.path.islink(sub):
                symlinked_dirs.add(os.path.normpath(sub))
        for name in sorted(filenames):
            if name.endswith(".md"):
                yield Path(dirpath) / name


def _frontmatter_lines(text: str):
    """Frontmatter lines; ``[]`` when there is none; None when the fence is broken."""
    _open, fm, _close, _body, err = split_frontmatter(split_lines_lf_crlf(text))
    if err == NO_OPENING_FENCE_REASON:
        return []
    return None if err else fm


def _line_matches(regex: re.Pattern, lines) -> bool:
    return any(regex.search(line.rstrip("\r\n")) for line in lines)


def grep_files(vault: str, folders: list[str], regex: re.Pattern,
               frontmatter_only: bool = False):
    """Return ``(matches, scanned, skipped)``.

    ``matches`` is sorted. ``skipped`` maps each name in ``SKIP_KINDS`` to a
    count; ``symlinked_dirs`` counts subdirectories that were not descended and
    ``unreadable_dirs`` counts directories that could not be listed.
    """
    root = _check_vault(vault)
    dirs = _folder_dirs(vault, root, folders)
    seen: set[str] = set()
    symlinked_dirs: set[str] = set()
    unreadable_dirs: set[str] = set()
    matches: list[str] = []
    scanned = 0
    skipped = dict.fromkeys(SKIP_KINDS, 0)
    for d in dirs:
        for path in _iter_md(d, symlinked_dirs, unreadable_dirs):
            key = os.path.normpath(str(path))
            if key in seen:
                continue
            seen.add(key)
            if not _is_inside(path, root):
                skipped["outside_vault"] += 1
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    skipped["too_large"] += 1
                    continue
                text = _read_text(path)
            except OSError:
                skipped["unreadable"] += 1
                continue
            if frontmatter_only:
                lines = _frontmatter_lines(text)
                if lines is None:
                    skipped["bad_frontmatter"] += 1
                    continue
            else:
                lines = split_lines_lf_crlf(text)
            scanned += 1
            if _line_matches(regex, lines):
                matches.append(key)
    skipped["symlinked_dirs"] = len(symlinked_dirs)
    skipped["unreadable_dirs"] = len(unreadable_dirs)
    return sorted(matches), scanned, skipped


_COMMENT_RE = re.compile(r"\s+#.*$")


def _quote_end(val: str, start: int = 0) -> int:
    """Index of the quote closing the one at ``val[start]``; -1 if unclosed.

    YAML escapes: in ``'...'`` a doubled ``''`` is a literal quote; in
    ``"..."`` a backslash escapes the next character.
    """
    quote = val[start]
    i = start + 1
    while i < len(val):
        ch = val[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if quote == "'" and val[i + 1:i + 2] == "'":
                i += 2
                continue
            return i
        i += 1
    return -1


def _unescape(inner: str, quote: str) -> str:
    if quote == "'":
        return inner.replace("''", "'")
    return re.sub(r'\\(["\\])', r"\1", inner)


def _scalar(val: str) -> str:
    """A YAML scalar: a quoted value keeps everything inside its quotes; an
    unquoted value loses a trailing `` # comment``."""
    val = val.strip()
    if val[:1] in ("'", '"'):
        end = _quote_end(val)
        if end != -1:
            return _unescape(val[1:end], val[0])
        return val.strip("\"'")
    if val.startswith("#"):
        return ""
    return _COMMENT_RE.sub("", val)


def _flow_items(inner: str) -> list[str]:
    """Split the inside of a ``[a, "b, c"]`` flow list on commas outside quotes."""
    items: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch in ("'", '"'):
            end = _quote_end(inner, i)
            if end == -1:  # unclosed: the quote runs to the end of the list
                end = len(inner) - 1
            buf.append(inner[i:end + 1])
            i = end + 1
            continue
        if ch == ",":
            items.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    items.append("".join(buf))
    return [_scalar(t) for t in items if t.strip()]


def _parse_fields(fm_lines: list[str]) -> dict:
    """Top-level ``key: value`` scalars plus ``tags`` (block or flow list).

    A repeated key: the last value wins, as in vault_index._parse_note_detailed,
    the index behind the skills' FTS fast path.
    """
    meta: dict = {}
    tags: list[str] = []
    in_tags = False
    for raw in fm_lines:
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if in_tags and stripped.startswith("- "):
            tags.append(_scalar(stripped[2:]))
            continue
        if in_tags and not stripped:
            continue
        in_tags = False
        if line[:1].isspace() or ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key, val = key.strip(), val.strip()
        if key == "tags":
            close = val.rfind("]")
            tail = val[close + 1:].strip() if close != -1 else ""
            if val.startswith("[") and close != -1 and (not tail or tail.startswith("#")):
                tags.extend(_flow_items(val[1:close]))
                continue
            val = _scalar(val)
            if val:
                tags.append(val)
            else:
                in_tags = True
            continue
        meta[key] = _scalar(val)
    meta["tags"] = tags
    return meta


def _empty_row(path: str, error: str | None) -> dict:
    row = {"path": path}
    row.update({f: None for f in META_FIELDS})
    row.update({"tags": None, "title": None, "snippet": None, "error": error})
    return row


def _describe_parse_failure(reason: str) -> str:
    # Content-free category only: the raw reason can quote note text.
    global _UTILS_WARNED
    try:
        from obsidian_utils import _describe_note_parse_failure
    except ImportError as exc:
        if not _UTILS_WARNED:
            _UTILS_WARNED = True
            print(f"vault_scan: obsidian_utils unavailable: {exc}", file=sys.stderr)
        return "unknown"
    return _describe_note_parse_failure(reason)


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
    breakdown = ", ".join(f"{k}={skipped[k]}" for k in SKIP_KINDS)
    print(
        f"vault_scan: {len(matches)} match(es), {scanned} file(s) scanned, "
        f"{sum(skipped.values())} skipped ({breakdown})",
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
    if _IMPORT_ERROR is not None:
        print(f"ERROR: cannot import vault_scan dependencies: {_IMPORT_ERROR}",
              file=sys.stderr)
        return 1
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
