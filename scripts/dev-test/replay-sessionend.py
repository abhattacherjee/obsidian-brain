#!/usr/bin/env python3
"""
Drive the production SessionEnd hook code path against a captured JSONL
fixture and emit a machine-readable outcome.

Usage:
    replay-sessionend.py --jsonl PATH --cwd PATH
                         [--config PATH] [--mode sessionend|reaper]
                         [--dry-run] [--json]

Modes:
    sessionend (default) — synthesize SessionEnd hook input dict and call
                           hooks.obsidian_session_log._run() directly.
    reaper               — call hooks.obsidian_session_reaper
                           ._reap_orphaned_sessions(); requires #125.

Exit codes:
    0 = ran to completion (outcome printed; may be SKIPPED_*, OK, EXCEPTION,
        or NO_LOG_LINE_EMITTED — caller inspects outcome= field)
    1 = bug in this script (e.g., malformed --json output)
    2 = argparse error or reaper module not yet imported (pre-#125)
    3 = vault-sentinel safety stop
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "hooks"))
sys.path.insert(0, str(_REPO_ROOT))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jsonl", required=True, type=Path)
    p.add_argument("--cwd", required=True, type=str)
    p.add_argument("--config", type=Path, default=None,
                   help="Override $HOME/.claude/obsidian-brain-config.json")
    p.add_argument("--mode", choices=["sessionend", "reaper"], default="sessionend")
    p.add_argument("--dry-run", action="store_true",
                   help="Patch write_vault_note to record calls without writing")
    p.add_argument("--json", dest="emit_json", action="store_true",
                   help="Emit JSON object instead of human-readable key=value lines")
    return p.parse_args(argv)


def _check_vault_sentinel(config_path: Path) -> tuple[bool, str]:
    """Return (ok, message). If guard active and vault path looks real, ok=False."""
    if os.environ.get("_REAL_VAULT_GUARD") != "1":
        return True, ""
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except Exception:
        return True, ""  # No config to read — let later code handle it
    vault = cfg.get("vault_path", "")
    real_vault = str(Path("~/obsidian").expanduser())
    resolved = str(Path(vault).expanduser().resolve()) if vault else ""
    if resolved.startswith(real_vault):
        return False, f"refusing to run: vault path {vault} appears to be the user's real vault"
    return True, ""


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        try:
            print(json.dumps(payload, ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            print(f"BUG: JSON emit failed: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        for k, v in payload.items():
            print(f"{k}={v}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config_path = args.config or Path(os.path.expanduser("~/.claude/obsidian-brain-config.json"))
    ok, msg = _check_vault_sentinel(config_path)
    if not ok:
        print(msg, file=sys.stderr)
        return 3

    if args.mode == "reaper":
        return _run_reaper(args)
    return _run_sessionend(args)


def _run_sessionend(args: argparse.Namespace) -> int:
    raise NotImplementedError  # filled in Task 6


def _run_reaper(args: argparse.Namespace) -> int:
    raise NotImplementedError  # filled in Task 7


if __name__ == "__main__":
    sys.exit(main())
