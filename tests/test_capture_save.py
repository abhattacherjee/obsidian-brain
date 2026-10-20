"""Tests for hooks/capture_save.py (#425)."""

import re
from pathlib import Path

import pytest

import capture_save
import capture_state
from tfidf import _cosine_similarity

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _cand(**kw):
    c = {"type": "insight", "title": "jsonl keeps rows before autocompact",
         "body": "The transcript file keeps every row from before a compaction.", "topic": "transcripts"}
    c.update(kw)
    return c


@pytest.fixture
def no_match(monkeypatch):
    monkeypatch.setattr(capture_save, "find_match", lambda *a, **k: None)


def _front(path):
    text = path.read_text()
    return text.split("---", 2)[1], text



def test_summary_leaves_out_headings():
    body = "## Error\nThe registry listed two\n  services.\n\n## Fix\nRemoved them.\n#hashtag stays"
    assert capture_save.summary_of(body) == "The registry listed two services. Removed them. #hashtag stays"
    assert len(capture_save.summary_of("word " * 100)) == capture_save.SUMMARY_CHARS

def test_new_insight_uses_template_fields(mock_config, tmp_vault, no_match):
    row = capture_save.save_candidate(_cand(), session_id=SID, project="obsidian-brain",
                                      session_note="2026-10-18-obsidian-brain-1a2b",
                                      today="2026-10-18", config=mock_config)
    assert row["kind"] == "new" and row["status"] == "active"
    assert row["summary"] and "\n" not in row["summary"] and len(row["summary"]) <= capture_save.SUMMARY_CHARS
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
    monkeypatch.setattr(capture_save, "find_match", lambda *a, **k: "claude-insights/2026-10-01-old-note-abcd.md")
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


def _fake_results(monkeypatch, results, vault=None, guard=True):
    """Fake search results; ``vault`` gets a file for each relative result path (find_match reads the top note)."""
    for r in results:
        if vault is not None and not Path(r["path"]).is_absolute():
            (vault / r["path"]).parent.mkdir(parents=True, exist_ok=True)
            (vault / r["path"]).write_text("---\ntype: claude-insight\n---\n\n# Note\n\nOriginal text.\n")
    monkeypatch.setattr(capture_save.vault_index, "ensure_index", lambda v, f: "db")
    monkeypatch.setattr(capture_save.vault_index, "compute_query_vector",
                        lambda db, q: {"x": 1.0} if q.strip() else {})
    monkeypatch.setattr(capture_save.vault_index, "search_vault",
                        lambda db, q, note_type=None, limit=3, include_vectors=True:
                        [dict(r) for r in results if r["type"] == note_type])
    if guard:
        monkeypatch.setattr(capture_save.compress_guard, "is_high_confidence_match", lambda res, **kw: True)


def test_rejected_folder_never_matches(mock_config, tmp_vault, monkeypatch):
    _fake_results(monkeypatch, [
        {"path": "claude-insights/_rejected/a.md", "type": "claude-insight", "rank": -40.0},
        {"path": "claude-insights/b.md", "type": "claude-insight", "rank": -30.0},
    ], vault=tmp_vault)
    assert capture_save.find_match("q", mock_config) == "claude-insights/b.md"


def test_session_note_top_result_is_no_match(mock_config, monkeypatch):
    _fake_results(monkeypatch, [
        {"path": "claude-sessions/s.md", "type": "claude-session", "rank": -50.0},
        {"path": "claude-insights/b.md", "type": "claude-insight", "rank": -30.0},
    ])
    assert capture_save.find_match("q", mock_config) is None


def test_find_match_respects_guard(mock_config, tmp_vault, monkeypatch):
    _fake_results(monkeypatch, [{"path": "claude-insights/b.md", "type": "claude-insight", "rank": -30.0}], vault=tmp_vault)
    monkeypatch.setattr(capture_save.compress_guard, "is_high_confidence_match", lambda res, **kw: False)
    assert capture_save.find_match("q", mock_config) is None


def test_session_note_for_composes_name(mock_config, monkeypatch):
    monkeypatch.setattr(capture_save.obsidian_utils, "_first_seen_date", lambda sid: "2026-10-18")
    name = capture_save.session_note_for(SID, "Obsidian Brain", mock_config)
    assert name.startswith("2026-10-18-obsidian-brain-")


def test_find_match_returns_vault_relative_path(mock_config, tmp_vault, monkeypatch):
    abs_path = str(tmp_vault / "claude-insights" / "b.md")
    (tmp_vault / "claude-insights" / "b.md").write_text("# B\n\nOriginal text.\n")
    _fake_results(monkeypatch, [{"path": abs_path, "type": "claude-insight", "rank": -30.0}])
    assert capture_save.find_match("q", mock_config) == "claude-insights/b.md"


