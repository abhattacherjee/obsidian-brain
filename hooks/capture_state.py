"""capture_state.py -- on-disk state for background auto-capture (#425).

Everything lives under ``<_SECURE_DIR>/capture/`` (0o700):

- ``<sid>.json``   read offset into the session's transcript, plus project
                   and the time of the last batch. Outlives SessionEnd, so the
                   startup sweep can find tails that were never processed.
- ``<sid>.claim``  O_EXCL claim: at most one worker per session at a time.
- ``<sid>.flush``  set by PreCompact: run the next batch now, not in 10 min.
- ``captures.jsonl`` the capture ledger the Captures pane reads. Append-only;
                   an update appends a superseding row with the same ``id``.
- ``installed_at`` first-use timestamp; the sweep never reaches back past it.
- ``PAUSED``      reason and timestamp for a pause of new capture batches.

Python stdlib only. Every file is written 0o600.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import secrets
import sys
import tempfile
import time
from pathlib import Path

import obsidian_utils

CLAIM_TTL_SECONDS = 900
PAUSE_TTL_SECONDS = 7200
LEDGER_NAME = "captures.jsonl"

_stale_pause_logged: tuple | None = None
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_STATE_DEFAULTS = {
    "offset": 0,
    "transcript_path": None,
    "project": None,
    "last_batch_at": 0.0,
    # Candidates parsed for the slice at ``offset`` but not all saved yet, so a
    # retry resumes them instead of asking the model again: {end, candidates, next}.
    "pending": None,
    # Transcript size when the sweep last found the session not eligible.
    "checked_size": -1,
}


def capture_dir() -> Path:
    """Return the capture state directory, creating it 0o700."""
    d = Path(obsidian_utils._SECURE_DIR) / "capture"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _sid_path(sid: str, suffix: str) -> Path:
    # Session ids become file names, so anything outside the UUID-ish
    # alphabet is refused rather than sanitised.
    if not isinstance(sid, str) or not _SID_RE.match(sid):
        raise ValueError(f"invalid session id: {sid!r}")
    return capture_dir() / f"{sid}{suffix}"


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def pause(reason: str) -> None:
    """Pause new capture batches for up to two hours; replace any previous pause."""
    global _stale_pause_logged
    _atomic_write(capture_dir() / "PAUSED", json.dumps({"reason": reason, "ts": time.time()}))
    _stale_pause_logged = None


def resume() -> None:
    """Remove only the pause flag, leaving session state, claims and the ledger intact."""
    global _stale_pause_logged
    try:
        (capture_dir() / "PAUSED").unlink()
    except FileNotFoundError:
        pass
    _stale_pause_logged = None


def is_paused() -> bool:
    """True while PAUSED's mtime is under the TTL; log each expired flag once per process."""
    global _stale_pause_logged
    path = capture_dir() / "PAUSED"
    try:
        st = path.stat()
    except FileNotFoundError:
        _stale_pause_logged = None
        return False
    age = time.time() - st.st_mtime
    if age < PAUSE_TTL_SECONDS:
        return True
    # Keep the stale flag for diagnostics. A replacement gets its own warning,
    # and repeated checks in launch/sweep/run_batch do not flood the hook log.
    identity = (path, st.st_dev, st.st_ino, st.st_mtime_ns)
    if _stale_pause_logged != identity:
        _stale_pause_logged = identity
        append_capture_log("unknown", None, "pause-expired", f"age={age:.0f}s")
    return False


def get_state(sid: str) -> dict:
    """Return the session's capture state; defaults when missing or unreadable."""
    state = dict(_STATE_DEFAULTS)
    path = _sid_path(sid, ".json")  # outside the try: a bad id must raise, not read as "new"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if isinstance(data, dict):
        state.update({k: data[k] for k in data if k in _STATE_DEFAULTS})
    return state


