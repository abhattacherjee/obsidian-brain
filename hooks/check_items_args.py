"""
Order-independent argument parser for /check-items.

Extracted to a module for unit-testability. Used by SKILL.md Step 1.
Per spec § Invocation contract (lines 35-52).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass
class Scope:
    mode: str = "current"
    project: str | None = None
    window_days: int = 14
    show_all: bool = False
    dry_run: bool = False
    no_cache: bool = False


_WINDOW_RE = re.compile(r"^(\d+)d$")
_WORKSPACE_ROOT = os.path.expanduser("~/dev/claude_workspace")


def _known_projects():
    try:
        return {
            name for name in os.listdir(_WORKSPACE_ROOT)
            if os.path.isdir(os.path.join(_WORKSPACE_ROOT, name))
        }
    except OSError:
        return set()


def parse_scope(argv):
    """Parse argv tokens into a Scope. Flags and positionals are order-
    independent. Unknown tokens are ignored."""
    scope = Scope()
    projects = _known_projects()
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
            continue
        m = _WINDOW_RE.match(tok)
        if m:
            scope.window_days = int(m.group(1))
            continue
        if tok in projects:
            scope.mode = "project"
            scope.project = tok
            continue
    return scope
