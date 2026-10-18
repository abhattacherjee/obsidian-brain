"""Tests for hooks/capture_reject.py and the indexer's _rejected/ skip (#425)."""

import pytest

import capture_reject
import capture_state
import vault_index

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

NOTE = """---
type: claude-insight
date: 2026-10-18
project: demo
tags:
  - claude/insight
  - claude/auto
---

# Zebra quokka capture

The zebra quokka fact that should be searchable.
"""


def _new_capture(tmp_vault, name="2026-10-18-zebra-quokka-1234.md", project="demo", title="Zebra quokka capture"):
    path = tmp_vault / "claude-insights" / name
    path.write_text(NOTE)
    cid = capture_state.ledger_append({"session_id": SID, "project": project, "type": "insight", "title": title,
                                       "status": "active", "kind": "new", "note_path": f"claude-insights/{name}"})
    return cid, path


def _db(tmp_vault):
    return vault_index.ensure_index(str(tmp_vault), ["claude-sessions", "claude-insights"],
                                    db_path=vault_index._default_db_path())


def test_reject_new_moves_to_rejected(mock_config, tmp_vault):
    cid, path = _new_capture(tmp_vault)
    db = _db(tmp_vault)
    assert vault_index.search_vault(db, "zebra quokka")
    row = capture_reject.reject(cid, mock_config)
    assert not path.exists()
    assert (tmp_vault / "claude-insights" / "_rejected" / path.name).exists()
    assert row["status"] == "rejected" and row["rejected_path"] == f"claude-insights/_rejected/{path.name}"
    assert vault_index.search_vault(db, "zebra quokka") == []
    assert vault_index.search_vault(_db(tmp_vault), "zebra quokka") == []  # a later sync keeps it out
    assert capture_state.ledger_get(cid)["status"] == "rejected"


def test_reject_updated_removes_only_its_section(mock_config, tmp_vault):
    note = tmp_vault / "claude-insights" / "hand-written.md"
    keep = "## Update (2026-10-17) — auto-capture aaaaaaaaaaaa\n\nKeep me.\n"
    drop = "## Update (2026-10-18) — auto-capture bbbbbbbbbbbb\n\n**Drop**\n\nDrop me.\n"
    note.write_text(NOTE + "\n" + keep + "\n" + drop + "\n## Later\n\nTail.\n")
    cid = capture_state.ledger_append({"session_id": SID, "project": "demo", "status": "active", "kind": "updated",
                                       "note_path": "claude-insights/hand-written.md",
                                       "update_heading": drop.split("\n")[0], "title": "Drop"})
    capture_reject.reject(cid, mock_config)
    text = note.read_text()
    assert "Keep me." in text and "Tail." in text and "# Zebra quokka capture" in text
    assert "Drop me." not in text and "bbbbbbbbbbbb" not in text
    saved = (tmp_vault / "claude-insights" / "_rejected" / f"hand-written-update-{cid}.md").read_text()
    assert saved.startswith("## Update (2026-10-18) — auto-capture bbbbbbbbbbbb") and "Drop me." in saved


def test_reject_twice_is_noop(mock_config, tmp_vault):
    cid, _ = _new_capture(tmp_vault)
    first = capture_reject.reject(cid, mock_config)
    second = capture_reject.reject(cid, mock_config)
    assert first["status"] == second["status"] == "rejected"
    assert len(list((tmp_vault / "claude-insights" / "_rejected").iterdir())) == 1


def test_reject_unknown_id(mock_config):
    with pytest.raises(KeyError):
        capture_reject.reject("doesnotexist", mock_config)


def test_reject_path_containment(mock_config, tmp_vault):
    outside = tmp_vault.parent / "outside.md"
    outside.write_text("x")
    cid = capture_state.ledger_append({"session_id": SID, "status": "active", "kind": "new",
                                       "note_path": "../outside.md"})
    with pytest.raises(ValueError):
        capture_reject.reject(cid, mock_config)
    assert outside.exists()


def test_sync_skips_rejected_dir(mock_config, tmp_vault):
    rejected = tmp_vault / "claude-insights" / "_rejected"
    rejected.mkdir()
    (rejected / "old.md").write_text(NOTE)
    db = _db(tmp_vault)
    assert vault_index.search_vault(db, "zebra quokka") == []


def test_remove_note_is_safe_when_missing(tmp_vault):
    db = _db(tmp_vault)
    vault_index.remove_note(db, str(tmp_vault / "claude-insights" / "never-indexed.md"))
    vault_index.remove_note(str(tmp_vault / "no.db"), "x")  # no database: no error


def test_recent_rejections_newest_first(mock_config, tmp_vault):
    a, _ = _new_capture(tmp_vault, name="a.md", title="first")
    b, _ = _new_capture(tmp_vault, name="b.md", title="second")
    _new_capture(tmp_vault, name="c.md", project="other", title="elsewhere")
    capture_reject.reject(a, mock_config)
    capture_reject.reject(b, mock_config)
    assert capture_reject.recent_rejections("demo") == ["second", "first"]
    assert capture_reject.recent_rejections("other") == []
