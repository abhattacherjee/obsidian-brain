"""Bounded sources never acknowledge unread or unsupported bytes."""
import json
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from transcripts import ParsedRecords, SourceCursor, SourceRecord, read_source


@pytest.fixture
def source_case(tmp_path, monkeypatch):
    native = tmp_path / "native"
    path = native / "projects" / "repo" / "session.jsonl"
    path.parent.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(native))
    context = SimpleNamespace(host="claude", native_session_id="native-session", transcript_path=path)
    return context, path


def parser(context, rows, header, state):
    return ParsedRecords(tuple(SourceRecord(str(row.offset), "user", row.data.get("text", ""), row.offset)
                               for row in rows), {"session_id": header.data.get("sessionId")}, parser_state={"id": "safe"})


def write_rows(path, rows):
    path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))


def read(context, cursor=SourceCursor(), callback=parser):
    return read_source(context, cursor, time.monotonic() + 2, callback)


def cursor_for(batch):
    return SourceCursor(batch.source_generation, batch.consumed_offset, batch.source_identity,
                        batch.anchor_digest, batch.parser_state, known_size=batch.source_size,
                        exhausted=batch.source_complete)


def test_complete_source_and_append_preserve_identity(source_case):
    context, path = source_case
    write_rows(path, [{"sessionId": "native-session", "text": "first"}])
    first = read(context)
    assert first.status == "ok" and first.source_complete
    assert first.consumed_offset == path.stat().st_size
    with path.open("ab") as stream:
        stream.write(b'{"text":"second"}\n')
    second = read(context, cursor_for(first))
    assert second.source_generation == first.source_generation
    assert second.source_identity == first.source_identity
    assert [record.text for record in second.records] == ["second"]
    assert second.metadata["session_id"] == "native-session"
    json.dumps(asdict(second))


def test_partial_final_line_is_not_consumed(source_case):
    context, path = source_case
    first_line = b'{"sessionId":"native-session","text":"first"}\n'
    path.write_bytes(first_line + b'{"text":"sec')
    batch = read(context)
    assert batch.status == "partial" and not batch.source_complete
    assert batch.consumed_offset == len(first_line)
    with path.open("ab") as stream:
        stream.write(b'ond"}\n')
    following = read(context, cursor_for(batch))
    assert [record.text for record in following.records] == ["second"]


def test_deadline_does_not_consume_source(source_case):
    context, path = source_case
    write_rows(path, [{"text": "unread"}])
    batch = read_source(context, SourceCursor(), time.monotonic() - 1, parser)
    assert batch.status == "partial" and batch.consumed_offset == 0
    assert not batch.source_complete


@pytest.mark.parametrize("change", ["rotation", "truncate", "rewrite_tail"])
def test_source_changes_reset_generation_with_visible_loss(source_case, change):
    context, path = source_case
    write_rows(path, [{"sessionId": "native-session", "text": "first"}, {"text": "second"}])
    first = read(context)
    if change == "rotation":
        path.rename(path.with_suffix(".old"))
        write_rows(path, [{"sessionId": "native-session", "text": "replacement"}])
    elif change == "truncate":
        write_rows(path, [{"sessionId": "native-session", "text": "first"}])
    else:
        path.write_bytes(path.read_bytes().replace(b'second', b'edited'))
    second = read(context, cursor_for(first))
    assert second.source_generation != first.source_generation
    assert second.loss_of_input and second.warnings
    assert second.records


def test_live_containment_rejects_symlink_escape_and_history_is_explicit(source_case, tmp_path):
    context, path = source_case
    outside = tmp_path / "historical.jsonl"
    write_rows(outside, [{"sessionId": "native-session", "text": "import"}])
    path.symlink_to(outside)
    assert read(context).status == "unavailable"
    history = read(context, SourceCursor(historical=True))
    assert history.status == "ok" and history.records


def test_disappeared_source_is_loss_not_success(source_case):
    context, path = source_case
    write_rows(path, [{"text": "first"}])
    first = read(context)
    path.unlink()
    missing = read(context, cursor_for(first))
    assert missing.status == "unavailable" and missing.loss_of_input


@pytest.mark.parametrize("bad", [b'not json\n', b'[]\n', b'{"x":NaN}\n', b'\xff\n'])
def test_strict_bad_source_never_consumes_invalid_record(source_case, bad):
    context, path = source_case
    path.write_bytes(bad)
    batch = read(context)
    assert batch.status == "unsupported" and batch.consumed_offset == 0
    assert not batch.source_complete


def test_unknown_substantive_record_blocks_its_offset(source_case):
    context, path = source_case
    good = b'{"text":"good"}\n'
    path.write_bytes(good + b'{"unknown":"layout"}\n')
    def blocked(context, rows, header, state):
        return ParsedRecords((SourceRecord("first", "user", "good", 0),), status="partial",
                             blocked_offset=len(good), warnings=("Unknown layout",))
    batch = read(context, callback=blocked)
    assert batch.consumed_offset == len(good)
    assert not batch.source_complete


def test_oversized_record_and_bounded_batch(source_case, monkeypatch):
    import transcripts
    context, path = source_case
    monkeypatch.setattr(transcripts, "MAX_RECORD_BYTES", 100)
    path.write_bytes(b'{"text":"' + b'x' * 101 + b'"}\n')
    assert read(context).status == "unsupported"
    monkeypatch.setattr(transcripts, "MAX_RECORD_BYTES", 1000)
    monkeypatch.setattr(transcripts, "MAX_BATCH_BYTES", 50)
    write_rows(path, [{"text": "first"}, {"text": "second"}, {"text": "third"}, {"text": "fourth"}])
    batch = read(context)
    assert batch.consumed_offset <= 50 and not batch.source_complete
    assert batch.status == "partial"


def test_source_larger_than_16mb_is_read_incrementally_without_loss(source_case):
    context, path = source_case
    text = 'x' * (512 * 1024)
    write_rows(path, [{'sessionId': 'native-session', 'text': text}] + [{'text': text}] * 33)
    assert path.stat().st_size > 16 * 1024 * 1024
    cursor = SourceCursor()
    sequences = []
    for _ in range(10):
        batch = read(context, cursor)
        assert not batch.loss_of_input
        assert batch.consumed_offset > cursor.offset
        sequences.extend(record.sequence for record in batch.records)
        cursor = cursor_for(batch)
        if batch.source_complete:
            break
        assert batch.status == 'partial'
    assert batch.source_complete and batch.consumed_offset == path.stat().st_size
    assert len(sequences) == len(set(sequences)) == 34


def test_oversized_later_record_keeps_exact_private_source_reference(source_case):
    context, path = source_case
    good = b'{"sessionId":"native-session","text":"good"}\n'
    path.write_bytes(good + b'{"text":"' + b'x' * (1024 * 1024) + b'"}\n')
    batch = read(context)
    assert batch.status == 'partial' and not batch.source_complete
    assert batch.consumed_offset == path.stat().st_size
    assert [record.text for record in batch.records] == ['good']
    reference = batch.parser_state['_deferred_source_rows'][0]
    assert reference['offset'] == len(good)
    assert reference['end_offset'] == path.stat().st_size
    assert reference['reason'] == 'oversized:unrecognized'
    assert reference['digest_kind'] == 'sha256-chunks-v1'
    assert 'row_sha256' not in reference
    following = read(context, cursor_for(batch))
    assert following.status == 'partial' and following.consumed_offset == path.stat().st_size
    assert not following.source_complete and not following.records
