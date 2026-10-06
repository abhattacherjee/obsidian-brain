#!/usr/bin/env python3
"""Thin native entry; deadlines include shared runtime imports and resolution."""
import time
STARTED_AT = time.monotonic()

import sys
from pathlib import Path

RESOURCE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RESOURCE_ROOT / "hooks"))

# Native packaging supplies explicit --host, --client and --event arguments.
try:
    from brain_cli import main
    main(["--resource-root", str(RESOURCE_ROOT), *sys.argv[1:], "hook"], started_at=STARTED_AT)
except (Exception, SystemExit) as error:
    print(f"[obsidian-brain] native lifecycle failed: {error}", file=sys.stderr)
# Hook capture/config errors always fail open. Policy output remains native JSON.
