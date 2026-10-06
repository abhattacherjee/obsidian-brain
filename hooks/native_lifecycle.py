"""Native lifecycle integration; hooks capture facts without AI or index rebuilds."""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from runtime_context import using_runtime_context

EVENTS = {"session_start", "resume", "stop", "pre_compact", "session_end", "recover"}
STDIN_CAP_CHARS = 1000000


def _warn(stage, error):
    print(f"[obsidian-brain] {stage} failed: {error}", file=sys.stderr)


def _context_hint(context, deadline):
    """Read at most one indexed session after checking existing vault identity."""
    if time.monotonic() >= deadline or not context.index_path.is_file():
        return None
    coordination = context.index_path.parent / ("." + context.index_path.name + ".coordination") / "state.sqlite3"
    if not coordination.is_file():
        return None
    connections = []
    try:
        identity = sqlite3.connect(coordination.as_uri() + "?mode=ro", uri=True, timeout=0)  # noqa: vault-db-connect — bounded read-only identity check
        connections.append(identity)
        identity.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        row = identity.execute("SELECT vault FROM identity LIMIT 1").fetchone()
        if not row or row[0] != str(context.vault_path.resolve()):
            return None
        from obsidian_utils import indexed_folders
        folders = indexed_folders(dict(context.config), strict=True)
        sessions = str(context.config.get("sessions_folder", "claude-sessions"))
        if sessions not in folders:
            return None
        root = (context.vault_path / sessions).resolve()
        root.relative_to(context.vault_path.resolve())
        connection = sqlite3.connect(context.index_path.as_uri() + "?mode=ro", uri=True, timeout=0)  # noqa: vault-db-connect — bounded read-only selected index
        connections.append(connection)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        project = context.canonical_project_root.name.lower().replace(" ", "-").replace("_", "-")
        row = connection.execute(
            "SELECT path, date, substr(body,1,8192) FROM notes "
            "WHERE project=? AND type='claude-session' ORDER BY date DESC, rowid DESC LIMIT 1",
            (project,),
        ).fetchone()
        if not row or time.monotonic() >= deadline:
            return None
        Path(row[0]).resolve().relative_to(root)
        body = row[2] or ""
        summary = body.split("## Summary", 1)[-1].split("\n## ", 1)[0].strip()[:1000]
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                f"Obsidian context: Last session for {project} ({row[1]}): {summary}"}}
    except (OSError, ValueError, sqlite3.Error):
        return None
    finally:
        for connection in reversed(connections):
            connection.close()


def dispatch(context, event, payload, started_at):
    """Use the wrapper-entry time for every recovery, capture and lookup budget."""
    if os.environ.get("OBSIDIAN_BRAIN_NESTED_AI") == "1":
        return None
    if event not in EVENTS:
        raise ValueError("Unknown lifecycle event")
    payload = payload if isinstance(payload, dict) else {}
    if event == "session_start" and payload.get("source") == "resume":
        event = "resume"
    budget = 1.0 if event in {"session_start", "resume", "recover"} else 2.5
    deadline = started_at + budget
    turn_id = payload.get("turn_id")
    turn_id = turn_id if isinstance(turn_id, str) and turn_id else None
    with using_runtime_context(context):
        try:
            from capture import CaptureEvent, capture_checkpoint, recover_registered
            if event in {"session_start", "resume", "recover"}:
                recovered = recover_registered(context, max_sources=8, deadline=deadline, include_active=True)
                if recovered.status != "complete" or recovered.pending_sources or recovered.loss_of_input:
                    _warn("recovery", "; ".join(recovered.warnings) or
                          f"status={recovered.status}, pending={recovered.pending_sources}, loss_of_input={recovered.loss_of_input}")
            if event != "recover":
                options = {"kind": event, "turn_id": turn_id}
                if event == "session_end" and payload.get("reason") == "clear":
                    options["trigger"] = "clear"
                if event == "pre_compact" and payload.get("trigger") in {"auto", "manual"}:
                    options["trigger"] = payload["trigger"]
                result = capture_checkpoint(context, CaptureEvent(**options), deadline)
                if result.status not in {"complete", "filtered"}:
                    _warn("capture", "; ".join(result.warnings) or result.status)
        except Exception as exc:
            _warn("capture", exc)
        if event == "stop":
            # A capture failure is not a policy decision. The gate stays fail-open.
            try:
                from obsidian_retro_gate import native_decision
                return native_decision(context, payload)
            except Exception as exc:
                _warn("retro gate", exc)
                return None
        if event in {"session_start", "resume"}:
            return _context_hint(context, deadline)
    return None


def emit(context, event, payload, started_at):
    output = dispatch(context, event, payload, started_at)
    if output is not None:
        print(json.dumps(output))
    return output


def emit_bound(context, event, payload=None):
    """Existing bound hook APIs keep their bounded stdin contract."""
    started = time.monotonic()
    if payload is None:
        try:
            from brain_cli import read_payload
            payload = read_payload(sys.stdin.read(STDIN_CAP_CHARS + 1))
        except Exception as exc:
            _warn("native input", exc)
            payload = {}
    return emit(context, event, payload, started)
