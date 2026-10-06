"""Coordinate vault publications independently of the replaceable search index."""

import contextlib
import fcntl
import hashlib
import json
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
    "type", "date", "project", "tags", "session_id", "source_session",
    "agent_provider", "agent_session_id", "capture_state", "capture_completeness",
    "capture_revision", "summary_revision", "parent_session", "source_revision", "trigger",
    "status", "author_host", "operation_id",
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
    if any(not isinstance(value, (str, list)) for value in values.values()):
        raise ValueError("Managed metadata must contain strings or string lists")
    if any(isinstance(value, list) and any(not isinstance(item, str) for item in value)
           for value in values.values()):
        raise ValueError("Managed metadata lists must contain strings")
    return values


def _metadata_lines(document):
    frontmatter = _frontmatter(document)
    result = {}
    if frontmatter:
        for line in frontmatter[0].splitlines():
            match = re.match(r"^([a-z_]+):(?:[ \t]*(.*))$", line)
            if match and match[1] in _METADATA_KEYS:
                if match[1] in result:
                    raise ValueError("The note has duplicate managed metadata")
                result[match[1]] = line
    return result


def _fault(point):
    """Fault-injection seam used by crash tests; production has no triggers."""


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
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


def coordination_path(context):
    return _private_dir(context.index_path.parent /
                        ("." + context.index_path.name + ".coordination"))


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
    native_home = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    return RuntimeContext("claude", "cli", "", project, project, None, vault,
                          native_home / "obsidian-brain-config.json", MappingProxyType({}),
                          Path(__file__).resolve().parent.parent,
                          Path(vault_index._default_db_path()).resolve(),
                          Path(obsidian_utils._SECURE_DIR))


