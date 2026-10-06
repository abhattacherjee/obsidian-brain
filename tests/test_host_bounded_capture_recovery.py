"""Recovery visits a bounded, persistent queue without ending active sessions."""
from dataclasses import replace
import json
import time

import pytest

import capture
import note_transactions
import transcripts
from test_host_note_transactions import context


def batch(ctx, records=(), offset=100):
    return transcripts.TranscriptBatch(
        status="ok", records=tuple(records), source_generation="generation",
        consumed_offset=offset, metadata={"native_session_id": ctx.native_session_id},
        source_identity="synthetic", source_complete=True, source_size=offset,
    )


def register(ctx, monkeypatch, *, active=False):
    records = [transcripts.SourceRecord("message", "user", "synthetic fact", 0)]
    if not active:
        records.append(transcripts.SourceRecord("interruption", "system", "", 1,
                                                kind="interruption"))
    monkeypatch.setattr(transcripts, "read_records", lambda c, cursor, deadline: batch(c, records))
    result = capture.capture_checkpoint(ctx, capture.CaptureEvent("stop", min_messages=1),
                                        time.monotonic() + 5)
    assert result.status == "complete", result.warnings


def source_state(ctx):
    connection = note_transactions.connect_coordination(ctx)
    try:
        return connection.execute("SELECT cursor,state FROM source_sessions WHERE scope=?",
                                  (ctx.session_key,)).fetchone()
    finally:
        connection.close()


def test_bounded_queue_reaches_sources_after_first_eight_and_persists_position(context, monkeypatch):
    sources = [replace(context, native_session_id="source-%02d" % i) for i in range(19)]
    for ctx in sources:
        register(ctx, monkeypatch)
    visited = []
    def read(ctx, cursor, deadline):
        visited.append(ctx.native_session_id)
        return batch(ctx, offset=cursor.offset)
    monkeypatch.setattr(transcripts, "read_records", read)
    for expected in (sources[:8], sources[8:16], sources[16:] + sources[:5]):
        visited.clear()
        # A fresh RuntimeContext and connection must reuse the durable queue position.
        result = capture.recover_registered(replace(context), 8, time.monotonic() + 5)
        assert visited == [ctx.native_session_id for ctx in expected]
        assert result.status == "pending"
    assert len(set(ctx.native_session_id for ctx in sources)) == 19


@pytest.mark.parametrize("bound", [0, 1, 3])
def test_max_sources_is_a_hard_read_bound(context, monkeypatch, bound):
    for i in range(5):
        register(replace(context, native_session_id="bounded-%d" % i), monkeypatch)
    visited = []
    def read(ctx, cursor, deadline):
        visited.append(ctx.native_session_id)
        return batch(ctx, offset=cursor.offset)
    monkeypatch.setattr(transcripts, "read_records", read)
    result = capture.recover_registered(context, bound, time.monotonic() + 5)
    assert len(visited) == bound
    assert result.status == "pending"


def test_default_recovery_skips_active_sources_without_finalizing(context, monkeypatch):
    register(context, monkeypatch, active=True)
    note = next(context.vault_path.rglob("*.md"))
    before = note.read_bytes()
    monkeypatch.setattr(transcripts, "read_records", lambda *args: pytest.fail("active source read"))
    result = capture.recover_registered(context, 8, time.monotonic() + 5)
    assert result.status == "complete"
    assert source_state(context)[1] == "active"
    assert note.read_bytes() == before


def test_include_active_replays_new_facts_and_keeps_active(context, monkeypatch):
    context = replace(context, config={"min_messages": 1})
    register(context, monkeypatch, active=True)
    fresh = transcripts.SourceRecord("second", "assistant", "later synthetic fact", 100)
    monkeypatch.setattr(transcripts, "read_records", lambda ctx, cursor, deadline: batch(ctx, [fresh], 200))
    result = capture.recover_registered(context, 8, time.monotonic() + 5, include_active=True)
    assert result.status == "complete", result.warnings
    assert source_state(context)[1] == "active"
    note = next(context.vault_path.rglob("*.md"))
    assert "later synthetic fact" in note.read_text()
    assert 'capture_state: "active"' in note.read_text()