def set_state(sid: str, **fields) -> None:
    """Merge ``fields`` into the session's state and write it atomically."""
    unknown = set(fields) - set(_STATE_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown state fields: {sorted(unknown)}")
    state = get_state(sid)
    state.update(fields)
    _atomic_write(_sid_path(sid, ".json"), json.dumps(state))


def known_sessions() -> list[str]:
    """Session ids that have a state file."""
    return sorted(p.stem for p in capture_dir().glob("*.json") if _SID_RE.match(p.stem))


def claim(sid: str, ttl: int = CLAIM_TTL_SECONDS) -> str | None:
    """Take the session's worker claim; returns its token, or None when a live claim exists.

    The token goes to ``renew`` and ``release``, so a worker only ever touches
    its own claim, never one another worker took over after it went stale.
    """
    path = _sid_path(sid, ".claim")
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue  # released between our open and stat: try again
            if age <= ttl:
                return None
            # A worker that died without releasing: take the claim over.
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        token = f"{os.getpid()}-{secrets.token_hex(8)}"
        with os.fdopen(fd, "w") as fh:
            fh.write(token)
        return token
    return None


def _holds(sid: str, token: str) -> bool:
    try:
        return _sid_path(sid, ".claim").read_text().strip() == token
    except OSError:
        return False


def renew(sid: str, token: str) -> bool:
    """Push a held claim's expiry back by a full TTL. False when it is no longer ours."""
    if not _holds(sid, token):
        return False
    try:
        os.utime(_sid_path(sid, ".claim"))
    except OSError:
        return False
    return True


def claim_is_live(sid: str, ttl: int = CLAIM_TTL_SECONDS) -> bool:
    try:
        return time.time() - _sid_path(sid, ".claim").stat().st_mtime <= ttl
    except FileNotFoundError:
        return False


def active_claims() -> list[str]:
    """Sorted session ids with regular claim files still inside the existing claim TTL."""
    return sorted(path.stem for path in capture_dir().glob("*.claim")
                  if _SID_RE.fullmatch(path.stem) and not path.is_symlink()
                  and path.is_file() and claim_is_live(path.stem))


def release(sid: str, token: str) -> None:
    """Drop the claim, but only while it is still the one ``token`` names."""
    if not _holds(sid, token):
        return
    try:
        _sid_path(sid, ".claim").unlink()
    except FileNotFoundError:
        pass


def set_flush(sid: str) -> None:
    """Ask for the next batch to run now (set by PreCompact)."""
    _atomic_write(_sid_path(sid, ".flush"), str(time.time()))


def has_flush(sid: str) -> bool:
    """True while a flush is asked for; ``pop_flush`` clears it once a batch starts."""
    return _sid_path(sid, ".flush").exists()


def pop_flush(sid: str) -> bool:
    """True once after ``set_flush``; consumes the flag."""
    try:
        _sid_path(sid, ".flush").unlink()
        return True
    except FileNotFoundError:
        return False


def _ledger_path() -> Path:
    return capture_dir() / LEDGER_NAME


def ledger_append(row: dict) -> str:
    """Append one ledger row under an exclusive lock; return its ``id``."""
    row = dict(row)
    row.setdefault("id", secrets.token_hex(6))
    row.setdefault("ts", time.time())
    line = json.dumps(row, ensure_ascii=False) + "\n"
    path = _ledger_path()
    fd = os.open(str(path), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return row["id"]


def _merged_rows() -> list[dict]:
    """Ledger rows merged by id (later rows win), in order of first appearance."""
    merged: dict[str, dict] = {}
    try:
        text = _ledger_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    for raw in text.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        if row["id"] in merged:
            first_ts = merged[row["id"]]["ts"]
            merged[row["id"]].update(row)
            merged[row["id"]]["ts"] = first_ts
        else:
            merged[row["id"]] = row
    return list(merged.values())


def ledger_list(session: str | None = None, limit: int = 200, since: float | None = None) -> list[dict]:
    """Merged ledger rows, newest first, optionally for one session or from ``since`` (epoch s)."""
    rows = [r for r in _merged_rows()
            if (session is None or r.get("session_id") == session)
            and (since is None or float(r.get("ts", 0) or 0) >= since)]
    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return rows[:limit]


def ledger_get(capture_id: str) -> dict | None:
    for row in _merged_rows():
        if row["id"] == capture_id:
            return row
    return None


def notes_created_by(session_id: str) -> set[str]:
    """Vault-relative paths of every note ``session_id`` created through auto-capture (any status)."""
    return {r["note_path"] for r in _merged_rows()
            if r.get("kind") == "new" and r.get("session_id") == session_id and isinstance(r.get("note_path"), str)}


def ledger_update(capture_id: str, **fields) -> dict:
    """Append a superseding row for ``capture_id``; return the merged row."""
    row = ledger_get(capture_id)
    if row is None:
        raise KeyError(capture_id)
    new = {**row, **fields, "id": capture_id}
    ledger_append(new)
    return new


def installed_at() -> float:
    """First-use time of auto-capture; written once, then read back."""
    path = capture_dir() / "installed_at"
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            return float(path.read_text().strip())
        except (OSError, ValueError):
            return 0.0
    now = time.time()
    with os.fdopen(fd, "w") as fh:
        fh.write(str(now))
    return now


def _log_dir() -> Path:
    """Where the shared hook log lives; tests point it at a tmp dir."""
    return Path.home() / ".claude"


def append_capture_log(project: str, sid: str | None, outcome: str, detail: str = "") -> None:
    """One ``Capture`` line in the shared hook log (same file and rotation as SessionEnd)."""
    try:
        log_dir = _log_dir()
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_path = log_dir / obsidian_utils._HOOK_LOG_NAME
        try:
            if log_path.exists() and log_path.stat().st_size >= obsidian_utils._HOOK_LOG_MAX_BYTES:
                os.replace(log_path, log_dir / (obsidian_utils._HOOK_LOG_NAME + ".1"))
        except OSError:
            pass
        iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        clean = obsidian_utils._sanitize_log_field
        parts = [
            iso,
            "Capture",
            f"project={clean(project or '', 'unknown').replace(' ', '_')}",
            f"sid={(sid or 'none')[:8]}",
            f"outcome={clean(outcome, 'unknown').replace(' ', '_')}",
            f"detail={clean(detail).replace(' ', '_')}",
        ]
        fd = os.open(str(log_path), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(" ".join(parts) + "\n")
    except Exception as exc:  # noqa: BLE001 -- telemetry never breaks capture
        print(f"[obsidian-brain] capture log write failed: {exc}", file=sys.stderr)
