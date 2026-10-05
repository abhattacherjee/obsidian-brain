"""A checkpoint is durable before publication, and publication precedes cursor commit."""
from dataclasses import replace
import time

import pytest

import capture
import note_transactions
from test_host_note_transactions import context


@pytest.mark.parametrize("point", ["after_checkpoint", "after_replace", "before_cursor_commit"])
def test_replay_after_crash_converges_without_duplicates(context, monkeypatch, point):
    note = context.vault_path / "session.md"
    def crash(where):
        if where == point:
            raise RuntimeError("injected crash")
    monkeypatch.setattr(capture, "_fault", crash)
    monkeypatch.setattr(note_transactions, "_fault", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        capture.publish_events(context, "source", "generation-1", 100,
                               [("event-1", "User: hello\n")], note)
    assert capture.read_cursor(context, "source", "generation-1") == 0
    monkeypatch.setattr(capture, "_fault", lambda point: None)
    monkeypatch.setattr(note_transactions, "_fault", lambda point: None)
    result = capture.recover_pending(context, max_sources=8, deadline=time.monotonic()+1)
    assert result.status == "complete"
    assert capture.read_cursor(context, "source", "generation-1") == 100
    assert note.read_text().count("User: hello") == 1
    capture.publish_events(context, "source", "generation-1", 100,
                           [("event-1", "User: hello\n")], note)
    assert note.read_text().count("User: hello") == 1


def test_crash_before_checkpoint_has_no_committed_activity(context, monkeypatch):
    def crash(point):
        if point == "before_checkpoint":
            raise RuntimeError("injected crash")
    monkeypatch.setattr(capture, "_fault", crash)
    note = context.vault_path / "session.md"
    with pytest.raises(RuntimeError, match="injected crash"):
        capture.publish_events(context, "source", "generation", 100, [("event", "hello")], note)
    assert capture.read_cursor(context, "source", "generation") == 0
    assert not note.exists()


def test_same_native_id_on_two_hosts_is_not_one_checkpoint(context):
    claude = replace(context, host="claude", client="claude-code")
    codex_note = context.vault_path / "codex.md"
    claude_note = context.vault_path / "claude.md"
    capture.publish_events(context, "source", "generation", 100, [("event", "Codex fact")], codex_note)
    assert capture.read_cursor(claude, "source", "generation") == 0
    capture.publish_events(claude, "source", "generation", 200, [("event", "Claude fact")], claude_note)
    assert "Codex fact" in codex_note.read_text()
    assert "Claude fact" in claude_note.read_text()


def test_same_session_keeps_facts_when_resumed_in_another_project(context):
    note = context.vault_path / "session.md"
    capture.publish_events(context, "source", "generation", 100, [("first", "first project fact")], note)
    other_root = context.worktree / "other-project"
    other_root.mkdir()
    resumed = replace(context, canonical_project_root=other_root, worktree=other_root)
    result = capture.publish_events(resumed, "source", "generation", 200,
                                    [("second", "second project fact")], note)
    assert result.status == "complete"
    assert "first project fact" in note.read_text()
    assert "second project fact" in note.read_text()


def test_index_rebuild_does_not_remove_durable_checkpoint(context, monkeypatch):
    def crash(point):
        if point == "after_checkpoint":
            raise RuntimeError("injected crash")
    monkeypatch.setattr(capture, "_fault", crash)
    note = context.vault_path / "session.md"
    with pytest.raises(RuntimeError):
        capture.publish_events(context, "source", "generation", 100, [("event", "hello")], note)
    context.index_path.write_bytes(b"replaceable index")
    context.index_path.unlink()
    monkeypatch.setattr(capture, "_fault", lambda point: None)
    result = capture.recover_pending(context, max_sources=8, deadline=time.monotonic()+1)
    assert result.status == "complete"
    assert "hello" in note.read_text()


def test_expired_recovery_deadline_retains_pending_work(context, monkeypatch):
    def crash(point):
        if point == "after_checkpoint":
            raise RuntimeError("injected crash")
    monkeypatch.setattr(capture, "_fault", crash)
    with pytest.raises(RuntimeError):
        capture.publish_events(context, "source", "generation", 100, [("event", "hello")], context.vault_path / "session.md")
    monkeypatch.setattr(capture, "_fault", lambda point: None)
    result = capture.recover_pending(context, max_sources=8, deadline=time.monotonic()-1)
    assert result.status == "pending"
    assert result.pending_sources == 1
    assert capture.read_cursor(context, "source", "generation") == 0


def test_older_pending_checkpoint_cannot_replace_a_newer_capture(context, monkeypatch):
    note = context.vault_path / "session.md"
    def crash(point):
        if point == "after_checkpoint":
            raise RuntimeError("injected crash")
    monkeypatch.setattr(capture, "_fault", crash)
    with pytest.raises(RuntimeError):
        capture.publish_events(context, "source", "generation", 100, [("first", "first fact")], note)
    monkeypatch.setattr(capture, "_fault", lambda point: None)
    assert capture.publish_events(context, "source", "generation", 200,
                                  [("second", "second fact")], note).status == "complete"
    published = note.read_bytes()
    result = capture.recover_pending(context, max_sources=8, deadline=time.monotonic()+1)
    assert result.status == "complete"
    assert note.read_bytes() == published
    assert note.read_text().count("first fact") == 1
    assert note.read_text().count("second fact") == 1
    assert capture.read_cursor(context, "source", "generation") == 200


def test_prepared_operation_replay_keeps_immutable_checkpoint_body(context, monkeypatch):
    note = context.vault_path / "session.md"
    def after_replace(point):
        if point == "after_replace":
            raise RuntimeError("injected crash")
    monkeypatch.setattr(note_transactions, "_fault", after_replace)
    with pytest.raises(RuntimeError):
        capture.publish_events(context, "first-source", "generation", 100, [("first", "first fact")], note)
    monkeypatch.setattr(note_transactions, "_fault", lambda point: None)
    def after_checkpoint(point):
        if point == "after_checkpoint":
            raise RuntimeError("injected crash")
    monkeypatch.setattr(capture, "_fault", after_checkpoint)
    with pytest.raises(RuntimeError):
        capture.publish_events(context, "second-source", "generation", 200, [("second", "second fact")], note)
    monkeypatch.setattr(capture, "_fault", lambda point: None)
    result = capture.recover_pending(context, 8, time.monotonic()+1)
    assert result.status == "complete", result.warnings
    assert capture.read_cursor(context, "first-source", "generation") == 100
    assert capture.read_cursor(context, "second-source", "generation") == 200
    assert note.read_text().count("first fact") == 1
    assert note.read_text().count("second fact") == 1


def test_two_unpublished_checkpoints_recover_in_order(context, monkeypatch):
    note = context.vault_path / "session.md"
    def crash(point):
        if point == "after_checkpoint":
            raise RuntimeError("injected crash")
    monkeypatch.setattr(capture, "_fault", crash)
    for source, offset, text in [("first-source", 100, "first fact"), ("second-source", 200, "second fact")]:
        with pytest.raises(RuntimeError):
            capture.publish_events(context, source, "generation", offset, [(source, text)], note)
    monkeypatch.setattr(capture, "_fault", lambda point: None)
    result = capture.recover_pending(context, 8, time.monotonic()+1)
    assert result.status == "complete", result.warnings
    assert capture.read_cursor(context, "first-source", "generation") == 100
    assert capture.read_cursor(context, "second-source", "generation") == 200
    assert note.read_text().count("first fact") == 1
    assert note.read_text().count("second fact") == 1


def test_managed_edit_between_checkpoint_replays_is_preserved(context, monkeypatch):
    note = context.vault_path / "session.md"
    def crash(point):
        if point == "after_checkpoint":
            raise RuntimeError("injected crash")
    monkeypatch.setattr(capture, "_fault", crash)
    for source, offset, text in [("first-source", 100, "first fact"), ("second-source", 200, "second fact")]:
        with pytest.raises(RuntimeError):
            capture.publish_events(context, source, "generation", offset, [(source, text)], note)
    monkeypatch.setattr(capture, "_fault", lambda point: None)
    assert capture.recover_pending(context, 1, time.monotonic()+1).status == "pending"
    edited = note.read_text().replace("first fact", "manual capture edit")
    note.write_text(edited)
    result = capture.recover_pending(context, 8, time.monotonic()+1)
    assert result.status == "conflict"
    assert note.read_text() == edited
    assert capture.read_cursor(context, "second-source", "generation") == 0


def test_historical_pending_checkpoint_without_body_stays_pending(context):
    with note_transactions.ownership_lock(context):
        connection = note_transactions.connect_coordination(context)
        try:
            connection.execute("""CREATE TABLE checkpoints (
                id TEXT PRIMARY KEY, scope TEXT, source TEXT, generation TEXT,
                offset INTEGER, note TEXT, expected TEXT, operation TEXT,
                phase TEXT, revision TEXT)""")
            connection.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?,?,?,?)",
                               ("historical", context.session_key, "source", "generation", 100,
                                str(context.vault_path / "session.md"), None, "historical-op",
                                "prepared", None))
            connection.commit()
        finally:
            connection.close()
    result = capture.recover_pending(context, 8, time.monotonic()+1)
    assert result.status == "pending"
    assert result.pending_sources == 1
    assert "immutable body" in result.warnings[0]
    assert capture.read_cursor(context, "source", "generation") == 0
    assert not (context.vault_path / "session.md").exists()


def test_user_ownership_marker_text_is_captured_as_literal(context):
    note = context.vault_path / "session.md"
    text = "User quoted <!-- obsidian-brain:capture:start --> and <!-- obsidian-brain:capture:end -->"
    result = capture.publish_events(context, "source", "generation", 100, [("event", text)], note)
    assert result.status == "complete", result.warnings
    assert capture.read_cursor(context, "source", "generation") == 100
    document = note.read_text()
    assert document.count("<!-- obsidian-brain:capture:start -->") == 1
    assert document.count("<!-- obsidian-brain:capture:end -->") == 1
    assert "&lt;!-- obsidian-brain:capture:start -->" in document
    assert "&lt;!-- obsidian-brain:capture:end -->" in document
    assert capture.publish_events(context, "source", "generation", 100, [("event", text)], note).status == "complete"
    assert note.read_text() == document
