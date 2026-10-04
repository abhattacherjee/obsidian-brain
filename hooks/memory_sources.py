"""Memory files the wiki can cite (#396, epic #383).

This is the one place that knows where a host keeps its memory files. On
Claude Code they are ``~/.claude/projects/<project-dir>/memory/*.md``. On any
other host there are none until #272 adds Codex. Skills never name these
paths; they call ``wiki.py memgrep``, which asks this module.

Every listed file is a regular, non-symlinked ``*.md`` directly inside a
non-symlinked ``memory/`` folder, resolved and checked to sit under
``~/.claude/projects``. ``MEMORY.md`` is an index, not a memory, so it is
skipped.
"""

from __future__ import annotations

import os
from pathlib import Path

from obsidian_utils import _CODEX_HOST_MARKERS

INDEX_NAME = "MEMORY.md"


def detect_host() -> str:
    """``codex`` when any Codex marker env var is set (not blank), else
    ``claude-code``."""
    for name in _CODEX_HOST_MARKERS:
        if os.environ.get(name, "").strip():
            return "codex"
    return "claude-code"


def _projects_root() -> Path:
    # Path.home() reads $HOME on POSIX, so tests can point it at a tmp dir.
    return Path.home() / ".claude" / "projects"


def memory_sources(host: str, config: dict | None = None) -> list:
    """Sorted, resolved memory file paths for ``host``. ``config`` is unused
    today; it is part of the interface for hosts that keep memory elsewhere."""
    if host != "claude-code":
        return []  # Codex memory arrives with #272
    root = _projects_root()
    try:
        real_root = root.resolve(strict=True)
        projects = sorted(root.iterdir())
    except OSError:
        return []
    out = []
    for proj in projects:
        mem = proj / "memory"
        try:
            if mem.is_symlink() or not mem.is_dir():
                continue
            files = sorted(mem.glob("*.md"))
        except OSError:
            continue  # one unreadable project must not hide the rest
        for f in files:
            try:
                if f.name == INDEX_NAME or f.is_symlink() or not f.is_file():
                    continue
                real = f.resolve(strict=True)
            except OSError:
                continue
            if real.is_relative_to(real_root):
                out.append(real)
    return out


def memory_name(path) -> str:
    """``<project-dir>/<file>.md``: how pages and payloads name a memory file."""
    p = Path(path)
    return f"{p.parent.parent.name}/{p.name}"
