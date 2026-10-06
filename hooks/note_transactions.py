"""Coordinate vault publications independently of the replaceable search index."""

import contextlib
import fcntl
import hashlib
import json
import math
from contextvars import ContextVar
import os
import re
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional, Sequence, Tuple

from runtime_context import RuntimeContext, current_runtime_context


@dataclass(frozen=True)
class NoteMutation:
    path: Path
    expected_revision: Optional[str]
    managed_changes: Mapping[str, str]
    operation_id: str
    file_mode: Optional[int] = None


@dataclass(frozen=True)
class WriteResult:
    status: str
    revision: Optional[str] = None
    pending_path: Optional[Path] = None
    warnings: Tuple[str, ...] = ()


class LockBusy(RuntimeError):
    pass


_HELD = threading.local()
_REGION = re.compile(
    r"<!-- obsidian-brain:(capture|summary):start -->\n(.*?)"
    r"<!-- obsidian-brain:\1:end -->", re.DOTALL,
)
_METADATA_KEYS = frozenset({
    "type", "date", "project", "tags", "session_id", "source_session", "source_session_note",
    "agent_provider", "agent_session_id", "capture_state", "capture_completeness",
    "capture_revision", "summary_revision", "parent_session", "source_revision", "trigger",
    "status", "author_host", "operation_id", "project_path", "git_branch",
    "duration_minutes", "created_at",
})


def _frontmatter(document):
    if not (document or "").startswith("---\n"):
        return None
    end = document.find("\n---\n", 4)
    if end < 0:
        raise ValueError("The note has incomplete frontmatter")
    return document[4:end], end + 5


def _metadata_changes(changes):
    if "metadata" not in changes:
        return {}
    values = json.loads(changes["metadata"])
    if not isinstance(values, dict) or not set(values).issubset(_METADATA_KEYS):
        raise ValueError("Unknown managed metadata field")
    if "duration_minutes" in values:
        duration = values["duration_minutes"]
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or duration < 0):
            raise ValueError("Managed duration must be a finite nonnegative number")
    if any(not isinstance(value, (str, list)) for key, value in values.items()
           if key != "duration_minutes"):
        raise ValueError("Managed metadata must contain strings or string lists")
    if any(isinstance(value, list) and any(not isinstance(item, str) for item in value)
           for value in values.values()):
        raise ValueError("Managed metadata lists must contain strings")
    return values


def _metadata_lines(document):
    frontmatter = _frontmatter(document)
    result = {}
    if frontmatter:
        lines = frontmatter[0].splitlines()
        for index, line in enumerate(lines):
            match = re.match(r"^([a-z_]+):(?:[ \t]*(.*))$", line)
            if match and match[1] in _METADATA_KEYS:
                if match[1] in result:
                    raise ValueError("The note has duplicate managed metadata")
                value = line
                if match[1] == "tags" and not match[2]:
                    following = index + 1
                    while following < len(lines) and re.match(r"^[ \t]*- ", lines[following]):
                        value += "\n" + lines[following]
                        following += 1
                result[match[1]] = value
    return result


def _fault(point):
    """Fault-injection seam used by crash tests; production has no triggers."""


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _same_payload(prior, current):
    if prior == current:
        return True
    # Earlier journals omitted the original ID. The hash already bound it.
    old = json.loads(prior)
    new = json.loads(current)
    if isinstance(old, dict) and "operation" not in old and isinstance(new, dict):
        new = dict(new)
        new.pop("operation", None)
        return old == new
    return False


def _intent_digest(payload):
    value = json.loads(payload)
    if isinstance(value, dict):
        value.pop("operation", None)
    return _digest(json.dumps(value, sort_keys=True))


def _private_dir(path):
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("Private state paths cannot contain symbolic links")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Private state must be a directory")
    if info.st_uid != os.getuid():
        raise OSError("Operational state belongs to another user")
    path.chmod(0o700)
    return path


def session_state_path(context):
    """Versioned state includes vault, provider, native ID, and project root."""
    path = context.state_path / "v1"
    parts = (_digest(str(context.vault_path)), context.host, context.session_key,
             _digest(str(context.canonical_project_root)))
    _private_dir(path)
    for part in parts:
        path = _private_dir(path / part)
    return path


def _physical_vault(context):
    path = context.vault_path.resolve()
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("The selected vault must be a directory")
    return path, str(info.st_dev) + ":" + str(info.st_ino)


def coordination_location(context):
    """Use frozen account state and the physical vault, independent of HOME."""
    root = context.coordination_root
    if root is None or not root.is_absolute():
        raise ValueError("The coordination root must be frozen before writing")
    vault, identity = _physical_vault(context)
    path = root / "obsidian-brain" / "vaults" / _digest(identity)
    from runtime_context import path_is_within_physical_directory
    if path_is_within_physical_directory(path, vault):
        raise ValueError("Coordination state must be outside the vault")
    return path


def coordination_path(context):
    path = coordination_location(context)
    root = context.coordination_root
    if root.resolve() != root or root.is_symlink():
        raise ValueError("The frozen coordination root changed")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = root.stat()
    if details.st_uid != os.getuid() or not stat.S_ISDIR(details.st_mode) or stat.S_IMODE(details.st_mode) & 0o022:
        raise ValueError('The coordination root must be owned and not writable by others')
    if root.parent == Path('/var/tmp').resolve() and root.name == 'obsidian-brain-state-' + str(os.getuid()):
        root.chmod(0o700)
    for part in path.relative_to(root).parts:
        root = root / part
        if root.is_symlink():
            raise ValueError("Coordination paths cannot contain symbolic links")
        _private_dir(root)
    return path


_migration_seconds = ContextVar("coordination_migration_seconds", default=0.5)


@contextlib.contextmanager
def coordination_migration_budget(seconds=30):
    """An explicit admin operation may migrate beyond the hook's short budget."""
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 300:
        raise ValueError("Migration needs a finite positive bounded duration")
    token = _migration_seconds.set(seconds)
    try:
        yield
    finally:
        _migration_seconds.reset(token)


def _migrate_coordination(context, destination):
    """Import each selected old namespace, including the old path-based key."""
    candidates = [context.index_path.parent / ("." + context.index_path.name + ".coordination"),
        context.coordination_root / 'obsidian-brain' / 'vaults' / _digest(str(context.vault_path.resolve()))]
    for old in dict.fromkeys(candidates):
        if old != destination.parent:
            _merge_prior_journal(context, destination, old)


