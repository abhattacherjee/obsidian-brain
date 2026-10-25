"""#458: /recall's related list keeps at most 1 retro (the first in layer order) and fills the freed slots."""

import builtins
import sqlite3
import sys
from pathlib import Path

import pytest

import obsidian_utils
import related_select
import vault_index

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
FOLDERS = ["claude-sessions", "claude-insights"]
ALL_TYPES = ["claude-insight", "claude-decision", "claude-error-fix", "claude-retro"]


def _note(vault, name, *, ntype="claude-insight", date="2026-10-01", session="other", tags=(), body="plain words"):
    fm = ["---", f"type: {ntype}", f"date: {date}", f"source_session: {session}", "project: demo", "tags:",
          "  - claude/project/demo", *[f"  - {t}" for t in tags], "---", "", f"# Title {name}", "", body, ""]
    p = Path(vault) / "claude-insights" / f"{name}.md"
    p.write_text("\n".join(fm))
    return p


def _index(tmp_vault, tmp_path):
    return vault_index.ensure_index(str(tmp_vault), FOLDERS, db_path=str(tmp_path / "idx.db"))


def _related(db, *, limit=20, tags=(), summary="", note_types=ALL_TYPES):
    hits = vault_index.query_related_notes(db, "demo", [SID], list(tags), summary, note_types=note_types, limit=limit)
    return [Path(h["path"]).stem for h in hits]


# --- select_related (pure) -------------------------------------------------

def _rows(*types):
    return [{"path": f"p{i}", "type": t} for i, t in enumerate(types)]


def test_select_related_keeps_the_first_retro_only_and_fills_limit():
    rows = _rows("claude-retro", "claude-insight", "claude-retro", "claude-decision", "claude-retro", "claude-insight")
    got = [r["path"] for r in related_select.select_related(rows, 4)]
    assert got == ["p0", "p1", "p3", "p5"]


def test_select_related_limit_zero_and_null_type():
    rows = _rows(None, "claude-retro", None)
    assert related_select.select_related(rows, 0) == []
    assert related_select.select_related(rows, -1) == []
    assert [r["path"] for r in related_select.select_related(rows, 3)] == ["p0", "p1", "p2"]  # NULL type is not a retro


def test_select_related_same_date_retros_keep_input_order():
    rows = [{"path": "first", "type": "claude-retro", "date": "2026-10-24"},
            {"path": "second", "type": "claude-retro", "date": "2026-10-24"}]
    assert [r["path"] for r in related_select.select_related(rows, 20)] == ["first"]


def test_select_related_max_retros_is_a_keyword():
    rows = _rows("claude-retro", "claude-retro", "claude-retro")
    assert related_select.RELATED_MAX_RETROS == 1
    assert [r["path"] for r in related_select.select_related(rows, 20, max_retros=2)] == ["p0", "p1"]
    assert related_select.select_related(rows, 20, max_retros=0) == []
    assert vault_index.select_related is related_select.select_related  # one implementation


# --- query_related_notes ---------------------------------------------------

def test_five_backlinked_retros_give_one_retro_the_newest_and_twenty_results(tmp_vault, tmp_path):
    for d in range(20, 25):
        _note(tmp_vault, f"retro-{d}", ntype="claude-retro", date=f"2026-10-{d}", session=SID)
    for d in range(1, 31):
        _note(tmp_vault, f"ins-{d:02d}", date=f"2026-09-{d:02d}", session=SID)
    got = _related(_index(tmp_vault, tmp_path))
    assert len(got) == 20
    assert got[0] == "retro-24"
    assert [g for g in got if g.startswith("retro")] == ["retro-24"]
    assert got[1:] == [f"ins-{d:02d}" for d in range(30, 11, -1)]


