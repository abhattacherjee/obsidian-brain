#!/usr/bin/env python3
"""Backfill curated note importance (#449), with a dry run by default."""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))

import vault_index  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="importance_backfill.py",
        description="Backfill derived importance for non-session notes (#449).",
    )
    parser.add_argument("--apply", action="store_true",
                        help="update the index (default: dry run)")
    parser.add_argument("--db", default=None, help="index DB path")
    args = parser.parse_args(argv)
    try:
        result = vault_index.backfill_importance(
            args.db or vault_index._default_db_path(), apply=args.apply,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    mode = "apply" if result["applied"] else "dry run"
    print(f"importance backfill ({mode})")
    for key in ("scanned", "changed", "unreadable", "by_value", "from_to"):
        print(f"{key}: {result[key]}")
    if result["applied"]:
        print(f"updated: {result['updated']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
