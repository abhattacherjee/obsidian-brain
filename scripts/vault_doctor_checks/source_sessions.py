"""vault_doctor check: detect and repair stale source_session backlinks.

Full implementation comes in Tasks 7-8. This stub exists so the registry
has a module to discover for Task 6 tests.
"""

from __future__ import annotations

from . import Issue, Result

NAME = "source-sessions"
DESCRIPTION = "Detect and repair stale source_session backlinks"
DEFAULT_WINDOW_DAYS = 7


def scan(vault_path, sessions_folder, insights_folder, days, project=None) -> list[Issue]:
    raise NotImplementedError("implemented in Task 7")


def apply(issues, backup_root) -> list[Result]:
    raise NotImplementedError("implemented in Task 8")
