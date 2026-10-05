"""Only explicit repair operations may replace invalid UTF-8 source bytes."""
import hashlib

from note_transactions import NoteMutation, apply_mutations, record_raw_read
from test_host_note_transactions import context


def test_explicit_repair_checks_exact_corrupt_source(context):
    note = context.vault_path / "corrupt.md"
    original = b"old\xff content\r\n"
    note.write_bytes(original)
    revision = record_raw_read(context, note, original)
    assert revision == hashlib.sha256(original).hexdigest()
    result = apply_mutations(context, [NoteMutation(note, revision,
                                                   {"repair_document": "repaired\n"}, "repair")])
    assert result.status == "applied"
    assert note.read_text() == "repaired\n"


def test_stale_corruption_repair_preserves_newer_raw_bytes(context):
    note = context.vault_path / "corrupt.md"
    original = b"old\xff content"
    note.write_bytes(original)
    revision = record_raw_read(context, note, original)
    edited = b"manual\xfe edit"
    note.write_bytes(edited)
    result = apply_mutations(context, [NoteMutation(note, revision,
                                                   {"repair_document": "repaired\n"}, "repair")])
    assert result.status == "conflict"
    assert note.read_bytes() == edited


def test_replacement_character_repair_still_replaces_invalid_bytes(context):
    note = context.vault_path / "corrupt.md"
    original = b"old\xff content"
    note.write_bytes(original)
    revision = record_raw_read(context, note, original)
    repaired = original.decode("utf-8", errors="replace")
    result = apply_mutations(context, [NoteMutation(note, revision,
                                                   {"repair_document": repaired}, "repair")])
    assert result.status == "applied"
    assert note.read_bytes() == repaired.encode("utf-8")


def test_ordinary_document_write_never_silently_repairs_corruption(context):
    note = context.vault_path / "corrupt.md"
    original = b"old\xff content"
    note.write_bytes(original)
    revision = record_raw_read(context, note, original)
    result = apply_mutations(context, [NoteMutation(note, revision,
                                                   {"document": "replacement"}, "normal-write")])
    assert result.status == "conflict"
    assert note.read_bytes() == original


def test_raw_read_of_valid_note_keeps_managed_region_provenance(context):
    note = context.vault_path / "valid.md"
    original = ("<!-- obsidian-brain:capture:start -->\nold facts\n"
                "<!-- obsidian-brain:capture:end -->\n").encode("utf-8")
    note.write_bytes(original)
    revision = record_raw_read(context, note, original)
    note.write_bytes(original + b"\nmanual outside edit\n")
    result = apply_mutations(context, [NoteMutation(note, revision,
                                                   {"capture": "new facts"}, "capture")])
    assert result.status == "applied"
    assert "manual outside edit" in note.read_text()
    assert "new facts" in note.read_text()
