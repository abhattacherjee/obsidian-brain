"""
Order-independent argument parser for /check-items.

Extracted to a module for unit-testability. Used by SKILL.md Step 1.
Per spec § Invocation contract (lines 35-52).
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

from obsidian_utils import get_workspace_roots


@dataclass
class Scope:
    mode: str = "current"
    project: str | None = None
    window_days: int = 14
    show_all: bool = False
    dry_run: bool = False
    no_cache: bool = False
    unknown_tokens: list[str] = field(default_factory=list)


_WINDOW_RE = re.compile(r"^(\d+)d$")


def _known_projects():
    projects: set[str] = set()
    for root in get_workspace_roots():
        try:
            projects.update(
                name for name in os.listdir(root)
                if os.path.isdir(os.path.join(root, name))
            )
        except OSError:
            continue
    return projects


def _vault_known_projects() -> set[str]:
    """Project names that have session notes in the vault index.

    A notes-only project (#318: `abhishek-work-vault`) has no directory
    under any workspace root, so `_known_projects()` alone cannot recognise
    it and `parse_scope` would drop its name. The vault index already
    records one `project` per note, which is the authoritative list of
    projects /check-items can actually triage.

    Any failure (no index yet, unreadable DB, schema drift) returns an empty
    set — the workspace-root list still works, so a lookup failure must
    degrade rather than break argument parsing.
    """
    try:
        import vault_index
        conn = vault_index._connect(vault_index._default_db_path())
        try:
            rows = conn.execute(
                "SELECT DISTINCT project FROM notes "
                "WHERE project IS NOT NULL AND project != ''"
            ).fetchall()
        finally:
            conn.close()
        return {str(r[0]) for r in rows if r and r[0]}
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(f"[check-items] vault project lookup unavailable: {exc}",
              file=sys.stderr)
        return set()


def parse_scope(argv):
    """Parse argv tokens into a Scope. Flags and positionals are order-
    independent. Unknown tokens are recorded on scope.unknown_tokens rather
    than silently dropped."""
    scope = Scope()
    projects = _known_projects() | _vault_known_projects()
    for tok in argv:
        if tok == "--show-all":
            scope.show_all = True
            continue
        if tok == "--dry-run":
            scope.dry_run = True
            continue
        if tok == "--no-cache":
            scope.no_cache = True
            continue
        if tok in ("all", "--vault"):
            scope.mode = "vault"
            scope.project = None  # clear stale project if 'all' appears after a project token
            continue
        m = _WINDOW_RE.match(tok)
        if m:
            scope.window_days = int(m.group(1))
            continue
        if tok in projects:
            scope.mode = "project"
            scope.project = tok
            continue
        scope.unknown_tokens.append(tok)
    return scope