def test_find_match_refuses_path_outside_vault(mock_config, monkeypatch):
    _fake_results(monkeypatch, [{"path": "/etc/passwd", "type": "claude-insight", "rank": -30.0}])
    assert capture_save.find_match("q", mock_config) is None


def test_demote_keeps_a_longer_fence_intact():
    body = "````md\n```\n# not a heading\n```\n````\n# Real"
    out = capture_save._demote_headings(body)
    assert "\n# not a heading\n" in out and out.endswith("### Real")


# --- #427: updates must refine the same fact -------------------------------

OTHER = "ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee"
ZEBRA = ("Zebra quokka ledger rotation",
         "The zebra quokka ledger rotates at one hundred kilobytes and keeps one backup file named dot one.")
PELICAN = ("Pelican harbour cron jitter",
           "Pelican harbour scheduling uses cron with random jitter so nightly jobs never collide on the hour.")


def _note(vault, name, title, body, extra=""):
    path = vault / "claude-insights" / name
    path.write_text(f"---\ntype: claude-insight\ndate: 2026-10-01\ntags:\n  - claude/insight\n---\n\n"
                    f"# {title}\n\n{body}\n{extra}")
    return path


def _auto_section(cid, title, body, marker=True):
    end = f"\n{capture_save.end_marker(cid)}\n" if marker else ""
    return f"\n## Update (2026-10-02) — auto-capture {cid}\n\n**{title}**\n\n{body}\n{end}"


def _filler(vault, n=5):
    for i in range(n):
        _note(vault, f"filler-{i}.md", f"Filler {i} topic", f"Unrelated filler words number {i} about gardening tomatoes and soil.")


def _created(sid, rel):
    capture_state.ledger_append({"session_id": sid, "project": "p", "type": "insight", "title": "t",
                                 "status": "active", "kind": "new", "note_path": rel})


def _query(title, body):
    return f"{title}\n{body[:300]}"


def test_notes_created_by_lists_only_that_sessions_new_rows():
    _created(SID, "claude-insights/a.md")
    _created(OTHER, "claude-insights/b.md")
    capture_state.ledger_append({"session_id": SID, "status": "active", "kind": "updated",
                                 "note_path": "claude-insights/c.md"})
    rid = capture_state.ledger_append({"session_id": SID, "status": "active", "kind": "new",
                                       "note_path": "claude-insights/d.md"})
    capture_state.ledger_update(rid, status="rejected")  # any status counts
    assert capture_state.notes_created_by(SID) == {"claude-insights/a.md", "claude-insights/d.md"}
    assert capture_state.notes_created_by("nobody") == set()


def test_same_session_note_is_skipped(mock_config, tmp_vault):
    _filler(tmp_vault)
    _note(tmp_vault, "zebra.md", *ZEBRA)
    _created(SID, "claude-insights/zebra.md")
    q = _query(*ZEBRA)
    assert capture_save.find_match(q, mock_config, session_id=OTHER) == "claude-insights/zebra.md"
    assert capture_save.find_match(q, mock_config, session_id=SID) is None


def test_same_session_ledger_path_is_normalised(mock_config, tmp_vault):
    """A ledger path written with a doubled slash (folder configured as ``claude-insights/``) still matches."""
    _filler(tmp_vault)
    _note(tmp_vault, "zebra.md", *ZEBRA)
    _created(SID, "claude-insights//zebra.md")
    assert capture_save.find_match(_query(*ZEBRA), mock_config, session_id=SID) is None


def test_same_session_skip_lets_the_next_result_match(mock_config, tmp_vault, monkeypatch):
    _created(SID, "claude-insights/own.md")
    _fake_results(monkeypatch, [
        {"path": str(tmp_vault / "claude-insights" / "own.md"), "type": "claude-insight", "rank": -40.0},
        {"path": "claude-insights/other.md", "type": "claude-insight", "rank": -30.0},
    ], vault=tmp_vault)
    (tmp_vault / "claude-insights" / "own.md").write_text("# Own\n\nOriginal text.\n")
    assert capture_save.find_match("q", mock_config, session_id=SID) == "claude-insights/other.md"
    assert capture_save.find_match("q", mock_config, session_id=OTHER) == "claude-insights/own.md"


