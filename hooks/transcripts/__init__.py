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
    source_actor: Optional[str] = None
    source_recipient: Optional[str] = None


@dataclass(frozen=True)
class RawRecord:
    offset: int
    end_offset: int
    data: Mapping
    unparsed_reason: Optional[str] = None


@dataclass(frozen=True)
class DeferredRow:
    offset: int
    end_offset: int
    reason: str


@dataclass(frozen=True)
class ParsedRecords:
    records: Tuple[SourceRecord, ...] = ()
    metadata: Mapping = field(default_factory=dict)
    status: str = "ok"
    warnings: Tuple[str, ...] = ()
    blocked_offset: Optional[int] = None
    parser_state: Mapping = field(default_factory=dict)
    deferred_rows: Tuple[DeferredRow, ...] = ()


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
MAX_DEFERRED_ROWS = 32
MAX_DEFERRED_REPLAY_BYTES = 1024 * 1024


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


MAX_OPAQUE_CHUNKS = 256
MAX_OPAQUE_BYTES = MAX_OPAQUE_CHUNKS * MAX_RECORD_BYTES


def _source_version(info):
    return [info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _drain_opaque(stream, value, identity, generation, offset, deadline, budget,
                  verification_budget=None):
    """Verify bounded chunk hashes before advancing an uninterpreted row."""
    import hashlib
    import os
    import time
    keys = {'source_identity', 'generation', 'start', 'cursor', 'chunk_sha256',
            'chunk_lengths', 'complete', 'end', 'verification_cursor', 'verification_version'}
    if value is None:
        value = dict(source_identity=identity, generation=generation, start=offset,
            cursor=offset, chunk_sha256=[], chunk_lengths=[], complete=False, end=None,
            verification_cursor=0, verification_version=None)
    else:
        value = dict(value)
    lengths = value.get('chunk_lengths')
    hashes = value.get('chunk_sha256')
    import re
    if (set(value) != keys or value['source_identity'] != identity or value['generation'] != generation
            or type(value['start']) is not int or value['start'] < 0
            or type(value['cursor']) is not int or value['cursor'] != offset
            or not isinstance(lengths, list) or not isinstance(hashes, list)
            or len(lengths) != len(hashes) or len(lengths) > MAX_OPAQUE_CHUNKS
            or any(type(n) is not int or not 0 < n <= MAX_RECORD_BYTES for n in lengths)
            or any(not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h) for h in hashes)
            or value['cursor'] != value['start'] + sum(lengths)
            or type(value['complete']) is not bool
            or (type(value['end']) is not int or value['end'] != value['cursor'] if value['complete'] else value['end'] is not None)
            or type(value['verification_cursor']) is not int
            or not 0 <= value['verification_cursor'] <= len(lengths)
            or (value['verification_version'] is not None and
                (not isinstance(value['verification_version'], list) or len(value['verification_version']) != 3
                 or any(type(n) is not int or n < 0 for n in value['verification_version'])))):
        raise ValueError('Invalid opaque drain state')
    lengths = list(lengths)
    hashes = list(hashes)
    value['chunk_lengths'] = lengths
    value['chunk_sha256'] = hashes
    version = _source_version(os.fstat(stream.fileno()))
    if value['verification_version'] != version:
        value['verification_cursor'] = 0
        value['verification_version'] = version
    # Full-prefix checks get a separate finite allowance. An append between
    # hooks must not consume the entire forward budget rechecking old bytes.
    if verification_budget is None:
        verification_budget = [MAX_OPAQUE_BYTES]
    used = 0
    for index in range(value['verification_cursor'], len(lengths)):
        count = lengths[index]
        if count > verification_budget[0] or time.monotonic() >= deadline:
            return value, used, False
        stream.seek(value['start'] + sum(lengths[:index]))
        chunk = stream.read(count)
        verification_budget[0] -= len(chunk)
        if len(chunk) != count or hashlib.sha256(chunk).hexdigest() != hashes[index]:
            raise ValueError('Opaque source bytes changed')
        value['verification_cursor'] = index + 1
    if not value['complete']:
        stream.seek(value['cursor'])
        while used < budget and time.monotonic() < deadline:
            if sum(lengths) >= MAX_OPAQUE_BYTES or len(lengths) >= MAX_OPAQUE_CHUNKS:
                return value, used, False
            chunk = stream.readline(min(MAX_RECORD_BYTES, budget-used, MAX_OPAQUE_BYTES-sum(lengths)))
            if not chunk:
                break
            lengths.append(len(chunk))
            hashes.append(hashlib.sha256(chunk).hexdigest())
            value['cursor'] += len(chunk)
            value['verification_cursor'] = len(lengths)
            used += len(chunk)
            if chunk.endswith(b'\n'):
                value['complete'] = True
                value['end'] = value['cursor']
                break
    if _source_version(os.fstat(stream.fileno())) != version:
        value['verification_cursor'] = 0
        value['verification_version'] = None
        return value, used, False
    return value, used, value['complete']


