"""Durable scrubbed checkpoints, note publication, and committed source cursors."""

import contextlib
import hashlib
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Tuple
from session_lookup import SessionLookupPending

from note_transactions import (
    LockBusy, NoteMutation, apply_mutations, connect_coordination, ownership_lock,
    read_revision,
)


@dataclass(frozen=True)
class CaptureResult:
    status: str
    applied_revision: Optional[str] = None
    pending_sources: int = 0
    loss_of_input: bool = False
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CaptureEvent:
    kind: str
    turn_id: Optional[str] = None
    note_path: Optional[Path] = None
    min_messages: Optional[int] = None
    trigger: Optional[str] = None


def _fault(point):
    """Crash-test seam with no production triggers."""


def _scope(context):
    # A native session can resume in another working tree. Its facts stay in
    # one session; private caches and operation ownership also include roots.
    return context.session_key


def _schema(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS capture_events (
            scope TEXT, source TEXT, generation TEXT, event TEXT, text TEXT,
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            UNIQUE(scope, source, generation, event));
        CREATE TABLE IF NOT EXISTS checkpoints (
            id TEXT PRIMARY KEY, scope TEXT, source TEXT, generation TEXT,
            offset INTEGER, note TEXT, expected TEXT, operation TEXT,
            phase TEXT, revision TEXT, body TEXT);
        CREATE TABLE IF NOT EXISTS cursors (
            scope TEXT, source TEXT, generation TEXT, offset INTEGER,
            PRIMARY KEY(scope, source, generation));
        CREATE TABLE IF NOT EXISTS checkpoint_owners (
            checkpoint TEXT PRIMARY KEY, project_root TEXT NOT NULL,
            worktree TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS checkpoint_metadata (
            checkpoint TEXT PRIMARY KEY, metadata TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_sessions (
            scope TEXT PRIMARY KEY, descriptor TEXT NOT NULL, cursor TEXT NOT NULL,
            note TEXT, state TEXT NOT NULL, completeness TEXT NOT NULL,
            first_date TEXT NOT NULL, messages INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS native_events (
            scope TEXT, event TEXT, content TEXT NOT NULL, role TEXT NOT NULL,
            PRIMARY KEY(scope,event));
        CREATE TABLE IF NOT EXISTS source_owners (
            scope TEXT PRIMARY KEY, host TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS recovery_positions (
            host TEXT PRIMARY KEY, position INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS capture_timing (
            scope TEXT PRIMARY KEY, first REAL NOT NULL, latest REAL NOT NULL);
    """)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(checkpoints)")}
    if "body" not in columns:
        connection.execute("ALTER TABLE checkpoints ADD COLUMN body TEXT")
    connection.commit()


def _cursor(connection, scope, source, generation):
    row = connection.execute(
        "SELECT offset FROM cursors WHERE scope=? AND source=? AND generation=?",
        (scope, source, generation),
    ).fetchone()
    return row[0] if row else 0


def read_cursor(context, source, generation):
    with ownership_lock(context):
        with contextlib.closing(connect_coordination(context)) as connection:
            _schema(connection)
            return _cursor(connection, _scope(context), source, generation)


def _publish(context, connection, checkpoint):
    identifier, scope, source, generation, offset, note, expected, operation, phase, revision, body = checkpoint
    if body is None:
        return CaptureResult("pending", pending_sources=1,
                             warnings=("Historical checkpoint lacks its immutable body; reconciliation is required",))
    current_revision = read_revision(context, note)
    current_regions = connection.execute(
        "SELECT regions FROM revisions WHERE path=? AND revision=?", (note, current_revision),
    ).fetchone()
    current_capture = json.loads(current_regions[0]).get("capture") if current_regions else None
    checkpoint_metadata = connection.execute(
        "SELECT metadata FROM checkpoint_metadata WHERE checkpoint=?", (identifier,),
    ).fetchone()
    metadata_keys = {"metadata:" + key for key in json.loads(checkpoint_metadata[0])} if checkpoint_metadata else set()

    def unchanged_owned_regions(published_regions):
        before = json.loads(published_regions[0]) if published_regions else {}
        now = json.loads(current_regions[0]) if current_regions else {}
        return bool(current_capture) and all(before.get(key) == now.get(key)
                                             for key in {"capture"} | metadata_keys)
    # A newer publication contains every event retained before its checkpoint.
    # Verify that region before acknowledging an older checkpoint. Never write
    # the older body over the newer note, and preserve edits outside capture.
    publications = connection.execute("""
        SELECT revision FROM checkpoints WHERE scope=? AND note=?
        AND phase IN ('applied','committed')
        AND rowid >= (SELECT rowid FROM checkpoints WHERE id=?) ORDER BY rowid DESC
    """, (scope, note, identifier)).fetchall()
    for published_revision, in publications:
        published_regions = connection.execute(
            "SELECT regions FROM revisions WHERE path=? AND revision=?", (note, published_revision),
        ).fetchone()
        published_capture = json.loads(published_regions[0]).get("capture") if published_regions else None
        if current_capture and current_capture == published_capture and unchanged_owned_regions(published_regions):
            _commit_cursor(connection, checkpoint, current_revision)
            return CaptureResult("complete", current_revision)
    if phase == "prepared":
        # Only our earlier publication may advance this checkpoint's baseline.
        # Matching capture hashes preserve user edits outside managed capture.
        earlier = connection.execute("""
            SELECT revision FROM checkpoints WHERE scope=? AND note=?
            AND phase IN ('applied','committed')
            AND rowid < (SELECT rowid FROM checkpoints WHERE id=?) ORDER BY rowid DESC
        """, (scope, note, identifier)).fetchall()
        for earlier_revision, in earlier:
            regions = connection.execute(
                "SELECT regions FROM revisions WHERE path=? AND revision=?", (note, earlier_revision),
            ).fetchone()
            earlier_capture = json.loads(regions[0]).get("capture") if regions else None
            if current_capture and current_capture == earlier_capture and unchanged_owned_regions(regions):
                expected = current_revision
                break
        connection.execute("UPDATE checkpoints SET expected=?, phase='ready' WHERE id=?",
                           (expected, identifier))
        connection.commit()
    # Resume can change projects. A prepared operation must keep its original
    # root-qualified identity so a replaced note can be acknowledged after a crash.
    owner = connection.execute(
        "SELECT project_root, worktree FROM checkpoint_owners WHERE checkpoint=?",
        (identifier,),
    ).fetchone()
    operation_context = replace(context, canonical_project_root=Path(owner[0]),
                                worktree=Path(owner[1])) if owner else context
    changes = {"capture": body}
    if checkpoint_metadata:
        changes["metadata"] = checkpoint_metadata[0]
    result = apply_mutations(operation_context, [NoteMutation(Path(note), expected,
                                                   changes, operation)])
    if result.status not in {"applied", "unchanged"}:
        count = connection.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE scope=? AND phase!='committed'", (scope,),
        ).fetchone()[0]
        return CaptureResult("conflict" if result.status == "conflict" else "pending",
                             result.revision, count, warnings=result.warnings)
    connection.execute("UPDATE checkpoints SET phase='applied', revision=? WHERE id=?",
                       (result.revision, identifier))
    connection.commit()
    _fault("before_cursor_commit")
    _commit_cursor(connection, checkpoint, result.revision)
    return CaptureResult("complete", result.revision)


def _commit_cursor(connection, checkpoint, revision):
    identifier, scope, source, generation, offset = checkpoint[:5]
    connection.execute("""
        INSERT INTO cursors VALUES (?, ?, ?, ?)
        ON CONFLICT(scope, source, generation) DO UPDATE SET
            offset=MAX(cursors.offset, excluded.offset)
    """, (scope, source, generation, offset))
    connection.execute("UPDATE checkpoints SET phase='committed', revision=? WHERE id=?",
                       (revision, identifier))
    connection.commit()


def publish_events(context, source, generation, offset, events, note_path, *,
                   metadata=None, checkpoint_tag=None):
    """Retain facts before writing a note; acknowledge input only after publication."""
    from obsidian_utils import scrub_secrets
    if not isinstance(offset, int) or offset < 0 or not source or not generation:
        raise ValueError("A checkpoint needs a source, generation, and byte offset")
    scope = _scope(context)
    safe_events = []
    for event_id, text in events:
        if not isinstance(event_id, str) or not event_id or not isinstance(text, str):
            raise ValueError("Capture events need stable string identities and text")
        safe_events.append((event_id, scrub_secrets(text).replace("<!-- obsidian-brain:", "&lt;!-- obsidian-brain:")))
    session_owner = "session:" + scope + "\0" + str(context.canonical_project_root)
    with ownership_lock(context), ownership_lock(context, key=session_owner):
        with contextlib.closing(connect_coordination(context)) as connection:
            _schema(connection)
            for event_id, text in safe_events:
                prior = connection.execute(
                    "SELECT text FROM capture_events WHERE scope=? AND source=? AND generation=? AND event=?",
                    (scope, source, generation, event_id),
                ).fetchone()
                if prior and prior[0] != text:
                    return CaptureResult("conflict", warnings=("A source event identity changed content",))
            if checkpoint_tag is None and _cursor(connection, scope, source, generation) >= offset:
                return CaptureResult("complete", read_revision(context, note_path))
            key = hashlib.sha256(json.dumps(
                [scope, source, generation, offset] + ([checkpoint_tag] if checkpoint_tag is not None else []),
                separators=(",", ":"),
            ).encode()).hexdigest()
            checkpoint = connection.execute("SELECT * FROM checkpoints WHERE id=?", (key,)).fetchone()
            if checkpoint is None:
                expected = read_revision(context, note_path)
                prior_publication = connection.execute(
                    "SELECT revision FROM checkpoints WHERE scope=? AND note=? "
                    "AND phase IN ('applied','committed') ORDER BY rowid DESC LIMIT 1",
                    (scope, str(note_path)),
                ).fetchone()
                if prior_publication:
                    expected = prior_publication[0]
                _fault("before_checkpoint")
                connection.executemany(
                    "INSERT OR IGNORE INTO capture_events(scope,source,generation,event,text) VALUES (?,?,?,?,?)",
                    [(scope, source, generation, identity, text) for identity, text in safe_events],
                )
                body = "\n".join(row[0] for row in connection.execute(
                    "SELECT text FROM capture_events WHERE scope=? ORDER BY sequence", (scope,),
                ))
                connection.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                   (key, scope, source, generation, offset, str(note_path), expected,
                                    "capture-" + key, "prepared", None, body))
                connection.execute("INSERT INTO checkpoint_owners VALUES (?,?,?)",
                                   (key, str(context.canonical_project_root), str(context.worktree)))
                if metadata is not None:
                    fields = dict(metadata)
                    # This revision names the normalized capture region, not
                    # the full document containing its own revision field.
                    fields["capture_revision"] = hashlib.sha256(
                        (body if body.endswith("\n") else body + "\n").encode()).hexdigest()
                    connection.execute("INSERT INTO checkpoint_metadata VALUES (?,?)",
                                       (key, json.dumps(fields, sort_keys=True)))
                connection.commit()
                _fault("after_checkpoint")
                checkpoint = connection.execute("SELECT * FROM checkpoints WHERE id=?", (key,)).fetchone()
            return _publish(context, connection, checkpoint)


def recover_pending(context, max_sources, deadline):
    """Replay retained facts within a caller's source count and monotonic budget."""
    scope = _scope(context)
    revision = None
    session_owner = "session:" + scope + "\0" + str(context.canonical_project_root)
    with ownership_lock(context, deadline=deadline), ownership_lock(
            context, key=session_owner, deadline=deadline):
        with contextlib.closing(connect_coordination(context)) as connection:
            _schema(connection)
            rows = connection.execute(
                "SELECT id,source FROM checkpoints WHERE scope=? AND phase!='committed' ORDER BY rowid LIMIT 65", (scope,),
            ).fetchall()
            completed = 0
            sources = set()
            for identifier, source in rows:
                if (time.monotonic() >= deadline or completed >= 64 or
                        (source not in sources and len(sources) >= max_sources)):
                    break
                sources.add(source)
                row = connection.execute("SELECT * FROM checkpoints WHERE id=?", (identifier,)).fetchone()
                result = _publish(context, connection, row)
                if result.status != "complete":
                    return result
                revision = result.applied_revision
                completed += 1
            pending = connection.execute(
                "SELECT COUNT(DISTINCT source) FROM checkpoints WHERE scope=? AND phase!='committed'",
                (scope,),
            ).fetchone()[0]
            return CaptureResult("pending" if pending else "complete", revision, pending)


def _descriptor(context):
    return {"host": context.host, "client": context.client,
            "native_session_id": context.native_session_id,
            "canonical_project_root": str(context.canonical_project_root),
            "worktree": str(context.worktree),
            "transcript_path": str(context.transcript_path) if context.transcript_path else None,
            "state_path": str(context.state_path), "config_path": str(context.config_path)}


def _note_identity(path):
    """Read only explicit single-line identity fields; never infer from a name."""
    from note_transactions import _metadata_lines
    if not path.exists():
        return None
    with path.open("rb") as stream:
        header = stream.read(64 * 1024)
    if header.startswith(b"---\n"):
        end = header.find(b"\n---\n", 4)
        if end < 0:
            raise ValueError("The session identity header is incomplete or exceeds its size bound")
        header = header[:end + 5]
    else:
        return {}
    fields = {}
    for key, line in _metadata_lines(header.decode("utf-8")).items():
        raw = line.split(":", 1)[1].strip()
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw.strip("'\"")
        fields[key] = value
    return fields


def _matches_identity(context, fields):
    return (fields.get("agent_provider", "claude") == context.host and
            fields.get("agent_session_id", fields.get("session_id")) == context.native_session_id)


def _choose_note(context, first_date, deadline):
    import re
    project = re.sub(r"[^A-Za-z0-9._-]+", "-", context.canonical_project_root.name).strip("-") or "project"
    folder = context.vault_path / str(context.config.get("sessions_folder", "claude-sessions"))
    from note_transactions import _contained
    _contained(context, folder / "placeholder.md")
    from session_lookup import find_existing_session
    existing = find_existing_session(context, deadline)
    if existing is not None:
        return existing
    digest = context.session_key
    for length in (16, 24, 32, 64):
        path = folder / (first_date + "-" + project + "-" + context.host + "-" + digest[:length] + ".md")
        identity = _note_identity(path)
        if identity is None or _matches_identity(context, identity):
            return path
    raise ValueError("Every session filename collides with another native identity")


def _snapshot(context, note, first_date, body, trigger, deadline):
    if time.monotonic() >= deadline:
        return CaptureResult("pending", pending_sources=1,
                             warnings=("Snapshot deadline reached",))
    normalized = body if body.endswith("\n") else body + "\n"
    revision = hashlib.sha256(normalized.encode()).hexdigest()
    identity = hashlib.sha256((context.session_key + "\0" + revision + "\0" + trigger).encode()).hexdigest()
    folder = context.vault_path / str(context.config.get("sessions_folder", "claude-sessions"))
    path = folder / (first_date + "-" + context.host + "-snapshot-" + identity + ".md")
    from note_transactions import _contained
    _contained(context, path)
    metadata = {"type": "claude-snapshot", "date": first_date,
                "status": "auto-logged", "project": context.canonical_project_root.name,
                "session_id": context.native_session_id,
                "agent_provider": context.host, "agent_session_id": context.native_session_id,
                "source_session": context.native_session_id,
                "parent_session": "[[" + note.stem + "]]", "source_revision": revision,
                "capture_revision": revision,
                "trigger": trigger, "tags": ["claude/snapshot"]}
    from note_transactions import _render
    document = _render("", {"metadata": json.dumps(metadata), "capture": body})
    # Snapshots have one publication identity and are never amended in place.
    result = apply_mutations(context, [NoteMutation(path, None, {"document": document},
                                                   "snapshot-" + identity)])
    return CaptureResult("complete" if result.status in {"applied", "unchanged"} else result.status,
                         result.revision, warnings=result.warnings)


def capture_checkpoint(context, event, deadline):
    """Retain normalized facts and source state before any visible publication."""
    import sqlite3
    from datetime import datetime, timezone
    from dataclasses import asdict
    from obsidian_utils import scrub_secrets
    from transcripts import SourceCursor, read_records
    kinds = {"session_start", "resume", "stop", "pre_compact", "session_end", "recover"}
    if event.kind not in kinds or not context.native_session_id:
        return CaptureResult("conflict", warnings=("A lifecycle event needs a native session identity",))
    if time.monotonic() >= deadline:
        return CaptureResult("pending", pending_sources=1, warnings=("Capture deadline reached",))
    try:
        with ownership_lock(context, deadline=deadline):
            with contextlib.closing(connect_coordination(context)) as connection:
                _schema(connection)
                scope = _scope(context)
                row = connection.execute("SELECT * FROM source_sessions WHERE scope=?", (scope,)).fetchone()
                cursor = SourceCursor(**json.loads(row[2])) if row else SourceCursor()
                batch = read_records(context, cursor, deadline)
                native_id = batch.metadata.get("native_session_id")
                if native_id is not None and native_id != context.native_session_id:
                    return CaptureResult("conflict", warnings=("The transcript belongs to another native session",))
                if native_id is None and not row:
                    return CaptureResult("pending", pending_sources=1, loss_of_input=batch.loss_of_input,
                                         warnings=batch.warnings + ("Native transcript identity is unverified",))
                first_date = row[6] if row else datetime.now(timezone.utc).date().isoformat()
                if not row:
                    for record in batch.records:
                        if record.timestamp:
                            try:
                                first_date = datetime.fromisoformat(record.timestamp.replace("Z", "+00:00")).date().isoformat()
                                break
                            except ValueError:
                                continue
                state = row[4] if row else "active"
                if event.kind in {"session_start", "resume"}:
                    state = "active"
                if any(record.kind == "interruption" for record in batch.records):
                    state = "interrupted"
                if event.kind == "session_end":
                    state = "ended"
                source_loss = bool(batch.loss_of_input or cursor.parser_state.get("_capture_loss"))
                completeness = "partial" if batch.status != "ok" or source_loss else "complete"
                for record in batch.records:
                    if record.role not in {"user", "assistant", "tool"} or not record.text:
                        continue
                    safe = scrub_secrets(record.text).replace("<!-- obsidian-brain:", "&lt;!-- obsidian-brain:")
                    text = record.role.capitalize() + ": " + safe
                    prior = connection.execute("SELECT content,role FROM native_events WHERE scope=? AND event=?",
                                               (scope, record.source_id)).fetchone()
                    if prior and prior != (text, record.role):
                        connection.rollback()
                        return CaptureResult("conflict", warnings=("A native event identity changed content",))
                    connection.execute("INSERT OR IGNORE INTO native_events VALUES (?,?,?,?)",
                                       (scope, record.source_id, text, record.role))
                    if record.timestamp:
                        try:
                            instant = datetime.fromisoformat(record.timestamp.replace("Z", "+00:00"))
                            timestamp = instant.replace(tzinfo=timezone.utc).timestamp() if instant.tzinfo is None else instant.timestamp()
                            connection.execute("INSERT INTO capture_timing VALUES (?,?,?) ON CONFLICT(scope) "
                                               "DO UPDATE SET first=MIN(first,excluded.first),latest=MAX(latest,excluded.latest)",
                                               (scope, timestamp, timestamp))
                        except (ValueError, OverflowError):
                            pass
                events = connection.execute("SELECT event,content FROM native_events WHERE scope=? ORDER BY rowid",
                                            (scope,)).fetchall()
                messages = connection.execute("SELECT COUNT(*) FROM native_events WHERE scope=? AND role='user'",
                                              (scope,)).fetchone()[0]
                retained_note = bool(row and row[3])
                selected_note = Path(row[3]) if retained_note else None
                if event.note_path is not None:
                    if selected_note is not None and selected_note.resolve() != event.note_path.resolve():
                        connection.rollback()
                        return CaptureResult("conflict", warnings=("A session cannot switch its retained note path",))
                    selected_note = event.note_path
                threshold = event.min_messages if event.min_messages is not None else context.config.get("min_messages", 3)
                if not isinstance(threshold, int) or threshold < 0:
                    raise ValueError("The message threshold must be a nonnegative integer")
                minimum_duration = context.config.get("min_duration_minutes", 2)
                if not isinstance(minimum_duration, (int, float)) or minimum_duration < 0:
                    raise ValueError("The minimum duration must be a nonnegative number")
                timing = connection.execute("SELECT first,latest FROM capture_timing WHERE scope=?", (scope,)).fetchone()
                duration = timing[1] - timing[0] if timing else 0
                too_short = 0 < duration < minimum_duration * 60
                if selected_note is None and messages and messages >= threshold and not too_short:
                    selected_note = _choose_note(context, first_date, deadline)
                if selected_note is not None:
                    from note_transactions import _contained
                    selected_note = _contained(context, selected_note)
                    identity = _note_identity(selected_note)
                    if identity is not None and not _matches_identity(context, identity):
                        connection.rollback()
                        return CaptureResult("conflict", warnings=("The selected note belongs to another native session",))
                next_cursor = SourceCursor(batch.source_generation or cursor.generation,
                    batch.consumed_offset, batch.source_identity or cursor.source_identity,
                    batch.anchor_digest, {**batch.parser_state, "_capture_loss": source_loss}, cursor.historical,
                    batch.source_size, batch.source_complete)
                connection.execute("INSERT INTO source_sessions VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET "
                                   "descriptor=excluded.descriptor,cursor=excluded.cursor,note=excluded.note,"
                                   "state=excluded.state,completeness=excluded.completeness,messages=excluded.messages",
                                   (scope, json.dumps(_descriptor(context)), json.dumps(asdict(next_cursor)),
                                    str(selected_note) if selected_note else None, state, completeness, first_date, messages))
                connection.execute("INSERT OR IGNORE INTO source_owners VALUES (?,?)", (scope, context.host))
                connection.commit()
                _fault("after_source_retention")
                # Thresholds decide whether to start a note. Once retained,
                # that note must receive later facts and lifecycle changes.
                if selected_note is None or (not retained_note and
                        (not messages or messages < threshold or too_short)):
                    return CaptureResult("complete" if batch.status == "ok" else "pending",
                                         loss_of_input=batch.loss_of_input, warnings=batch.warnings)
                if time.monotonic() >= deadline:
                    return CaptureResult("pending", pending_sources=1, loss_of_input=batch.loss_of_input,
                                         warnings=batch.warnings + ("Facts retained; publication deadline reached",))
                metadata = {"agent_provider": context.host, "agent_session_id": context.native_session_id,
                            "capture_state": state, "capture_completeness": completeness}
                if not selected_note.exists():
                    metadata.update({"type": "claude-session", "date": first_date,
                                      "status": "auto-logged",
                                     "project": context.canonical_project_root.name,
                                     "session_id": context.native_session_id,
                                     "tags": ["claude/session"]})
                body = "\n".join(text for _, text in events)
                tag = hashlib.sha256(json.dumps([batch.source_generation, metadata, events],
                                                sort_keys=True).encode()).hexdigest()
                result = publish_events(context, "native", "stable", batch.consumed_offset, events,
                                        selected_note, metadata=metadata, checkpoint_tag=tag)
                snapshot_trigger = (("pre_compact:" + event.trigger) if event.trigger else "pre_compact") if event.kind == "pre_compact" else event.trigger
                snapshot_enabled = (event.kind == "pre_compact" and context.config.get("snapshot_on_compact", True)
                                    or snapshot_trigger == "clear" and context.config.get("snapshot_on_clear", True))
                if result.status == "complete" and snapshot_enabled:
                    result = _snapshot(context, selected_note, first_date, body, snapshot_trigger, deadline)
                pending_input = batch.status != "ok" and result.status == "complete"
                return replace(result, status="pending" if pending_input else result.status,
                               pending_sources=max(result.pending_sources, int(pending_input)),
                               loss_of_input=source_loss,
                               warnings=result.warnings + batch.warnings)
    except LockBusy as exc:
        return CaptureResult("pending", pending_sources=1, warnings=(str(exc),))
    except SessionLookupPending as exc:
        return CaptureResult("pending", pending_sources=1, warnings=(str(exc),))
    except (OSError, sqlite3.Error) as exc:
        return CaptureResult("pending", pending_sources=1, warnings=(type(exc).__name__,))
    except (ValueError, TypeError) as exc:
        return CaptureResult("conflict", warnings=(str(exc),))


def recover_registered(context, max_sources, deadline, include_active=False):
    """Replay only registered sources; never discover sources with a vault scan."""
    if not isinstance(max_sources, int) or max_sources < 0:
        raise ValueError("Recovery source bound must be nonnegative")
    try:
        with ownership_lock(context, deadline=deadline):
            with contextlib.closing(connect_coordination(context)) as connection:
                _schema(connection)
                position_row = connection.execute("SELECT position FROM recovery_positions WHERE host=?",
                                                  (context.host,)).fetchone()
                position = position_row[0] if position_row else 0
                # Rotate a bounded queue so the first eight sources cannot
                # starve later sources. No unbounded registry read is needed.
                rows = connection.execute("SELECT s.rowid,s.descriptor FROM source_sessions s "
                    "JOIN source_owners o ON s.scope=o.scope WHERE o.host=? AND (? OR s.state!='active') "
                    "ORDER BY (s.rowid<=?),s.rowid LIMIT ?",
                    (context.host, int(include_active), position, max_sources + 1)).fetchall()
                completed = 0
                warnings = []
                for position, descriptor in rows[:max_sources]:
                    if time.monotonic() >= deadline:
                        break
                    data = json.loads(descriptor)
                    recovered = replace(context, **{key: Path(value) if key in {
                        "canonical_project_root", "worktree", "transcript_path", "state_path", "config_path"
                    } and value is not None else value for key, value in data.items()})
                    pending = recover_pending(recovered, max_sources, deadline)
                    result = capture_checkpoint(recovered, CaptureEvent("recover"), deadline)
                    warnings.extend(pending.warnings + result.warnings)
                    connection.execute("INSERT INTO recovery_positions VALUES (?,?) ON CONFLICT(host) "
                                       "DO UPDATE SET position=excluded.position", (context.host, position))
                    connection.commit()
                    if pending.status == "complete" and result.status == "complete":
                        completed += 1
                remaining = len(rows) - completed
                return CaptureResult("pending" if remaining else "complete", pending_sources=remaining,
                                     warnings=tuple(warnings))
    except LockBusy as exc:
        return CaptureResult("pending", pending_sources=1, warnings=(str(exc),))