@contextlib.contextmanager
def ownership_lock(context, key="vault", deadline=None):
    """Use an OS lock, retain its inode, and release only our owner token.

    Vault ownership comes before session ownership. Nested calls in one thread
    reuse ownership. A dead process releases its OS lock without an age rule.
    """
    path = coordination_path(context) / (_digest(key) + ".lock")
    held = getattr(_HELD, "locks", {})
    _HELD.locks = held
    if str(path) in held:
        yield held[str(path)][1]
        return
    vault_key = str(path.parent / (_digest("vault") + ".lock"))
    if key != "vault" and vault_key not in held:
        raise ValueError("Acquire vault ownership before session ownership")
    deadline = time.monotonic() + 0.1 if deadline is None else deadline
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
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
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        os.write(fd, token.encode())
        held[str(path)] = (fd, token)
        try:
            yield token
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.read(fd, 128).decode() == token:
                os.ftruncate(fd, 0)
            held.pop(str(path), None)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def connect_coordination(context):
    """Caller must hold vault ownership. Rebuilds never remove this database."""
    path = coordination_path(context) / "state.sqlite3"
    # Coordination survives search-index replacement and has its own schema.
    connection = sqlite3.connect(str(path), timeout=0.1)  # noqa: vault-db-connect
    os.chmod(path, 0o600)
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS identity (vault TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS revisions (
            path TEXT, revision TEXT, regions TEXT,
            PRIMARY KEY(path, revision));
        CREATE TABLE IF NOT EXISTS operations (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, phase TEXT NOT NULL,
            document TEXT, revision TEXT);
    """)
    identity = connection.execute("SELECT vault FROM identity").fetchone()
    vault = str(context.vault_path.resolve())
    if identity and identity[0] != vault:
        connection.close()
        raise ValueError("The selected coordination database belongs to another vault")
    if not identity:
        connection.execute("INSERT INTO identity VALUES (?)", (vault,))
    connection.commit()
    return connection


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
        lines = frontmatter[0].splitlines() if frontmatter else []
        for key, value in metadata.items():
            rendered = key + ": " + json.dumps(value, ensure_ascii=False)
            if key in existing_fields:
                lines[lines.index(existing_fields[key])] = rendered
            else:
                lines.append(rendered)
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
            result += "\n\n## " + heading + "\n\n" + block + "\n"
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


def _pending(context, operation_id, payload):
    directory = _private_dir(session_state_path(context) / "pending")
    path = directory / (_digest(operation_id) + ".json")
    _atomic_write(path, payload)
    return path


def _apply_one(context, connection, mutation):
    path = _contained(context, mutation.path)
    if not mutation.operation_id or not mutation.managed_changes:
        raise ValueError("A mutation needs content and an operation ID")
    if mutation.file_mode not in {None, 0o600, 0o644}:
        raise ValueError("Unsupported note permissions")
    payload = json.dumps({"path": str(path), "expected": mutation.expected_revision,
                          "changes": dict(mutation.managed_changes),
                          "mode": mutation.file_mode}, sort_keys=True)
    key = _digest(context.session_key + "\0" + str(context.canonical_project_root) +
                  "\0" + mutation.operation_id)
    prior = connection.execute("SELECT payload, phase, document, revision FROM operations WHERE id=?",
                               (key,)).fetchone()
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
        if prior[0] != payload:
            return WriteResult("conflict", revision, warnings=("Operation ID has different content",))
        if prior[1] == "applied":
            return WriteResult("unchanged", revision)
        if prior[3] == revision and revision is not None:
            if mutation.file_mode is not None:
                os.chmod(path, mutation.file_mode)
            connection.execute("UPDATE operations SET phase='applied' WHERE id=?", (key,))
            _record_read(connection, path, current)
            connection.commit()
            return WriteResult("unchanged", revision)
    expected = mutation.expected_revision
    valid = revision == expected
    if not valid and "document" not in mutation.managed_changes and not repair and expected is not None:
        baseline = connection.execute("SELECT regions FROM revisions WHERE path=? AND revision=?",
                                      (str(path), expected)).fetchone()
        if baseline:
            before = json.loads(baseline[0])
            now = _regions(current)
            checks = set(mutation.managed_changes)
            if "metadata" in checks:
                checks.remove("metadata")
                checks.update("metadata:" + key for key in _metadata_changes(mutation.managed_changes))
            if "summary" in checks:
                checks.add("capture")
            valid = current is not None and all(before.get(region) == now.get(region) for region in checks)
    if not valid:
        return WriteResult("conflict", revision, _pending(context, key, payload),
                           ("The source revision changed; the original note was preserved",))
    rendered = prior[2] if prior else _render(current, mutation.managed_changes)
    next_revision = _digest(rendered)
    if not prior:
        connection.execute("INSERT INTO operations VALUES (?, ?, 'prepared', ?, ?)",
                           (key, payload, rendered, next_revision))
        connection.commit()
    changed = current_bytes != rendered.encode("utf-8")
    if changed:
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
    return WriteResult("applied" if changed else "unchanged", next_revision)


def move_note(context, source, destination, expected_revision, operation_id,
              expected_dst_revision=None) -> WriteResult:
    """Move without overwriting a destination; retain enough intent to replay.

    A hard link publishes the destination without replacing another file. The
    source is removed only while both names still refer to the verified bytes.
    A crash between those steps leaves both names available for recovery.
    """
    try:
        with ownership_lock(context):
            source = _contained(context, source)
            destination = _contained(context, destination)
            if source == destination or not operation_id or expected_dst_revision is not None:
                raise ValueError("A move needs distinct paths, an operation ID, and an absent destination")
            payload = json.dumps({"action": "move", "source": str(source),
                                  "destination": str(destination), "expected": expected_revision}, sort_keys=True)
            key = _digest(context.session_key + "\0" + str(context.canonical_project_root) +
                          "\0" + operation_id)
            with contextlib.closing(connect_coordination(context)) as connection:
                prior = connection.execute(
                    "SELECT payload, phase, revision FROM operations WHERE id=?", (key,),
                ).fetchone()
                if prior and prior[0] != payload:
                    return WriteResult("conflict", warnings=("Operation ID has different content",))
                if prior and prior[1] == "applied":
                    return WriteResult("unchanged", prior[2])
                original = _read_bytes(source)
                target = _read_bytes(destination)
                revision = hashlib.sha256(original).hexdigest() if original is not None else None
                target_revision = hashlib.sha256(target).hexdigest() if target is not None else None
                if prior and original is None and target_revision == expected_revision:
                    connection.execute("UPDATE operations SET phase='applied' WHERE id=?", (key,))
                    connection.commit()
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
                destination.parent.mkdir(parents=True, exist_ok=True)
                if _contained(context, source) != source or _contained(context, destination) != destination:
                    raise ValueError("Move containment changed before publication")
                if target is None:
                    os.link(str(source), str(destination), follow_symlinks=False)
                _fault("after_move_link")
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
                return WriteResult("applied", expected_revision)
    except LockBusy as exc:
        return WriteResult("pending", warnings=(str(exc),))
    except ValueError as exc:
        return WriteResult("conflict", warnings=(str(exc),))
    except (OSError, sqlite3.Error) as exc:
        return WriteResult("pending", warnings=(str(exc),))


def delete_note(context, path, expected_revision, operation_id) -> WriteResult:
    """Delete an obsolete note only at the caller's recorded revision.

    Retain the intent before deletion. A retry acknowledges an already missing
    file; an acknowledged operation never deletes a newly created replacement.
    """
    payload = None
    key = None
    try:
        with ownership_lock(context):
            path = _contained(context, path)
            if not operation_id:
                raise ValueError("A deletion needs an operation ID")
            payload = json.dumps({"action": "delete", "path": str(path),
                                  "expected": expected_revision}, sort_keys=True)
            key = _digest(context.session_key + "\0" + str(context.canonical_project_root) +
                          "\0" + operation_id)
            with contextlib.closing(connect_coordination(context)) as connection:
                prior = connection.execute(
                    "SELECT payload, phase FROM operations WHERE id=?", (key,),
                ).fetchone()
                current = _read(path)
                revision = _digest(current) if current is not None else None
                if prior:
                    if prior[0] != payload:
                        return WriteResult("conflict", revision,
                                           warnings=("Operation ID has different content",))
                    if prior[1] == "applied":
                        return WriteResult("unchanged", revision)
                    if current is None:
                        connection.execute("UPDATE operations SET phase='applied' WHERE id=?", (key,))
                        connection.commit()
                        return WriteResult("unchanged")
                if revision != expected_revision:
                    return WriteResult("conflict", revision, _pending(context, key, payload),
                                       ("The source revision changed; the original note was preserved",))
                if not prior:
                    connection.execute("INSERT INTO operations VALUES (?, ?, 'prepared', NULL, ?)",
                                       (key, payload, expected_revision))
                    connection.commit()
                if current is not None:
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
                return WriteResult("applied" if current is not None else "unchanged")
    except LockBusy as exc:
        # The caller retains the deletion request and can retry with its stable ID.
        return WriteResult("pending", warnings=(str(exc),))
    except ValueError as exc:
        return WriteResult("conflict", warnings=(str(exc),))
    except (OSError, sqlite3.Error) as exc:
        return WriteResult("pending", warnings=(str(exc),))


def apply_mutations(context, mutations: Sequence[NoteMutation]) -> WriteResult:
    result = WriteResult("unchanged")
    try:
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
                              for item in mutations], sort_keys=True)
        pending = _pending(context, _digest(payload), payload)
        return WriteResult("pending", pending_path=pending, warnings=(str(exc),))
    except ValueError as exc:
        return WriteResult("conflict", warnings=(str(exc),))
    except (OSError, sqlite3.Error) as exc:
        return WriteResult("pending", warnings=(str(exc),))
