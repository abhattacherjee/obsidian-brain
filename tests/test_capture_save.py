"""Tests for hooks/capture_save.py (#425)."""

import datetime
import re
import stat
import time
from pathlib import Path
from types import SimpleNamespace

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
    name = Path(row["note_path"]).name
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
    assert "type: claude-decision" in dfront and "status: provisional" in dfront
    assert "status: active" not in dfront  # #455: the template's status is replaced, not doubled
    assert "type: claude-error-fix" in efront
    assert "source_session_note" not in dfront  # omitted, not written empty


@pytest.mark.parametrize("ctype", ["insight", "decision", "error-fix"])
def test_every_new_note_is_saved_provisional(mock_config, tmp_vault, no_match, ctype):
    """#455: a new auto note starts out of the default retrieval pool; selection promotes it."""
    row = capture_save.save_candidate(_cand(type=ctype, score=8, about_issue=12), session_id=SID, project="p",
                                      session_note="", config=mock_config, repo="/work/p")
    front, _ = _front(tmp_vault / row["note_path"])
    assert re.findall(r"^status: .*$", front, flags=re.M) == ["status: provisional"]
    assert "source: auto-capture\nstatus: provisional\n" in front or ctype == "decision"
    assert (row["score"], row["about_issue"], row["repo"]) == (8, 12, "/work/p")
    assert capture_state.ledger_get(row["id"])["score"] == 8


def test_row_fields_are_none_without_worker_data(mock_config, tmp_vault, no_match):
    row = capture_save.save_candidate(_cand(), session_id=SID, project="p", session_note="", config=mock_config)
    assert row["score"] is None and row["about_issue"] is None and row["repo"] is None


def test_update_path_writes_no_status(mock_config, tmp_vault, monkeypatch):
    existing = tmp_vault / "claude-insights" / "2026-10-01-old-note-abcd.md"
    existing.write_text("---\ntype: claude-insight\ndate: 2026-10-01\ntags:\n  - claude/insight\n---\n\n# Old\n\nBody.\n")
    monkeypatch.setattr(capture_save, "find_match", lambda *a, **k: "claude-insights/2026-10-01-old-note-abcd.md")
    row = capture_save.save_candidate(_cand(score=9), session_id=SID, project="p", today="2026-10-18", config=mock_config)
    assert row["kind"] == "updated" and "status:" not in existing.read_text()


def test_frontmatter_status_none_keeps_the_template():
    front = capture_save._frontmatter(_cand(type="decision"), session_id=SID, project="p", session_note="",
                                      today="2026-10-18", status=None)
    assert "status: active" in front and "provisional" not in front


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
    def fake_search(db, q, note_type=None, limit=3, include_vectors=True, log_access=True):
        # Dedup is not a read: its hits must not feed activation (#448).
        assert log_access is False
        return [dict(r) for r in results if r["type"] == note_type]

    monkeypatch.setattr(capture_save.vault_index, "search_vault", fake_search)
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
    note = tmp_vault / first["note_path"]  # drop source_session: only the ledger rule can see SID made it
    note.write_text(re.sub(r"^source_session: .*\n", "", note.read_text(), flags=re.M))
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


@pytest.mark.parametrize("heading", [
    "## Update (2026-10-20) — auto-capture abcdef123456 extra",   # trailing text after the id
    "## Update (2026-10-20) — auto-capture abc",                  # id too short
    "## Update (2026-10-20) — auto-capture abcdef1234567",        # id too long
    "## Update (2026-10-20) — auto-capture abcdef12345g",         # not hex
    "## Update (2026-10-20) — auto-capture abcdef123456 ",        # trailing space
    "### Update (2026-10-20) — auto-capture abcdef123456",        # wrong level
])
def test_only_the_exact_auto_heading_is_ours(heading):
    """A near-miss heading is a hand heading: strip_auto_updates keeps it and hand_updated sees it (C-015)."""
    assert capture_save.AUTO_HEADING_RE.match(heading) is None
    text = f"# T\n\nOriginal.\n\n{heading}\n\nhand words\n"
    assert capture_save.strip_auto_updates(text) == text


