"""Tests for note_writer.set_frontmatter_fields (#455)."""

import os

import pytest

import note_writer

NOTE = ("---\ntype: claude-insight\ndate: 2026-10-23\nstatus: provisional\ntags:\n  - claude/insight\n"
        "---\n\n# Title\n\nstatus: body text stays\n")


@pytest.fixture
def note(tmp_vault):
    p = tmp_vault / "claude-insights" / "n.md"
    p.write_text(NOTE)
    return p


def test_replace_insert_and_remove(tmp_vault, note):
    assert note_writer.set_frontmatter_fields(str(tmp_vault), str(note),
                                              {"status": "promoted", "capture_merit": 0.6512, "capture_rank": 3})
    text = note.read_text()
    assert "status: promoted\n" in text and "status: provisional" not in text
    assert "capture_merit: 0.6512\ncapture_rank: 3\n---\n" in text  # inserted at the end of the block
    assert "status: body text stays" in text  # the body is never touched
    assert note_writer.set_frontmatter_fields(str(tmp_vault), "claude-insights/n.md", {"capture_rank": None})
    assert "capture_rank" not in note.read_text()


def test_crlf_is_kept(tmp_vault, note):
    note.write_bytes(NOTE.replace("\n", "\r\n").encode())
    note_writer.set_frontmatter_fields(str(tmp_vault), str(note), {"status": "promoted", "promoted_by": "access"})
    raw = note.read_bytes()
    assert b"status: promoted\r\n" in raw and b"promoted_by: access\r\n---\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_no_change_returns_false_and_keeps_mtime(tmp_vault, note):
    os.utime(note, ns=(1_000_000_000, 1_000_000_000))
    assert note_writer.set_frontmatter_fields(str(tmp_vault), str(note), {"status": "provisional", "gone": None}) is False
    assert note.stat().st_mtime_ns == 1_000_000_000


def test_a_continuation_value_is_replaced_whole(tmp_vault, note):
    note.write_text("---\ntype: x\nstatus:\n  - a\n  - b\ndate: d\n---\nbody\n")
    note_writer.set_frontmatter_fields(str(tmp_vault), str(note), {"status": "promoted"})
    assert note.read_text() == "---\ntype: x\nstatus: promoted\ndate: d\n---\nbody\n"


def test_repeated_key_keeps_one_line(tmp_vault, note):
    note.write_text("---\nstatus: a\nstatus: b\n---\nbody\n")
    note_writer.set_frontmatter_fields(str(tmp_vault), str(note), {"status": "c"})
    assert note.read_text() == "---\nstatus: c\n---\nbody\n"


@pytest.mark.parametrize("fields", [
    {"status": "promoted\nevil: 1"},
    {"status": "a b"},
    {"status": "x#y"},
    {"status": ""},
    {"status": True},
    {"status": float("nan")},
    {"status": float("inf")},
    {"status": ["a"]},
    {"bad key": "a"},
    {"tags\n": "a"},
])
def test_values_and_keys_outside_the_allow_list_are_refused(tmp_vault, note, fields):
    with pytest.raises(ValueError):
        note_writer.set_frontmatter_fields(str(tmp_vault), str(note), fields)
    assert note.read_text() == NOTE


def test_path_outside_the_vault_is_refused(tmp_vault, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere") / "n.md"
    other.write_text(NOTE)
    with pytest.raises(ValueError):
        note_writer.set_frontmatter_fields(str(tmp_vault), str(other), {"status": "promoted"})
    with pytest.raises(ValueError):
        note_writer.set_frontmatter_fields(str(tmp_vault), "../elsewhere/n.md", {"status": "promoted"})
    assert other.read_text() == NOTE


def test_note_without_frontmatter_is_refused(tmp_vault, note):
    note.write_text("# no frontmatter\n")
    with pytest.raises(ValueError):
        note_writer.set_frontmatter_fields(str(tmp_vault), str(note), {"status": "promoted"})


def test_concurrent_change_is_refused(tmp_vault, note, monkeypatch):
    real = note_writer._atomic_rewrite

    def racing(dest, content, expect_stat=None):
        dest.write_text(NOTE + "edited in Obsidian\n")  # a writer that ignores the lock
        return real(dest, content, expect_stat)

    monkeypatch.setattr(note_writer, "_atomic_rewrite", racing)
    with pytest.raises(OSError):
        note_writer.set_frontmatter_fields(str(tmp_vault), str(note), {"status": "promoted"})
    assert note.read_text().endswith("edited in Obsidian\n") and "status: provisional" in note.read_text()


def test_locked_note_is_refused(tmp_vault, note):
    lock, _ = note_writer._acquire_lock(note)
    try:
        with pytest.raises(OSError):
            note_writer.set_frontmatter_fields(str(tmp_vault), str(note), {"status": "promoted"})
    finally:
        note_writer._release_lock(lock)
    assert note.read_text() == NOTE
