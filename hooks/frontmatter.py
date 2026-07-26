"""Shared frontmatter-splitting logic (#277).

Extracted from ``note_writer.py`` (#269), which was the first call site to
get this right: a bounded, shape-checked scan for the closing ``---`` fence,
rather than an unbounded "first bare ``---`` anywhere in the file" scan. That
bug class had already recurred independently in other modules (most recently
``vault_index.py``'s 40-line bound silently dropping notes from the search
index) BECAUSE the fix lived only in ``note_writer.py`` and had to be
hand-copied rather than imported. This module exists so there is exactly one
copy of the logic for every caller to share.

Stdlib ``re`` only — this module must import nothing else from this package.
``obsidian_utils.py`` imports ``vault_index.py``; ``note_writer.py`` imports
``obsidian_utils.py``; so ``vault_index.py`` importing ``note_writer.py``
would close a cycle. This module breaks that: both of them can import IT
without either importing the other.
"""
from __future__ import annotations

import re

# Shape of a line that may legitimately appear INSIDE frontmatter, used to
# bound split_frontmatter's closing-fence search (see its docstring).
MAX_FRONTMATTER_LINES = 1000
_FM_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*\s*:")
_FM_ITEM_RE = re.compile(r"^\s*-\s")
# An indented continuation: multi-line YAML values, block scalars (`x: |`),
# and nested mappings all produce these.
_FM_CONT_RE = re.compile(r"^\s+\S")


def split_lines_lf_crlf(text: str) -> list[str]:
    """Split ``text`` into lines, each with its terminator attached,
    recognizing ONLY ``"\\r\\n"`` and ``"\\n"`` as line terminators.

    Deliberately NOT ``str.splitlines(keepends=True)``: that method also
    treats a bare ``"\\r"`` (not part of a ``"\\r\\n"`` pair) -- plus
    ``\\v``, ``\\f``, and several Unicode separators -- as a line break.
    A note body can legitimately contain a bare ``\\r`` (e.g. a pasted
    terminal progress-bar redraw inside a fenced code block); splitting on
    it would be the same class of corruption ``_normalize_eol`` guards
    against, just via a different function. Every line this parses is
    later reassembled with a plain ``"".join(...)``, so this is lossless
    for reconstruction regardless of where it draws boundaries -- but where
    it draws them still matters for ``_is_trailing_marker``/frontmatter-key
    matching and the "does the file already end in a newline" check, so it
    must not draw a boundary at a bare ``\\r`` that isn't really one.
    """
    lines: list[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            lines.append(text[start:i + 1])
            start = i + 1
            i += 1
        elif ch == "\r" and i + 1 < n and text[i + 1] == "\n":
            lines.append(text[start:i + 2])
            start = i + 2
            i += 2
        else:
            i += 1
    if start < n:
        lines.append(text[start:])
    return lines


def split_frontmatter(lines: list[str]):
    """Split ``lines`` (from ``split_lines_lf_crlf``) into the
    frontmatter fence pair and body.

    Returns ``(open_fence_line, frontmatter_lines, close_fence_line,
    body_lines, error)``. On success ``error`` is None; on failure the first
    four are None and ``error`` names WHICH failure occurred.

    The distinct error strings matter: the SKILL.md call sites tell the model
    to surface this message to the user, so a wrong diagnosis sends someone to
    repair a file that is not broken. Reporting "no closing '---'" on a note
    whose fence demonstrably exists (it was just past the line bound) is
    exactly that failure.

    The closing-fence search is BOUNDED and SHAPE-CHECKED, not "first ``---``
    anywhere in the file". On a note whose closing fence is missing (e.g.
    corrupted by an earlier bad write) but whose body contains a ``---``
    horizontal rule, the unbounded version treated the title heading and body
    prose as frontmatter: ``last_updated`` and new tags were inserted among
    the prose and the update section was appended after the rule, at rc 0.

    Every candidate frontmatter line must therefore be blank, ``key:``-shaped,
    a ``- `` list item, or an indented continuation (multi-line YAML values).
    A ``# Title`` heading or a prose paragraph is none of those, so the scan
    stops and the whole command fails loudly instead of silently mutating the
    body -- that shape check is the guard doing the real work here.

    ``MAX_FRONTMATTER_LINES`` is a second, cruder bound for a pathological
    file whose body happens to be all key-shaped lines. It is NOT a "notes
    are small" assumption: it was 200, which false-rejected 11 well-formed
    notes in the live vault -- /emerge and /standup output with long
    ``projects:`` lists, whose closing fences sit as deep as line 460. 1000
    is ~2x headroom over the deepest observed. Raise it, do not remove it.
    """
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None, None, None, None, (
            "malformed frontmatter (file does not open with a '---' fence)"
        )
    for i in range(1, min(len(lines), MAX_FRONTMATTER_LINES + 1)):
        stripped = lines[i].rstrip("\r\n")
        if stripped == "---":
            return lines[0], lines[1:i], lines[i], lines[i + 1:], None
        if not stripped.strip():
            continue
        if (
            _FM_KEY_RE.match(stripped)
            or _FM_ITEM_RE.match(stripped)
            or _FM_CONT_RE.match(stripped)
        ):
            continue
        return None, None, None, None, (
            "malformed or missing frontmatter (no closing '---'; stopped at a "
            f"line that is not frontmatter: {stripped[:60]!r})"
        )
    if len(lines) > MAX_FRONTMATTER_LINES:
        return None, None, None, None, (
            f"frontmatter exceeds {MAX_FRONTMATTER_LINES} lines (limit reached "
            "before the frontmatter block ended -- the note may be fine; this "
            "is a size limit, not a missing fence)"
        )
    return None, None, None, None, "malformed or missing frontmatter (no closing '---')"
