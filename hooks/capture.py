"""Durable scrubbed checkpoints, note publication, and committed source cursors."""

import contextlib
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from note_transactions import (
    NoteMutation, apply_mutations, connect_coordination, ownership_lock,
    read_revision,
)


@dataclass(frozen=True)
class CaptureResult:
    status: str
    applied_revision: Optional[str] = None
    pending_sources: int = 0
    loss_of_input: bool = False
    warnings: Tuple[str, ...] = ()


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
        if current_capture and current_capture == published_capture:
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
            if current_capture and current_capture == earlier_capture:
                expected = current_revision
                break
        connection.execute("UPDATE checkpoints SET expected=?, phase='ready' WHERE id=?",
                           (expected, identifier))
        connection.commit()
    result = apply_mutations(context, [NoteMutation(Path(note), expected,
                                                   {"capture": body}, operation)])
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


def publish_events(context, source, generation, offset, events, note_path):
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
            if _cursor(connection, scope, source, generation) >= offset:
                return CaptureResult("complete", read_revision(context, note_path))
            key = hashlib.sha256(json.dumps(
                [scope, source, generation, offset], separators=(",", ":"),
            ).encode()).hexdigest()
            checkpoint = connection.execute("SELECT * FROM checkpoints WHERE id=?", (key,)).fetchone()
            if checkpoint is None:
                expected = read_revision(context, note_path)
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
                "SELECT * FROM checkpoints WHERE scope=? AND phase!='committed' ORDER BY rowid", (scope,),
            ).fetchall()
            completed = 0
            for row in rows:
                if completed >= max_sources or time.monotonic() >= deadline:
                    break
                result = _publish(context, connection, row)
                if result.status != "complete":
                    return result
                revision = result.applied_revision
                completed += 1
            pending = len(rows) - completed
            return CaptureResult("pending" if pending else "complete", revision, pending)
