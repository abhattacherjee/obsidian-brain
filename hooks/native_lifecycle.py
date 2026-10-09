"""Native lifecycle integration; hooks capture facts without AI or index rebuilds."""
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from runtime_context import using_runtime_context

EVENTS = {"session_start", "resume", "stop", "pre_compact", "session_end", "recover"}
STDIN_CAP_CHARS = 1000000


def _warn(stage, error):
    print(f"[obsidian-brain] {stage} failed: {error}", file=sys.stderr)


def _context_summary(content):
    """Inject summary prose, or state that capture still needs a summary."""
    from obsidian_utils import owned_summary_source
    body = owned_summary_source(content)
    match = re.search(r"^## Summary[ \t]*\r?\n(.*?)(?=^#{1,2}[ \t]|\Z)",
                      body, re.MULTILINE | re.DOTALL)
    text = match.group(1).strip() if match else ""
    return text[:1000] if text else "Summary pending; session activity is retained."


def _context_hint(context, deadline):
    """Read at most one indexed session after checking existing vault identity."""
    if time.monotonic() >= deadline:
        return None
    from note_transactions import coordination_location
    coordination = coordination_location(context) / "state.sqlite3"
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
        project = context.project_name
        registered = None
        if identity.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_sessions'").fetchone():
            registered = identity.execute("SELECT note,first_date,descriptor FROM source_sessions "
                "WHERE note IS NOT NULL AND json_extract(descriptor,'$.canonical_project_root')=? "
                "ORDER BY first_date DESC,rowid DESC LIMIT 1", (str(context.canonical_project_root),)).fetchone()
        candidate = None
        if registered:
            from session_lookup import _identity
            descriptor = json.loads(registered[2])
            path = Path(registered[0]).resolve()
            fields = _identity(path, context.vault_path.resolve(), root, deadline)
            if (fields and fields.get('type') == 'claude-session'
                    and fields.get('agent_provider','claude') == descriptor.get('host')
                    and fields.get('agent_session_id',fields.get('session_id')) == descriptor.get('native_session_id')):
                with path.open() as stream:
                    candidate = (str(path),registered[1],stream.read(64*1024))
        if candidate:
            summary = _context_summary(candidate[2])
            return {"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":
                f"Obsidian context: Last session for {project} ({candidate[1]}): {summary}"}}
        if not context.index_path.is_file():
            return None
        from session_lookup import _readonly_index
        with _readonly_index(context.index_path, deadline) as connection:
            row = connection.execute(
                "SELECT path, date, substr(body, CASE WHEN instr(body, '<!-- obsidian-brain:summary:start -->') > 0 "
                "THEN instr(body, '<!-- obsidian-brain:summary:start -->') ELSE 1 END, 8192) FROM notes "
                "WHERE project=? AND type='claude-session' ORDER BY date DESC, rowid DESC LIMIT 1",
                (project,),
            ).fetchone()
        if not row or time.monotonic() >= deadline:
            return None
        Path(row[0]).resolve().relative_to(root)
        summary = _context_summary(row[2] or "")
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                f"Obsidian context: Last session for {project} ({row[1]}): {summary}"}}
    except (OSError, ValueError, sqlite3.Error):
        return None
    finally:
        for connection in reversed(connections):
            connection.close()


def _unsupported_hint(warnings, pending_sources):
    """Expose fixed parser classifications, never arbitrary diagnostic text."""
    labels = []
    for warning in warnings[:64]:
        if not isinstance(warning, str):
            continue
        label = None
        if warning in {"Transcript record exceeds the record limit",
                        "Ambiguous Codex user ownership retained",
                        "Deferred transcript rows remain pending",
                        "Deferred transcript source changed; input remains pending",
                        "Deferred transcript reference limit reached"}:
            label = warning
        elif warning in {"Oversized Claude transcript record retained", "Oversized Codex transcript record retained"}:
            label = warning
        elif warning in {"Unknown Codex payload retained", "Unknown or ambiguous Codex record retained"}:
            label = "Unknown substantive Codex transcript record"
        elif warning in {"Unknown substantive Claude transcript record", "Unknown substantive Claude content block"}:
            label = warning
        else:
            match = re.fullmatch(
                r"Unknown substantive (Claude|Codex) transcript record \(type=([a-z][a-z0-9_-]{0,47})\)", warning)
            if match and not match[2].startswith(("sk-", "sk_", "ghp_", "github_pat_", "xox", "eyj")):
                label = "Unknown substantive " + match[1] + " transcript record (type=" + match[2] + ")"
        if label and label not in labels and len(labels) < 4:
            labels.append(label)
    if not labels:
        return None
    notice = ("Obsidian capture is pending for at least " + str(pending_sources) + " source(s)"
              if pending_sources > 0 else "Obsidian capture retains unverified input for review")
    return (notice + ": " + "; ".join(labels)
            + ". Unresolved rows remain retained in the original source.")


