"""Deleting obsolete notes respects revisions and survives an interrupted ack."""
import pytest

import note_transactions
from test_host_note_transactions import context


def test_delete_preserves_a_note_changed_after_read(context):
    note = context.vault_path / "obsolete.md"
    note.write_text("original")
    revision = note_transactions.read_revision(context, note)
    note.write_text("manual edit")
    result = note_transactions.delete_note(context, note, revision, "delete-old")
    assert result.status == "conflict"
    assert note.read_text() == "manual edit"


def test_delete_replay_never_deletes_a_recreated_note(context, monkeypatch):
    note = context.vault_path / "obsolete.md"
    note.write_text("original")
    revision = note_transactions.read_revision(context, note)

    def crash(point):
        if point == "after_delete":
            raise RuntimeError("injected crash")

    monkeypatch.setattr(note_transactions, "_fault", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        note_transactions.delete_note(context, note, revision, "delete-old")
    assert not note.exists()
    monkeypatch.setattr(note_transactions, "_fault", lambda point: None)
    assert note_transactions.delete_note(context, note, revision, "delete-old").status == "unchanged"
    note.write_text("new note")
    assert note_transactions.delete_note(context, note, revision, "delete-old").status == "unchanged"
    assert note.read_text() == "new note"


def test_delete_refuses_symlink_escape(context, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    link = context.vault_path / "escape.md"
    link.symlink_to(outside)
    result = note_transactions.delete_note(context, link, None, "delete-outside")
    assert result.status == "conflict"
    assert outside.read_text() == "outside"
