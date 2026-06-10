"""vault_doctor check module registry and shared types.

Each check module in this package must export:
  - NAME: str (kebab-case identifier used on the CLI)
  - DESCRIPTION: str
  - DEFAULT_WINDOW_DAYS: int
  - scan(vault_path, sessions_folder, insights_folder, days, project=None) -> list[Issue]
  - apply(issues, backup_root) -> list[Result]
  - OPT_IN: bool (optional, default False) — True excludes the check from the
    default all-checks sweep; it only runs when named via --check

The registry auto-discovers modules in this package directory on first access.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Issue:
    check: str
    note_path: str
    project: str
    current_source: str
    proposed_source: str
    reason: str
    confidence: float = 1.0
    extra: dict = field(default_factory=dict)


@dataclass
class Result:
    check: str
    note_path: str
    status: str  # "applied" | "skipped" | "error" | "unresolved"
    backup_path: Optional[str] = None
    error: Optional[str] = None


_CHECKS: dict[str, object] = {}


def _discover() -> None:
    """Import every submodule and register ones that expose the check interface.

    Per-module exceptions (ImportError, SyntaxError, etc.) are logged to
    stderr and the offending module is skipped, so one broken check cannot
    take down the whole dispatcher. This keeps the system pluggable.
    """
    if _CHECKS:
        return
    package = __name__
    import sys as _sys
    for mod_info in pkgutil.iter_modules(__path__):
        try:
            mod = importlib.import_module(f"{package}.{mod_info.name}")
        except Exception as exc:  # noqa: BLE001
            print(
                f"[vault_doctor] failed to load check '{mod_info.name}': "
                f"{type(exc).__name__}: {exc}",
                file=_sys.stderr,
            )
            continue
        name = getattr(mod, "NAME", None)
        if name and callable(getattr(mod, "scan", None)) and callable(getattr(mod, "apply", None)):
            _CHECKS[name] = mod


def list_checks() -> list[str]:
    _discover()
    return sorted(_CHECKS.keys())


def get_check(name: str):
    _discover()
    if name not in _CHECKS:
        raise KeyError(f"unknown check: {name!r}; available: {list_checks()}")
    return _CHECKS[name]


def all_checks() -> list:
    """All registered checks minus opt-in ones (``OPT_IN = True``).

    Opt-in checks (e.g. one-shot audit tools like audit-historic-repairs)
    only run when explicitly named via get_check() / --check.
    """
    _discover()
    result = []
    for m in _CHECKS.values():
        flag = getattr(m, "OPT_IN", False)
        if not isinstance(flag, bool):
            raise TypeError(
                f"{getattr(m, 'NAME', m)}: OPT_IN must be bool, got {flag!r}"
            )
        if not flag:
            result.append(m)
    return result
