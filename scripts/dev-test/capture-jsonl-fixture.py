#!/usr/bin/env python3
"""
Truncate a real Claude Code session JSONL into a head/tail fixture under a
byte budget, validating that hooks/obsidian_utils parsers still round-trip.

Usage:
    capture-jsonl-fixture.py --source PATH --out PATH
                             [--max-bytes 102400]
                             [--head-records 30]
                             [--tail-records 30]
                             [--rename-cwd PATH]

Exit codes:
    0 = success
    1 = capture aborted (unreadable source, no parseable lines, can't fit
        budget, or round-trip validation failed)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make hooks/obsidian_utils importable when run from repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "hooks"))
sys.path.insert(0, str(_REPO_ROOT))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--max-bytes", type=int, default=102_400)
    p.add_argument("--head-records", type=int, default=30)
    p.add_argument("--tail-records", type=int, default=30)
    p.add_argument("--rename-cwd", type=str, default=None)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # Implementation in Task 2.
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
