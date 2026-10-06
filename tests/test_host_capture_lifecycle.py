"""Lifecycle state follows durable visible facts, independently of note thresholds."""
import json
import time
import pytest

import capture
import transcripts
from transcripts import SourceRecord, TranscriptBatch
from test_host_note_transactions import context


def batch(records=(), *, generation="one", offset=100, status="ok", loss=False):
    return TranscriptBatch(status, tuple(records), generation, offset,
                           metadata={"native_session_id": "native-id"},
                           source_identity=generation, source_complete=status == "ok",
                           loss_of_input=loss, source_size=offset)


def invoke(context, kind="stop", **kwargs):
    return capture.capture_checkpoint(context, capture.CaptureEvent(kind, **kwargs),
                                      time.monotonic() + 1)


def test_below_threshold_facts_survive_end_and_resume(context, monkeypatch):
    note = context.vault_path / "session.md"
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch([
        SourceRecord("m1", "user", "retained first message", 1)]))
    assert invoke(context, "session_end", note_path=note, min_messages=2).status == "complete"
    assert not note.exists()
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch([
        SourceRecord("m2", "user", "resumed answer", 101)], offset=200))
    assert invoke(context, "resume", note_path=note, min_messages=2).status == "complete"
    text = note.read_text()
    assert text.count("retained first message") == 1
    assert "resumed answer" in text
    assert 'capture_state: "active"' in text
    assert 'agent_provider: "codex"' in text


def test_rotation_replay_deduplicates_native_ids(context, monkeypatch):
    note = context.vault_path / "session.md"
    record = SourceRecord("native-message", "user", "one fact", 1)
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch([record]))
    assert invoke(context, note_path=note, min_messages=1).status == "complete"
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch(
        [record], generation="two", loss=True))
    result = invoke(context, note_path=note, min_messages=1)
    assert result.loss_of_input
    assert note.read_text().count("one fact") == 1
    assert 'capture_completeness: "partial"' in note.read_text()


def test_end_and_interruption_are_distinct(context, monkeypatch):
    note = context.vault_path / "session.md"
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch([
        SourceRecord("m1", "user", "visible fact", 1),
        SourceRecord("abort", "control", "interrupted", 2, kind="interruption")]))
    assert invoke(context, note_path=note, min_messages=1).status == "complete"
    assert 'capture_state: "interrupted"' in note.read_text()
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch(offset=100))
    assert invoke(context, "session_end", note_path=note, min_messages=1).status == "complete"
    assert 'capture_state: "ended"' in note.read_text()


def test_metadata_only_session_does_not_create_note(context, monkeypatch):
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch())
    assert invoke(context, "session_end", min_messages=0).status == "complete"
    assert not list(context.vault_path.rglob("*.md"))


def test_wrong_native_identity_does_not_adopt_source(context, monkeypatch):
    wrong = batch([SourceRecord("m", "user", "wrong session", 1)])
    from dataclasses import replace
    monkeypatch.setattr(transcripts, "read_records", lambda *args: replace(
        wrong, metadata={"native_session_id": "another-session"}))
    assert invoke(context, min_messages=1).status == "conflict"
    assert not list(context.vault_path.rglob("*.md"))


def test_compaction_snapshot_is_immutable_and_replay_is_idempotent(context, monkeypatch):
    note = context.vault_path / "session.md"
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch([
        SourceRecord("m1", "user", "before compaction", 1)]))
    assert invoke(context, "pre_compact", note_path=note, min_messages=1).status == "complete"
    snapshots = list(context.vault_path.rglob("*snapshot*.md"))
    assert len(snapshots) == 1
    original = snapshots[0].read_bytes()
    assert invoke(context, "pre_compact", note_path=note, min_messages=1).status == "complete"
    assert len(list(context.vault_path.rglob("*snapshot*.md"))) == 1
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch([
        SourceRecord("m2", "assistant", "after compaction", 101)], offset=200))
    invoke(context, note_path=note, min_messages=1)
    assert snapshots[0].read_bytes() == original
    assert "after compaction" in note.read_text()