def test_expired_deadline_does_not_read_publish_or_lose_retained_input(context, monkeypatch):
    register(context, monkeypatch)
    note = next(context.vault_path.rglob("*.md"))
    before = note.read_bytes()
    cursor = source_state(context)[0]
    monkeypatch.setattr(transcripts, "read_records", lambda *args: pytest.fail("expired read"))
    result = capture.recover_registered(context, 8, time.monotonic() - 1)
    assert result.status == "pending"
    assert not result.loss_of_input
    assert note.read_bytes() == before
    assert source_state(context)[0] == cursor


def test_recovery_queue_does_not_cross_native_hosts(context, monkeypatch):
    other_host = "claude" if context.host == "codex" else "codex"
    claude = replace(context, host=other_host, client="claude-code" if other_host == "claude" else "codex-cli")
    register(context, monkeypatch)
    register(claude, monkeypatch)
    visited = []
    def read(ctx, cursor, deadline):
        visited.append((ctx.host, ctx.native_session_id))
        return batch(ctx, offset=cursor.offset)
    monkeypatch.setattr(transcripts, "read_records", read)
    capture.recover_registered(context, 8, time.monotonic() + 5)
    assert visited == [(context.host, context.native_session_id)]
    visited.clear()
    capture.recover_registered(claude, 8, time.monotonic() + 5)
    assert visited == [(other_host, claude.native_session_id)]


def test_duplicate_native_event_replay_is_once_and_changed_content_conflicts(context, monkeypatch):
    register(context, monkeypatch, active=True)
    note = next(context.vault_path.rglob("*.md"))
    original = note.read_bytes()
    duplicate = transcripts.SourceRecord("message", "user", "synthetic fact", 0)
    monkeypatch.setattr(transcripts, "read_records", lambda ctx, cursor, deadline: batch(ctx, [duplicate], 200))
    assert capture.capture_checkpoint(context, capture.CaptureEvent("stop", min_messages=1),
                                      time.monotonic() + 5).status == "complete"
    assert note.read_text().count("User: synthetic fact") == 1
    changed = replace(duplicate, text="changed fact")
    monkeypatch.setattr(transcripts, "read_records", lambda ctx, cursor, deadline: batch(ctx, [changed], 300))
    assert capture.capture_checkpoint(context, capture.CaptureEvent("stop", min_messages=1),
                                      time.monotonic() + 5).status == "conflict"
    assert note.read_bytes() == original
    assert json.loads(source_state(context)[0])["offset"] == 200


def test_explicit_outside_note_is_rejected_without_registry_or_publication(context, monkeypatch):
    outside = context.worktree / "outside.md"
    record = transcripts.SourceRecord("message", "user", "synthetic fact", 0)
    monkeypatch.setattr(transcripts, "read_records", lambda ctx, cursor, deadline: batch(ctx, [record]))
    result = capture.capture_checkpoint(context, capture.CaptureEvent("stop", note_path=outside, min_messages=1),
                                        time.monotonic() + 5)
    assert result.status == "conflict"
    assert not outside.exists()
    assert source_state(context) is None


def test_registered_source_rejects_changed_native_metadata_before_retention(context, monkeypatch):
    register(context, monkeypatch, active=True)
    note = next(context.vault_path.rglob("*.md"))
    before = note.read_bytes()
    old_cursor = source_state(context)[0]
    fresh = transcripts.SourceRecord("foreign", "user", "foreign synthetic fact", 100)
    wrong = replace(batch(context, [fresh], 200), metadata={"native_session_id": "another-native-id"})
    monkeypatch.setattr(transcripts, "read_records", lambda *args: wrong)
    result = capture.recover_registered(context, 8, time.monotonic() + 5, include_active=True)
    assert result.status == "pending"
    assert any("another native session" in warning for warning in result.warnings)
    assert note.read_bytes() == before
    assert source_state(context)[0] == old_cursor
    connection = note_transactions.connect_coordination(context)
    try:
        assert connection.execute("SELECT COUNT(*) FROM native_events WHERE event='foreign'").fetchone()[0] == 0
    finally:
        connection.close()
