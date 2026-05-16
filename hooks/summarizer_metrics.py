"""Append-only JSONL telemetry sink for upgrade_batch() (issue #74).

One JSON object per upgrade_batch() invocation. Owner-only (0o600). Rotates at
100 KB to <name>.jsonl.1, overwriting any prior rotation. Never raises —
instrumentation must not be able to break /recall.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

METRICS_PATH: Path = Path.home() / ".claude" / "obsidian-brain-summarizer-metrics.jsonl"
ROTATE_BYTES: int = 100 * 1024  # 100 KB


def append_metrics_record(record: dict) -> None:
    """Append one record as a JSON line. Rotates at ROTATE_BYTES.
    Swallows all errors after a single stderr warning per call."""
    try:
        parent = METRICS_PATH.parent
        parent.mkdir(mode=0o700, exist_ok=True)

        # Rotate first if the existing file is over the threshold. Check BEFORE
        # append so the post-rotation file contains only this call's line.
        if METRICS_PATH.exists() and METRICS_PATH.stat().st_size > ROTATE_BYTES:
            rotated = METRICS_PATH.with_suffix(".jsonl.1")
            os.replace(METRICS_PATH, rotated)  # atomic; overwrites any prior

        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        new_file = not METRICS_PATH.exists()
        with open(METRICS_PATH, "a", encoding="utf-8") as f:
            f.write(line)
        if new_file:
            os.chmod(METRICS_PATH, 0o600)
    except Exception as exc:  # noqa: BLE001 — instrumentation never raises
        print(f"[obsidian-brain] summarizer_metrics append failed: {exc}", file=sys.stderr)
