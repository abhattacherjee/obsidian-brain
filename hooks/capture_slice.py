"""capture_slice.py -- read a byte range of a live transcript for auto-capture (#425).

A transcript ``.jsonl`` is appended to while the session runs, and keeps every
row from before an autocompact. ``read_slice`` reads from a stored byte
offset and never returns, or points past, a half-written last line, so the
next batch starts exactly where this one stopped.
"""

from __future__ import annotations

import json
from pathlib import Path

import obsidian_utils

_MAX_TEXT_PER_TURN = 4000
END_KEY = "_capture_end"  # byte offset just past the row's line, set by read_slice


def read_slice(path: Path, start: int, max_bytes: int = 400_000) -> tuple[list[dict], int]:
    """Rows from ``start`` up to the last complete line within ``max_bytes``.

    Returns ``(rows, end_offset)``; ``end_offset`` is always ``start`` or a
    position just after a ``\\n``. Lines that are not JSON objects are skipped
    but still consumed. A single line longer than ``max_bytes`` is skipped
    whole so the offset can never stall on it.
    """
    with open(path, "rb") as fh:
        fh.seek(start)
        chunk = fh.read(max_bytes)
        cut = chunk.rfind(b"\n")
        if cut < 0:
            if len(chunk) < max_bytes:
                return [], start  # only a partial line so far: wait for more
            # One line bigger than the window: skip to its end.
            while True:
                more = fh.read(max_bytes)
                if not more:
                    return [], start
                nl = more.find(b"\n")
                if nl >= 0:
                    return [], fh.tell() - len(more) + nl + 1
    body = chunk[: cut + 1]
    rows: list[dict] = []
    pos = start
    for line in body.split(b"\n")[:-1]:
        pos += len(line) + 1
        if not line.strip():
            continue
        try:
            row = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(row, dict):
            row[END_KEY] = pos
            rows.append(row)
    return rows, start + cut + 1


def _clip(text: str) -> str:
    text = text.strip()
    if len(text) > _MAX_TEXT_PER_TURN:
        return text[:_MAX_TEXT_PER_TURN] + " […]"
    return text


def _render_row(row: dict) -> list[str]:
    if row.get("type") == "system" and row.get("subtype") == "compact_boundary":
        return ["--- (context compacted here) ---"]
    if row.get("isMeta"):
        return []
    out = []
    for text in obsidian_utils.extract_user_messages([row]):
        out.append("USER: " + _clip(text))
    for text in obsidian_utils.extract_assistant_messages([row]):
        out.append("ASSISTANT: " + _clip(text))
    for tool in obsidian_utils.extract_tool_uses([row]):
        detail = tool.get("detail") or ""
        out.append(f"TOOL {tool.get('name', '?')}: {detail}".rstrip(": ")[:300])
    for err in obsidian_utils._extract_errors([row]):
        out.append("ERROR: " + _clip(err))
    return out


def render_slice_upto(rows: list[dict], max_chars: int = 120_000) -> tuple[str, int]:
    """Plain-text view of ``rows`` for the worker prompt, secrets scrubbed.

    Keeps transcript order: user and assistant text, tool calls (name and a
    short detail), tool errors, and compaction markers. Skips meta rows
    (injected reminders) and tool-result bodies. Stops before the row that
    would pass ``max_chars`` and returns how many rows it used, so the caller
    moves its offset only past rows the worker saw. The first row is always
    used (cut short if it is alone too long), so a batch always progresses.
    """
    out: list[str] = []
    size = 0
    used = 0
    for row in rows:
        lines = _render_row(row)
        add = sum(len(x) + 1 for x in lines)
        if used and size + add > max_chars:
            break
        out += lines
        size += add
        used += 1
    text = obsidian_utils.scrub_secrets("\n".join(out))
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[… slice truncated]"
    return text, used


def render_slice(rows: list[dict], max_chars: int = 120_000) -> str:
    """``render_slice_upto``'s text alone."""
    return render_slice_upto(rows, max_chars)[0]


def first_user_text(path: Path, max_bytes: int = 200_000) -> str:
    """Text of the first non-meta user message in the transcript, or ''."""
    try:
        rows, _ = read_slice(path, 0, max_bytes)
    except OSError:
        return ""
    for row in rows:
        if row.get("isMeta"):
            continue
        texts = obsidian_utils.extract_user_messages([row])
        if texts:
            return texts[0]
    return ""


def count_user_messages(path: Path, max_bytes: int = 5_000_000) -> int:
    """Number of non-meta user text messages in the first ``max_bytes``."""
    try:
        rows, _ = read_slice(path, 0, max_bytes)
    except OSError:
        return 0
    return sum(len(obsidian_utils.extract_user_messages([r])) for r in rows if not r.get("isMeta"))
