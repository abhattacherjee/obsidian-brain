"""Tests for hooks/capture_save.py (#425)."""

import re

import pytest

import capture_save
import capture_state

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _cand(**kw):
    c = {"type": "insight", "title": "jsonl keeps rows before autocompact",
         "body": "The transcript file keeps every row from before a compaction.", "topic": "transcripts"}
    c.update(kw)
    return c


@pytest.fixture
def no_match(monkeypatch):
    monkeypatch.setattr(capture_save, "find_match", lambda query, config: None)


def _front(path):
    text = path.read_text()
    return text.split("---", 2)[1], text


def test_new_insight_uses_template_fields(mock_config, tmp_vault, no_match):
    row = capture_save.save_candidate(_cand(), session_id=SID, project="obsidian-brain",
                                      session_note="2026-10-18-obsidian-brain-1a2b",
                                      today="2026-10-18", config=mock_config)
    assert row["kind"] == "new" and row["status"] == "active"
    name = row["note_path"].split("/", 1)[1]
    assert re.match(r"^\d{4}-\d{2}-\d{2}-[a-z0-9-]{1,50}-[0-9a-f]{4}\.md$", name)
    front, text = _front(tmp_vault / row["note_path"])
    assert "type: claude-insight" in front
    assert "source: auto-capture" in front
    assert f"source_session: {SID}" in front
    assert 'source_session_note: "[[2026-10-18-obsidian-brain-1a2b]]"' in front
    for tag in ("claude/auto", "claude/insight", "claude/project/obsidian-brain", "claude/topic/transcripts"):
        assert f"  - {tag}" in front
    assert "{{" not in text
    assert "# jsonl keeps rows before autocompact" in text
    assert capture_state.ledger_list()[0]["id"] == row["id"]


def test_decision_and_error_fix_types(mock_config, tmp_vault, no_match):
    d = capture_save.save_candidate(_cand(type="decision", title="Use claude -p workers"), session_id=SID,
                                    project="p", session_note="", config=mock_config)
    e = capture_save.save_candidate(_cand(type="error-fix", title="Ledger append race"), session_id=SID,
                                    project="p", session_note="", config=mock_config)
    dfront, _ = _front(tmp_vault / d["note_path"])
    efront, _ = _front(tmp_vault / e["note_path"])
    assert "type: claude-decision" in dfront and "status: active" in dfront
    assert "type: claude-error-fix" in efront
    assert "source_session_note" not in dfront  # omitted, not written empty


def test_match_appends_update(mock_config, tmp_vault, monkeypatch):
    existing = tmp_vault / "claude-insights" / "2026-10-01-old-note-abcd.md"
    existing.write_text("---\ntype: claude-insight\ndate: 2026-10-01\ntags:\n  - claude/insight\n---\n\n# Old note\n\nBody.\n")
    monkeypatch.setattr(capture_save, "find_match", lambda q, c: "claude-insights/2026-10-01-old-note-abcd.md")
    row = capture_save.save_candidate(_cand(), session_id=SID, project="p", today="2026-10-18", config=mock_config)
    text = existing.read_text()
    assert row["kind"] == "updated"
    assert row["update_heading"] == f"## Update (2026-10-18) — auto-capture {row['id']}"
    assert text.count(row["update_heading"]) == 1
    assert "claude/auto" in text.split("---", 2)[1]


def test_unknown_type_and_empty_body_rejected(mock_config, no_match):
    with pytest.raises(ValueError):
        capture_save.save_candidate(_cand(type="poem"), session_id=SID, project="p", config=mock_config)
    with pytest.raises(ValueError):
        capture_save.save_candidate(_cand(body="   "), session_id=SID, project="p", config=mock_config)
    assert capture_state.ledger_list() == []


def test_secrets_scrubbed_in_note(mock_config, tmp_vault, no_match):
    token = "ghp_" + "b" * 36
    row = capture_save.save_candidate(_cand(body=f"use {token} here", title=f"token {token}"),
                                      session_id=SID, project="p", session_note="", config=mock_config)
    text = (tmp_vault / row["note_path"]).read_text()
    assert token not in text and token not in row["title"]


def test_filename_collision_retries_once(mock_config, tmp_vault, no_match, monkeypatch):
    hashes = iter(["dead", "beef"])
    monkeypatch.setattr(capture_save, "_hash4", lambda: next(hashes))
    title = "collide"
    (tmp_vault / "claude-insights" / f"2026-10-18-{title}-dead.md").write_text("taken")
    row = capture_save.save_candidate(_cand(title=title), session_id=SID, project="p", session_note="",
                                      today="2026-10-18", config=mock_config)
    assert row["note_path"].endswith(f"2026-10-18-{title}-beef.md")


def _fake_results(monkeypatch, results):
    monkeypatch.setattr(capture_save.vault_index, "ensure_index", lambda v, f: "db")
    monkeypatch.setattr(capture_save.vault_index, "compute_query_vector", lambda db, q: {"x": 1.0})
    monkeypatch.setattr(capture_save.vault_index, "search_vault",
                        lambda db, q, note_type=None, limit=3, include_vectors=True:
                        [r for r in results if r["type"] == note_type])
    monkeypatch.setattr(capture_save.compress_guard, "is_high_confidence_match", lambda res, query_vec=None: True)


def test_rejected_folder_never_matches(mock_config, monkeypatch):
    _fake_results(monkeypatch, [
        {"path": "claude-insights/_rejected/a.md", "type": "claude-insight", "rank": -40.0},
        {"path": "claude-insights/b.md", "type": "claude-insight", "rank": -30.0},
    ])
    assert capture_save.find_match("q", mock_config) == "claude-insights/b.md"


def test_session_note_top_result_is_no_match(mock_config, monkeypatch):
    _fake_results(monkeypatch, [
        {"path": "claude-sessions/s.md", "type": "claude-session", "rank": -50.0},
        {"path": "claude-insights/b.md", "type": "claude-insight", "rank": -30.0},
    ])
    assert capture_save.find_match("q", mock_config) is None


def test_find_match_respects_guard(mock_config, monkeypatch):
    _fake_results(monkeypatch, [{"path": "claude-insights/b.md", "type": "claude-insight", "rank": -30.0}])
    monkeypatch.setattr(capture_save.compress_guard, "is_high_confidence_match", lambda res, query_vec=None: False)
    assert capture_save.find_match("q", mock_config) is None


def test_session_note_for_composes_name(mock_config, monkeypatch):
    monkeypatch.setattr(capture_save.obsidian_utils, "_first_seen_date", lambda sid: "2026-10-18")
    name = capture_save.session_note_for(SID, "Obsidian Brain", mock_config)
    assert name.startswith("2026-10-18-obsidian-brain-")


def test_find_match_returns_vault_relative_path(mock_config, tmp_vault, monkeypatch):
    abs_path = str(tmp_vault / "claude-insights" / "b.md")
    _fake_results(monkeypatch, [{"path": abs_path, "type": "claude-insight", "rank": -30.0}])
    assert capture_save.find_match("q", mock_config) == "claude-insights/b.md"


def test_find_match_refuses_path_outside_vault(mock_config, monkeypatch):
    _fake_results(monkeypatch, [{"path": "/etc/passwd", "type": "claude-insight", "rank": -30.0}])
    assert capture_save.find_match("q", mock_config) is None


def test_demote_keeps_a_longer_fence_intact():
    body = "````md\n```\n# not a heading\n```\n````\n# Real"
    out = capture_save._demote_headings(body)
    assert "\n# not a heading\n" in out and out.endswith("### Real")