def test_save_candidate_from_the_creating_session_makes_a_new_note(mock_config, tmp_vault):
    _filler(tmp_vault)
    first = capture_save.save_candidate(_cand(title=ZEBRA[0], body=ZEBRA[1]), session_id=SID, project="p",
                                        session_note="", config=mock_config)
    again = _cand(title=ZEBRA[0], body=ZEBRA[1] + " Rotation happens on write.")
    same = capture_save.save_candidate(again, session_id=SID, project="p", session_note="", config=mock_config)
    other = capture_save.save_candidate(again, session_id=OTHER, project="p", session_note="", config=mock_config)
    assert first["kind"] == "new" and same["kind"] == "new"
    assert other["kind"] == "updated" and other["note_path"] in (first["note_path"], same["note_path"])


def test_candidate_quoting_an_auto_section_is_new(mock_config, tmp_vault):
    """The grown text no longer pulls updates in: only the original text counts."""
    _filler(tmp_vault)
    hub = _note(tmp_vault, "hub.md", *ZEBRA, extra=_auto_section("a1b2c3d4e5f6", *PELICAN))
    q = _query(*PELICAN)
    db = capture_save.vault_index.ensure_index(str(tmp_vault), ["claude-sessions", "claude-insights"],
                                               db_path=capture_save.vault_index._default_db_path())
    full = capture_save.vault_index.compute_query_vector(db, hub.read_text())
    assert _cosine_similarity(capture_save.vault_index.compute_query_vector(db, q), full) >= 0.35  # whole note: would match
    assert capture_save.find_match(q, mock_config, session_id=OTHER) is None


def test_old_unmarked_auto_section_is_stripped_too(mock_config, tmp_vault):
    _filler(tmp_vault)
    _note(tmp_vault, "hub.md", *ZEBRA, extra=_auto_section("a1b2c3d4e5f6", *PELICAN, marker=False))
    assert capture_save.find_match(_query(*PELICAN), mock_config, session_id=OTHER) is None


def test_user_written_update_counts_as_original_text(mock_config, tmp_vault):
    _filler(tmp_vault)
    user = f"\n## Update (2026-10-02)\n\n{PELICAN[1]}\n"
    _note(tmp_vault, "hub.md", *ZEBRA, extra=user)
    assert capture_save.find_match(_query(*PELICAN), mock_config, session_id=OTHER) == "claude-insights/hub.md"


def test_strip_auto_updates_keeps_user_sections_and_tail():
    text = ("# T\n\nOriginal.\n"
            + _auto_section("a1b2c3d4e5f6", "A", "auto one")
            + "typed after the marker\n"
            + "\n## Update (2026-10-03)\n\nuser one\n"
            + _auto_section("0123456789ab", "B", "```\n## not a heading\n```\nauto two", marker=False)
            + "\n## Later\n\ntail\n")
    out = capture_save.strip_auto_updates(text)
    assert "Original." in out and "user one" in out and "tail" in out and "typed after the marker" in out
    assert "auto one" not in out and "auto two" not in out and "auto-capture" not in out


def test_strip_auto_updates_needs_the_exact_heading():
    near = "## Update (2026-10-03) — auto-capture A1B2C3D4E5F6\n\nupper-case id is not ours\n"
    assert capture_save.strip_auto_updates(near) == near


def test_empty_query_vector_saves_as_new(mock_config, tmp_vault, monkeypatch):
    _fake_results(monkeypatch, [{"path": "claude-insights/b.md", "type": "claude-insight", "rank": -30.0}],
                  vault=tmp_vault)
    monkeypatch.setattr(capture_save.vault_index, "compute_query_vector",
                        lambda db, q: {} if q == "the of and" else {"x": 1.0})
    assert capture_save.find_match("the of and", mock_config) is None


def test_unreadable_top_note_saves_as_new(mock_config, tmp_vault, monkeypatch):
    _fake_results(monkeypatch, [{"path": "claude-insights/gone.md", "type": "claude-insight", "rank": -30.0,
                                 "title": "Gone note"}])  # a title alone would give a non-empty vector
    assert capture_save.find_match("q", mock_config) is None


def test_note_with_no_original_text_saves_as_new(mock_config, tmp_vault, monkeypatch):
    _fake_results(monkeypatch, [{"path": "claude-insights/b.md", "type": "claude-insight", "rank": -30.0,
                                 "title": ""}])
    (tmp_vault / "claude-insights" / "b.md").write_text(
        "---\ntype: claude-insight\n---\n" + _auto_section("a1b2c3d4e5f6", "A", "only auto text"))
    assert capture_save.find_match("q", mock_config) is None