def test_the_exact_auto_heading_matches():
    assert capture_save.AUTO_HEADING_RE.match("## Update (2026-10-20) — auto-capture abcdef123456")
    assert capture_save.AUTO_HEADING_RE.match("## Update (2026-10-20) — auto-capture abcdef123456\n") is None


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
    (tmp_vault / "claude-insights" / "b.md").write_text(
        f"---\ntype: claude-insight\nsource_session: {SID}\n---\n\n# Note\n\nOriginal text.\n")
    assert capture_save.find_match("q", mock_config, session_id=SID) is None  # readable: SID's own note
    monkeypatch.setattr(capture_save.obsidian_utils, "_peek_frontmatter_field", lambda path, field: None)
    assert capture_save.find_match("q", mock_config, session_id=SID) == "claude-insights/b.md"


# --- #427 review fixes ---------------------------------------------------------

def test_hub_with_auto_sections_still_takes_an_on_topic_update(mock_config, tmp_vault):
    """AC3 inverse: an auto section on a note must not stop a refinement of its original fact."""
    _filler(tmp_vault)
    _note(tmp_vault, "hub.md", *ZEBRA, extra=_auto_section("a1b2c3d4e5f6", *PELICAN))
    more = ("Zebra quokka ledger rotation backup",
            "The zebra quokka ledger backup file dot one is replaced on each rotation at one hundred kilobytes.")
    assert capture_save.find_match(_query(*more), mock_config, session_id=OTHER) == "claude-insights/hub.md"


def _two_results(monkeypatch, tmp_vault, runner_up_rank):
    """Top note at cosine ~0.371 (query {a:1}, note {a:1, b:2.5}); a runner-up at ``runner_up_rank``."""
    _fake_results(monkeypatch, [{"path": "claude-insights/b.md", "type": "claude-insight", "rank": -30.0},
                                {"path": "claude-insights/c.md", "type": "claude-insight", "rank": runner_up_rank}],
                  vault=tmp_vault, guard=False)
    monkeypatch.setattr(capture_save.vault_index, "compute_query_vector",
                        lambda db, q: {"a": 1.0} if q == "q" else {"a": 1.0, "b": 2.5})


def test_close_runner_up_needs_the_rescue_floor_not_the_threshold(mock_config, tmp_vault, monkeypatch):
    _two_results(monkeypatch, tmp_vault, -10.0)  # far apart: the rank gap passes, 0.371 >= 0.35
    assert capture_save.find_match("q", mock_config) == "claude-insights/b.md"
    _two_results(monkeypatch, tmp_vault, -29.9)  # close: only a rescue (>= 0.40) could pass it
    assert capture_save.find_match("q", mock_config) is None


def test_find_match_error_saves_a_new_note_and_logs_it(mock_config, tmp_vault, monkeypatch):
    def boom(*a, **k):
        raise capture_save.vault_index.sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(capture_save, "find_match", boom)
    logged = []
    monkeypatch.setattr(capture_save.capture_state, "append_capture_log", lambda *a: logged.append(a))
    row = capture_save.save_candidate(_cand(), session_id=SID, project="p", session_note="", config=mock_config)
    assert row["kind"] == "new" and (tmp_vault / row["note_path"]).exists()
    assert len(logged) == 1 and logged[0][2] == "match-failed" and "database is locked" in logged[0][3]


def test_bom_note_frontmatter_is_stripped(tmp_vault):
    note = tmp_vault / "claude-insights" / "bom.md"
    note.write_bytes("\ufeff---\ntype: claude-insight\nproject: secretproject\n---\n\n# T\n\nBody.\n".encode())
    text = capture_save.original_text(note)
    assert "secretproject" not in text and "Body." in text and "\ufeff" not in text


