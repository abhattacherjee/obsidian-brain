"""Memory files the wiki can cite (#396, epic #383).

This is the one place that knows where a host keeps its memory files. On
Claude Code they are ``~/.claude/projects/<project-dir>/memory/*.md``. On any
other host there are none until #272 adds Codex. Skills never name these
paths; they call ``wiki.py memgrep``, which asks this module.

Every listed file is a regular, non-symlinked ``*.md`` directly inside a
non-symlinked ``memory/`` folder of a non-symlinked project folder, resolved
and checked to sit under ``~/.claude/projects``. ``MEMORY.md`` is an index,
not a memory, so it is skipped.
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


def memory_sources(host: str, config: dict | None = None, errors: list | None = None) -> list:
    """Memory file paths for ``host``: sorted, resolved, unique. ``config``
    is unused today; it is part of the interface for hosts that keep memory
    elsewhere.

    When ``errors`` is a list, each failure is appended as ``{"path",
    "error"}``: a projects root that exists but cannot be listed, a project
    or ``memory/`` folder that cannot be read, or a file that cannot be
    resolved. A missing root is not an error (no memory yet)."""
    if host != "claude-code":
        return []  # Codex memory arrives with #272

    def fail(path, exc) -> None:
        if errors is not None:
            errors.append({"path": str(path), "error": str(exc)})

    root = _projects_root()
    try:
        real_root = root.resolve(strict=True)
        projects = sorted(root.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        fail(root, exc)
        return []
    found: dict = {}
    for proj in projects:
        mem = proj / "memory"
        try:
            if proj.is_symlink() or mem.is_symlink() or not mem.is_dir():
                continue
            # iterdir, not glob: glob returns [] for a folder it cannot read.
            files = sorted(f for f in mem.iterdir() if f.name.endswith(".md"))
        except OSError as exc:
            fail(mem, exc)
            continue  # one unreadable project must not hide the rest
        for f in files:
            try:
                if f.name == INDEX_NAME or f.is_symlink() or not f.is_file():
                    continue
                real = f.resolve(strict=True)
            except OSError as exc:
                fail(f, exc)
                continue
            if real.is_relative_to(real_root):
                found[str(real)] = real
    return [found[k] for k in sorted(found)]


def failed_scopes(errors) -> tuple:
    """Classify ``memory_sources`` errors as ``(root_failed, projects,
    names)``: whether the projects root itself failed, the project folders
    whose ``memory/`` could not be read, and the ``<project>/<file>.md``
    names that could not be resolved. A path outside the root is ignored."""
    root = _projects_root()
    root_failed, projects, names = False, set(), set()
    for e in errors or []:
        p = Path(str(e.get("path", "")))
        if p == root:
            root_failed = True
            continue
        try:
            parts = p.relative_to(root).parts
        except ValueError:
            continue
        if len(parts) == 3 and parts[1] == "memory":
            names.add(f"{parts[0]}/{parts[2]}")
        elif parts:
            projects.add(parts[0])
    return root_failed, projects, names


def memory_name(path) -> str:
    """``<project-dir>/<file>.md``: how pages and payloads name a memory file."""
    p = Path(path)
    return f"{p.parent.parent.name}/{p.name}"
