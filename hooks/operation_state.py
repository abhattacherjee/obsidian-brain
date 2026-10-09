"""Scoped private operation artifacts with explicit identity and content checks."""
import hashlib
import contextlib
import fcntl
import time
import json
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path

_SCHEMA = 1
_NAME = re.compile(r'^[a-z][a-z0-9_-]*\.(json|md)$')
_ID = re.compile(r'^[0-9a-f]{32}$')


def _no_symlinks(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('Operation state must not contain symlinks')


def _scope(context):
    if context is None:
        raise ValueError('Native operation context unavailable')
    return {'schema': _SCHEMA, 'host': context.host, 'session': context.session_key,
            'vault': str(context.vault_path.resolve()),
            'project': str(context.canonical_project_root.resolve()),
            'model': context.config.get('codex_ai_model', context.config.get('codex_summary_model'))
                     if context.host == 'codex' else context.config.get('summary_model'),
            'backend': context.host}


def operation_directory(context, operation_id=None):
    _scope(context)
    _no_symlinks(context.state_path.absolute())
    from note_transactions import session_state_path
    operation_id = operation_id or uuid.uuid4().hex
    if not isinstance(operation_id, str) or not _ID.fullmatch(operation_id):
        raise ValueError('Invalid operation identity')
    selected_state = context.state_path.resolve()
    vault = context.vault_path.resolve()
    if selected_state == vault or vault in selected_state.parents:
        raise ValueError('Operation state must be outside the vault')
    root = session_state_path(context) / 'jobs'
    root.mkdir(mode=0o700, exist_ok=True)
    _no_symlinks(root)
    path = root / operation_id
    _no_symlinks(path)
    path.mkdir(mode=0o700, exist_ok=True)
    _no_symlinks(path)
    for directory in (root, path):
        if directory.stat().st_uid != os.getuid():
            raise ValueError('Operation state has another owner')
        directory.chmod(0o700)
    return operation_id, path


def _artifact(context, operation_id, name):
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError('Invalid operation artifact name')
    _, directory = operation_directory(context, operation_id)
    path = directory / name
    _no_symlinks(path)
    return path


def _write_private(path, content, context):
    root = context.state_path.resolve()
    vault = context.vault_path.resolve()
    if root == vault or vault in root.parents or root not in path.resolve().parents:
        raise ValueError('Private publication must stay outside the vault in selected state')
    _no_symlinks(path)
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix='.artifact-')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        _no_symlinks(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_private(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Operation artifact is not owner-only')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            data = stream.read(4_000_001)
        if len(data) > 4_000_000:
            raise ValueError('Operation artifact exceeds size limit')
        return data
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _manifest(context, operation_id, semantic=None):
    path = _artifact(context, operation_id, 'manifest.json')
    if path.exists():
        manifest = json.loads(_read_private(path))
        if manifest.get('scope') != _scope(context):
            raise ValueError('Operation scope changed')
        if semantic is not None and manifest.get('semantic') != semantic:
            raise ValueError('Operation inputs changed')
        return path, manifest
    return path, {'scope': _scope(context), 'semantic': semantic, 'files': {}}


@contextlib.contextmanager
def _operation_lock(context, operation_id):
    _, directory = operation_directory(context, operation_id)
    path = directory / '.operation.lock'
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    owned = False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Operation lock is not owner-only')
        deadline = time.monotonic() + 2.0
        while not owned:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                owned = True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Operation state ownership unavailable')
                time.sleep(0.01)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('Operation lock ownership changed')
        yield
    finally:
        if owned:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _store_artifact_locked(context, operation_id, name, content, *, semantic=None):
    """Write only inside the native session's UUID job directory."""
    if name == 'manifest.json':
        raise ValueError('Manifest is managed internally')
    path = _artifact(context, operation_id, name)
    manifest_path, manifest = _manifest(context, operation_id, semantic)
    content = content.encode('utf-8') if isinstance(content, str) else content
    if not isinstance(content, bytes) or len(content) > 4_000_000:
        raise ValueError('Invalid operation artifact content')
    _write_private(path, content, context)
    manifest['files'][name] = hashlib.sha256(content).hexdigest()
    _write_private(manifest_path, json.dumps(manifest, sort_keys=True).encode(), context)
    return path


def read_artifact(context, operation_id, name, *, semantic=None, supplied_path=None):
    path = _artifact(context, operation_id, name)
    if supplied_path is not None and Path(supplied_path).absolute() != path.absolute():
        raise ValueError('Artifact path does not match operation')
    _, manifest = _manifest(context, operation_id, semantic)
    content = _read_private(path)
    if manifest['files'].get(name) != hashlib.sha256(content).hexdigest():
        raise ValueError('Operation artifact content changed')
    return content


def operation_semantic(context, operation_id):
    """Return checked semantic identity, without loading artifact contents."""
    _, manifest = _manifest(context, operation_id)
    return manifest['semantic']


def store_artifact(context, operation_id, name, content, *, semantic=None):
    """Publish under persistent OS ownership; never take over a live holder."""
    with _operation_lock(context, operation_id):
        return _store_artifact_locked(context, operation_id, name, content, semantic=semantic)