def _merge_prior_journal(context, destination, old):
    """Merge a verified journal once; preserve both inputs on collision."""
    source = old / "state.sqlite3"
    if not source.exists():
        return
    if old.resolve() != old or old.is_symlink() or source.is_symlink() or not source.is_file():
        raise ValueError("Prior coordination state must be a regular private journal")
    details = source.stat()
    if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o600:
        raise ValueError("Prior coordination state must be private and owned")
    stamp = [details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns, details.st_ctime_ns]
    wal = source.with_name(source.name + '-wal')
    if wal.exists():
        if wal.is_symlink() or not wal.is_file() or wal.stat().st_uid != os.getuid():
            raise ValueError("Prior coordination WAL is not owned regular state")
        info = wal.stat()
        stamp += [info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    migration = _digest(str(source) + json.dumps(stamp))
    if destination.exists():
        with contextlib.closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True, timeout=0)) as existing:  # noqa: vault-db-connect — private coordination migration receipt
            if existing.execute("SELECT 1 FROM sqlite_master WHERE name='journal_migrations'").fetchone():
                if existing.execute("SELECT 1 FROM journal_migrations WHERE id=?", (migration,)).fetchone():
                    return
    lock = old / (_digest("vault") + ".lock")
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusy("A prior writer still owns the vault") from exc
        with contextlib.closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=0)) as prior:  # noqa: vault-db-connect — verified private prior coordination journal
            if not coordination_identity_matches(context,prior):
                return
            fd_new, temporary = tempfile.mkstemp(prefix=".coord-", dir=str(destination.parent))
            os.close(fd_new)
            limit = time.monotonic() + _migration_seconds.get()
            def within_budget(*unused):
                if time.monotonic() >= limit:
                    raise LockBusy("Prior journal migration remains pending; use an explicit admin migration budget")
            try:
                with contextlib.closing(sqlite3.connect(temporary, timeout=0)) as snapshot:  # noqa: vault-db-connect — private migration snapshot
                    prior.backup(snapshot, pages=128, progress=within_budget)
                    if not destination.exists():
                        snapshot.execute("CREATE TABLE IF NOT EXISTS journal_migrations(id TEXT PRIMARY KEY)")
                        snapshot.execute("INSERT OR IGNORE INTO journal_migrations VALUES (?)", (migration,))
                        snapshot.commit()
                    else:
                        with contextlib.closing(sqlite3.connect(destination, timeout=0)) as target:  # noqa: vault-db-connect — same-vault private coordination merge
                            target.execute("BEGIN IMMEDIATE")
                            try:
                                for name, sql in snapshot.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                                    if name in {'identity', 'journal_migrations'}:
                                        continue
                                    quoted = '"' + name.replace('"', '""') + '"'
                                    columns = snapshot.execute("PRAGMA table_info(" + quoted + ")").fetchall()
                                    if not target.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone():
                                        target.execute(sql)
                                    target_columns = target.execute("PRAGMA table_info(" + quoted + ")").fetchall()
                                    if columns != target_columns:
                                        raise ValueError("Prior coordination schema conflicts with the current journal")
                                    keys = [index for index, column in enumerate(columns) if column[5]] or list(range(len(columns)))
                                    where = ' AND '.join('"' + columns[index][1].replace('"', '""') + '" IS ?' for index in keys)
                                    for row in snapshot.execute("SELECT * FROM " + quoted):
                                        within_budget()
                                        found = target.execute("SELECT * FROM " + quoted + " WHERE " + where,
                                                               tuple(row[index] for index in keys)).fetchone()
                                        if found is not None and found != row:
                                            raise ValueError("Prior coordination rows conflict; both journals were preserved")
                                        if found is None:
                                            target.execute("INSERT INTO " + quoted + " VALUES (" + ','.join('?' for _ in row) + ")", row)
                                target.execute("CREATE TABLE IF NOT EXISTS journal_migrations(id TEXT PRIMARY KEY)")
                                target.execute("INSERT INTO journal_migrations VALUES (?)", (migration,))
                                target.commit()
                            except BaseException:
                                target.rollback()
                                raise
                if not destination.exists():
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, destination)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def context_for_vault(vault_path):
    """Legacy Claude APIs enter the same writer; native callers supply context."""
    vault = Path(vault_path).resolve()
    context = current_runtime_context()
    if context is not None:
        if context.vault_path.resolve() != vault:
            raise ValueError("A writer cannot switch the selected runtime vault")
        return context
    import obsidian_utils
    import vault_index
    project = Path.cwd().resolve()
    from runtime_adapters.claude import legacy_writer_context
    return legacy_writer_context(vault, project, Path(__file__).resolve().parent.parent,
                                 Path(vault_index._default_db_path()).resolve(),
                                 Path(obsidian_utils._SECURE_DIR))


