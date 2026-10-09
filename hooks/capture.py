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
    notice_pending_sources: Optional[int] = None
    notice_warnings: Optional[Tuple[str, ...]] = None
    notice_loss_of_input: Optional[bool] = None


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
        CREATE TABLE IF NOT EXISTS native_snapshots (
            identity TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS native_events (
            scope TEXT, event TEXT, content TEXT NOT NULL, role TEXT NOT NULL,
            PRIMARY KEY(scope,event));
        CREATE TABLE IF NOT EXISTS source_owners (
            scope TEXT PRIMARY KEY, host TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS recovery_positions (
            host TEXT PRIMARY KEY, position INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS retired_capture_sources (
            scope TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS retired_source_versions (
            scope TEXT PRIMARY KEY, version TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS native_event_order (
            scope TEXT, event TEXT, identity TEXT, generation TEXT, offset INTEGER,
            PRIMARY KEY(scope,event));
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
    if body is None and phase == "committed":
        return CaptureResult("complete", revision)
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
            _commit_cursor(connection, checkpoint, current_revision, context)
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
    operation_context = context
    if owner and (Path(owner[0]) != context.canonical_project_root or Path(owner[1]) != context.worktree):
        operation_context = replace(context, canonical_project_root=Path(owner[0]),
                                    worktree=Path(owner[1]))
    changes = {"capture": body}
    if checkpoint_metadata:
        changes["metadata"] = checkpoint_metadata[0]
    if expected is None:
        from note_transactions import _render
        scaffold = "# Session\n\n## Summary\n\n## Key Decisions\n\n## Changes Made\n\n"
        changes = {"document": _render(scaffold, changes)}
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
    _commit_cursor(connection, checkpoint, result.revision, operation_context)
    return CaptureResult("complete", result.revision)


def _commit_cursor(connection, checkpoint, revision, context):
    identifier, scope, source, generation, offset = checkpoint[:5]
    connection.execute("""
        INSERT INTO cursors VALUES (?, ?, ?, ?)
        ON CONFLICT(scope, source, generation) DO UPDATE SET
            offset=MAX(cursors.offset, excluded.offset)
    """, (scope, source, generation, offset))
    connection.execute("UPDATE checkpoints SET phase='committed', revision=? WHERE id=?",
                       (revision, identifier))
    latest = connection.execute("SELECT id,operation FROM checkpoints WHERE scope=? AND note=? "
                                "AND phase='committed' ORDER BY rowid DESC LIMIT 1",
                                (scope, checkpoint[5])).fetchone()
    connection.execute("UPDATE checkpoints SET body=NULL WHERE scope=? AND note=? "
                       "AND phase='committed' AND id!=?", (scope, checkpoint[5], latest[0]))
    connection.commit()
    from note_transactions import compact_capture_operations
    compact_capture_operations(context, note_path=checkpoint[5], retain_operation_id=latest[1])


def pending_source_warnings(parser_state, source_host):
    """Return only controlled diagnostics from bounded retained row references."""
    import re
    warnings = []
    if source_host not in {"claude", "codex"} or not isinstance(parser_state, dict):
        return warnings
    if isinstance(parser_state.get("_opaque_drain"), dict):
        warnings.append("Oversized " + source_host.capitalize() + " transcript record retained")
    for reference in parser_state.get("_deferred_source_rows", ())[:32]:
        if not isinstance(reference, dict):
            continue
        reason = reference.get("reason", "")
        if not isinstance(reason, str):
            continue
        match = re.fullmatch(r"unknown_schema:([a-z][a-z0-9_-]{0,47})", reason)
        if match and not match[1].startswith(("sk-", "sk_", "ghp_", "github_pat_", "xox", "eyj")):
            warning = "Unknown substantive " + source_host.capitalize() + " transcript record (type=" + match[1] + ")"
        elif reason == "oversized:unrecognized":
            warning = "Oversized " + source_host.capitalize() + " transcript record retained"
        elif reason == "ambiguous_ownership":
            warning = "Deferred transcript rows remain pending"
        else:
            continue
        if warning not in warnings and len(warnings) < 4:
            warnings.append(warning)
    return warnings


def _partial_status(batch):
    """Describe unsupported types without copying any native row fields."""
    import re
    labels = []
    candidates = []
    for warning in batch.warnings[:64]:
        if not isinstance(warning, str):
            continue
        match = re.fullmatch(r"Unknown substantive (?:Claude|Codex) transcript record \(type=([a-z][a-z0-9_-]{0,47})\)", warning)
        if match:
            candidates.append(match[1])
        elif warning in {"Oversized Claude transcript record retained", "Oversized Codex transcript record retained"}:
            candidates.append("oversized")
    for reference in batch.parser_state.get("_deferred_source_rows", ())[:32]:
        reason = reference.get("reason", "")
        if isinstance(reason, str) and reason.startswith("unknown_schema:"):
            candidates.append(reason[len("unknown_schema:"):])
    for label in candidates:
        if (re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", label)
                and not label.startswith(("sk-", "sk_", "ghp_", "github_pat_", "xox", "eyj"))
                and label not in labels and len(labels) < 4):
            labels.append(label)
    return ("Capture status: partial; unresolved record type(s): " + ", ".join(labels) + "."
            if labels else "Capture status: partial; unresolved source input.")


def retained_statistics(context):
    """Read the selected retained session's bounded telemetry without creating state."""
    import math
    import sqlite3
    from note_transactions import coordination_location
    path = coordination_location(context) / "state.sqlite3"
    if not path.is_file():
        return 0, 0.0
    try:
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)) as connection:  # noqa: vault-db-connect — bounded read-only private capture telemetry
            row = connection.execute("SELECT s.messages,t.first,t.latest FROM source_sessions s "
                "LEFT JOIN capture_timing t ON t.scope=s.scope WHERE s.scope=?", (_scope(context),)).fetchone()
        if not row:
            return 0, 0.0
        count = row[0] if type(row[0]) is int and row[0] >= 0 else 0
        duration = max(0.0, (row[2]-row[1])/60) if row[1] is not None and row[2] is not None else 0.0
        return count, duration if math.isfinite(duration) else 0.0
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return 0, 0.0


def publish_events(context, source, generation, offset, events, note_path, *,
                   metadata=None, checkpoint_tag=None, native_order=False, capture_status=""):
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
                if native_order:
                    # The native caller supplies all retained facts in verified
                    # source order. Other callers keep their append semantics.
                    body = _native_body(safe_events)
                    if capture_status:
                        body += "\n\n" + capture_status
                else:
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
            "invocation_cwd": str(context.invocation_cwd or context.worktree),
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
        if key == "tags" and raw.startswith("\n"):
            raw = raw.lstrip()
        if key == "tags" and "\n" in line:
            value = [item.lstrip()[2:].strip().strip('"') for item in line.splitlines()[1:]]
            fields[key] = value
            continue
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw.strip("'\"")
        fields[key] = value
    return fields


def _matches_identity(context, fields):
    return (fields.get("agent_provider", "claude") == context.host and
            fields.get("agent_session_id", fields.get("session_id")) == context.native_session_id)


def _git_branch(context):
    """Read one bounded native worktree HEAD without starting Git."""
    gitdir = context.worktree / ".git"
    try:
        if gitdir.is_file():
            with gitdir.open() as stream:
                marker = stream.read(4096)
            if not marker.startswith("gitdir: "):
                return ""
            gitdir = (context.worktree / marker[8:].strip()).resolve()
        with (gitdir / "HEAD").open() as stream:
            head = stream.read(1024).strip()
        prefix = "ref: refs/heads/"
        branch = head[len(prefix):] if head.startswith(prefix) else ""
        return branch if len(branch) <= 512 and not any(ord(c) < 32 for c in branch) else ""
    except OSError:
        return ""


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


def planned_note_path(context, deadline=None):
    """Return the deterministic selected path without claiming that it exists."""
    import sqlite3
    from datetime import datetime, timezone
    from note_transactions import coordination_location
    deadline = deadline if deadline is not None else time.monotonic() + 0.1
    first_date = datetime.now(timezone.utc).date().isoformat()
    database = coordination_location(context) / "state.sqlite3"
    if database.is_file():
        with contextlib.closing(sqlite3.connect(database.as_uri()+"?mode=ro", uri=True, timeout=0)) as connection:  # noqa: vault-db-connect — bounded read-only selected planned filename
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_sessions'").fetchone():
                row = connection.execute("SELECT first_date FROM source_sessions WHERE scope=?", (_scope(context),)).fetchone()
                if row:
                    first_date = row[0]
    return _choose_note(context, first_date, deadline)


def _neutralize_dialogue(text):
    """Keep conversation text readable without adding note structure or links."""
    from obsidian_utils import escape_wikilinks
    return escape_wikilinks(text).replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ↵ ")


def _native_body(events):
    lines = []
    count = 0
    for _, text in events:
        if text == "Coordination message":
            count += 1
            continue
        if count:
            lines.append("Coordination message" if count == 1 else "Coordination messages (" + str(count) + ")")
            count = 0
        lines.append(text)
    if count:
        lines.append("Coordination message" if count == 1 else "Coordination messages (" + str(count) + ")")
    return "\n".join(lines)


def _snapshot_created_at():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _snapshot(context, note, first_date, body, trigger, deadline):
    if time.monotonic() >= deadline:
        return CaptureResult("pending", pending_sources=1,
                             warnings=("Snapshot deadline reached",))
    normalized = body if body.endswith("\n") else body + "\n"
    revision = hashlib.sha256(normalized.encode()).hexdigest()
    identity = hashlib.sha256((context.session_key + "\0" + revision + "\0" + trigger).encode()).hexdigest()
    folder = context.vault_path / str(context.config.get("sessions_folder", "claude-sessions"))
    from obsidian_utils import slugify
    project = context.project_name
    filename_project = slugify(context.canonical_project_root.name)
    path = folder / (first_date + "-" + filename_project + "-" + context.host + "-snapshot-" + identity + ".md")
    from note_transactions import _contained
    _contained(context, path)
    with ownership_lock(context), contextlib.closing(connect_coordination(context)) as connection:
        _schema(connection)
        row = connection.execute("SELECT created_at FROM native_snapshots WHERE identity=?", (identity,)).fetchone()
        created_at = row[0] if row else _snapshot_created_at()
        connection.execute("INSERT OR IGNORE INTO native_snapshots VALUES (?,?)", (identity, created_at))
        connection.commit()
    metadata = {"type": "claude-snapshot", "date": first_date, "created_at": created_at,
                "status": "auto-logged", "project": project,
                "session_id": context.native_session_id,
                "agent_provider": context.host, "agent_session_id": context.native_session_id,
                "source_session": context.native_session_id,
                "parent_session": "[[" + note.stem + "]]",
                "source_session_note": "[[" + note.stem + "]]", "source_revision": revision,
                "capture_revision": revision,
                "trigger": trigger, "tags": ["claude/snapshot", "claude/project/" + project, "claude/auto"]}
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
                try:
                    source_version = _source_version(context.transcript_path)
                except (OSError, ValueError, TypeError):
                    source_version = None
                batch = read_records(context, cursor, deadline)
                retained_warnings = pending_source_warnings(batch.parser_state, context.host)
                batch = replace(batch, warnings=tuple(dict.fromkeys((*batch.warnings, *retained_warnings))))
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
                    if record.role not in {"user", "assistant", "tool", "agent"} or not record.text:
                        continue
                    # Tool payloads can contain whole files and credentials. Keep
                    # parser fidelity, but publish only a fixed tool category.
                    if record.role == "agent":
                        text = "Coordination message"
                    elif record.role == "tool":
                        text = None
                    elif record.tool_name is not None:
                        category = record.tool_category if record.tool_category in {
                            "shell", "read", "edit", "write", "search", "agent", "web", "planning"
                        } else "tool"
                        text = "Assistant: Tool action (" + category + ")"
                    else:
                        safe = scrub_secrets(record.text).replace("<!-- obsidian-brain:", "&lt;!-- obsidian-brain:")
                        text = record.role.capitalize() + ": " + _neutralize_dialogue(safe)
                    identity_facts = [record.role, record.text, record.tool_name, record.tool_category]
                    if record.role == "agent":
                        identity_facts += [record.kind, record.source_actor, record.source_recipient]
                    digest = "sha256:" + hashlib.sha256(json.dumps(
                        identity_facts, separators=(",", ":")).encode()).hexdigest()
                    prior = connection.execute("SELECT content,role FROM native_events WHERE scope=? AND event=?",
                                               (scope, record.source_id)).fetchone()
                    legacy_text = record.role.capitalize() + ": " + scrub_secrets(record.text).replace(
                        "<!-- obsidian-brain:", "&lt;!-- obsidian-brain:")
                    if prior and prior not in {(digest, record.role), (legacy_text, record.role)}:
                        connection.rollback()
                        return CaptureResult("conflict", warnings=("A native event identity changed content",))
                    connection.execute("INSERT INTO native_events VALUES (?,?,?,?) "
                                       "ON CONFLICT(scope,event) DO UPDATE SET content=excluded.content",
                                       (scope, record.source_id, digest, record.role))
                    connection.execute("INSERT OR IGNORE INTO native_event_order VALUES (?,?,?,?,?)",
                        (scope, record.source_id, batch.source_identity, batch.source_generation, record.sequence))
                    if text is not None:
                        connection.execute("INSERT OR IGNORE INTO capture_events(scope,source,generation,event,text) "
                                           "VALUES (?,?,?,?,?) ON CONFLICT(scope,source,generation,event) DO UPDATE SET text=excluded.text",
                                           (scope, "native", "stable", record.source_id, text))
                    if record.timestamp:
                        try:
                            instant = datetime.fromisoformat(record.timestamp.replace("Z", "+00:00"))
                            timestamp = instant.replace(tzinfo=timezone.utc).timestamp() if instant.tzinfo is None else instant.timestamp()
                            connection.execute("INSERT INTO capture_timing VALUES (?,?,?) ON CONFLICT(scope) "
                                               "DO UPDATE SET first=MIN(first,excluded.first),latest=MAX(latest,excluded.latest)",
                                               (scope, timestamp, timestamp))
                        except (ValueError, OverflowError):
                            pass
                # Upgrade legacy retained visible facts in this selected scope.
                for identity, content, role in connection.execute(
                        "SELECT event,content,role FROM native_events WHERE scope=? AND content NOT LIKE 'sha256:%'",
                        (scope,)).fetchall():
                    if role != "tool":
                        connection.execute("INSERT OR IGNORE INTO capture_events(scope,source,generation,event,text) "
                                           "VALUES (?,?,?,?,?)", (scope, "native", "stable", identity, content))
                # Upgrade retained conversation formatting without changing event identities.
                for event_id, text in connection.execute(
                        "SELECT event,text FROM capture_events WHERE scope=? AND source='native' AND generation='stable'",
                        (scope,)).fetchall():
                    safe_text = _neutralize_dialogue(text)
                    if safe_text != text:
                        connection.execute("UPDATE capture_events SET text=? WHERE scope=? AND source='native' AND generation='stable' AND event=?",
                                           (safe_text, scope, event_id))
                events = connection.execute("SELECT event,text FROM capture_events "
                    "WHERE scope=? AND source='native' AND generation='stable' ORDER BY sequence", (scope,)).fetchall()
                positions = {event: (identity, generation, offset) for event, identity, generation, offset
                    in connection.execute("SELECT event,identity,generation,offset FROM native_event_order WHERE scope=?", (scope,))}
                # Sort only facts proven to belong to one frozen source generation.
                # UUIDs and naked offsets cannot establish order across rotations.
                groups = {(value[0], value[1]) for value in positions.values()}
                if len(groups) == 1 and all(event in positions for event, _ in events):
                    events.sort(key=lambda item: positions[item[0]][2])
                elif positions:
                    ordered = []
                    seen_positions = {}
                    for event_id, text in events:
                        position = positions.get(event_id)
                        if position:
                            group = position[:2]
                            if position[2] < seen_positions.get(group, -1):
                                text = "[Late replay from an earlier source position] " + text
                            seen_positions[group] = max(position[2], seen_positions.get(group, -1))
                        ordered.append((event_id, text))
                    events = ordered
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
                connection.execute("DELETE FROM retired_capture_sources WHERE scope=?", (scope,))
                connection.execute("DELETE FROM retired_source_versions WHERE scope=?", (scope,))
                connection.commit()
                _fault("after_source_retention")
                # Thresholds decide whether to start a note. Once retained,
                # that note must receive later facts and lifecycle changes.
                if selected_note is None or (not retained_note and
                        (not messages or messages < threshold or too_short)):
                    if state == "ended":
                        _retire_source(connection, scope, context, batch, completeness, source_version)
                    return CaptureResult("complete" if batch.status == "ok" else "pending",
                                         pending_sources=int(batch.status != "ok"),
                                         loss_of_input=batch.loss_of_input, warnings=batch.warnings)
                if time.monotonic() >= deadline:
                    return CaptureResult("pending", pending_sources=1, loss_of_input=batch.loss_of_input,
                                         warnings=batch.warnings + ("Facts retained; publication deadline reached",))
                metadata = {"agent_provider": context.host, "agent_session_id": context.native_session_id,
                            "capture_state": state, "capture_completeness": completeness,
                            "duration_minutes": duration / 60, "git_branch": _git_branch(context)}
                if not selected_note.exists():
                    project = context.project_name
                    metadata.update({"type": "claude-session", "date": first_date,
                                      "status": "auto-logged",
                                     "project": project,
                                     "project_path": str(context.canonical_project_root),
                                     "git_branch": _git_branch(context),
                                     "duration_minutes": duration / 60,
                                     "session_id": context.native_session_id,
                                     "tags": ["claude/session", "claude/project/" + project, "claude/auto"]})
                status_line = _partial_status(batch) if completeness == "partial" else ""
                body = _native_body(events)
                if status_line:
                    body += "\n\n" + status_line
                tag = hashlib.sha256(json.dumps([batch.source_generation, metadata, events, status_line],
                                                sort_keys=True).encode()).hexdigest()
                result = publish_events(context, "native", "stable", batch.consumed_offset, events,
                                        selected_note, metadata=metadata, checkpoint_tag=tag, native_order=True, capture_status=status_line)
                snapshot_trigger = (("pre_compact:" + event.trigger) if event.trigger else "pre_compact") if event.kind == "pre_compact" else event.trigger
                snapshot_enabled = (event.kind == "pre_compact" and context.config.get("snapshot_on_compact", True)
                                    or snapshot_trigger == "clear" and context.config.get("snapshot_on_clear", True))
                if result.status == "complete" and snapshot_enabled:
                    result = _snapshot(context, selected_note, first_date, body, snapshot_trigger, deadline)
                if result.status == "complete" and state == "ended":
                    _retire_source(connection, scope, context, batch, completeness, source_version)
                pending_input = batch.status != "ok" and result.status == "complete"
                retired_version = connection.execute("SELECT version FROM retired_source_versions WHERE scope=?",
                                                     (scope,)).fetchone()
                review_only = bool(retired_version and source_version is not None
                                   and json.loads(retired_version[0]) == source_version)
                return replace(result, status="pending" if pending_input else result.status,
                               pending_sources=max(result.pending_sources, int(pending_input and not review_only)),
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


def _source_version(path):
    """A cheap change detector; it never acknowledges or removes retained refs."""
    import stat
    path = Path(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise ValueError("The retained source is not a regular file")
    return [str(path.resolve()), before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns]


def _retire_source(connection, scope, context, batch, completeness, source_version):
    if completeness == "complete":
        if batch.source_complete:
            _retire_complete_source(connection, scope)
        return
    state = batch.parser_state
    refs = state.get("_deferred_source_rows", ())
    allowed_warnings = set(pending_source_warnings(state, context.host)) | {
        "Deferred transcript rows remain pending", "Oversized transcript bytes remain pending"}
    # Retirement changes automatic scheduling, never completeness or evidence.
    # Ownership ambiguity and unfinished byte verification require replay.
    if (batch.status != "partial" or batch.loss_of_input or state.get("_capture_loss")
            or state.get("_opaque_drain") or not refs
            or any(warning not in allowed_warnings for warning in batch.warnings)
            or batch.consumed_offset != batch.source_size
            or any(not isinstance(ref, dict) or not isinstance(ref.get("reason"), str)
                   or not (ref["reason"].startswith("unknown_schema:")
                           or ref["reason"] == "oversized:unrecognized") for ref in refs)):
        return
    try:
        version = _source_version(context.transcript_path)
    except (OSError, ValueError, TypeError):
        return
    if version != source_version or version[3] != batch.source_size:
        return
    if connection.execute("SELECT 1 FROM checkpoints WHERE scope=? AND phase!='committed' LIMIT 1",
                          (scope,)).fetchone():
        return
    connection.execute("INSERT OR REPLACE INTO retired_source_versions VALUES (?,?)",
                       (scope, json.dumps(version)))
    connection.execute("INSERT OR IGNORE INTO retired_capture_sources VALUES (?)", (scope,))
    connection.commit()


def _retire_complete_source(connection, scope):
    if not connection.execute("SELECT 1 FROM checkpoints WHERE scope=? AND phase!='committed' LIMIT 1",
                              (scope,)).fetchone():
        connection.execute("INSERT OR IGNORE INTO retired_capture_sources VALUES (?)", (scope,))
        connection.commit()


def recover_registered(context, max_sources, deadline, include_active=False, replay_retired=False):
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
                rows = connection.execute("SELECT s.rowid,s.descriptor,s.cursor,s.completeness,s.scope,v.version FROM source_sessions s "
                    "JOIN source_owners o ON s.scope=o.scope LEFT JOIN retired_source_versions v ON v.scope=s.scope WHERE o.host=? AND (? OR s.state!='active') "
                    "AND (v.version IS NOT NULL OR NOT EXISTS(SELECT 1 FROM retired_capture_sources r WHERE r.scope=s.scope)) "
                    "ORDER BY (s.rowid<=?),s.rowid LIMIT ?",
                    (context.host, int(include_active), position, max_sources + 1)).fetchall()
                pending_by_position = {position: int(completeness != "complete" and version is None)
                                       for position, descriptor, cursor, completeness, scope, version in rows}
                warnings_by_position = {position: pending_source_warnings(json.loads(cursor).get("parser_state", {}), context.host)
                                        if completeness != "complete" else []
                                        for position, descriptor, cursor, completeness, scope, version in rows}
                notice_pending = dict(pending_by_position)
                notice_loss = {}
                notice_positions = {position for position, descriptor, *_ in rows
                    if Path(json.loads(descriptor).get("canonical_project_root", "")) == context.canonical_project_root}
                for position, descriptor, cursor, completeness, scope, version in rows[:max_sources]:
                    if time.monotonic() >= deadline:
                        break
                    data = json.loads(descriptor)
                    recovered = replace(context, **{key: Path(value) if key in {
                        "canonical_project_root", "worktree", "invocation_cwd", "transcript_path", "state_path", "config_path"
                    } and value is not None else value for key, value in data.items()})
                    if version is not None and not replay_retired:
                        try:
                            unchanged = _source_version(recovered.transcript_path) == json.loads(version)
                        except (OSError, ValueError, TypeError):
                            unchanged = False
                        if unchanged:
                            pending_by_position[position] = 0
                            connection.execute("INSERT INTO recovery_positions VALUES (?,?) ON CONFLICT(host) "
                                               "DO UPDATE SET position=excluded.position", (context.host, position))
                            connection.commit()
                            continue
                        connection.execute("DELETE FROM retired_source_versions WHERE scope=?", (scope,))
                        connection.execute("DELETE FROM retired_capture_sources WHERE scope=?", (scope,))
                        connection.commit()
                    pending = recover_pending(recovered, max_sources, deadline)
                    result = capture_checkpoint(recovered, CaptureEvent("recover"), deadline)
                    warnings_by_position[position] = [*pending.warnings, *result.warnings]
                    connection.execute("INSERT INTO recovery_positions VALUES (?,?) ON CONFLICT(host) "
                                       "DO UPDATE SET position=excluded.position", (context.host, position))
                    connection.commit()
                    pending_by_position[position] = (max(1, pending.pending_sources, result.pending_sources)
                        if pending.status != "complete" or result.status != "complete" else 0)
                    notice_pending[position] = pending_by_position[position]
                    if pending.status == "complete" and result.status == "pending" and result.pending_sources == 0:
                        # Only verified retired review input has this result;
                        # conflicts and failed publication still need work.
                        notice_pending[position] = 0
                    notice_loss[position] = pending.loss_of_input or result.loss_of_input
                actual_pending = sum(pending_by_position.values())
                return CaptureResult("pending" if actual_pending else "complete", pending_sources=actual_pending,
                                     warnings=tuple(warning for values in warnings_by_position.values() for warning in values),
                                     notice_pending_sources=sum(notice_pending[position] for position in notice_positions),
                                     notice_loss_of_input=any(notice_loss.get(position, False) for position in notice_positions),
                                     notice_warnings=tuple(warning for position in notice_positions
                                         for warning in warnings_by_position[position]))
    except LockBusy as exc:
        return CaptureResult("pending", pending_sources=1, warnings=(str(exc),))
