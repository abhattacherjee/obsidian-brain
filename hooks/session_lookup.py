"""Read-only bounded lookup by full origin identity, with legacy adoption."""
import hashlib
import contextlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Optional

MAX_CANDIDATES = 9
MAX_FRONTMATTER_BYTES = 64 * 1024


class SessionLookupPending(ValueError):
    """Lookup cannot prove one legacy identity within its bounded work."""


def _deadline(deadline):
    if time.monotonic() >= deadline:
        raise SessionLookupPending('Session lookup deadline reached')


def _identity(path, vault, folder, deadline):
    _deadline(deadline)
    try:
        selected = path.resolve()
        selected.relative_to(vault)
        selected.relative_to(folder)
        if not stat.S_ISREG(selected.stat().st_mode):
            return None
        descriptor = os.open(selected, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except (OSError, ValueError):
        return None
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            return None
        try:
            current = selected.stat()
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                return None
            selected.resolve().relative_to(folder)
            selected.resolve().relative_to(vault)
        except (OSError, ValueError):
            return None
        opening = stream.readline(MAX_FRONTMATTER_BYTES)
        if len(opening) == MAX_FRONTMATTER_BYTES and not opening.endswith(b'\n'):
            raise SessionLookupPending('Session candidate frontmatter exceeds byte limit')
        if opening.rstrip(b'\r\n') != b'---':
            return None
        used = len(opening)
        fields = {}
        while used < MAX_FRONTMATTER_BYTES:
            _deadline(deadline)
            raw = stream.readline(MAX_FRONTMATTER_BYTES - used)
            if not raw:
                return None
            used += len(raw)
            if raw.rstrip(b'\r\n') == b'---':
                return fields
            try:
                line = raw.decode('utf-8').rstrip('\r\n')
            except UnicodeError:
                return None
            if ':' not in line or line.startswith((' ', '\t')):
                continue
            key, value = line.split(':', 1)
            if key not in {'agent_provider', 'agent_session_id', 'session_id', 'type'}:
                continue
            if key in fields:
                raise SessionLookupPending('Session candidate has duplicate identity fields')
            value = value.strip()
            try:
                decoded = json.loads(value)
            except ValueError:
                decoded = value.strip("'\"")
            if not isinstance(decoded, str):
                return None
            fields[key] = decoded
        raise SessionLookupPending('Session candidate frontmatter exceeds byte limit')



def _cantopen(error):
    code = getattr(error, 'sqlite_errorcode', None)
    if code is not None:
        return isinstance(code, int) and code & 255 == getattr(sqlite3, 'SQLITE_CANTOPEN', 14)
    return str(error).strip().lower() == 'unable to open database file'


def _index_identity(path):
    try:
        info = path.stat()
    except OSError as exc:
        raise SessionLookupPending('Existing session index became unavailable') from exc
    if not stat.S_ISREG(info.st_mode):
        raise SessionLookupPending('Existing session index is not a regular file')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _index_sidecars_absent(path):
    return not any(os.path.lexists(str(path) + suffix) for suffix in ('-wal', '-shm'))


@contextlib.contextmanager
def _readonly_index(path, deadline):
    """Retry only a closed WAL index that the native SQLite cannot open read-only."""
    path = path.resolve()
    connection = None
    immutable_identity = None
    try:
        try:
            connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0)  # noqa: vault-db-connect — selected existing read-only index
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            connection.execute('PRAGMA schema_version').fetchone()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
                connection = None
            if not _cantopen(exc) or not _index_sidecars_absent(path):
                raise
            _deadline(deadline)
            immutable_identity = _index_identity(path)
            connection = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=0)  # noqa: vault-db-connect — closed WAL fallback never creates sidecars
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        yield connection
        if immutable_identity is not None and (
                not _index_sidecars_absent(path) or _index_identity(path) != immutable_identity):
            raise SessionLookupPending('Existing session index changed during read-only lookup')
    finally:
        if connection is not None:
            connection.close()