def test_one_scorer_for_find_match_and_split(mock_config, tmp_vault):
    """split-updates and find_match score with the same original-text vector."""
    _filler(tmp_vault)
    hub = _note(tmp_vault, "hub.md", *ZEBRA, extra=_auto_section("a1b2c3d4e5f6", *PELICAN))
    db = capture_save.vault_index.ensure_index(str(tmp_vault), ["claude-sessions", "claude-insights"],
                                               db_path=capture_save.vault_index._default_db_path())
    vec = capture_save.original_vector(db, hub)
    assert vec == capture_save.vault_index.compute_query_vector(db, f"{ZEBRA[0]}\n{capture_save.original_text(hub)}")
    assert "pelican" not in vec and "insight" not in vec


def test_auto_heading_quoted_in_a_fence_stays_original_text():
    quoted = "## Update (2026-10-02) — auto-capture a1b2c3d4e5f6"
    text = f"# T\n\nHow a section looks:\n\n```markdown\n{quoted}\n\nquoted body\n```\n\nAfter the fence.\n"
    out = capture_save.strip_auto_updates(text)
    assert quoted in out and "quoted body" in out and "After the fence." in out


# --- #465: provisional placement and the local-day update cap -----------------

@pytest.fixture
def capture_env(mock_config, tmp_path, monkeypatch):
    """All new placement/cap tests use an explicit temporary DB and ledger."""
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", str(tmp_path / "capture-test.db"))
    monkeypatch.setattr(capture_save.obsidian_utils, "_SECURE_DIR", str(tmp_path / "capture-state"))
    monkeypatch.setattr(capture_save, "find_match", lambda *a, **k: None)
    return mock_config


def _capture_clock(monkeypatch, day="2026-10-28"):
    date = datetime.date.fromisoformat(day)
    midnight = datetime.datetime.combine(date, datetime.time.min).timestamp()
    now = datetime.datetime.combine(date, datetime.time(12)).timestamp()

    class FakeDate(datetime.date):
        @classmethod
        def today(cls):
            return date

    class FakeDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime.fromtimestamp(now, tz)

    monkeypatch.setattr(capture_save, "datetime", SimpleNamespace(
        date=FakeDate, datetime=FakeDatetime, time=datetime.time))
    monkeypatch.setattr(capture_state, "time", SimpleNamespace(time=lambda: now))
    return midnight, now


def _cap_target(config, monkeypatch, provisional=False):
    folder = Path(config["insights_folder"])
    if provisional:
        folder /= "_provisional"
    rel = (folder / "target.md").as_posix()
    path = Path(config["vault_path"]) / rel
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text("---\ntype: claude-insight\nstatus: provisional\ntags: [claude/insight]\n---\n\n# Target\n\nOriginal.\n")
    monkeypatch.setattr(capture_save, "find_match", lambda *a, **k: rel)
    return rel, path


def _save_for_cap(config):
    return capture_save.save_candidate(_cand(), session_id=SID, project="p", session_note="", config=config)


@pytest.mark.parametrize("folder", ["claude-insights", "custom/insights"])
def test_new_capture_uses_private_provisional_home(capture_env, folder):
    capture_env["insights_folder"] = folder
    ledger = capture_state.capture_dir() / capture_state.LEDGER_NAME
    assert not ledger.exists()  # missing ledger is a normal first capture
    row = _save_for_cap(capture_env)
    note = Path(capture_env["vault_path"]) / row["note_path"]
    assert row["note_path"] == f"{folder}/_provisional/{note.name}"
    assert "status: provisional\n" in note.read_text()
    assert stat.S_IMODE(note.stat().st_mode) == 0o600
    assert stat.S_IMODE(note.parent.stat().st_mode) == 0o700
    assert capture_state.ledger_get(row["id"])["note_path"] == row["note_path"]
    assert "capped_update_of" not in row


@pytest.mark.parametrize("home", ["", "_provisional"])
@pytest.mark.parametrize("occupied_by", ["file", "directory", "dangling-symlink"])
def test_new_filename_checks_both_r1_homes(capture_env, monkeypatch, home, occupied_by):
    folder = Path(capture_env["vault_path"]) / capture_env["insights_folder"]
    (folder / home).mkdir(mode=0o700, exist_ok=True)
    occupied = folder / home / "2026-10-28-collision-dead.md"
    if occupied_by == "directory":
        occupied.mkdir()
    elif occupied_by == "dangling-symlink":
        occupied.symlink_to(occupied.parent / "missing.md")
    else:
        occupied.write_text("existing")
    hashes = iter(["dead", "beef"])
    monkeypatch.setattr(capture_save, "_hash4", lambda: next(hashes))
    assert capture_save.new_filename(str(folder.parent), folder.name, "2026-10-28", "collision") == \
        "2026-10-28-collision-beef.md"


