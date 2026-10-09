"""Private cache and calendar markers scoped to the selected runtime session."""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import time


def directory(context, name):
    if name not in {"cache", "calendar", "locks", "retro-gate", "logs", "doctor-backups"}:
        raise ValueError("Unknown session state directory")
    from operation_state import _no_symlinks
    from note_transactions import session_state_path
    root = context.state_path.absolute()
    _no_symlinks(root)
    if root.resolve() == context.vault_path.resolve() or context.vault_path.resolve() in root.resolve().parents:
        raise ValueError("Session state must stay outside the vault")
    scoped = session_state_path(context) / name
    _no_symlinks(scoped)
    scoped.mkdir(mode=0o700, exist_ok=True)
    if scoped.stat().st_uid != os.getuid():
        raise ValueError("Session state belongs to another user")
    scoped.chmod(0o700)
    return scoped


def cross_run_directory(context, name="cache"):
    """Reuse cache state for this vault, provider and project across sessions."""
    if name != "cache":
        raise ValueError("Unknown cross-run state directory")
    from note_transactions import coordination_path, _digest, _private_dir
    from operation_state import _no_symlinks
    root = coordination_path(context) / "auxiliary" / context.host / _digest(str(context.canonical_project_root)) / name
    _no_symlinks(root)
    return _private_dir(root)


def cross_run_write(context, name, value):
    if name not in {"deep-acted-items.json", "check-items-classifications.json"}:
        raise ValueError("Unknown cross-run cache file")
    from dataclasses import replace
    from note_transactions import coordination_path
    from operation_state import _write_private
    path = cross_run_directory(context) / name
    _write_private(path, json.dumps(value).encode(),
                   replace(context, state_path=coordination_path(context)))


def _read(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError("Session state is not a private file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("Session state must be an object")
        return value
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _private_path(context, path):
    from operation_state import _no_symlinks
    path = Path(path).absolute()
    names = {"cache": {"values.json", "write.lock"},
             "calendar": {"first-seen.json", "write.lock"}}
    kind = path.parent.name
    if kind not in names or path.name not in names[kind]:
        raise ValueError("Unknown private session state file")
    _no_symlinks(path)
    root = directory(context, kind)
    if path.resolve() != root / path.name:
        raise ValueError("Session state file belongs to another directory")
    return path


def _write(context, path, value):
    path = _private_path(context, path)
    from operation_state import _no_symlinks
    _no_symlinks(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o600)
        _no_symlinks(path)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


@contextlib.contextmanager
def _locked(context, path):
    path = _private_path(context, path)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("Session state lock is not private")
        os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + 0.1
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)
        yield
    finally:
        os.close(descriptor)


def cache_get(context, key):
    try:
        return _read(directory(context, "cache") / "values.json").get(key)
    except (OSError, ValueError):
        return None


def cache_update(context, key=None, value=None, *, remove=(), clear=False):
    root = directory(context, "cache")
    path = root / "values.json"
    with _locked(context, root / "write.lock"):
        try:
            document = _read(path)
        except (FileNotFoundError, json.JSONDecodeError):
            document = {}
        if clear:
            document = {}
        for item in remove:
            document.pop(item, None)
        if key is not None:
            document[key] = value
        _write(context, path, document)


def first_seen_date(context):
    root = directory(context, "calendar")
    path = root / "first-seen.json"
    with _locked(context, root / "write.lock"):
        try:
            value = _read(path)["first_seen_date"]
            return datetime.date.fromisoformat(value).isoformat()
        except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
            pass
        today = datetime.date.today().isoformat()
        _write(context, path, {"first_seen_date": today})
        return today