def test_retros_in_tag_and_fts_layers_are_skipped_once_one_is_kept(tmp_vault, tmp_path):
    _note(tmp_vault, "l1-retro", ntype="claude-retro", date="2026-10-10", session=SID)
    _note(tmp_vault, "l1-ins", date="2026-10-09", session=SID)
    for i in range(2):
        _note(tmp_vault, f"tag-retro-{i}", ntype="claude-retro", date="2026-10-20", tags=["claude/topic/kiwi"])
        _note(tmp_vault, f"tag-ins-{i}", tags=["claude/topic/kiwi"])
        _note(tmp_vault, f"fts-retro-{i}", ntype="claude-retro", date="2026-10-21", body="zebrafinch mangosteen")
        _note(tmp_vault, f"fts-ins-{i}", body="zebrafinch mangosteen")
    got = _related(_index(tmp_vault, tmp_path), tags=["claude/topic/kiwi"], summary="zebrafinch mangosteen")
    assert [g for g in got if "retro" in g] == ["l1-retro"]
    assert sorted(g for g in got if "ins" in g) == ["fts-ins-0", "fts-ins-1", "l1-ins", "tag-ins-0", "tag-ins-1"]


def test_a_layer_is_not_stopped_early_by_skipped_retros(tmp_vault, tmp_path):
    _note(tmp_vault, "l1-ins", date="2026-10-01", session=SID)
    for d in range(10, 16):  # 7 Layer 1 candidates >= limit 5, but only 2 are selectable
        _note(tmp_vault, f"l1-retro-{d}", ntype="claude-retro", date=f"2026-10-{d}", session=SID)
    _note(tmp_vault, "tag-a", date="2026-09-02", tags=["claude/topic/kiwi"])
    _note(tmp_vault, "tag-b", date="2026-09-01", tags=["claude/topic/kiwi"])
    _note(tmp_vault, "fts-a", body="zebrafinch mangosteen")
    got = _related(_index(tmp_vault, tmp_path), limit=5, tags=["claude/topic/kiwi"], summary="zebrafinch mangosteen")
    assert got == ["l1-retro-15", "l1-ins", "tag-a", "tag-b", "fts-a"]


def test_layer3_fills_slots_freed_by_skipped_retros(tmp_vault, tmp_path):
    strong = " ".join(["zebrafinch mangosteen"] * 30)
    for i in range(6):  # the FTS top ranks are all retros
        _note(tmp_vault, f"fts-retro-{i}", ntype="claude-retro", body=strong)
    for i in range(5):
        _note(tmp_vault, f"fts-ins-{i}", body="zebrafinch mangosteen and many other unrelated filler words here")
    got = _related(_index(tmp_vault, tmp_path), limit=5, summary="zebrafinch mangosteen")
    assert len(got) == 5
    assert sum("retro" in g for g in got) == 1


def test_layer3_fills_when_its_top_rows_repeat_layer1(tmp_vault, tmp_path):
    """Rows already taken in Layer 1 come back from FTS too; they must not use up its fetch."""
    strong = " ".join(["zebrafinch mangosteen"] * 30)
    for i in range(3):
        _note(tmp_vault, f"l1-retro-{i}", ntype="claude-retro", date=f"2026-10-1{i}", session=SID, body=strong)
    for i in range(3):
        _note(tmp_vault, f"fts-retro-{i}", ntype="claude-retro", body=strong)
    for i in range(2):
        _note(tmp_vault, f"fts-ins-{i}", body="zebrafinch mangosteen and many other unrelated filler words here")
    got = _related(_index(tmp_vault, tmp_path), limit=3, summary="zebrafinch mangosteen")
    assert got[0] == "l1-retro-2"
    assert sorted(got[1:]) == ["fts-ins-0", "fts-ins-1"]


def test_note_types_without_retros_is_unchanged(tmp_vault, tmp_path):
    for d in range(1, 26):
        _note(tmp_vault, f"ins-{d:02d}", date=f"2026-09-{d:02d}", session=SID)
    _note(tmp_vault, "retro", ntype="claude-retro", date="2026-10-24", session=SID)
    got = _related(_index(tmp_vault, tmp_path), note_types=["claude-insight", "claude-decision"])
    assert got == [f"ins-{d:02d}" for d in range(25, 5, -1)]