def test_three_updates_append_and_fourth_is_new(capture_env, monkeypatch):
    _capture_clock(monkeypatch)
    rel, target = _cap_target(capture_env, monkeypatch)
    assert capture_save.MAX_UPDATES_PER_NOTE_PER_DAY == 3
    rows = [_save_for_cap(capture_env) for _ in range(3)]
    assert all(row["kind"] == "updated" and row["note_path"] == rel for row in rows)
    before = target.read_bytes()
    capped = _save_for_cap(capture_env)
    assert target.read_bytes() == before
    assert before.count(b"## Update (") == 3
    assert capped["kind"] == "new" and capped["capped_update_of"] == rel
    assert Path(capped["note_path"]).parent.as_posix() == "claude-insights/_provisional"
    assert "update_heading" not in capped
    assert capture_state.ledger_get(capped["id"])["capped_update_of"] == rel
    assert "status: provisional\n" in (target.parents[1] / capped["note_path"]).read_text()


def test_old_home_rows_still_cap_a_moved_note(capture_env, monkeypatch):
    _, now = _capture_clock(monkeypatch)
    rel, target = _cap_target(capture_env, monkeypatch, provisional=True)
    for _ in range(3):
        capture_state.ledger_append({"kind": "updated", "note_path": "claude-insights/target.md",
                                     "status": "active", "ts": now})
    before = target.read_bytes()
    capped = _save_for_cap(capture_env)
    assert capped["kind"] == "new" and capped["capped_update_of"] == rel
    assert target.read_bytes() == before


def test_rejected_update_and_new_rows_do_not_count(capture_env, monkeypatch):
    _, now = _capture_clock(monkeypatch)
    rel, _ = _cap_target(capture_env, monkeypatch)
    ids = [capture_state.ledger_append({"kind": "updated", "note_path": rel,
                                       "status": "active", "ts": now}) for _ in range(3)]
    capture_state.ledger_update(ids[-1], status="rejected")
    capture_state.ledger_append({"kind": "new", "note_path": rel, "status": "active", "ts": now})
    third = _save_for_cap(capture_env)
    assert third["kind"] == "updated" and "capped_update_of" not in third
    assert _save_for_cap(capture_env)["kind"] == "new"


def test_cap_reads_past_default_ledger_limit(capture_env, monkeypatch):
    _, now = _capture_clock(monkeypatch)
    rel, _ = _cap_target(capture_env, monkeypatch)
    ids = [capture_state.ledger_append({"kind": "updated", "note_path": rel,
                                       "status": "active", "ts": now - 10}) for _ in range(3)]
    # A superseding row is the same update; it keeps its original timestamp.
    capture_state.ledger_update(ids[0], note_path="claude-insights/_provisional/target.md", ts=now)
    for _ in range(205):
        capture_state.ledger_append({"kind": "updated", "note_path": "claude-insights/unrelated.md",
                                     "status": "active", "ts": now})
    assert _save_for_cap(capture_env)["capped_update_of"] == rel


@pytest.mark.parametrize("day", ["2026-10-28", "2026-03-08", "2026-11-01"])
def test_update_cap_resets_at_local_midnight(capture_env, monkeypatch, day):
    # Explicit non-UTC local time, including both DST changes. Midnight's
    # offset may differ from noon's, so reusing now().utcoffset() is wrong.
    try:
        with monkeypatch.context() as local:
            local.setenv("TZ", "MST7MDT,M3.2.0,M11.1.0")
            time.tzset()
            midnight, _ = _capture_clock(local, day)
            rel, _ = _cap_target(capture_env, local)
            for offset in (-1, 0, 1):
                capture_state.ledger_append({"kind": "updated", "note_path": rel,
                                             "status": "active", "ts": midnight + offset})
            # Two count (inclusive since); the pre-midnight row does not.
            assert _save_for_cap(capture_env)["kind"] == "updated"
            assert _save_for_cap(capture_env)["capped_update_of"] == rel
    finally:
        time.tzset()


