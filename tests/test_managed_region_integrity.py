"""Incomplete ownership markers are preserved for repair, not overwritten."""
import pytest

from note_transactions import NoteMutation, apply_mutations, read_revision
from test_host_note_transactions import context


@pytest.mark.parametrize("original", [
    "user text\n<!-- obsidian-brain:capture:start -->\nunclosed capture",
    "user text\n<!-- obsidian-brain:unknown:start -->\nunknown owner",
])
def test_malformed_region_preserves_original_note(context, original):
    note = context.vault_path / "session.md"
    note.write_text(original)
    revision = read_revision(context, note)
    result = apply_mutations(context, [NoteMutation(note, revision,
                                                   {"capture": "new facts"}, "capture-update")])
    assert result.status == "conflict"
    assert note.read_text() == original
