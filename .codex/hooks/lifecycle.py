#!/usr/bin/env python3
"""Compatibility entry using the shared adjacent-resource native wrapper."""
import time
STARTED_AT = time.monotonic()

import sys
from pathlib import Path

RESOURCE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RESOURCE_ROOT / "hooks"))
try:
    from native_entry import run
    run(started_at=STARTED_AT)
except (Exception, SystemExit):
    print("[obsidian-brain] native lifecycle failed; capture is deferred.", file=sys.stderr)
