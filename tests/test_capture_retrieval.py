"""#455: provisional auto notes are out of /recall related insights and /vault-ask by default."""

import builtins
import sys
from pathlib import Path

import pytest

import capture_select
import capture_state
import obsidian_utils
import vault_index

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
FOLDERS = ["claude-sessions", "claude-insights"]


def _write(vault, rel, fm: dict, body: str):
    lines = ["---"] + [f"{k}: {v}" for k, v in fm.items() if k != "tags"] + ["tags:"]
    lines += [f"  - {t}" for t in fm.get("tags", [])] + ["---", "", body, ""]
    p = Path(vault) / rel
    p.write_text("\n".join(lines))
    return p


def _insight(vault, name, *, status=None, session=SID, tags=(), body="plain words", title=None):
    fm = {"type": "claude-insight", "date": "2026-10-23", "created_at": "2026-10-23T10:00:00+00:00",
          "source_session": session, "project": "demo", "source": "auto-capture"}
    if status:
        fm["status"] = status
    fm["tags"] = ["claude/insight", "claude/project/demo", *tags]
    return _write(vault, f"claude-insights/{name}.md", fm, f"# {title or name}\n\n{body}")


def _related(db, include_provisional=False):
    hits = vault_index.query_related_notes(
        db, "demo", [SID], ["claude/topic/kiwi"], "zebrafinch mangosteen",
        note_types=["claude-insight"], limit=20, include_provisional=include_provisional)
    return {Path(h["path"]).stem: h["layer"] for h in hits}


def test_query_related_notes_excludes_provisional_in_every_layer(tmp_vault, tmp_path):
    v = str(tmp_vault)
    for status in ("provisional", "promoted", None):
        tag = status or "legacy"
        _insight(v, f"backlink-{tag}", status=status)
        _insight(v, f"tag-{tag}", status=status, session="other", tags=["claude/topic/kiwi"])
        _insight(v, f"fts-{tag}", status=status, session="other", body="zebrafinch mangosteen notes")
    db = vault_index.ensure_index(v, FOLDERS, db_path=str(tmp_path / "idx.db"))
    got = _related(db)
    assert got == {"backlink-promoted": "backlink", "backlink-legacy": "backlink", "tag-promoted": "tag",
                   "tag-legacy": "tag", "fts-promoted": "fts", "fts-legacy": "fts"}
    every = _related(db, include_provisional=True)
    assert {k: every[k] for k in ("backlink-provisional", "tag-provisional", "fts-provisional")} == \
        {"backlink-provisional": "backlink", "tag-provisional": "tag", "fts-provisional": "fts"}


def test_search_vault_includes_provisional_unless_asked_not_to(tmp_vault, tmp_path):
    v = str(tmp_vault)
    _insight(v, "prov", status="provisional", body="quokka facts")
    _insight(v, "kept", status="promoted", body="quokka facts")
    _insight(v, "legacy", body="quokka facts")
    db = vault_index.ensure_index(v, FOLDERS, db_path=str(tmp_path / "idx.db"))
    default = {Path(r["path"]).stem for r in vault_index.search_vault(db, "quokka", log_access=False)}
    strict = {Path(r["path"]).stem for r in vault_index.search_vault(db, "quokka", log_access=False,
                                                                       include_provisional=False)}
    assert default == {"prov", "kept", "legacy"} and strict == {"kept", "legacy"}


# --- /recall brief: both promotion paths -------------------------------------------

@pytest.fixture
def recall_vault(mock_config, monkeypatch):
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: "current-sid")
    monkeypatch.setattr(obsidian_utils, "cache_get", lambda *a: None)
    monkeypatch.setattr(obsidian_utils, "cache_set", lambda *a: None)
    v = mock_config["vault_path"]
    _write(v, "claude-sessions/2026-10-23-demo-aaaa.md",
           {"type": "claude-session", "date": "2026-10-23", "session_id": SID, "project": "demo",
            "status": "summarized", "tags": ["claude/session", "claude/project/demo"]},
           "# Session: demo work\n\n## Summary\n\nWorked on demo.\n")
    return mock_config


def _brief(config):
    return obsidian_utils.build_context_brief(config["vault_path"], "claude-sessions", "claude-insights", "demo")


def _capture(config, name, score, body):
    p = _insight(config["vault_path"], name, status="provisional", body=body, title=f"Title {name}")
    rel = f"claude-insights/{name}.md"
    cid = capture_state.ledger_append({"session_id": SID, "project": "demo", "type": "insight", "title": name,
                                       "status": "active", "kind": "new", "note_path": rel, "score": score})
    return p, cid


def test_recall_shows_a_provisional_note_only_once_selection_picks_it(recall_vault):
    _capture(recall_vault, "picked", 9, "wombat burrow depth")
    assert "Title picked" not in _brief(recall_vault)
    db = vault_index._default_db_path()
    assert capture_select.run_selection(SID, "demo", recall_vault, db=db)["promoted"] == 1
    assert "Title picked" in _brief(recall_vault)


def test_recall_shows_a_used_note_promoted_outside_the_cap(recall_vault):
    for i in range(10):
        _capture(recall_vault, f"strong{i}", 10, f"strongnote{chr(97 + i)} unique{chr(97 + i)}words here")
    weak, _ = _capture(recall_vault, "weak", 1, "platypus venom spur")
    db = vault_index.ensure_index(recall_vault["vault_path"], FOLDERS)  # noqa: no-default-db -- conftest isolates it
    capture_select.run_selection(SID, "demo", recall_vault, db=db)
    assert "Title weak" not in _brief(recall_vault)
    vault_index.search_vault(db, "platypus", log_access=True)  # a user-facing search hit
    capture_select.run_selection(SID, "demo", recall_vault, db=db)
    assert "promoted_by: access" in weak.read_text()
    assert "Title weak" in _brief(recall_vault)


def test_file_scan_fallback_leaves_provisional_out(tmp_vault, mock_config, monkeypatch):
    _insight(str(tmp_vault), "provnote", status="provisional", title="Provisional Insight")
    _insight(str(tmp_vault), "kept", status="promoted", title="Promoted Insight")
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: "test-sid")
    monkeypatch.setattr(obsidian_utils, "cache_get", lambda *a: None)
    monkeypatch.setattr(obsidian_utils, "cache_set", lambda *a: None)
    monkeypatch.delitem(sys.modules, "vault_index", raising=False)
    real = builtins.__import__

    def no_index(name, *args, **kwargs):
        if name == "vault_index":
            raise ImportError("vault_index not available")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_index)
    brief = obsidian_utils.build_context_brief(str(tmp_vault), "claude-sessions", "claude-insights", "demo")
    assert "Promoted Insight" in brief and "Provisional Insight" not in brief


def _skill(name):
    return (Path(__file__).resolve().parent.parent / "skills" / name / "SKILL.md").read_text()


def test_vault_ask_leaves_provisional_notes_out():
    text = _skill("vault-ask")
    step3 = text.split("### Step 3")[1].split("### Step 4")[0]
    step5 = text.split("### Step 5")[1].split("### Step 6")[0]
    assert "    include_provisional=False,\n" in step3
    assert "Drop every file whose `status` is `provisional` from `CANDIDATE_FILES` before scoring" in step5
    assert "`source_session_note`, `status`, `tags`" in step5
    assert "auto-captured notes with `status: provisional`" in _skill("recall")
