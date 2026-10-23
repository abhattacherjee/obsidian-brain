#!/usr/bin/env python3
"""One-off backfill of derived note importance (#449).

_sync re-derives only notes whose mtime changed, so rows indexed before
#449 keep the old default 5. Re-parse non-session notes to apply the rule:
frontmatter importance 1-10 wins; claude-insight, claude-decision and
claude-error-fix get 3 with source: auto-capture, otherwise 6. Other types
without an override and session scores are left untouched.

Usage:
    python3 scripts/importance_backfill.py                 # dry run
    python3 scripts/importance_backfill.py --apply         # write changes
    python3 scripts/importance_backfill.py --db PATH       # use another DB

Options:
    --apply     write the changes (default: dry run)
    --db PATH   index DB (default: $OBSIDIAN_BRAIN_DB or
                ~/.claude/obsidian-brain-vault.db)

No backup is taken because only the importance column changes and from_to
names every change.

Exit codes: 0 ok, 1 missing DB or failure, 2 usage error.
"""

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
                        help="write the changes (default: dry run)")
    parser.add_argument("--db", default=None,
                        help="index DB path (default: $OBSIDIAN_BRAIN_DB or "
                             "~/.claude/obsidian-brain-vault.db)")
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