def test_next_capture_preserves_manual_managed_edit(context, monkeypatch):
    note = context.vault_path / "session.md"
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch([
        SourceRecord("m1", "user", "first fact", 1)]))
    assert invoke(context, note_path=note, min_messages=1).status == "complete"
    note.write_text(note.read_text().replace("User: first fact", "User: manually corrected fact"))
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch([
        SourceRecord("m2", "assistant", "retained new fact", 101)], offset=200))
    assert invoke(context, note_path=note, min_messages=1).status == "conflict"
    assert "manually corrected fact" in note.read_text()
    assert "retained new fact" not in note.read_text()


def test_partial_batch_can_become_complete_without_source_loss(context, monkeypatch):
    note = context.vault_path / "session.md"
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch([
        SourceRecord("m1", "user", "first fact", 1)], status="partial"))
    invoke(context, note_path=note, min_messages=1)
    assert 'capture_completeness: "partial"' in note.read_text()
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch([
        SourceRecord("m2", "assistant", "completed answer", 101)], offset=200))
    invoke(context, note_path=note, min_messages=1)
    assert 'capture_completeness: "complete"' in note.read_text()


def test_short_session_retains_facts_until_duration_threshold(context, monkeypatch):
    note = context.vault_path / "session.md"
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch([
        SourceRecord("m1", "user", "first question", 1, "2026-10-05T00:00:00Z"),
        SourceRecord("m2", "assistant", "short answer", 2, "2026-10-05T00:00:30Z")]))
    assert invoke(context, note_path=note, min_messages=1).status == "complete"
    assert not note.exists()
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch([
        SourceRecord("m3", "user", "later question", 101, "2026-10-05T00:03:00Z")], offset=200))
    assert invoke(context, note_path=note, min_messages=1).status == "complete"
    assert "first question" in note.read_text()
    assert "later question" in note.read_text()


def test_crash_after_source_retention_replays_without_losing_facts(context, monkeypatch):
    note = context.vault_path / "session.md"
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch([
        SourceRecord("m1", "user", "durable before note", 1)]))

    def crash(point):
        if point == "after_source_retention":
            raise RuntimeError("injected source crash")

    monkeypatch.setattr(capture, "_fault", crash)
    with pytest.raises(RuntimeError, match="injected source crash"):
        invoke(context, note_path=note, min_messages=1)
    assert not note.exists()
    monkeypatch.setattr(capture, "_fault", lambda point: None)
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch())
    assert invoke(context, "recover", note_path=note, min_messages=1).status == "complete"
    assert note.read_text().count("durable before note") == 1


def test_snapshot_disable_and_clear_trigger_use_existing_toggles(context, monkeypatch):
    from dataclasses import replace
    note = context.vault_path / "session.md"
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch([
        SourceRecord("m1", "user", "visible fact", 1)]))
    disabled = replace(context, config={"snapshot_on_compact": False})
    assert invoke(disabled, "pre_compact", note_path=note, min_messages=1).status == "complete"
    assert not list(context.vault_path.rglob("*snapshot*.md"))
    assert invoke(context, "session_end", note_path=note, min_messages=1, trigger="clear").status == "complete"
    snapshots = list(context.vault_path.rglob("*snapshot*.md"))
    assert len(snapshots) == 1
    assert 'trigger: "clear"' in snapshots[0].read_text()


def test_native_claude_resume_continues_indexed_legacy_note(context, monkeypatch):
    import hashlib
    import sqlite3
    from dataclasses import replace
    claude = replace(context, host="claude", client="claude-code")
    folder = context.vault_path / "claude-sessions"
    folder.mkdir()
    digest = hashlib.sha256(claude.native_session_id.encode()).hexdigest()[:4]
    legacy = folder / ("2026-09-01-original-project-" + digest + ".md")
    original = '---\ntype: claude-session\nsession_id: native-id\ndate: 2026-09-01\n---\n# Original session\nManual prose.\n'
    legacy.write_text(original)
    connection = sqlite3.connect(context.index_path)
    connection.execute("CREATE TABLE notes(path TEXT PRIMARY KEY,type TEXT)")
    connection.execute("INSERT INTO notes VALUES (?,?)", (str(legacy), "claude-session"))
    connection.commit()
    connection.close()
    monkeypatch.setattr(transcripts, "read_records", lambda *args: batch([
        SourceRecord("m1", "user", "continued native fact", 1)]))
    assert invoke(claude, "resume", min_messages=1).status == "complete"
    assert list(folder.glob("*.md")) == [legacy]
    text = legacy.read_text()
    assert "Manual prose." in text
    assert "continued native fact" in text
    assert "date: 2026-09-01" in text
