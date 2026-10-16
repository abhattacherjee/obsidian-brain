"""A journaled move cannot overwrite a collision or erase an intervening edit."""
import pytest

import note_transactions as transactions
from test_host_note_transactions import context


def test_move_preserves_existing_destination(context):
    source = context.vault_path / "old.md"
    target = context.vault_path / "new.md"
    source.write_text("source")
    target.write_text("user destination")
    result = transactions.move_note(context, source, target,
                                    transactions.read_revision(context, source), "move")
    assert result.status == "conflict"
    assert source.read_text() == "source"
    assert target.read_text() == "user destination"


@pytest.mark.parametrize("point", ["after_move_link", "after_move"])
def test_move_replays_after_process_interruption(context, monkeypatch, point):
    source = context.vault_path / "old.md"
    target = context.vault_path / "new.md"
    source.write_text("source")
    revision = transactions.read_revision(context, source)

    def crash(where):
        if where == point:
            raise RuntimeError("injected crash")

    monkeypatch.setattr(transactions, "_fault", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        transactions.move_note(context, source, target, revision, "move")
    monkeypatch.setattr(transactions, "_fault", lambda where: None)
    result = transactions.move_note(context, source, target, revision, "move")
    assert result.status in {"applied", "unchanged"}
    assert not source.exists()
    assert target.read_text() == "source"


def test_move_rollback_preserves_intervening_destination_edit(context):
    source = context.vault_path / "old.md"
    target = context.vault_path / "new.md"
    source.write_text("source")
    revision = transactions.read_revision(context, source)
    result = transactions.move_note(context, source, target, revision, "move")
    target.write_text("manual edit")
    replay = transactions.move_note(context, source, target, revision, "move")
    assert replay.revision == result.revision
    rollback = transactions.move_note(context, target, source, result.revision, "rollback")
    assert rollback.status == "conflict"
    assert target.read_text() == "manual edit"
    assert not source.exists()