def find_existing_session(context, deadline) -> Optional[Path]:
    """Return one proven full-origin session path, without changing the index.

    Missing indexes and bounded nonmatches return None. Deadline, candidate
    overflow, and multiple full-identity matches remain pending, never guesses.
    """
    _deadline(deadline)
    if context.host not in {'claude', 'codex'} or not context.native_session_id:
        return None
    index = Path(context.index_path)
    vault = Path(context.vault_path).resolve()
    name = context.config.get('sessions_folder') or 'claude-sessions'
    if not isinstance(name, str) or name in {'.', './'} or Path(name).is_absolute() or name.startswith('~') or any(
        part == '..' or part.startswith('.') for part in Path(name).parts
    ):
        raise SessionLookupPending('Invalid selected sessions folder')
    folder = (vault / name).resolve()
    try:
        folder.relative_to(vault)
    except ValueError:
        raise SessionLookupPending('Selected sessions folder escapes vault')
    # Capture registers its published note before an index can know it.
    from note_transactions import coordination_location
    registry = coordination_location(context) / 'state.sqlite3'
    if registry.is_file():
        connection = None
        try:
            connection = sqlite3.connect(registry.as_uri() + '?mode=ro', uri=True, timeout=0)  # noqa: vault-db-connect — selected read-only capture registry
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_sessions'").fetchone():
                row = connection.execute('SELECT descriptor,note FROM source_sessions WHERE scope=?',
                                         (context.session_key,)).fetchone()
                if row and row[1]:
                    descriptor = json.loads(row[0])
                    if descriptor.get('host') == context.host and descriptor.get('native_session_id') == context.native_session_id:
                        path = Path(row[1])
                        fields = _identity(path, vault, folder, deadline)
                        if (fields and fields.get('type') == 'claude-session'
                                and fields.get('agent_provider', 'claude') == context.host
                                and fields.get('agent_session_id', fields.get('session_id')) == context.native_session_id):
                            return path.resolve()
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise SessionLookupPending('Registered session lookup is pending: ' + type(exc).__name__) from exc
        finally:
            if connection is not None:
                connection.close()
    if not index.is_file():
        return None
    native_digest = hashlib.sha256((context.host + '\0' + context.native_session_id).encode()).hexdigest()
    suffixes = ['-' + context.host + '-' + native_digest[:length] + '.md' for length in (16, 24, 32, 64)]
    if context.host == 'claude':
        suffixes.append('-' + hashlib.sha256(context.native_session_id.encode()).hexdigest()[:4] + '.md')
    prefix = str(folder) + os.sep
    try:
        with _readonly_index(index, deadline) as connection:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(notes)')}
            rows = []
            if {'agent_provider', 'agent_session_id'} <= columns:
                rows = connection.execute(
                    "SELECT path FROM notes WHERE type='claude-session' AND path>=? AND path<? "
                    "AND agent_provider=? AND agent_session_id=? ORDER BY path LIMIT ?",
                    (prefix, prefix + '\U0010ffff', context.host, context.native_session_id, MAX_CANDIDATES),
                ).fetchall()
                if len(rows) >= MAX_CANDIDATES:
                    raise SessionLookupPending('Session candidate limit reached')
            suffix_query = ' OR '.join('path LIKE ?' for _ in suffixes)
            fallback = connection.execute(
                "SELECT path FROM notes WHERE type='claude-session' AND path>=? AND path<? AND (" +
                suffix_query + ") ORDER BY path LIMIT ?",
                (prefix, prefix + '\U0010ffff', *['%' + suffix for suffix in suffixes], MAX_CANDIDATES),
            ).fetchall()
            if len(fallback) >= MAX_CANDIDATES:
                raise SessionLookupPending('Session candidate limit reached')
            rows = list({row[0]: row for row in rows + fallback}.values())
    except sqlite3.Error as exc:
        raise SessionLookupPending('Existing session index lookup is pending: ' + type(exc).__name__) from exc
    _deadline(deadline)
    if len(rows) >= MAX_CANDIDATES:
        raise SessionLookupPending('Session candidate limit reached')
    matches = set()
    for (value,) in rows:
        if not isinstance(value, str) or not Path(value).is_absolute():
            continue
        path = Path(value)
        identity = _identity(path, vault, folder, deadline)
        if identity is None:
            continue
        provider = identity.get('agent_provider')
        if 'agent_provider' not in identity and identity.get('session_id'):
            provider = 'claude'
        if (identity.get('type') == 'claude-session'
                and provider == context.host
                and identity.get('agent_session_id', identity.get('session_id')) == context.native_session_id):
            matches.add(path.resolve())
    _deadline(deadline)
    if len(matches) > 1:
        raise SessionLookupPending('Session lookup found multiple full-identity matches')
    return next(iter(matches)) if matches else None