def _exact_cosine(monkeypatch, tmp_vault):
    """Query {a:1} against note {a:3, b:4}: cosine exactly 0.6."""
    _fake_results(monkeypatch, [{"path": "claude-insights/b.md", "type": "claude-insight", "rank": -30.0}],
                  vault=tmp_vault, guard=False)
    monkeypatch.setattr(capture_save.vault_index, "compute_query_vector",
                        lambda db, q: {"a": 1.0} if q == "q" else {"a": 3.0, "b": 4.0})


def test_threshold_boundary(mock_config, tmp_vault, monkeypatch):
    _exact_cosine(monkeypatch, tmp_vault)
    assert capture_save.find_match("q", {**mock_config, "auto_capture_update_min_cosine": 0.6}) == "claude-insights/b.md"
    assert capture_save.find_match("q", {**mock_config, "auto_capture_update_min_cosine": 0.61}) is None


def test_default_threshold_is_point_three_five(mock_config, tmp_vault, monkeypatch):
    _exact_cosine(monkeypatch, tmp_vault)
    monkeypatch.setattr(capture_save.vault_index, "compute_query_vector",
                        lambda db, q: {"a": 1.0} if q == "q" else {"a": 1.0, "b": 2.7})  # cosine ~0.347
    assert capture_save.find_match("q", mock_config) is None
    monkeypatch.setattr(capture_save.vault_index, "compute_query_vector",
                        lambda db, q: {"a": 1.0} if q == "q" else {"a": 1.0, "b": 2.6})  # cosine ~0.359
    assert capture_save.find_match("q", mock_config) == "claude-insights/b.md"


@pytest.mark.parametrize("value", ["0.5", "abc", None, True, -0.1, 1.5, float("nan"), [0.5]])
def test_bad_threshold_falls_back_to_default(value):
    assert capture_save.update_min_cosine({"auto_capture_update_min_cosine": value}) == 0.35


@pytest.mark.parametrize("value", [0, 0.0, 0.5, 1])
def test_good_threshold_is_used(value):
    assert capture_save.update_min_cosine({"auto_capture_update_min_cosine": value}) == value


def test_threshold_default_is_in_config_defaults():
    import obsidian_utils
    assert obsidian_utils._DEFAULTS["auto_capture_update_min_cosine"] == 0.35


def _note_by(vault, name, sid, body_extra=""):
    """A hand-written note whose frontmatter names the session that wrote it."""
    path = vault / "claude-insights" / name
    path.write_text(f"---\ntype: claude-insight\ndate: 2026-10-01\nsource_session: {sid}\ntags:\n  - claude/insight\n---\n\n"
                    f"# {ZEBRA[0]}\n\n{ZEBRA[1]}\n{body_extra}")
    return path


def test_note_the_session_wrote_by_hand_is_skipped(mock_config, tmp_vault):
    """No ledger row: the frontmatter source_session alone makes it the session's own note."""
    _filler(tmp_vault)
    _note_by(tmp_vault, "zebra.md", SID)
    q = _query(*ZEBRA)
    assert capture_save.find_match(q, mock_config, session_id=SID) is None
    assert capture_save.find_match(q, mock_config, session_id=OTHER) == "claude-insights/zebra.md"


def test_source_session_outside_the_frontmatter_does_not_count(mock_config, tmp_vault):
    _filler(tmp_vault)
    _note(tmp_vault, "zebra.md", *ZEBRA, extra=f"\nsource_session: {SID}\n")
    assert capture_save.find_match(_query(*ZEBRA), mock_config, session_id=SID) == "claude-insights/zebra.md"


def test_note_without_source_session_still_goes_through_the_cosine_floor(mock_config, tmp_vault):
    _filler(tmp_vault)
    _note(tmp_vault, "zebra.md", *ZEBRA)  # frontmatter, but no source_session
    _note(tmp_vault, "osprey-hub.md", "Osprey lantern websocket retries",
          "Osprey lantern websocket clients retry with exponential backoff capped at thirty seconds.",
          extra=_auto_section("a1b2c3d4e5f6", *PELICAN))
    assert capture_save.find_match(_query(*ZEBRA), mock_config, session_id=SID) == "claude-insights/zebra.md"
    assert capture_save.find_match(_query(*PELICAN), mock_config, session_id=SID) is None


def test_unreadable_note_does_not_match_the_same_session_rule(mock_config, tmp_vault, monkeypatch):
    """A note whose frontmatter cannot be read is not the session's own; it still goes through the cosine floor."""
    _fake_results(monkeypatch, [{"path": "claude-insights/b.md", "type": "claude-insight", "rank": -30.0}],
                  vault=tmp_vault)
    monkeypatch.setattr(capture_save.obsidian_utils, "_peek_frontmatter_field", lambda path, field: None)
    assert capture_save.find_match("q", mock_config, session_id=SID) == "claude-insights/b.md"
