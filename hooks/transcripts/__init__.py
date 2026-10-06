"""Bounded native transcript records and explicit-host source reading."""
from dataclasses import dataclass, field, replace
from typing import Mapping, Optional, Tuple


@dataclass(frozen=True)
class SourceCursor:
    generation: Optional[str] = None
    offset: int = 0
    source_identity: Optional[str] = None
    anchor_digest: str = ""
    parser_state: Mapping = field(default_factory=dict)
    historical: bool = False
    known_size: int = 0
    exhausted: bool = False


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    role: str
    text: str
    sequence: int
    timestamp: Optional[str] = None
    kind: str = "message"
    tool_name: Optional[str] = None
    tool_category: Optional[str] = None
    turn_id: Optional[str] = None


@dataclass(frozen=True)
class RawRecord:
    offset: int
    end_offset: int
    data: Mapping


@dataclass(frozen=True)
class ParsedRecords:
    records: Tuple[SourceRecord, ...] = ()
    metadata: Mapping = field(default_factory=dict)
    status: str = "ok"
    warnings: Tuple[str, ...] = ()
    blocked_offset: Optional[int] = None
    parser_state: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class TranscriptBatch:
    status: str
    records: Tuple[SourceRecord, ...]
    source_generation: str
    consumed_offset: int
    warnings: Tuple[str, ...] = ()
    metadata: Mapping = field(default_factory=dict)
    source_identity: str = ""
    anchor_digest: str = ""
    source_complete: bool = False
    loss_of_input: bool = False
    parser_state: Mapping = field(default_factory=dict)
    source_size: int = 0


def read_records(context, cursor, deadline):
    """Dispatch only from the context's explicit host."""
    if context.host == "claude":
        from .claude import parse_rows
    elif context.host == "codex":
        from .codex import parse_rows
    else:
        return TranscriptBatch("unsupported", (), cursor.generation or "", cursor.offset,
                               ("Unsupported transcript host",))
    return read_source(context, cursor, deadline, parse_rows)


MAX_BATCH_BYTES = 4 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024


def _strict_json(line):
    import json
    def reject(value):
        raise ValueError("Nonfinite JSON number")
    value = json.loads(line.decode("utf-8"), parse_constant=reject)
    if not isinstance(value, dict):
        raise ValueError("Transcript row must be an object")
    return value


def _anchor(stream, offset):
    import hashlib
    stream.seek(max(0, offset - 64))
    return hashlib.sha256(stream.read(min(64, offset))).hexdigest()