@contextlib.contextmanager
def ownership_lock(context, key="vault", deadline=None):
    """Physical directory flock serializes every actor of the same vault."""
    vault, identity = _physical_vault(context)
    vault_key = "physical-vault:" + identity
    held = getattr(_HELD, "locks", {})
    _HELD.locks = held
    lock_key = vault_key if key == "vault" else vault_key + ":" + _digest(key)
    if lock_key in held:
        yield held[lock_key][1]
        return
    if key != "vault" and vault_key not in held:
        raise ValueError("Acquire vault ownership before session ownership")
    deadline = time.monotonic() + 0.1 if deadline is None else deadline
    if key == "vault":
        fd = os.open(str(vault), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(fd)
        if str(opened.st_dev) + ":" + str(opened.st_ino) != identity:
            os.close(fd)
            raise ValueError("The selected vault changed during locking")
    else:
        path = coordination_path(context) / (_digest(key) + ".lock")
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    token = uuid.uuid4().hex
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockBusy("Another writer owns the vault")
                time.sleep(min(0.005, max(0, deadline - time.monotonic())))
        held[lock_key] = (fd, token)
        try:
            yield token
        finally:
            held.pop(lock_key, None)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def connect_coordination(context):
    """Caller must hold vault ownership. Rebuilds never remove this database."""
    path = coordination_path(context) / "state.sqlite3"
    _migrate_coordination(context, path)
    # Coordination survives search-index replacement and has its own schema.
    connection = sqlite3.connect(str(path), timeout=0.1)  # noqa: vault-db-connect
    os.chmod(path, 0o600)
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS identity (vault TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS physical_identity (vault_id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS revisions (
            path TEXT, revision TEXT, regions TEXT,
            PRIMARY KEY(path, revision));
        CREATE TABLE IF NOT EXISTS operations (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, phase TEXT NOT NULL,
            document TEXT, revision TEXT);
        CREATE TABLE IF NOT EXISTS cancelled_operations (id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS operation_receipts (
            id TEXT PRIMARY KEY, payload_digest TEXT NOT NULL, revision TEXT);
    """)
    identity = connection.execute("SELECT vault FROM identity").fetchone()
    physical = connection.execute('SELECT vault_id FROM physical_identity').fetchone()
    vault = str(context.vault_path.resolve())
    actual = _physical_vault(context)[1]
    if physical:
        if physical != (actual,):
            connection.close()
            raise ValueError('The selected coordination database belongs to another physical vault')
    elif identity and not coordination_vault_matches(context,identity[0]):
        connection.close()
        raise ValueError('The selected coordination database belongs to another vault')
    if not identity:
        connection.execute('INSERT INTO identity VALUES (?)',(vault,))
    if not physical:
        connection.execute('INSERT INTO physical_identity VALUES (?)',(actual,))
    connection.commit()
    return connection



def coordination_vault_matches(context, stored_path):
    """Compare existing vault directories by inode rather than path spelling."""
    try:
        return os.path.samefile(stored_path, context.vault_path)
    except (OSError,TypeError,ValueError):
        return False


def coordination_identity_matches(context, connection):
    """Read-only identity validation for doctor and registered recovery."""
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='physical_identity'").fetchone():
        physical = connection.execute('SELECT vault_id FROM physical_identity').fetchone()
        if physical:
            return physical == (_physical_vault(context)[1],)
    stored = connection.execute('SELECT vault FROM identity').fetchone()
    return bool(stored) and coordination_vault_matches(context,stored[0])


def _contained(context, path):
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(context.vault_path.resolve())
    except ValueError as exc:
        raise ValueError("Note path is outside the selected vault") from exc
    if resolved == context.vault_path.resolve():
        raise ValueError("A vault root cannot be a note")
    return resolved


def _read_bytes(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _read(path):
    raw = _read_bytes(path)
    return raw.decode("utf-8") if raw is not None else None


def _regions(document):
    result = {match.group(1): _digest(match.group(2))
              for match in _REGION.finditer(document or "")}
    result.update({"metadata:" + key: _digest(line)
                   for key, line in _metadata_lines(document).items()})
    return result


def _record_read(connection, path, document):
    revision = _digest(document) if document is not None else None
    if revision is not None:
        try:
            regions = _regions(document)
        except ValueError:
            # A repair still needs the exact source hash when its metadata is
            # malformed. No region-level permission can be inferred from it.
            regions = {}
        connection.execute("INSERT OR IGNORE INTO revisions VALUES (?, ?, ?)",
                           (str(path), revision, json.dumps(regions)))
    return revision


def read_revision(context, path):
    with ownership_lock(context):
        with contextlib.closing(connect_coordination(context)) as connection:
            path = _contained(context, path)
            revision = _record_read(connection, path, _read(path))
            connection.commit()
            return revision


def record_read(context, path, document):
    """Record the exact bytes a caller read, before it starts drafting."""
    with ownership_lock(context):
        with contextlib.closing(connect_coordination(context)) as connection:
            revision = _record_read(connection, _contained(context, path), document)
            connection.commit()
            return revision


def record_raw_read(context, path, raw):
    """Bind an explicit corruption repair to the exact bytes read before repair."""
    if not isinstance(raw, bytes):
        raise ValueError("A raw source revision needs bytes")
    with ownership_lock(context):
        with contextlib.closing(connect_coordination(context)) as connection:
            path = _contained(context, path)
            revision = hashlib.sha256(raw).hexdigest()
            try:
                regions = _regions(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                regions = {}
            connection.execute("INSERT OR IGNORE INTO revisions VALUES (?, ?, ?)",
                               (str(path), revision, json.dumps(regions)))
            connection.commit()
            return revision


def _render(document, changes):
    if "repair_document" in changes:
        if len(changes) != 1:
            raise ValueError("A corruption repair cannot also change regions")
        return changes["repair_document"]
    if "document" in changes:
        if len(changes) != 1:
            raise ValueError("A document replacement cannot also change regions")
        return changes["document"]
    result = document or ""
    metadata = _metadata_changes(changes)
    if metadata:
        existing_fields = _metadata_lines(result)
        frontmatter = _frontmatter(result)
        header = frontmatter[0] if frontmatter else ""
        def scalar(value, bare=False):
            # Bare ordinary text remains compatible with legacy grep readers.
            if bare and isinstance(value, str) and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_/-]*", value) and value.lower() not in {"true", "false", "null", "yes", "no", "on", "off"}:
                return value
            return json.dumps(value, ensure_ascii=False)
        for key, value in metadata.items():
            rendered = (key + ":\n" + "\n".join("- " + scalar(item, True) for item in value)
                        if key == "tags" and isinstance(value, list) and value else
                        key + ": " + scalar(value, key == "status"))
            if key in existing_fields:
                header = re.sub(r"(?m)^" + re.escape(existing_fields[key]) + r"(?=\n|$)",
                                lambda match: rendered, header, count=1)
            else:
                header += ("\n" if header else "") + rendered
        lines = header.splitlines()
        remainder = result[frontmatter[1]:] if frontmatter else result
        result = "---\n" + "\n".join(lines) + "\n---\n" + remainder
    existing = list(_REGION.finditer(result))
    if result.count("<!-- obsidian-brain:") != 2 * len(existing):
        raise ValueError("The note has incomplete or unknown managed regions")
    if len({match.group(1) for match in existing}) != len(existing):
        raise ValueError("The note has duplicate managed regions")
    for region, content in changes.items():
        if region == "metadata":
            continue
        if region not in {"capture", "summary"}:
            raise ValueError("Unknown managed region")
        if "<!-- obsidian-brain:" in content:
            raise ValueError("Managed content cannot introduce ownership markers")
        block = ("<!-- obsidian-brain:" + region + ":start -->\n" + content +
                 ("" if content.endswith("\n") else "\n") +
                 "<!-- obsidian-brain:" + region + ":end -->")
        matches = [match for match in _REGION.finditer(result) if match.group(1) == region]
        if len(matches) > 1:
            raise ValueError("The note has duplicate managed regions")
        if matches:
            match = matches[0]
            result = result[:match.start()] + block + result[match.end():]
        else:
            heading = "Capture continuation" if region == "capture" else "Summary"
            prefix = "\n\n" if region == "summary" and content.lstrip().startswith("## Summary") else "\n\n## " + heading + "\n\n"
            result += prefix + block + "\n"
    return result


def _atomic_write(path, document, file_mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = file_mode if file_mode is not None else (
        stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600)
    fd, temporary = tempfile.mkstemp(prefix=".ob-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


_recovery_pending_intent = ContextVar("recovery_pending_intent", default=None)


def _normalized_pending_item(item):
    value = dict(item)
    if "action" not in value:
        value.setdefault("mode", None)
    return json.dumps(value, sort_keys=True)


MAX_PENDING_BYTES = 4 * 1024 * 1024
MAX_PENDING_DIRECTORIES = 4096


def _pending_payload_bound(payload):
    if len(payload.encode('utf-8')) > MAX_PENDING_BYTES:
        raise ValueError("Mutation is too large for the recoverable 4 MiB UTF-8 intent limit")


def _session_descriptor(context, directory):
    fields = ('vault_path', 'canonical_project_root', 'worktree', 'state_path',
              'native_home', 'user_home', 'coordination_root')
    return {'version': 1, 'uid': os.getuid(), 'physical_vault': _physical_vault(context)[1],
        'directory': str(directory), 'host': context.host, 'client': context.client,
        'native_session_id': context.native_session_id,
        **{key: str(getattr(context, key)) if getattr(context, key) is not None else None for key in fields}}



def _read_private_state(path, limit):
    fd = os.open(path, os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_NONBLOCK',0))
    with os.fdopen(fd,'rb') as stream:
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o600:
            raise ValueError('Private state must be an owned regular file')
        value = stream.read(limit+1)
    if len(value) > limit:
        raise ValueError('Private state exceeds its size bound')
    return value


def _register_pending_directory(context, directory):
    registry = _private_dir(coordination_path(context) / 'pending-directories')
    target = registry / (_digest(str(directory)) + '.json')
    value = json.dumps(_session_descriptor(context, directory), sort_keys=True)
    if len(value.encode()) > 16384:
        raise ValueError('Pending directory identity exceeds its size bound')
    if not target.exists():
        _atomic_write(target, value)
    else:
        previous = json.loads(_read_private_state(target,16384))
        current = json.loads(value)
        keys = ('version','uid','physical_vault','directory','host','native_session_id',
                'vault_path','canonical_project_root','state_path','coordination_root')
        if not isinstance(previous,dict) or any(previous.get(key) != current[key] for key in keys):
            raise ValueError('Pending directory registration conflicts with its identity')


def _registered_pending_directories(context, deadline, limit=MAX_PENDING_DIRECTORIES):
    """Read only owned registered actor directories, never discover native homes."""
    from dataclasses import replace
    registry = coordination_location(context) / 'pending-directories'
    if not registry.exists():
        return [], False
    if registry.resolve() != registry or registry.stat().st_uid != os.getuid():
        raise ValueError('Pending directory registry is not private owned state')
    rows = []
    truncated = False
    with os.scandir(registry) as entries:
        for entry in entries:
            if not entry.name.endswith('.json'):
                continue
            if len(rows) >= limit or time.monotonic() >= deadline:
                truncated = True
                break
            fd = os.open(entry.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(fd, 'rb') as stream:
                details = os.fstat(stream.fileno())
                if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o600:
                    raise ValueError('Pending registration must be an owned private regular file')
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError('Pending registration exceeds its size bound')
            value = json.loads(raw)
            fields = {'version','uid','physical_vault','directory','host','client','native_session_id',
                      'vault_path','canonical_project_root','worktree','state_path','native_home','user_home','coordination_root'}
            if (not isinstance(value, dict) or set(value) != fields or type(value['version']) is not int
                    or value['version'] != 1 or type(value['uid']) is not int or value['uid'] != os.getuid()
                    or value['physical_vault'] != _physical_vault(context)[1]
                    or value['host'] not in {'claude','codex'}
                    or value['client'] not in ({'claude-code','cli'} if value['host']=='claude' else {'codex-cli','codex-desktop','cli'})
                    or not isinstance(value['native_session_id'],str)):
                raise ValueError('Pending registration identity is invalid')
            changes = {}
            for key in ('vault_path','canonical_project_root','worktree','state_path','native_home','user_home','coordination_root'):
                item = value[key]
                if item is None and key == 'native_home':
                    changes[key] = None
                    continue
                if not isinstance(item,str) or not Path(item).is_absolute() or Path(item).resolve() != Path(item):
                    raise ValueError('Pending actor paths must be frozen and contained')
                changes[key] = Path(item)
            actor = replace(context, host=value['host'], client=value['client'], native_session_id=value['native_session_id'], **changes)
            directory = Path(value['directory'])
            expected = actor.state_path / 'v1' / _digest(str(actor.vault_path)) / actor.host / actor.session_key / _digest(str(actor.canonical_project_root)) / 'pending'
            if (directory != expected or directory.resolve() != directory
                    or _physical_vault(actor)[1] != _physical_vault(context)[1]
                    or actor.coordination_root != context.coordination_root
                    or entry.name != _digest(str(directory))+'.json'):
                raise ValueError('Pending directory escaped its registered actor')
            if directory.exists() and (not directory.is_dir() or directory.stat().st_uid != os.getuid()):
                raise ValueError('Pending directory is not owned private state')
            rows.append((entry.name, actor, directory))
    return sorted(rows, key=lambda row: row[0]), truncated


def _pending_intent_reference(path, deadline):
    """Fingerprint an unchanged private intent without exposing its content."""
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('Pending intent path contains a symbolic link')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        details = os.fstat(stream.fileno())
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                or stat.S_IMODE(details.st_mode) != 0o600
                or details.st_size > MAX_PENDING_BYTES):
            raise ValueError('Pending intent is not bounded private state')
        digest, size = hashlib.sha256(), 0
        while True:
            if time.monotonic() >= deadline:
                raise ValueError('Pending intent fingerprint deadline reached')
            raw = stream.read(min(1024 * 1024, MAX_PENDING_BYTES + 1 - size))
            if not raw:
                break
            size += len(raw)
            if size > MAX_PENDING_BYTES:
                raise ValueError('Pending intent exceeds its fingerprint bound')
            digest.update(raw)
        now = path.lstat()
        if any(parent.is_symlink() for parent in path.parents):
            raise ValueError('Pending intent path changed during fingerprinting')
        fields = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                value.st_mtime_ns, value.st_ctime_ns)
        if path.is_symlink() or fields(now) != fields(details) or size != details.st_size:
            raise ValueError('Pending intent changed during fingerprinting')
    return {'path': str(path), 'sha256': digest.hexdigest()}


def pending_mutation_inventory(context, limit=4096, deadline=None):
    """Inspect every bounded registered actor without replay or note changes."""
    deadline = time.monotonic()+1 if deadline is None else deadline
    if type(limit) is not int or not 0 < limit <= MAX_PENDING_DIRECTORIES:
        raise ValueError('Pending inventory requires a bounded positive limit')
    try:
        rows, truncated = _registered_pending_directories(context, deadline, limit)
    except (OSError,ValueError,TypeError,KeyError,RecursionError):
        return {'pending_mutations':0,'bounded':True,'selected_pending_registered':False,
                'status':'pending','warnings':['Pending registrations could not be verified']}
    count = 0
    references, warnings, references_bounded = [], [], False
    for _, actor, directory in rows:
        if not directory.exists():
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                if time.monotonic() >= deadline or count >= limit:
                    truncated = True
                    break
                if entry.name.endswith('.json'):
                    count += 1
                    if len(references) >= 32:
                        references_bounded = True
                        continue
                    try:
                        references.append(_pending_intent_reference(Path(entry.path), deadline))
                    except (OSError, ValueError):
                        references_bounded = True
                        if not warnings:
                            warnings.append('A pending intent could not be fingerprinted safely; it was preserved')
    if count:
        warnings.append('Unresolved mutation intents require an explicit operator choice; destination notes were preserved')
    selected = context.state_path / 'v1' / _digest(str(context.vault_path)) / context.host / context.session_key / _digest(str(context.canonical_project_root)) / 'pending'
    return {'pending_mutations': count, 'bounded': truncated,
            'pending_intents': references,
            'pending_intent_references_bounded': references_bounded,
            'warnings': warnings,
            'selected_pending_registered': any(directory == selected for _, _, directory in rows),
            'status': 'pending' if count or truncated else 'complete'}


def discard_pending_mutation(context, pending_path, expected_sha256):
    """Explicitly acknowledge an exact intent; never edit its destination note."""
    if not isinstance(expected_sha256,str) or not re.fullmatch('[0-9a-f]{64}',expected_sha256):
        return WriteResult('conflict', warnings=('An exact pending-intent SHA256 acknowledgment is required',))
    try:
        path = Path(pending_path)
        rows, truncated = _registered_pending_directories(context, time.monotonic()+1)
        if not any(path.parent == directory for _, _, directory in rows) or re.fullmatch(r'[0-9a-f]{64}\.json',path.name) is None:
            raise ValueError('Pending intent is outside registered same-vault state')
        with ownership_lock(context):
            fd = os.open(path, os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_NONBLOCK',0))
            with os.fdopen(fd,'rb') as stream:
                details = os.fstat(stream.fileno())
                raw = stream.read(MAX_PENDING_BYTES+1)
            if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                    or stat.S_IMODE(details.st_mode) != 0o600 or len(raw)>MAX_PENDING_BYTES
                    or hashlib.sha256(raw).hexdigest() != expected_sha256):
                raise ValueError('Pending intent changed before acknowledgment')
            _fault('before_pending_discard')
            now = path.lstat()
            if (now.st_dev,now.st_ino,now.st_size,now.st_mtime_ns) != (details.st_dev,details.st_ino,details.st_size,details.st_mtime_ns):
                raise ValueError('Pending intent changed before acknowledgment')
            if path.is_symlink() or path.read_bytes() != raw:
                raise ValueError('Pending intent changed before acknowledgment')
            actor = next(actor for _,actor,directory in rows if path.parent == directory)
            value = json.loads(raw)
            items = value if isinstance(value,list) else [value]
            with contextlib.closing(connect_coordination(context)) as connection:
                for item in items:
                    operation = item['operation']
                    if not isinstance(operation,str) or not operation:
                        raise ValueError('Pending acknowledgment needs original operation identities')
                    key = _digest(actor.session_key + "\0" + str(actor.canonical_project_root) + "\0" + operation)
                    connection.execute('INSERT OR IGNORE INTO cancelled_operations VALUES (?)',(key,))
                connection.commit()
            path.unlink()
        return WriteResult('unchanged')
    except (OSError,ValueError,TypeError,KeyError,RecursionError,LockBusy) as exc:
        return WriteResult('conflict', warnings=(str(exc),))


def _pending(context, operation_id, payload):
    _pending_payload_bound(payload)
    recovery = _recovery_pending_intent.get()
    if recovery is not None and recovery[0] == context:
        try:
            value = json.loads(payload)
            items = value if isinstance(value, list) else [value]
            if all(_normalized_pending_item(item) in recovery[3] for item in items):
                fd = os.open(recovery[1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0))
                with os.fdopen(fd, "rb") as stream:
                    details = os.fstat(stream.fileno())
                    if (stat.S_ISREG(details.st_mode) and details.st_uid == os.getuid()
                            and stat.S_IMODE(details.st_mode) == 0o600
                            and stream.read(4 * 1024 * 1024 + 1) == recovery[2]):
                        # The original batch still retains this exact intent.
                        return recovery[1]
        except (OSError, ValueError, TypeError):
            pass
    directory = _private_dir(session_state_path(context) / "pending")
    _register_pending_directory(context, directory)
    path = directory / (_digest(operation_id) + ".json")
    _atomic_write(path, payload)
    return path


def _clear_pending(context, operation_id):
    recovery = _recovery_pending_intent.get()
    if recovery is not None and recovery[0] == context:
        # Recovery acknowledges the original file only after its whole batch.
        return
    path = session_state_path(context) / "pending" / (_digest(operation_id) + ".json")
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _recover_pending_directory(context, directory, max_operations=8, deadline=None):
    """Replay bounded, private intents with their original CAS and operation IDs."""
    if isinstance(max_operations, bool) or not isinstance(max_operations, int) or max_operations < 1:
        raise ValueError("Recovery needs a positive operation bound")
    deadline = time.monotonic() + 1 if deadline is None else deadline
    if not directory.exists():
        return WriteResult("unchanged"), 0
    completed = 0
    attempted = 0
    warnings = []
    remaining = False
    cursor_path = directory / ".recovery-cursor"
    after = None
    try:
        fd = os.open(cursor_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            details = os.fstat(stream.fileno())
            if (stat.S_ISREG(details.st_mode) and details.st_uid == os.getuid()
                    and stat.S_IMODE(details.st_mode) == 0o600):
                cursor = json.loads(stream.read(1024))
                candidate = cursor.get("after")
                if (isinstance(candidate, str) and len(candidate) == 69
                        and candidate.endswith(".json")
                        and all(char in "0123456789abcdef" for char in candidate[:-5])):
                    after = candidate
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    # Resume after a retained conflict so a full batch of conflicts cannot
    # repeatedly hide later intents. A missing cursor restarts on the next pass.
    reached_cursor = after is None
    last_attempted = None
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            if time.monotonic() >= deadline:
                remaining = True
                break
            if not reached_cursor:
                if entry.name == after:
                    reached_cursor = True
                continue
            if completed >= max_operations or time.monotonic() >= deadline:
                remaining = True
                break
            last_attempted = entry.name
            try:
                fd = os.open(entry.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                with os.fdopen(fd, "rb") as stream:
                    details = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                            or stat.S_IMODE(details.st_mode) != 0o600):
                        raise ValueError("Pending intents must be private owned regular files")
                    raw = stream.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError("Pending intent exceeds the recovery size bound")
                value = json.loads(raw)
                items = value if isinstance(value, list) else [value]
                results = []
                token = _recovery_pending_intent.set((
                    context, Path(entry.path), raw,
                    frozenset(_normalized_pending_item(item) for item in items)))
                try:
                    for item in items:
                        if attempted >= max_operations or time.monotonic() >= deadline:
                            results.append(WriteResult("pending", warnings=("Pending batch recovery is incomplete",)))
                            remaining = True
                            break
                        attempted += 1
                        operation = item["operation"]
                        if not isinstance(operation, str) or not operation:
                            raise ValueError("Pending intent has no original operation ID")
                        if item.get("action") == "move":
                            result = move_note(context, item["source"], item["destination"], item["expected"], operation)
                        elif item.get("action") == "delete":
                            result = delete_note(context, item["path"], item["expected"], operation)
                        else:
                            result = apply_mutations(context, [NoteMutation(Path(item["path"]), item["expected"], item["changes"], operation, item.get("mode"))])
                        results.append(result)
                        if result.status not in {"applied", "unchanged"}:
                            break
                finally:
                    _recovery_pending_intent.reset(token)
                if results and all(result.status in {"applied", "unchanged"} for result in results):
                    path = Path(entry.path)
                    # A new deferral owns a different inode even if its bytes
                    # happen to match. Never acknowledge that concurrent intent.
                    _fault('before_pending_recovery_unlink')
                    try:
                        now = path.lstat()
                        same_owner = (stat.S_ISREG(now.st_mode) and now.st_uid == os.getuid()
                            and stat.S_IMODE(now.st_mode) == 0o600
                            and (now.st_dev,now.st_ino) == (details.st_dev,details.st_ino))
                        if same_owner and path.read_bytes() == raw:
                            path.unlink()
                        else:
                            remaining = True
                            warnings.append('Pending intent changed during recovery; new intent was preserved')
                    except FileNotFoundError:
                        pass
                else:
                    remaining = True
                    warnings.append("A pending mutation still conflicts with its source revision")
            except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
                remaining = True
                warnings.append("Pending mutation could not be replayed safely: " + type(exc).__name__)
            completed += 1
    if last_attempted is not None:
        try:
            _atomic_write(cursor_path, json.dumps({"after": last_attempted}))
        except OSError:
            remaining = True
            warnings.append("Pending recovery position could not be saved")
        if after is not None:
            remaining = remaining or any(path.name.endswith(".json") for path in directory.iterdir())
    elif after is not None:
        # The cursor was removed or reached the directory end. Wrap fairly.
        with contextlib.suppress(FileNotFoundError):
            cursor_path.unlink()
        remaining = any(path.name.endswith(".json") for path in directory.iterdir())
    return WriteResult("pending" if remaining else "unchanged", warnings=tuple(warnings[:8])), max(completed, attempted)


def recover_pending_mutations(context, max_operations=8, deadline=None):
    """Recover registered same-vault actors fairly with original operation IDs."""
    if type(max_operations) is not int or max_operations < 1:
        raise ValueError('Recovery needs a positive operation bound')
    deadline = time.monotonic()+1 if deadline is None else deadline
    # Register the selected old directory too, without scanning native homes.
    selected = context.state_path / 'v1' / _digest(str(context.vault_path)) / context.host / context.session_key / _digest(str(context.canonical_project_root)) / 'pending'
    if selected.exists():
        _register_pending_directory(context, selected)
    try:
        rows, truncated = _registered_pending_directories(context, deadline)
    except (OSError,ValueError,TypeError,KeyError,RecursionError):
        return WriteResult('pending',warnings=('Pending registrations could not be verified',))
    cursor = coordination_path(context) / 'pending-recovery-cursor.json'
    after = None
    if cursor.exists() and not cursor.is_symlink():
        try:
            value = json.loads(_read_private_state(cursor,1024))
            if isinstance(value.get('after'),str):
                after = value['after']
        except (OSError,ValueError,TypeError,AttributeError):
            pass
    if after:
        rows = [row for row in rows if row[0] > after]+[row for row in rows if row[0] <= after]
    used = 0
    warnings = []
    pending = truncated
    last = None
    for name, actor, directory in rows:
        if used >= max_operations or time.monotonic() >= deadline:
            pending = True
            break
        last = name
        result, count = _recover_pending_directory(actor,directory,max_operations-used,deadline)
        used += count
        pending = pending or result.status == 'pending'
        warnings.extend(result.warnings)
    if last:
        _atomic_write(cursor,json.dumps({'after':last}))
    return WriteResult('pending' if pending else 'unchanged',warnings=tuple(warnings[:8]))


def _apply_one(context, connection, mutation):
    path = _contained(context, mutation.path)
    if not mutation.operation_id or not mutation.managed_changes:
        raise ValueError("A mutation needs content and an operation ID")
    if mutation.file_mode not in {None, 0o600, 0o644}:
        raise ValueError("Unsupported note permissions")
    payload = json.dumps({"path": str(path), "expected": mutation.expected_revision,
                          "changes": dict(mutation.managed_changes),
                          "mode": mutation.file_mode, "operation": mutation.operation_id}, sort_keys=True, ensure_ascii=False)
    _pending_payload_bound(payload)
    key = _digest(context.session_key + "\0" + str(context.canonical_project_root) +
                  "\0" + mutation.operation_id)
    if connection.execute('SELECT 1 FROM cancelled_operations WHERE id=?',(key,)).fetchone():
        return WriteResult('conflict',warnings=('This operation intent was explicitly discarded',))
    prior = connection.execute("SELECT payload, phase, document, revision FROM operations WHERE id=?",
                               (key,)).fetchone()
    receipt = connection.execute("SELECT payload_digest, revision FROM operation_receipts WHERE id=?",
                                 (key,)).fetchone()
    if receipt:
        if receipt[0] != _intent_digest(payload):
            return WriteResult("conflict", warnings=("Operation ID has different content",))
        return WriteResult("unchanged", read_revision(context, path))
    current_bytes = _read_bytes(path)
    repair = "repair_document" in mutation.managed_changes
    try:
        current = current_bytes.decode("utf-8") if current_bytes is not None else None
    except UnicodeDecodeError:
        if not repair:
            return WriteResult("conflict", warnings=("The note is not valid UTF-8; explicit repair is required",))
        current = current_bytes.decode("utf-8", errors="replace")
    revision = hashlib.sha256(current_bytes).hexdigest() if current_bytes is not None else None
    if prior:
        if not _same_payload(prior[0], payload):
            return WriteResult("conflict", revision, warnings=("Operation ID has different content",))
        if prior[1] == "applied":
            _clear_pending(context, key)
            return WriteResult("unchanged", revision)
        if prior[3] == revision and revision is not None:
            if mutation.file_mode is not None:
                os.chmod(path, mutation.file_mode)
            connection.execute("UPDATE operations SET phase='applied' WHERE id=?", (key,))
            _record_read(connection, path, current)
            connection.commit()
            return WriteResult("unchanged", revision)
    managed = "document" not in mutation.managed_changes and not repair
    checks = set(mutation.managed_changes)
    if managed:
        if "metadata" in checks:
            checks.remove("metadata")
            checks.update("metadata:" + field for field in _metadata_changes(mutation.managed_changes))
        if "summary" in checks:
            checks.add("capture")
        # Replacement may have succeeded before its acknowledgement failed.
        # Preserve later user edits when the owned output is already present.
        if prior and current is not None:
            published = _regions(prior[2])
            now = _regions(current)
            if all(published.get(region) == now.get(region) for region in checks):
                if mutation.file_mode is not None:
                    os.chmod(path, mutation.file_mode)
                connection.execute("UPDATE operations SET phase='applied', document=?, revision=? WHERE id=?",
                                   (current, revision, key))
                _record_read(connection, path, current)
                connection.commit()
                return WriteResult("unchanged", revision)
    expected = mutation.expected_revision
    valid = revision == expected
    if not valid and managed and expected is not None:
        baseline = connection.execute("SELECT regions FROM revisions WHERE path=? AND revision=?",
                                      (str(path), expected)).fetchone()
        if baseline:
            before = json.loads(baseline[0])
            now = _regions(current)
            valid = current is not None and all(before.get(region) == now.get(region) for region in checks)
    if not valid:
        return WriteResult("conflict", revision, _pending(context, key, payload),
                           ("The source revision changed; the original note was preserved",))
    # Region CAS permits unowned edits. Render from the bytes just checked,
    # never from the whole document retained by an earlier failed attempt.
    rendered = _render(current, mutation.managed_changes)
    next_revision = _digest(rendered)
    if not prior:
        connection.execute("INSERT INTO operations VALUES (?, ?, 'prepared', ?, ?)",
                           (key, payload, rendered, next_revision))
    else:
        connection.execute("UPDATE operations SET document=?, revision=? WHERE id=?",
                           (rendered, next_revision, key))
    connection.commit()
    _pending(context, key, payload)
    _fault("after_prepare")
    changed = current_bytes != rendered.encode("utf-8")
    if changed:
        _fault("before_replace")
        if _contained(context, mutation.path) != path:
            raise ValueError("Note containment changed before publication")
        # Recheck user edits immediately before replacement as well as ownership.
        if _read_bytes(path) != current_bytes:
            return WriteResult("conflict", revision, _pending(context, key, payload),
                               ("The note changed during publication",))
        _atomic_write(path, rendered, mutation.file_mode)
        _fault("after_replace")
    elif mutation.file_mode is not None:
        os.chmod(path, mutation.file_mode)
    connection.execute("UPDATE operations SET phase='applied' WHERE id=?", (key,))
    _record_read(connection, path, rendered)
    connection.commit()
    _clear_pending(context, key)
    return WriteResult("applied" if changed else "unchanged", next_revision)


def compact_capture_operations(context, *, note_path, retain_operation_id):
    """Drop acknowledged capture bodies; keep replay digests and all pending work."""
    with ownership_lock(context):
        with contextlib.closing(connect_coordination(context)) as connection:
            path = str(_contained(context, note_path))
            retained = _digest(context.session_key + "\0" + str(context.canonical_project_root)
                               + "\0" + retain_operation_id)
            rows = connection.execute("SELECT id, payload, revision FROM operations WHERE phase='applied' AND id!=?",
                                      (retained,)).fetchall()
            for key, payload, revision in rows:
                value = json.loads(payload)
                if (value.get("path") != path or not isinstance(value.get("changes"), dict)
                        or "capture" not in value["changes"]):
                    continue
                connection.execute("INSERT OR IGNORE INTO operation_receipts VALUES (?, ?, ?)",
                                   (key, _intent_digest(payload), revision))
                connection.execute("DELETE FROM operations WHERE id=?", (key,))
            connection.commit()


def move_note(context, source, destination, expected_revision, operation_id,
              expected_dst_revision=None) -> WriteResult:
    """Move without overwriting a destination; retain enough intent to replay.

    A hard link publishes the destination without replacing another file. The
    source is removed only while both names still refer to the verified bytes.
    A crash between those steps leaves both names available for recovery.
    """
    payload = json.dumps({"action": "move", "source": str(source), "destination": str(destination),
                          "expected": expected_revision, "operation": operation_id}, sort_keys=True, ensure_ascii=False)
    key = _digest(context.session_key + "\0" + str(context.canonical_project_root) + "\0" + operation_id)
    try:
        with ownership_lock(context):
            source = _contained(context, source)
            destination = _contained(context, destination)
            if source == destination or not operation_id or expected_dst_revision is not None:
                raise ValueError("A move needs distinct paths, an operation ID, and an absent destination")
            payload = json.dumps({"action": "move", "source": str(source),
                                  "destination": str(destination), "expected": expected_revision,
                                  "operation": operation_id}, sort_keys=True, ensure_ascii=False)
            key = _digest(context.session_key + "\0" + str(context.canonical_project_root) +
                          "\0" + operation_id)
            with contextlib.closing(connect_coordination(context)) as connection:
                if connection.execute('SELECT 1 FROM cancelled_operations WHERE id=?',(key,)).fetchone():
                    return WriteResult('conflict',warnings=('This operation intent was explicitly discarded',))
                prior = connection.execute(
                    "SELECT payload, phase, revision FROM operations WHERE id=?", (key,),
                ).fetchone()
                if prior and not _same_payload(prior[0], payload):
                    return WriteResult("conflict", warnings=("Operation ID has different content",))
                if prior and prior[1] == "applied":
                    _clear_pending(context, key)
                    return WriteResult("unchanged", prior[2])
                original = _read_bytes(source)
                target = _read_bytes(destination)
                revision = hashlib.sha256(original).hexdigest() if original is not None else None
                target_revision = hashlib.sha256(target).hexdigest() if target is not None else None
                if prior and original is None and target_revision == expected_revision:
                    connection.execute("UPDATE operations SET phase='applied' WHERE id=?", (key,))
                    connection.commit()
                    _clear_pending(context, key)
                    return WriteResult("unchanged", expected_revision)
                if original is None or revision != expected_revision:
                    return WriteResult("conflict", revision, _pending(context, key, payload),
                                       ("The move source changed; existing files were preserved",))
                if target is not None and (not prior or not os.path.samefile(source, destination)):
                    return WriteResult("conflict", revision, _pending(context, key, payload),
                                       ("The move destination exists; existing files were preserved",))
                if not prior:
                    connection.execute("INSERT INTO operations VALUES (?, ?, 'prepared', NULL, ?)",
                                       (key, payload, expected_revision))
                    connection.commit()
                _pending(context, key, payload)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _fault("before_move_link")
                if _contained(context, source) != source or _contained(context, destination) != destination:
                    raise ValueError("Move containment changed before publication")
                if target is None:
                    os.link(str(source), str(destination), follow_symlinks=False)
                _fault("after_move_link")
                if (_contained(context, source) != source or
                        _contained(context, destination) != destination):
                    raise ValueError("Move containment changed before source removal")
                if (_read_bytes(source) != original or _read_bytes(destination) != original or
                        not os.path.samefile(source, destination)):
                    return WriteResult("conflict", revision, _pending(context, key, payload),
                                       ("The note changed during the move; existing files were preserved",))
                source.unlink()
                for parent in {source.parent, destination.parent}:
                    directory = os.open(str(parent), os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                _fault("after_move")
                connection.execute("UPDATE operations SET phase='applied' WHERE id=?", (key,))
                connection.commit()
                _clear_pending(context, key)
                return WriteResult("applied", expected_revision)
    except LockBusy as exc:
        return WriteResult("pending", pending_path=_pending(context, key, payload), warnings=(str(exc),))
    except ValueError as exc:
        return WriteResult("conflict", warnings=(str(exc),))
    except (OSError, sqlite3.Error) as exc:
        return WriteResult("pending", warnings=(str(exc),))


def delete_note(context, path, expected_revision, operation_id) -> WriteResult:
    """Delete an obsolete note only at the caller's recorded revision.

    Retain the intent before deletion. A retry acknowledges an already missing
    file; an acknowledged operation never deletes a newly created replacement.
    """
    payload = json.dumps({"action": "delete", "path": str(path),
                          "expected": expected_revision, "operation": operation_id}, sort_keys=True, ensure_ascii=False)
    key = _digest(context.session_key + "\0" + str(context.canonical_project_root) + "\0" + operation_id)
    try:
        with ownership_lock(context):
            path = _contained(context, path)
            if not operation_id:
                raise ValueError("A deletion needs an operation ID")
            payload = json.dumps({"action": "delete", "path": str(path),
                                  "expected": expected_revision, "operation": operation_id}, sort_keys=True, ensure_ascii=False)
            key = _digest(context.session_key + "\0" + str(context.canonical_project_root) +
                          "\0" + operation_id)
            with contextlib.closing(connect_coordination(context)) as connection:
                if connection.execute('SELECT 1 FROM cancelled_operations WHERE id=?',(key,)).fetchone():
                    return WriteResult('conflict',warnings=('This operation intent was explicitly discarded',))
                prior = connection.execute(
                    "SELECT payload, phase FROM operations WHERE id=?", (key,),
                ).fetchone()
                current = _read(path)
                revision = _digest(current) if current is not None else None
                if prior:
                    if not _same_payload(prior[0], payload):
                        return WriteResult("conflict", revision,
                                           warnings=("Operation ID has different content",))
                    if prior[1] == "applied":
                        _clear_pending(context, key)
                        return WriteResult("unchanged", revision)
                    if current is None:
                        connection.execute("UPDATE operations SET phase='applied' WHERE id=?", (key,))
                        connection.commit()
                        _clear_pending(context, key)
                        return WriteResult("unchanged")
                if revision != expected_revision:
                    return WriteResult("conflict", revision, _pending(context, key, payload),
                                       ("The source revision changed; the original note was preserved",))
                if not prior:
                    connection.execute("INSERT INTO operations VALUES (?, ?, 'prepared', NULL, ?)",
                                       (key, payload, expected_revision))
                    connection.commit()
                _pending(context, key, payload)
                if current is not None:
                    _fault("before_unlink")
                    if _contained(context, path) != path or _read(path) != current:
                        return WriteResult("conflict", revision, _pending(context, key, payload),
                                           ("The note changed during deletion",))
                    path.unlink()
                    directory = os.open(str(path.parent), os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                    _fault("after_delete")
                connection.execute("UPDATE operations SET phase='applied' WHERE id=?", (key,))
                connection.commit()
                _clear_pending(context, key)
                return WriteResult("applied" if current is not None else "unchanged")
    except LockBusy as exc:
        # The caller retains the deletion request and can retry with its stable ID.
        return WriteResult("pending", pending_path=_pending(context, key, payload), warnings=(str(exc),))
    except ValueError as exc:
        return WriteResult("conflict", warnings=(str(exc),))
    except (OSError, sqlite3.Error) as exc:
        return WriteResult("pending", warnings=(str(exc),))


def apply_mutations(context, mutations: Sequence[NoteMutation]) -> WriteResult:
    result = WriteResult("unchanged")
    try:
        _pending_payload_bound(json.dumps([{"path":str(item.path),"expected":item.expected_revision,
            "changes":dict(item.managed_changes),"operation":item.operation_id,"mode":item.file_mode}
            for item in mutations],sort_keys=True, ensure_ascii=False))
        with ownership_lock(context):
            with contextlib.closing(connect_coordination(context)) as connection:
                for mutation in mutations:
                    result = _apply_one(context, connection, mutation)
                    if result.status in {"conflict", "pending"}:
                        return result
        return result
    except LockBusy as exc:
        payload = json.dumps([{"path": str(item.path), "expected": item.expected_revision,
                               "changes": dict(item.managed_changes), "operation": item.operation_id,
                               "mode": item.file_mode}
                              for item in mutations], sort_keys=True, ensure_ascii=False)
        pending = _pending(context, _digest(payload), payload)
        return WriteResult("pending", pending_path=pending, warnings=(str(exc),))
    except ValueError as exc:
        return WriteResult("conflict", warnings=(str(exc),))
    except (OSError, sqlite3.Error) as exc:
        return WriteResult("pending", warnings=(str(exc),))