def _discover_codex_child_metadata(stream, opaque, identity, generation, header, context, deadline):
    """Inspect bounded framing without acknowledging inherited source bytes."""
    import os
    import time
    version = _source_version(os.fstat(stream.fileno()))
    allowance = [MAX_OPAQUE_BYTES]
    copy = dict(opaque)
    try:
        reserve = sum(copy['chunk_lengths'])
        copy, used, ready = _drain_opaque(stream, copy, identity, generation,
            copy['cursor'], deadline, max(0, allowance[0]-reserve), allowance)
        allowance[0] -= used
        if not ready:
            return None
        stream.seek(copy['end'])
        while allowance[0] > 0 and time.monotonic() < deadline:
            start = stream.tell()
            line = stream.readline(min(MAX_RECORD_BYTES+1, allowance[0]))
            allowance[0] -= len(line)
            if not line or len(line) > MAX_RECORD_BYTES or not line.endswith(b'\n'):
                return None
            data = _strict_json(line)
            if data.get('type') != 'session_meta':
                continue
            child = data.get('payload')
            parent = header.data.get('payload') or {}
            if (not isinstance(child, dict)
                    or (child.get('id') or child.get('session_id')) != context.native_session_id
                    or child.get('forked_from_id') != (parent.get('id') or parent.get('session_id'))
                    or _source_version(os.fstat(stream.fileno())) != version):
                return None
            return RawRecord(start, stream.tell(), data)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return None
    return None


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
    state = dict(cursor.parser_state)
    retained_refs = [dict(ref) if isinstance(ref, dict) else ref for ref in state.get("_deferred_source_rows", ())]
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
        if retained_refs:
            warnings.append("Deferred transcript source changed; input remains pending")
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
            state = {"_deferred_source_rows": retained_refs} if retained_refs else {}
            loss = True
            warnings.append("Transcript generation changed; source restarted with possible input loss")
        elif not generation:
            generation = identity
        # Replay only retained byte ranges whose selected source and hash still
        # match. Raw bodies live in the original source, never in cursor state.
        opaque = state.get("_opaque_drain")
        verification_budget = [MAX_OPAQUE_BYTES]
        replay_rows = []
        verified_refs = {}
        replay_bytes = 0
        position = int(state.get("_deferred_replay_position", 0)) % max(1, len(retained_refs))
        ordered_refs = retained_refs[position:] + retained_refs[:position]
        attempted = 0
        for ref in ordered_refs[:MAX_DEFERRED_ROWS]:
            if time.monotonic() >= deadline:
                break
            attempted += 1
            try:
                if ref.get("reason") == "oversized:unrecognized":
                    if (ref.get('digest_kind') != 'sha256-chunks-v1'
                            or ref['source_path'] != str(path) or ref['source_identity'] != identity
                            or ref['source_generation'] != generation):
                        raise ValueError('Opaque deferred identity changed')
                    check = dict(source_identity=identity, generation=generation, start=ref['offset'],
                        cursor=ref['end_offset'], end=ref['end_offset'], complete=True,
                        chunk_lengths=ref['chunk_lengths'], chunk_sha256=ref['chunk_sha256'],
                        verification_cursor=ref.get('verification_cursor', 0),
                        verification_version=ref.get('verification_version'))
                    check, used, _ = _drain_opaque(stream, check, identity, generation,
                        ref['end_offset'], deadline, MAX_DEFERRED_REPLAY_BYTES-replay_bytes,
                        verification_budget)
                    replay_bytes += used
                    ref['verification_cursor'] = check['verification_cursor']
                    ref['verification_version'] = check['verification_version']
                    warnings.append("Oversized transcript bytes remain pending")
                    continue
                start, end = ref["offset"], ref["end_offset"]
                if (ref["source_path"] != str(path) or ref["source_identity"] != identity
                        or ref["source_generation"] != generation
                        or type(start) is not int or type(end) is not int
                        or start < 0 or not 0 < end-start <= MAX_RECORD_BYTES):
                    raise ValueError("Deferred identity changed")
                if replay_bytes + end-start > MAX_DEFERRED_REPLAY_BYTES:
                    # Start next time at the row we could not fit this time.
                    attempted -= 1
                    break
                stream.seek(start)
                raw = stream.read(end-start)
                replay_bytes += len(raw)
                if (len(raw) != end-start or not raw.endswith(b"\n")
                        or hashlib.sha256(raw).hexdigest() != ref["row_sha256"]):
                    raise ValueError("Deferred bytes changed")
                replay_rows.append(RawRecord(start, end, _strict_json(raw)))
                verified_refs[start] = ref
            except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
                warnings.append("Deferred transcript source changed; input remains pending")
        next_position = (position + max(1, attempted)) % max(1, len(retained_refs))
        stream.seek(consumed)
        rows = []
        row_hashes = {}
        opaque_manifests = {}
        status = "ok"
        read_bytes = replay_bytes
        while True:
            if read_bytes >= MAX_BATCH_BYTES:
                warnings.append("Transcript batch limit reached")
                status = "partial"
                break
            if time.monotonic() >= deadline:
                warnings.append("Transcript deadline reached")
                status = "partial"
                break
            start = stream.tell()
            if opaque is not None:
                try:
                    opaque, used, ready = _drain_opaque(stream, opaque, identity, generation,
                                                       consumed, deadline, MAX_BATCH_BYTES-read_bytes,
                                                       verification_budget)
                except (ValueError, TypeError, KeyError):
                    warnings.append("Opaque transcript source changed; input remains pending")
                    status = "partial"
                    break
                read_bytes += used
                consumed = opaque['cursor']
                warnings.append("Oversized transcript bytes remain pending")
                status = "partial"
                if not ready:
                    if len(opaque['chunk_lengths']) >= MAX_OPAQUE_CHUNKS or sum(opaque['chunk_lengths']) >= MAX_OPAQUE_BYTES:
                        warnings.append("Opaque transcript drain limit reached; input remains pending")
                    break
                rows.append(RawRecord(opaque['start'], opaque['end'], {}, "oversized"))
                opaque_manifests[opaque['start']] = opaque
                opaque = None
                stream.seek(consumed)
                if read_bytes >= MAX_BATCH_BYTES:
                    break
                continue
            line = stream.readline(MAX_RECORD_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_RECORD_BYTES:
                read_bytes += len(line)
                opaque = dict(source_identity=identity, generation=generation, start=start,
                    cursor=start, chunk_sha256=[], chunk_lengths=[], complete=False, end=None,
                    verification_cursor=0, verification_version=None)
                stream.seek(start)
                continue
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
            row_hashes[start] = hashlib.sha256(line).hexdigest()
            read_bytes += len(line)
            consumed = end
        replayed_offsets = set()
        try:
            combined = {row.offset: row for row in replay_rows}
            combined.update((row.offset, row) for row in rows)
            parsed = parser(context, tuple(rows), header,
                            {**state, "_source_generation": generation})
            if (context.host == 'codex' and opaque is not None
                    and parsed.blocked_offset == header.offset
                    and parsed.warnings == ('Selected Codex session metadata is pending',)):
                selected = _discover_codex_child_metadata(stream, opaque, identity, generation,
                    header, context, deadline)
                if selected is not None:
                    parsed = parser(context, tuple(rows), header,
                        {**state, '_source_generation': generation}, selected_metadata=selected)
            if opaque_manifests:
                opaque_offsets = set(opaque_manifests)
                already_deferred = {row.offset for row in parsed.deferred_rows}
                parsed = replace(parsed,
                    records=tuple(record for record in parsed.records if record.sequence not in opaque_offsets),
                    deferred_rows=parsed.deferred_rows + tuple(
                        DeferredRow(row.offset, row.end_offset, "oversized:unrecognized")
                        for row in rows if row.offset in opaque_offsets and row.offset not in already_deferred),
                    status="partial" if parsed.status == "ok" else parsed.status)

            if replay_rows and time.monotonic() < deadline:
                # Historical deferrals must not revoke a proof renewed later in
                # the source. Replay sees current proof but cannot mutate it.
                replay_state = {**parsed.parser_state, "_source_generation": generation}
                if any(ref.get('reason') == 'ambiguous_ownership' for ref in verified_refs.values()):
                    # A later section proof cannot retroactively attribute an
                    # earlier untagged row. Its own native proof is required.
                    replay_state['requires_session_proof'] = True
                replay = parser(context, tuple(replay_rows), header, replay_state)
                replayed_offsets = {row.offset for row in replay_rows
                    if replay.blocked_offset is None or row.offset < replay.blocked_offset}
                merged_status = parsed.status if replay.status == "ok" else (
                    replay.status if parsed.status == "ok" else parsed.status)
                blocked = parsed.blocked_offset
                if replay.blocked_offset is not None:
                    warnings.append("Deferred transcript rows remain pending")
                    merged_status = "partial" if merged_status == "ok" else merged_status
                proof_state = dict(parsed.parser_state)
                if context.host == "codex" and "seen_ids" in proof_state:
                    # Stable ID knowledge can grow; adjacency and ownership
                    # proofs remain those of the current forward cursor.
                    proof_state["seen_ids"] = list(dict.fromkeys(
                        list(proof_state["seen_ids"]) + list(replay.parser_state.get("seen_ids", ()))))[:2048]
                parsed = replace(parsed, parser_state=proof_state,
                    records=tuple(sorted(parsed.records + replay.records, key=lambda record: record.sequence)),
                    metadata={**replay.metadata, **parsed.metadata}, status=merged_status,
                    warnings=tuple(dict.fromkeys(parsed.warnings + replay.warnings)),
                    blocked_offset=blocked,
                    deferred_rows=parsed.deferred_rows + replay.deferred_rows)
        except (ValueError, TypeError, KeyError, RecursionError):
            warnings.append("Transcript adapter rejected the source")
            consumed = rows[0].offset if rows else consumed
            saved_drain = state.get('_opaque_drain')
            if saved_drain is not None and consumed != saved_drain.get('cursor'):
                state.pop('_opaque_drain', None)
            return result("unsupported")
        warnings.extend(parsed.warnings)
        import re
        deferred = {row.offset: row for row in parsed.deferred_rows}
        # A supported metadata row may legitimately emit no chat record. It is
        # resolved only when parsing positively succeeds without a deferral.
        remaining = []
        for ref in retained_refs:
            start = ref.get("offset")
            if (start in replayed_offsets and start not in deferred
                    and parsed.status == "ok" and parsed.blocked_offset is None):
                continue
            if (start in replayed_offsets and start not in deferred
                    and any(record.sequence == start for record in parsed.records)):
                continue
            if (start in replayed_offsets and start not in deferred
                    and parsed.blocked_offset is None and time.monotonic() < deadline):
                try:
                    row = next(row for row in replay_rows if row.offset == start)
                    understood = parser(context, (row,), header,
                        {**parsed.parser_state, "_source_generation": generation})
                    if understood.status == "ok" and not understood.deferred_rows and understood.blocked_offset is None:
                        continue
                except (ValueError, TypeError, KeyError, RecursionError):
                    pass
            remaining.append(ref)
        existing = {(ref.get("source_identity"), ref.get("source_generation"), ref.get("offset"))
                    for ref in remaining}
        for row in parsed.deferred_rows:
            reason = row.reason
            if not (reason == "ambiguous_ownership" or
                    (reason == "oversized:unrecognized" and row.offset in opaque_manifests) or
                    re.fullmatch(r"unknown_schema:[a-z][a-z0-9_-]{0,47}", reason)):
                consumed = min(consumed, row.offset)
                warnings.append("Unverified transcript deferral remains pending")
                status = "partial"
                continue
            raw_row = combined.get(row.offset)
            if raw_row is None or raw_row.end_offset != row.end_offset:
                consumed = min(consumed, row.offset)
                status = "partial"
                continue
            key = (identity, generation, row.offset)
            if key in existing:
                continue
            if len(remaining) >= MAX_DEFERRED_ROWS:
                consumed = min(consumed, row.offset)
                warnings.append("Deferred transcript reference limit reached")
                status = "partial"
                continue
            if reason == "oversized:unrecognized" and row.offset in opaque_manifests:
                manifest = opaque_manifests[row.offset]
                remaining.append({"source_path": str(path), "source_identity": identity,
                    "source_generation": generation, "offset": row.offset,
                    "end_offset": row.end_offset, "reason": reason,
                    "digest_kind": "sha256-chunks-v1", "chunk_lengths": manifest['chunk_lengths'],
                    "chunk_sha256": manifest['chunk_sha256']})
                existing.add(key)
                continue
            digest = row_hashes.get(row.offset)
            if digest is None and row.offset in verified_refs:
                digest = verified_refs[row.offset]["row_sha256"]
            if digest is None:
                consumed = min(consumed, row.offset)
                status = "partial"
                continue
            remaining.append({"source_path": str(path), "source_identity": identity,
                "source_generation": generation, "offset": row.offset,
                "end_offset": row.end_offset, "row_sha256": digest, "reason": reason})
            existing.add(key)
        state = dict(parsed.parser_state)
        state.pop('_opaque_drain', None)
        if remaining:
            state["_deferred_source_rows"] = remaining
            state["_deferred_replay_position"] = next_position
            status = "partial"
            warnings.append("Deferred transcript rows remain pending")
        if parsed.status != "ok":
            status = parsed.status if status == "ok" or parsed.status in {"unsupported", "unavailable"} else status
        if parsed.blocked_offset is not None:
            consumed = min(consumed, parsed.blocked_offset)
            if status == "ok":
                status = "partial"
        if opaque is not None and consumed == opaque['cursor']:
            state['_opaque_drain'] = opaque
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