def test_superseding_yesterdays_update_does_not_make_it_today(capture_env, monkeypatch):
    midnight, now = _capture_clock(monkeypatch)
    rel, _ = _cap_target(capture_env, monkeypatch)
    cid = capture_state.ledger_append({"kind": "updated", "note_path": rel, "status": "active", "ts": midnight - 1})
    capture_state.ledger_update(cid, ts=now, note_path="claude-insights/_provisional/target.md")
    for _ in range(2):
        capture_state.ledger_append({"kind": "updated", "note_path": rel, "status": "active", "ts": now})
    assert _save_for_cap(capture_env)["kind"] == "updated"


def test_unreadable_cap_ledger_never_appends_unchecked(capture_env, monkeypatch):
    _capture_clock(monkeypatch)
    _, target = _cap_target(capture_env, monkeypatch)
    before = target.read_bytes()

    def unreadable(*args, **kwargs):
        raise OSError("ledger unreadable")

    monkeypatch.setattr(capture_state, "ledger_list", unreadable)
    with pytest.raises(OSError, match="ledger unreadable"):
        _save_for_cap(capture_env)
    assert target.read_bytes() == before


@pytest.mark.parametrize("folder", ["../escape", ".obsidian/plugins", "/absolute"])
def test_invalid_provisional_folder_creates_nothing(capture_env, folder):
    capture_env["insights_folder"] = folder
    with pytest.raises(ValueError):
        _save_for_cap(capture_env)
    assert capture_state.ledger_list() == []


def test_provisional_home_symlink_cannot_escape(capture_env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    vault = tmp_path / "vault"
    (vault / "insights").mkdir(parents=True)
    (vault / "insights" / "_provisional").symlink_to(outside, target_is_directory=True)
    capture_env.update(vault_path=str(vault), insights_folder="insights")
    with pytest.raises(ValueError):
        _save_for_cap(capture_env)
    assert list(outside.iterdir()) == [] and capture_state.ledger_list() == []


def test_failed_capped_write_never_logs_a_success(capture_env, monkeypatch):
    _, now = _capture_clock(monkeypatch)
    rel, target = _cap_target(capture_env, monkeypatch)
    for _ in range(3):
        capture_state.ledger_append({"kind": "updated", "note_path": rel, "status": "active", "ts": now})
    before = target.read_bytes()
    with monkeypatch.context() as failure:
        failure.setattr(capture_save.note_writer, "run_write", lambda *a, **k: 1)
        with pytest.raises(RuntimeError, match="write failed"):
            _save_for_cap(capture_env)
    assert target.read_bytes() == before and len(capture_state.ledger_list()) == 3
    assert _save_for_cap(capture_env)["capped_update_of"] == rel  # retry still takes the capped path


def test_relocation_between_append_and_ledger_write_cannot_orphan_the_row(capture_env, monkeypatch):
    """X-003: the update row never names a path that a concurrent move emptied."""
    import note_placement
    import vault_index
    rel, target = _cap_target(capture_env, monkeypatch)
    vault = capture_env["vault_path"]
    db = vault_index.ensure_index(vault, [capture_env["insights_folder"]],
                                  db_path=str(capture_state.capture_dir().parent / "reloc.db"))
    real, attempts = capture_state.ledger_append, []

    def append_after_a_move_attempt(row):
        if not attempts:
            attempts.append(note_placement.place(capture_env, target, "provisional", db))
        return real(row)

    monkeypatch.setattr(capture_state, "ledger_append", append_after_a_move_attempt)
    row = _save_for_cap(capture_env)
    assert attempts, "the move attempt never ran"
    assert row["kind"] == "updated"
    merged = {r["id"]: r for r in capture_state._merged_rows()}
    assert (Path(vault) / merged[row["id"]]["note_path"]).is_file()
    assert not list(Path(vault).rglob("*.ob-lock"))
