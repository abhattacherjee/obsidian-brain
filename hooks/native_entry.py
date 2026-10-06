#!/usr/bin/env python3
"""Run an explicitly bound native hook from its installed resource tree."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

STARTED_AT = time.monotonic()
RESOURCE_ROOT = Path(__file__).resolve().parent.parent


def run(argv=None, *, started_at=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(value == "--resource-root" or value.startswith("--resource-root=")
           for value in arguments):
        print("[obsidian-brain] native resource root must be adjacent to the entry point.",
              file=sys.stderr)
        return 0
    selection = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    selection.add_argument("--host")
    selection.add_argument("--client")
    try:
        selected, _ = selection.parse_known_args(arguments)
    except SystemExit:
        print("[obsidian-brain] native lifecycle arguments are invalid; capture was skipped; no source was retained.",
              file=sys.stderr)
        return 0
    if selected.host == "codex" and not selected.client:
        print("[obsidian-brain] native client identity is unavailable; capture was skipped; no source was retained. "
              "CLI/Desktop handoff requires a current-client binding.", file=sys.stderr)
        return 0
    sys.path.insert(0, str(RESOURCE_ROOT / "hooks"))
    try:
        from brain_cli import main
        main(["--resource-root", str(RESOURCE_ROOT), *arguments, "hook"],
             started_at=STARTED_AT if started_at is None else started_at)
    except (Exception, SystemExit):
        print("[obsidian-brain] native lifecycle failed; capture was skipped; no source was retained.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
