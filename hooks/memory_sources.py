"""Memory files the wiki can cite (#396, epic #383).

The runtime resolver selects the host's memory root. Codex native memory
discovery is unsupported because it has no equivalent memory-file API.
Skills call ``wiki.py memgrep``, which asks this module.

Every listed file is a regular, non-symlinked ``*.md`` directly inside a
non-symlinked ``memory/`` folder of a non-symlinked project folder, resolved
and checked to sit under the selected projects root. ``MEMORY.md`` is an index,
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
    from runtime_context import current_runtime_context
    context = current_runtime_context()
    if context:
        return "claude-code" if context.host == "claude" else context.host
    for name in _CODEX_HOST_MARKERS:
        if os.environ.get(name, "").strip():
            return "codex"
    return "claude-code"


def _projects_root() -> Path | None:
    from runtime_context import current_runtime_context, native_memory_projects_root
    context = current_runtime_context()
    if context is not None:
        return native_memory_projects_root(context)
    return _legacy_projects_root()


def _legacy_projects_root() -> Path:
    """Read-only memory discovery for explicit unbound compatibility callers."""
    from runtime_context import historical_source_roots
    return historical_source_roots("claude")[0]


def memory_sources(host: str, config: dict | None = None, errors: list | None = None) -> list:
    """Memory file paths for ``host``: sorted, resolved, unique. ``config``
    is unused today; it is part of the interface for hosts that keep memory
    elsewhere.

    When ``errors`` is a list, each failure is appended as ``{"path",
    "error"}``: a projects root that exists but cannot be listed, a project
    or ``memory/`` folder that cannot be read, or a file that cannot be
    resolved. A missing root is not an error (no memory yet)."""
    from runtime_context import current_runtime_context
    context = current_runtime_context()
    if context is not None:
        host = context.host
    if host not in {"claude-code", "claude"}:
        if errors is not None:
            errors.append({"path": "", "error": "Native memory discovery is unsupported for " + host,
                           "host": host, "unsupported": True})
        return []

    def fail(path, exc) -> None:
        if errors is not None:
            errors.append({"path": str(path), "error": str(exc)})

    root = _projects_root()
    if root is None:
        return []
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
    if any(error.get("unsupported") for error in errors or []):
        return True, set(), set()
    root = _projects_root()
    if root is None:
        return True, set(), set()
    root_failed, projects, names = False, set(), set()
    for e in errors or []:
        if e.get("unsupported"):
            root_failed = True
            continue
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
