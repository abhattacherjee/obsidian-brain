"""Link parsing shared by scripts/vault_doctor_checks/memory_index.py and hooks/memory_consolidate.py."""

from __future__ import annotations

import re
from pathlib import Path

# Every run below is bounded. An unbounded run backtracks from every start
# position, which is quadratic in the file size: dropping the {0,200} bound
# takes _BARE_MD_RE from 0.044 s to 8.26 s on 80 KB of "a", and from 0.031 s
# to 5.87 s on 80 KB of "a." (measured here). This is hardening, not a bug
# anyone is hitting — the longest [\w.-] run across the author's 1,112 real
# memory files is 79 characters. 300 and 200 are far above any real memory
# filename or link target.

# An explicit pointer at a local file, in the CommonMark forms that name one:
# ``](name.md)``, ``](./name.md)``, ``](name.md "Title")``, ``](<name.md>)``.
# The optional title and angle brackets are not decoration — without them a
# pointer at a missing file produces no dangling row at all, which reads as
# clean.
_MD_LINK_RE = re.compile(
    r"\]\(\s*<?([^)<>\s]{1,300}\.md)>?"
    r"""(?:\s+(?:"[^"]{0,300}"|'[^']{0,300}'|\([^)]{0,300}\)))?\s*\)"""
)
# ``[[name]]``, ``[[name|alias]]``, ``[[name#heading]]``.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]{1,300})(?:[|#][^\[\]]{0,300})?\]\]")
# A bare filename anywhere in the prose. Deliberately unanchored: every hit is
# intersected with the store's real filenames before it counts, so a false
# match costs nothing while a missed one would invent an orphan. A name inside
# a path (``docs/archive/note.md``) counts, and so does one wrapped in
# underscore emphasis (``_note.md_``). The {0,200} bound is what keeps this
# linear; there is deliberately no lookbehind, because one would reject the
# ``_note.md_`` start position and invent an orphan for a note that really is
# mentioned.
_BARE_MD_RE = re.compile(r"[A-Za-z0-9][\w.-]{0,200}\.md")


def _link_targets(text: str) -> set[str]:
    """Every ``.md`` basename this text could be pointing at (generous)."""
    out: set[str] = set()
    for m in _MD_LINK_RE.findall(text):
        out.add(Path(m).name)
    for m in _WIKILINK_RE.findall(text):
        # Basename, matching the markdown-link path above: ``[[archive/a]]``
        # points at the same entry as ``[[a]]``, and treating the whole
        # subpath as a filename would invent an orphan.
        name = Path(m.strip()).name
        if not name:
            continue
        out.add(name if name.endswith(".md") else f"{name}.md")
    for m in _BARE_MD_RE.findall(text):
        out.add(Path(m).name)
    return out


def _md_child_link_targets(text: str) -> set[str]:
    """Markdown-link targets naming a direct child of the store (dangling only).

    A leading ``./`` is stripped first: ``](./name.md)`` names a direct child
    exactly as ``](name.md)`` does, and dropping it left a real dangling
    pointer unreported.

    After that strip, a target still containing a slash is about something
    else — an external URL (``https://…/README.md``), a file above the store
    (``../outside.md``) or one in a subdirectory (``sub/deep.md``). Taking its
    basename would invent a dangling row at a path the index never claimed;
    the ``reference`` memory type is defined as pointers to external
    resources, so index lines holding URLs are expected. Recursion into
    subdirectories is deliberately not added: the store is flat by
    convention, one MEMORY.md beside its entries.
    """
    out: set[str] = set()
    for m in _MD_LINK_RE.findall(text):
        target = m[2:] if m.startswith("./") else m
        if "/" in target or "\\" in target:
            continue
        out.add(target)
    return out


def line_pointers(line: str, store_names: set[str]) -> set[str]:
    """Return store entries named by direct Markdown links on the line."""
    return _md_child_link_targets(line) & store_names


def line_asides(line: str, store_names: set[str]) -> set[str]:
    """Return other store entries mentioned on the line."""
    return (_link_targets(line) & store_names) - line_pointers(line, store_names)
