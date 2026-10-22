"""vault_doctor check module registry and shared types.

Each check module in this package must export:
  - NAME: str (kebab-case identifier used on the CLI)
  - DESCRIPTION: str
  - DEFAULT_WINDOW_DAYS: int
  - scan(vault_path, sessions_folder, insights_folder, days, project=None) -> list[Issue]
  - apply(issues, backup_root) -> list[Result]
  - OPT_IN: bool (optional, default False) — True excludes the check from the
    default all-checks sweep; it only runs when named via --check
  - SCAN_EXPLICIT: bool (optional, default False) — True makes the dispatcher
    pass ``explicit=<named via --check>`` to scan()

The registry auto-discovers modules in this package directory on first access.
"""

from __future__ import annotations

import datetime
import glob
import importlib
import os
import pkgutil
import time
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
    # Set from literals in check modules (e.g. 0.99, 0.5, 0.0). If a check
    # ever computes confidence arithmetically, round to 2 decimals first —
    # vault_doctor's --min-confidence filter compares with exact >=, and a
    # float artifact like 0.8999999999 would silently miss the threshold.
    confidence: float = 1.0
    extra: dict = field(default_factory=dict)


@dataclass
class Result:
    check: str
    note_path: str
    status: str  # "applied" | "skipped" | "error" | "unresolved"
    backup_path: Optional[str] = None
    error: Optional[str] = None


# A session whose transcript was written this recently, and which has no
# SessionEnd in the hook log, is treated as still running (#438). Its session
# note, snapshot list and snapshot parent are only written at SessionEnd, so
# their absence is expected, not a defect.
LIVE_WINDOW_SECONDS = 3600
_HOOK_LOGS = ("obsidian-brain-hook.log", "obsidian-brain-hook.log.1")
# SessionEnd runs after the last transcript write; allow for clock skew.
END_SLACK_SECONDS = 300


def _session_end_logged(sid: str, since: float) -> bool:
    """True when the hook log (current or rotated) has a SessionEnd for sid
    logged at or after ``since`` (less END_SLACK_SECONDS).

    A SessionEnd older than the transcript's last write belongs to an earlier
    run of a session that was resumed, so it does not end liveness. A line
    whose timestamp does not parse counts as seen, so the gap is reported.
    The log writes the first 8 characters of the session id. An unreadable
    log counts as "not seen", so only the transcript mtime bound applies.
    """
    needle = " SessionEnd "
    tag = f" sid={sid[:8]} "
    claude_dir = os.path.join(os.path.expanduser("~"), ".claude")
    for name in _HOOK_LOGS:
        try:
            with open(os.path.join(claude_dir, name), encoding="utf-8",
                      errors="replace") as fh:
                for line in fh:
                    if needle not in line or tag not in line:
                        continue
                    try:
                        logged = datetime.datetime.fromisoformat(
                            line.split(" ", 1)[0]).timestamp()
                    except ValueError:
                        return True
                    if logged >= since - END_SLACK_SECONDS:
                        return True
        except OSError:
            continue
    return False


def session_is_live(sid: str, jsonl_path: str | None = None,
                    now: float | None = None) -> bool:
    """True when the session still looks like it is running (#438).

    Live means the transcript was modified within LIVE_WINDOW_SECONDS and the
    hook log has no SessionEnd for it logged after that write (a resumed
    session's earlier SessionEnd does not count). Anything unknown (empty sid, no
    transcript, unreadable mtime) is NOT live, so a real gap is still
    reported.
    """
    if not sid:
        return False
    if jsonl_path is None:
        pattern = os.path.join(os.path.expanduser("~"), ".claude", "projects",
                               "*", f"{glob.escape(sid)}.jsonl")
        matches = glob.glob(pattern)
        if not matches:
            return False
        jsonl_path = max(matches, key=lambda p: os.path.getmtime(p)
                         if os.path.exists(p) else 0)
    try:
        mtime = os.path.getmtime(jsonl_path)
    except OSError:
        return False
    if (time.time() if now is None else now) - mtime > LIVE_WINDOW_SECONDS:
        return False
    return not _session_end_logged(sid, mtime)


_CHECKS: dict[str, object] = {}


def _discover() -> None:
    """Import every submodule and register ones that expose the check interface.

    Per-module exceptions (ImportError, SyntaxError, etc.) are logged to
    stderr and the offending module is skipped, so one broken check cannot
    take down the whole dispatcher. This keeps the system pluggable.
    Modules that import cleanly but do not expose the check interface
    (NAME + callable scan/apply) are also warned about on stderr.
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
        else:
            # A module that imports fine but lacks the interface (typo'd
            # NAME/scan/apply, helper accidentally dropped into the package)
            # must not vanish silently — its check would simply never run.
            print(
                f"[vault_doctor] module {mod_info.name} loaded but does not "
                f"expose the check interface; skipped",
                file=_sys.stderr,
            )


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