def _pending_reason(warnings):
    fixed = {
        "Transcript batch limit reached", "Transcript deadline reached",
        "Transcript header exceeds the record limit", "Native transcript identity is unverified",
        "Selected Codex session metadata is pending",
        "Transcript deadline reached before reading", "Capture deadline reached",
        "Facts retained; publication deadline reached", "Session lookup deadline reached",
        "Session candidate limit reached", "Deferred transcript reference limit reached",
        "Incomplete final transcript line retained", "Malformed transcript row retained",
        "Oversized transcript bytes remain pending",
        "Opaque transcript drain limit reached; input remains pending",
        "Existing session index changed during read-only lookup",
        "Session lookup found multiple full-identity matches",
    }
    exception_names = ("OSError|FileNotFoundError|PermissionError|IsADirectoryError|"
                       "NotADirectoryError|Error|DatabaseError|OperationalError|"
                       "IntegrityError|ProgrammingError")
    selected = []
    for warning in warnings[:64]:
        if not isinstance(warning, str):
            continue
        if warning in fixed or re.fullmatch(
                r"(?:Registered session lookup|Existing session index lookup) is pending: (?:"
                + exception_names + r")", warning):
            if warning not in selected:
                selected.append(warning)
        if len(selected) == 4:
            break
    return "; ".join(selected)


def _report_capture_result(stage, result):
    if result.status == "pending":
        failure_types = {"OSError", "FileNotFoundError", "PermissionError", "IsADirectoryError",
                         "NotADirectoryError", "BlockingIOError", "Error", "DatabaseError",
                         "OperationalError", "IntegrityError", "ProgrammingError"}
        if (not result.applied_revision and len(result.warnings) == 1
                and result.warnings[0] in failure_types):
            _warn(stage, result.warnings[0])
            return
        status = "partial" if result.applied_revision else "pending"
        diagnostic = _unsupported_hint(result.warnings, result.pending_sources)
        print("[obsidian-brain] " + stage + " " + status + ": " +
              (diagnostic or _pending_reason(result.warnings) or "Source input remains pending."), file=sys.stderr)
    else:
        _warn(stage, "; ".join(result.warnings) or result.status)


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
    warnings = []
    pending_sources = 0
    outcome = "exception"
    with using_runtime_context(context):
        try:
            from capture import CaptureEvent, capture_checkpoint, recover_registered
            capture_enabled = context.config.get("auto_log_enabled", True) is not False
            if not capture_enabled:
                outcome = "skipped_auto_log_off"
            if capture_enabled and event in {"session_start", "resume", "recover"}:
                recovered = recover_registered(context, max_sources=8, deadline=deadline, include_active=True)
                # Recovery can process other projects in this vault. Its
                # scheduling result stays global; notices belong to this root.
                if getattr(recovered, "notice_warnings", None) is not None:
                    from dataclasses import replace
                    scoped_pending = recovered.notice_pending_sources
                    recovered = replace(recovered, warnings=recovered.notice_warnings,
                                        pending_sources=scoped_pending,
                                        status="pending" if scoped_pending else "complete",
                                        loss_of_input=bool(recovered.notice_loss_of_input))
                warnings.extend(recovered.warnings[:32])
                pending_sources = max(pending_sources, recovered.pending_sources)
                if recovered.status != "complete" or recovered.pending_sources or recovered.loss_of_input:
                    _report_capture_result("recovery", recovered)
            if capture_enabled and event != "recover":
                options = {"kind": event, "turn_id": turn_id}
                if event == "session_end" and payload.get("reason") == "clear":
                    options["trigger"] = "clear"
                if event == "pre_compact" and payload.get("trigger") in {"auto", "manual"}:
                    options["trigger"] = payload["trigger"]
                result = capture_checkpoint(context, CaptureEvent(**options), deadline)
                outcome = ("ok_raw_note_only" if result.applied_revision else "skipped_below_threshold") if result.status == "complete" else (
                    "write_failed" if result.status == "conflict" else "pending_capture")
                missing_start = (event in {"session_start", "resume"} and not result.applied_revision
                    and any(warning == "Native transcript unavailable: FileNotFoundError" for warning in result.warnings)
                    and "Native transcript identity is unverified" in result.warnings)
                if missing_start:
                    outcome = "skipped_source_not_created"
                else:
                    warnings.extend(result.warnings[:32])
                    pending_sources = max(pending_sources, result.pending_sources)
                    if result.status not in {"complete", "filtered"}:
                        _report_capture_result("capture", result)
        except Exception as exc:
            _warn("capture", exc)
        if event == "session_end":
            try:
                from obsidian_utils import _append_sessionend_log, slugify
                from capture import retained_statistics
                messages, duration = retained_statistics(context)
                _append_sessionend_log(slugify(context.canonical_project_root.name),
                                       context.native_session_id, outcome, messages, duration)
            except Exception as exc:
                _warn("telemetry", type(exc).__name__)
        if event == "stop":
            # A capture failure is not a policy decision. The gate stays fail-open.
            try:
                from obsidian_retro_gate import native_decision
                return native_decision(context, payload)
            except Exception as exc:
                _warn("retro gate", exc)
                return None
        if event in {"session_start", "resume"}:
            output = _context_hint(context, deadline)
            diagnostic = _unsupported_hint(warnings, pending_sources)
            if diagnostic:
                existing = output["hookSpecificOutput"]["additionalContext"] if output else ""
                return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                        "additionalContext": (existing + "\n" + diagnostic).strip()}}
            return output
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
