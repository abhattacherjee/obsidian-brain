#!/usr/bin/env python3
"""
obsidian_retro_gate.py -- Stop hook enforcing /retro Step 7.5 classification.

When the /retro skill writes a vault note it calls
mark_retro_classification_pending(); this hook fires on every Stop event
and blocks Claude from finishing if a pending sentinel exists for the current
session — i.e., a retro was written but Step 7.5 (extract & classify Process
Improvements / Key Learnings) has not yet been completed.

Contract:
  - Reads a JSON object from stdin (capped at 1 MB).
  - Extracts session_id (str) and stop_hook_active (bool, default False).
  - If no pending sentinel → exits 0 silently (normal stop).
  - If stop_hook_active → clears sentinel + exits 0 (prevents infinite loop).
  - If sentinel is stale (> RETRO_GATE_TTL_SECONDS) → clears + exits 0.
  - Otherwise → prints {"decision": "block", "reason": "..."} to stdout and
    exits 0 to re-enter the model with the classification reminder.

Always exits 0 (fail-open). Never wedges Claude.
"""

from __future__ import annotations

import json
import os
import sys
import time

# ---------------------------------------------------------------------------
# Import shared utilities (same sys.path bootstrap as other hooks)
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from obsidian_utils import (  # noqa: E402
    RETRO_GATE_TTL_SECONDS,
    clear_retro_classification_pending,
    get_retro_classification_pending,
)

_BLOCK_REASON = (
    "You wrote a retro this session but Step 7.5 classification is not yet complete. "
    "Before stopping: re-read the '## Process Improvements' and '## Key Learnings' "
    "sections of the retro you just saved, then extract and classify every item — "
    "(a) file a GitHub issue for concrete deliverables, "
    "(b) write a memory entry for behavioral discipline items, "
    "(c) mark 'skip / already covered' with a citation for items already tracked. "
    "After filing all items (or confirming there are none), the skill calls "
    "clear_retro_classification_pending() to clear this gate. "
    "Do NOT print the 'Retrospective saved!' confirmation until classification is done."
)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Read stdin, check sentinel, block or pass through."""
    try:
        raw = sys.stdin.read(1_000_000)
        data = json.loads(raw)

        session_id = data.get("session_id") or ""
        stop_hook_active = bool(data.get("stop_hook_active", False))

        if not session_id:
            return  # nothing to gate on

        pending = get_retro_classification_pending(session_id)
        if pending is None:
            return  # no retro pending for this session

        if stop_hook_active:
            # Claude is already in a Stop-hook re-entry — clear and let it stop
            # to avoid an infinite block loop.
            clear_retro_classification_pending(session_id)
            return

        created_at = pending.get("created_at", 0)
        if time.time() - created_at > RETRO_GATE_TTL_SECONDS:
            # Sentinel is stale — don't hold the session hostage indefinitely.
            clear_retro_classification_pending(session_id)
            return

        # Block: sentinel is fresh and we're not already in a re-entry.
        print(json.dumps({"decision": "block", "reason": _BLOCK_REASON}))

    except Exception:
        # Fail-open: any unexpected error must not wedge Claude.
        return


if __name__ == "__main__":
    main()
