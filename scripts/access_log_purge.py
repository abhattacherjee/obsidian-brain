#!/usr/bin/env python3
"""One-off purge of access_log rows written by internal searches (#448).

Until #448, search_vault logged every hit of the plugin's own dedup checks
as a "search" access, which drowned ACT-R activation in noise from
2026-10-18 on. The log does not record the caller, so the cleanup rule
(chosen 2026-10-23) is: delete every row of one context type at or after
local midnight of --since. "recall" and "related" rows are kept by
default because only --context search is purged.

Usage:
    python3 scripts/access_log_purge.py --since 2026-10-18            # dry run
    python3 scripts/access_log_purge.py --since 2026-10-18 --apply    # back up, then delete

Options:
    --since YYYY-MM-DD   first local day to purge (from 00:00 local time)
    --context TYPE       context_type to purge: search (default), recall, related
    --apply              actually delete; without it nothing changes
    --list               also print every matching row (note path, local time)
    --db PATH            index DB (default: $OBSIDIAN_BRAIN_DB or
                         ~/.claude/obsidian-brain-vault.db)
    --backup-dir DIR     where --apply writes the backup copy
                         (default ~/.claude/obsidian-brain/)

Exit codes: 0 ok, 1 refused or failed (no DB, backup failed, DB error;
nothing deleted), 2 usage error.
"""

import argparse
import datetime
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))

import vault_index  # noqa: E402

CONTEXT_TYPES = ("search", "recall", "related")


def _local_midnight(value: str) -> float:
    try:
        day = datetime.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}")
    # Naive datetime: .timestamp() reads it as local time.
    return day.timestamp()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="access_log_purge.py",
        description="Purge access_log rows of one context type since a local date (#448).",
    )
    parser.add_argument("--since", required=True, type=_local_midnight,
                        help="first local day to purge, YYYY-MM-DD")
    parser.add_argument("--context", default="search", choices=CONTEXT_TYPES)
    parser.add_argument("--apply", action="store_true",
                        help="back up the DB, then delete (default: dry run)")
    parser.add_argument("--list", action="store_true", dest="list_rows",
                        help="print every matching row")
    parser.add_argument("--db", default=None, help="index DB path")
    parser.add_argument("--backup-dir", default=None, help="backup directory")
    args = parser.parse_args(argv)

    db = args.db or vault_index._default_db_path()
    # Checked here, not by catching FileNotFoundError below: a missing
    # backup directory component raises that too.
    if not os.path.isfile(db):
        print(f"ERROR: no index DB at {db}", file=sys.stderr)
        return 1
    try:
        result = vault_index.purge_access_log(
            db, args.context, args.since, apply=args.apply, backup_dir=args.backup_dir,
            list_rows=args.list_rows,
        )
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"ERROR: {exc}; nothing deleted", file=sys.stderr)
        return 1

    mode = "apply" if result["applied"] else "dry run"
    print(f"access_log purge ({mode}): context={args.context}, "
          f"since {datetime.datetime.fromtimestamp(args.since):%Y-%m-%d %H:%M} local")
    for path, ts in result.get("rows", []):
        print(f"  {datetime.datetime.fromtimestamp(ts):%Y-%m-%d %H:%M:%S}  {path}")
    for day, count in result["by_day"].items():
        print(f"  {day}  {count}")
    print(f"matched {result['matched']}")
    if result["applied"]:
        print(f"backup: {result['backup_path']}")
        print(f"deleted {result['deleted']} (before {result['before_total']}, "
              f"after {result['after_total']})")
    else:
        print("dry run: nothing deleted; re-run with --apply to delete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