def test_access_is_logged_only_for_selected_rows(tmp_vault, tmp_path):
    for d in range(20, 25):
        _note(tmp_vault, f"retro-{d}", ntype="claude-retro", date=f"2026-10-{d}", session=SID)
    for d in range(1, 4):
        _note(tmp_vault, f"ins-{d}", date=f"2026-09-0{d}", session=SID)
    db = _index(tmp_vault, tmp_path)
    got = _related(db)
    con = sqlite3.connect(db)
    try:
        logged = sorted(Path(p).stem for (p,) in con.execute(
            "SELECT note_path FROM access_log WHERE context_type = 'related'"))
    finally:
        con.close()
    assert logged == sorted(got) == ["ins-1", "ins-2", "ins-3", "retro-24"]


# --- /recall file-scan fallback --------------------------------------------

@pytest.fixture
def fallback_vault(mock_config, monkeypatch):
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: "test-sid")
    monkeypatch.setattr(obsidian_utils, "cache_get", lambda *a: None)
    monkeypatch.setattr(obsidian_utils, "cache_set", lambda *a: None)
    v = mock_config["vault_path"]
    for d in range(20, 25):
        _note(v, f"2026-10-{d}-retro", ntype="claude-retro", date=f"2026-10-{d}")
    for d in range(1, 3):
        _note(v, f"2026-09-0{d}-ins", date=f"2026-09-0{d}")
    return v


def _fallback_brief(v):
    brief = obsidian_utils.build_context_brief(v, "claude-sessions", "claude-insights", "demo")
    return [t for t in ("2026-10-20-retro", "2026-10-21-retro", "2026-10-22-retro", "2026-10-23-retro",
                        "2026-10-24-retro", "2026-09-01-ins", "2026-09-02-ins") if f"Title {t}" in brief], brief


def test_file_scan_fallback_keeps_one_retro_without_vault_index(fallback_vault, monkeypatch):
    monkeypatch.delitem(sys.modules, "vault_index", raising=False)
    real = builtins.__import__

    def no_index(name, *args, **kwargs):
        if name == "vault_index":
            raise ImportError("vault_index not available")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_index)
    shown, brief = _fallback_brief(fallback_vault)
    assert shown == ["2026-10-24-retro", "2026-09-01-ins", "2026-09-02-ins"]
    assert "insight_count: 3" in brief


def test_file_scan_fallback_keeps_one_retro_when_the_index_fails(fallback_vault, monkeypatch):
    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(vault_index, "ensure_index", broken)
    shown, _ = _fallback_brief(fallback_vault)
    assert shown == ["2026-10-24-retro", "2026-09-01-ins", "2026-09-02-ins"]


def test_layer3_pages_past_retros_that_outrank_every_insight(tmp_vault, tmp_path):
    # 41 retros rank above 20 matching insights; one fetch of limit*2 rows would give 1 retro and nothing else.
    for i in range(41):
        _note(tmp_vault, f"retro-{i:02d}", ntype="claude-retro", body="zebrafish " * 30)
    for i in range(20):
        _note(tmp_vault, f"ins-{i:02d}", body="zebrafish once")
    got = _related(_index(tmp_vault, tmp_path), summary="zebrafish")
    assert len(got) == 20
    assert len([g for g in got if g.startswith("retro")]) == 1
    assert len([g for g in got if g.startswith("ins")]) == 19


def test_query_related_notes_passes_max_retros_through(tmp_vault, tmp_path):
    for i in range(4):
        _note(tmp_vault, f"retro-{i}", ntype="claude-retro", session=SID)
    db = _index(tmp_vault, tmp_path)
    two = vault_index.query_related_notes(db, "demo", [SID], [], "", note_types=ALL_TYPES, max_retros=2)
    assert len(two) == 2
    none = vault_index.query_related_notes(db, "demo", [SID], [], "", note_types=ALL_TYPES, max_retros=0)
    assert none == []
