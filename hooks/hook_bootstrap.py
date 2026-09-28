"""Record a lifecycle hook that failed to import obsidian_utils (#371).

The four lifecycle hooks import ``obsidian_utils`` at module top level. When
that import fails (v3.6.0 raised ``TypeError`` on Python 3.9), the hook used to
exit 1 and leave no trace, because the hook-log writer lives in the module
that failed. Each hook now wraps that import and calls ``log_import_failure``.

Rules for this file: standard library only, no ``obsidian_utils`` import, and
no syntax newer than Python 3.9. It runs exactly when the rest of the plugin
cannot load, and a syntax error here cannot be caught. Keep it small.
"""

import datetime
import json
import os
import sys

# Must match obsidian_utils._HOOK_LOG_NAME / _HOOK_LOG_MAX_BYTES.
_HOOK_LOG_NAME = "obsidian-brain-hook.log"
_HOOK_LOG_MAX_BYTES = 100 * 1024
_DETAIL_MAX_CHARS = 300


def _field(value, default="unknown"):
    """One log token: no newlines, tabs or spaces."""
    s = str(value) if value else default
    for ch in ("\n", "\r", "\t", " "):
        s = s.replace(ch, "_")
    return s


def _payload():
    """The hook's JSON stdin as a dict, or {} on anything unexpected."""
    try:
        data = json.loads(sys.stdin.read(1_000_000) or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def failure_detail(exc):
    """``python=<version> <ExcType>: <message>``, on one line, capped."""
    text = f"python={sys.version.split()[0]} {type(exc).__name__}: {exc}"
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return text[:_DETAIL_MAX_CHARS]


def log_import_failure(event, exc):
    """Append one ``outcome=IMPORT_FAILED`` line to the hook log. Never raises.

    Same file, field order, 100 KB rotation and 0o600 mode as
    ``obsidian_utils._append_sessionend_log``, so
    ``awk '{print $5}'`` still yields the outcome. Returns the detail string.
    """
    detail = failure_detail(exc)
    try:
        payload = _payload()
        project = os.path.basename(str(payload.get("cwd") or os.getcwd()).rstrip("/"))
        sid = str(payload.get("session_id") or "unknown")[:8]
        log_dir = os.path.join(os.path.expanduser("~"), ".claude")
        os.makedirs(log_dir, mode=0o700, exist_ok=True)
        log_path = os.path.join(log_dir, _HOOK_LOG_NAME)
        try:
            if os.path.getsize(log_path) > _HOOK_LOG_MAX_BYTES:
                os.replace(log_path, log_path + ".1")
        except OSError:
            pass  # no log yet, or cannot rotate: still try to append
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        line = (
            f"{stamp} {_field(event)} project={_field(project)} sid={_field(sid)} "
            f"outcome=IMPORT_FAILED detail={detail}\n"
        )
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line)
        os.chmod(log_path, 0o600)
    except Exception as log_exc:
        try:
            print(f"[obsidian-brain] import failed ({detail}); log write failed: {log_exc}",
                  file=sys.stderr)
        except Exception:
            pass
    return detail


def session_start_notice(detail):
    """SessionStart stdout JSON telling the model the plugin did not load."""
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": (
            f"obsidian-brain failed to load ({detail}), so its hooks are off "
            "this session. Details: ~/.claude/obsidian-brain-hook.log "
            "(outcome=IMPORT_FAILED)."
        ),
    }})