def _read_source(context, cursor, deadline, parser):
    """Read a bounded strict JSONL batch, preserving incomplete source bytes."""
    import hashlib
    import os
    import stat
    import time
    import uuid
    from pathlib import Path

    generation = cursor.generation or ""
    identity = cursor.source_identity or ""
    consumed = cursor.offset
    anchor = cursor.anchor_digest
    state = cursor.parser_state
    size = 0
    loss = False
    warnings = []

    def result(status, records=(), metadata=None, complete=False):
        return TranscriptBatch(status, tuple(records), generation, consumed, tuple(warnings),
                               metadata or {}, identity, anchor, complete, loss, state, size)

    if not isinstance(cursor.offset, int) or cursor.offset < 0:
        warnings.append("Invalid transcript cursor")
        return result("unsupported")
    if time.monotonic() >= deadline:
        warnings.append("Transcript deadline reached before reading")
        return result("partial")
    if context.host not in {"claude", "codex"}:
        warnings.append("Unsupported transcript host")
        return result("unsupported")
    try:
        if context.transcript_path is None:
            raise ValueError("Native transcript path is unavailable")
        path = Path(context.transcript_path).resolve()
        from runtime_adapters import selected_home
        native_home = selected_home(context.host, context)
        roots = [native_home / "projects"] if context.host == "claude" else [native_home / "sessions", native_home / "archived_sessions"]
        if not cursor.historical and not any(path.is_relative_to(root.resolve()) for root in roots):
            raise ValueError("Transcript is outside the selected native storage")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except (OSError, ValueError) as exc:
        warnings.append("Native transcript unavailable: " + type(exc).__name__)
        loss = isinstance(exc, FileNotFoundError) and bool(cursor.source_identity or cursor.offset)
        return result("unavailable")
    with os.fdopen(fd, "rb") as stream:
        source_stat = os.fstat(stream.fileno())
        size = source_stat.st_size
        if not stat.S_ISREG(source_stat.st_mode):
            warnings.append("Native transcript is not a regular file")
            return result("unavailable")
        try:
            current = path.stat()
            if (current.st_dev, current.st_ino) != (source_stat.st_dev, source_stat.st_ino):
                raise ValueError("Transcript changed during opening")
            if not cursor.historical and not any(path.resolve().is_relative_to(root.resolve()) for root in roots):
                raise ValueError("Transcript escaped native storage")
        except (OSError, ValueError):
            warnings.append("Native transcript changed during opening")
            return result("unavailable")
        if time.monotonic() >= deadline:
            warnings.append("Transcript deadline reached")
            return result("partial")
        first_line = stream.readline(MAX_RECORD_BYTES + 1)
        if len(first_line) > MAX_RECORD_BYTES:
            warnings.append("Transcript header exceeds the record limit")
            return result("unsupported")
        if not first_line or not first_line.endswith(b"\n"):
            warnings.append("Transcript header is empty or incomplete")
            return result("partial")
        try:
            header = RawRecord(0, len(first_line), _strict_json(first_line))
        except (ValueError, UnicodeError, RecursionError):
            warnings.append("Malformed transcript header")
            return result("unsupported")
        actual_identity = hashlib.sha256((str(source_stat.st_dev) + ":" + str(source_stat.st_ino) + ":" +
                                          hashlib.sha256(first_line).hexdigest()).encode()).hexdigest()
        changed = ((cursor.source_identity is not None and cursor.source_identity != actual_identity)
                   or size < cursor.offset or size < cursor.known_size
                   or (cursor.anchor_digest and cursor.offset <= size and _anchor(stream, cursor.offset) != cursor.anchor_digest))
        identity = actual_identity
        if changed:
            generation = hashlib.sha256((identity + uuid.uuid4().hex).encode()).hexdigest()
            consumed = 0
            state = {}
            loss = True
            warnings.append("Transcript generation changed; source restarted with possible input loss")
        elif not generation:
            generation = identity
        stream.seek(consumed)
        rows = []
        status = "ok"
        read_bytes = 0
        while True:
            if time.monotonic() >= deadline:
                warnings.append("Transcript deadline reached")
                status = "partial"
                break
            start = stream.tell()
            line = stream.readline(MAX_RECORD_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_RECORD_BYTES:
                warnings.append("Transcript record exceeds the record limit")
                status = "partial" if rows else "unsupported"
                break
            if read_bytes + len(line) > MAX_BATCH_BYTES:
                warnings.append("Transcript batch limit reached")
                status = "partial"
                break
            if not line.endswith(b"\n"):
                warnings.append("Incomplete final transcript line retained")
                status = "partial"
                break
            try:
                data = _strict_json(line)
            except (ValueError, UnicodeError, RecursionError):
                warnings.append("Malformed transcript row retained")
                status = "partial" if rows else "unsupported"
                break
            end = stream.tell()
            rows.append(RawRecord(start, end, data))
            read_bytes += len(line)
            consumed = end
        try:
            parsed = parser(context, tuple(rows), header, {**state, "_source_generation": generation})
        except (ValueError, TypeError, KeyError, RecursionError):
            warnings.append("Transcript adapter rejected the source")
            consumed = rows[0].offset if rows else consumed
            return result("unsupported")
        warnings.extend(parsed.warnings)
        state = parsed.parser_state
        if parsed.status != "ok":
            status = parsed.status if status == "ok" or parsed.status in {"unsupported", "unavailable"} else status
        if parsed.blocked_offset is not None:
            consumed = min(consumed, parsed.blocked_offset)
            if status == "ok":
                status = "partial"
        records = tuple(replace(record, source_id="offset:" + generation + ":" + record.source_id[7:])
                        if record.source_id.startswith("offset:") else record
                        for record in parsed.records if record.sequence < consumed)
        size = os.fstat(stream.fileno()).st_size
        anchor = _anchor(stream, consumed)
        complete = status == "ok" and consumed == size
        return result(status, records, parsed.metadata, complete)



def read_source(context, cursor, deadline, parser):
    """Report transient source I/O errors without acknowledging unread bytes."""
    try:
        return _read_source(context, cursor, deadline, parser)
    except OSError:
        return TranscriptBatch("unavailable", (), cursor.generation or "", cursor.offset,
                               ("Native transcript could not be read",),
                               source_identity=cursor.source_identity or "",
                               anchor_digest=cursor.anchor_digest, parser_state=cursor.parser_state,
                               source_size=cursor.known_size)
