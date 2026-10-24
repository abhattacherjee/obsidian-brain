"""The /recall related-list selection rule (#458). Pure, stdlib only.

Spike #458: judges kept 1% of the retros that filled /recall's related list,
so the list keeps at most ``RELATED_MAX_RETROS`` of them. Its own module so
both readers use it: ``vault_index.query_related_notes`` (the index path) and
the file-scan fallback in ``obsidian_utils.build_context_brief``, which also
runs when ``vault_index`` cannot be imported.
"""

from __future__ import annotations

RELATED_MAX_RETROS = 1
RETRO_TYPE = "claude-retro"


def select_related(rows, limit: int, max_retros: int = RELATED_MAX_RETROS) -> list:
    """Pick up to ``limit`` rows in their given order, skipping retros once ``max_retros`` are taken.

    ``rows`` are mappings with a ``type`` key, in layer order. The kept retro
    is the first one in that order; nothing else is reordered. A row with no
    type, or any type but ``claude-retro``, is never skipped.
    """
    picked: list = []  # limit <= 0: the loop's first check stops it, so the result is []
    retros = 0
    for row in rows:
        if len(picked) >= limit:
            break
        if row.get("type") == RETRO_TYPE:
            if retros >= max_retros:
                continue
            retros += 1
        picked.append(row)
    return picked
