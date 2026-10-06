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

# An import failure must still exit 0 and leave a hook-log line (#371).
try:
    from obsidian_utils import (  # noqa: E402
        _hook_payload_codex_reason,
        RETRO_GATE_TTL_SECONDS,
        clear_retro_classification_pending,
        get_retro_classification_pending,
    )
except Exception as _exc:  # noqa: BLE001
    import hook_bootstrap as _boot  # noqa: E402
    _boot.log_import_failure("Stop", _exc)
    sys.exit(0)

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


def native_decision(context, data):
    """Block once per verified host/session/turn, with a session fallback."""
    import hashlib
    from note_transactions import session_state_path
    session_id = context.native_session_id
    pending = get_retro_classification_pending(session_id)
    if pending is None:
        return None
    if data.get("stop_hook_active") is True:
        clear_retro_classification_pending(session_id)
        return None
    if time.time() - pending.get("created_at", 0) > RETRO_GATE_TTL_SECONDS:
        clear_retro_classification_pending(session_id)
        return None
    turn = data.get("turn_id")
    turn = turn if isinstance(turn, str) and turn else None
    pending_turn = pending.get("turn_id")
    if pending_turn and turn and pending_turn != turn:
        return None
    key = hashlib.sha256(json.dumps([
        context.host, session_id, turn or "session", pending.get("created_at"),
        pending.get("retro_path"),
    ], sort_keys=True).encode("utf-8")).hexdigest()
    directory = session_state_path(context) / "retro-decisions"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    try:
        descriptor = os.open(str(directory / (key + ".json")), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"host": context.host, "session_key": context.session_key,
                   "turn_key": hashlib.sha256((turn or "session").encode()).hexdigest()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return {"decision": "block", "reason": _BLOCK_REASON}


def main(context=None, payload=None) -> None:
    """Read stdin, check sentinel, block or pass through."""
    from runtime_context import current_runtime_context, using_runtime_context
    if context is not None:
        with using_runtime_context(context):
            return main(payload=payload)
    context = current_runtime_context()
    if context is not None:
        from native_lifecycle import emit_bound
        return emit_bound(context, "stop", payload)
    try:
        if payload is None:
            raw = sys.stdin.read(1_000_000)
            data = json.loads(raw)
        else:
            data = payload

        if not isinstance(data, dict):
            return
        codex_reason = _hook_payload_codex_reason(data) if context is None else None
        if codex_reason:
            print(f"[obsidian-brain] Stop outcome=SKIPPED_CODEX_HOST reason={codex_reason}", file=sys.stderr)
            return

        session_id = context.native_session_id if context else data.get("session_id") or ""
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
