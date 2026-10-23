"""Index pre-filter for the /retro and /recall bulk scans (#447).

gather_session_evidence and build_context_brief keep their directory
listing, but a note the index proves irrelevant (indexed, mtime unchanged,
wrong session / snapshot / other project) skips its metadata read. These
tests pin: same output with and without the index, the index failing open
to a full scan, stale rows never hiding a note, malformed notes still
reported, and a read count that does not grow with irrelevant notes.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import obsidian_utils
import vault_index

SID = "target-sid-1111"
OTHER_SID = "other-sid-2222"
PROJECT = "obsidian-brain"

SESSIONS = "claude-sessions"
INSIGHTS = "claude-insights"


def _insight(path: Path, *, sid: str, typ: str = "claude-insight", project: str = PROJECT,
             title: str = "An insight", day: str = "2026-10-20") -> Path:
    path.write_text(
        "---\n"
        f"type: {typ}\n"
        f"date: {day}\n"
        f"created_at: {day}T10:00:00Z\n"
        f"project: {project}\n"
        f"source_session: {sid}\n"
        "tags:\n"
        "  - claude/insight\n"
        "---\n\n"
        f"# {title}\n\nBody of {path.stem}.\n",
        encoding="utf-8",
    )
    return path


def _session(path: Path, *, sid: str, project: str = PROJECT, day: str = "2026-10-20",
             typ: str = "claude-session") -> Path:
    path.write_text(
        "---\n"
        f"type: {typ}\n"
        f"date: {day}\n"
        f"session_id: {sid}\n"
        f"project: {project}\n"
        'git_branch: "develop"\n'
        "duration_minutes: 12\n"
        "tags:\n"
        "  - claude/session\n"
        "status: summarized\n"
        "---\n\n"
        f"# Session: {project}\n\n"
        "## Summary\n"
        f"Worked on {path.stem}.\n\n"
        "## Open Questions / Next Steps\n"
        "- [ ] follow up\n",
        encoding="utf-8",
    )
    return path


def _build_vault(vault: Path, n_irrelevant: int = 6) -> dict[str, list[Path]]:
    ins = vault / INSIGHTS
    ses = vault / SESSIONS
    relevant = [
        _insight(ins / "2026-10-20-relevant-insight.md", sid=SID),
        _insight(ins / "2026-10-20-relevant-decision.md", sid=SID, typ="claude-decision"),
        _insight(ins / "2026-10-20-relevant-fix.md", sid=SID, typ="claude-error-fix"),
        _insight(ins / "2026-10-19-relevant-retro.md", sid=SID, typ="claude-retro"),
    ]
    irrelevant = [
        _insight(ins / f"2026-10-1{i % 10}-other-{i:04d}.md", sid=f"{OTHER_SID}-{i}")
        for i in range(n_irrelevant)
    ]
    sessions = [
        _session(ses / "2026-10-21-obsidian-brain-aaaa.md", sid=SID, day="2026-10-21"),
        _session(ses / "2026-10-20-obsidian-brain-bbbb.md", sid=OTHER_SID, day="2026-10-20"),
    ]
    other_sessions = [
        _session(ses / f"2026-10-1{i % 10}-other-project-{i:04d}.md", sid=f"x-{i}",
                 project="other-project")
        for i in range(n_irrelevant)
    ]
    snaps = [
        _session(ses / f"2026-10-21-obsidian-brain-aaaa-snapshot-10{i:04d}.md", sid=SID,
                 typ="claude-snapshot", day="2026-10-21")
        for i in range(n_irrelevant)
    ]
    return {
        "relevant": relevant,
        "irrelevant": irrelevant,
        "sessions": sessions,
        "other_sessions": other_sessions,
        "snapshots": snaps,
    }


def _index(vault: Path, db: Path) -> None:
    vault_index.ensure_index(str(vault), [SESSIONS, INSIGHTS], db_path=str(db))


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point the default DB at a new file; returns a function that builds a
    fresh index there. Each run gets its own DB so the access rows one run
    logs cannot change the ranking another run sees."""
    counter = {"n": 0}

    def make(vault: Path) -> Path:
        counter["n"] += 1
        db = tmp_path / f"index-{counter['n']}.db"
        monkeypatch.setenv("OBSIDIAN_BRAIN_DB", str(db))
        _index(vault, db)
        return db

    return make


@pytest.fixture
def read_log(monkeypatch):
    """Record every path read_note_metadata_detailed is asked for."""
    seen: list[str] = []
    real = obsidian_utils.read_note_metadata_detailed

    def spy(file_path):
        seen.append(str(file_path))
        return real(file_path)

    monkeypatch.setattr(obsidian_utils, "read_note_metadata_detailed", spy)
    return seen


@pytest.fixture(autouse=True)
def _cacheable_sid(monkeypatch):
    # A real-looking sid, so read_note_metadata goes through the session
    # cache (and cache_deferred) the way it does live.
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda *a, **k: "bulk-scan-sid")


def _evidence(vault: Path) -> dict:
    return obsidian_utils.gather_session_evidence(
        str(vault), SESSIONS, INSIGHTS, SID, PROJECT,
    )


def _brief(vault: Path) -> str:
    return obsidian_utils.build_context_brief(str(vault), SESSIONS, INSIGHTS, PROJECT)


def _no_index(monkeypatch):
    monkeypatch.setattr(obsidian_utils, "_indexed_rows_under", lambda folder: None)


# --- parity ---------------------------------------------------------------


def test_evidence_parity_with_and_without_index(tmp_vault, fresh_db, monkeypatch):
    _build_vault(tmp_vault)
    fresh_db(tmp_vault)
    with_index = _evidence(tmp_vault)
    with monkeypatch.context() as m:
        _no_index(m)
        without = _evidence(tmp_vault)
    assert with_index == without
    assert len(with_index["insights"]) == 1
    assert len(with_index["decisions"]) == 1
    assert len(with_index["error_fixes"]) == 1
    assert len(with_index["retros"]) == 1


def test_brief_parity_with_and_without_index(tmp_vault, fresh_db, monkeypatch):
    _build_vault(tmp_vault)
    fresh_db(tmp_vault)
    with_index = _brief(tmp_vault)
    fresh_db(tmp_vault)
    with monkeypatch.context() as m:
        _no_index(m)
        without = _brief(tmp_vault)
    assert with_index == without
    assert "2026-10-21-obsidian-brain-aaaa.md" in with_index


def test_index_skips_irrelevant_reads(tmp_vault, fresh_db, read_log):
    vault = _build_vault(tmp_vault)
    fresh_db(tmp_vault)
    _evidence(tmp_vault)
    _brief(tmp_vault)
    read = set(read_log)
    for p in vault["irrelevant"] + vault["other_sessions"]:
        assert str(p) not in read
    # Snapshots are still read by find_snapshots_for_session's own path, but
    # never by the session-table scan's metadata read through the index.
    for p in vault["relevant"]:
        assert str(p) in read


# --- fail-open ------------------------------------------------------------


def _raise_db_error(*_a, **_k):
    raise sqlite3.OperationalError("database is locked")


def test_index_error_falls_back_to_full_scan_for_evidence(tmp_vault, fresh_db, monkeypatch,
                                                          read_log, capsys):
    vault = _build_vault(tmp_vault)
    fresh_db(tmp_vault)
    expected = _evidence(tmp_vault)
    read_log.clear()
    monkeypatch.setattr(vault_index, "_connect", _raise_db_error)
    assert obsidian_utils._indexed_rows_under(tmp_vault / INSIGHTS) is None
    got = _evidence(tmp_vault)
    assert got == expected
    # Full scan: the irrelevant notes were read this time.
    assert str(vault["irrelevant"][0]) in read_log
    assert "index pre-filter unavailable" in capsys.readouterr().err


def test_index_error_falls_back_to_full_scan_for_brief(tmp_vault, fresh_db, monkeypatch, read_log):
    vault = _build_vault(tmp_vault)
    fresh_db(tmp_vault)
    expected = _brief(tmp_vault)
    fresh_db(tmp_vault)
    read_log.clear()
    real_connect = vault_index._connect
    calls = {"n": 0}

    def fail_first(db_path):
        # The pre-filter is the first DB open in build_context_brief; later
        # opens (recall access log, ensure_index) behave as normal so the
        # rest of the brief is comparable.
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(db_path)

    monkeypatch.setattr(vault_index, "_connect", fail_first)
    got = _brief(tmp_vault)
    assert got == expected
    assert str(vault["other_sessions"][0]) in read_log


def test_missing_notes_table_falls_back(tmp_vault, tmp_path, monkeypatch):
    db = tmp_path / "empty.db"
    conn = vault_index._connect(str(db))
    conn.close()
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", str(db))
    assert obsidian_utils._indexed_rows_under(tmp_vault / INSIGHTS) is None


def test_missing_db_file_falls_back(tmp_vault, tmp_path, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", str(tmp_path / "absent.db"))
    assert obsidian_utils._indexed_rows_under(tmp_vault / INSIGHTS) is None


def test_no_vault_index_module_falls_back(tmp_vault, monkeypatch):
    monkeypatch.setattr(obsidian_utils, "_vault_index", None)
    assert obsidian_utils._indexed_rows_under(tmp_vault / INSIGHTS) is None


def test_production_db_guard_is_not_swallowed(tmp_vault, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", vault_index._REAL_PROD_DB)
    if not os.path.isfile(vault_index._REAL_PROD_DB):
        pytest.skip("no production DB on this machine")
    with pytest.raises(RuntimeError, match="refusing to open production"):
        obsidian_utils._indexed_rows_under(tmp_vault / INSIGHTS)


def test_rows_are_scoped_to_the_folder(tmp_vault, fresh_db):
    _build_vault(tmp_vault)
    sibling = tmp_vault / (INSIGHTS + "-archive")
    sibling.mkdir()
    _insight(sibling / "2026-10-20-archived.md", sid=SID)
    db = fresh_db(tmp_vault)
    vault_index.ensure_index(str(tmp_vault), [INSIGHTS + "-archive"], db_path=str(db))
    rows = obsidian_utils._indexed_rows_under(tmp_vault / INSIGHTS)
    assert rows
    assert all(p.startswith(str(tmp_vault / INSIGHTS) + os.sep) for p in rows)


# --- stale rows and notes the index does not know ---------------------------


def test_note_edited_after_indexing_is_found(tmp_vault, fresh_db):
    vault = _build_vault(tmp_vault)
    fresh_db(tmp_vault)
    moved = vault["irrelevant"][0]
    _insight(moved, sid=SID, title="Re-attributed")
    st = moved.stat()
    os.utime(moved, (st.st_atime, st.st_mtime + 10))
    bundle = _evidence(tmp_vault)
    assert str(moved) in [n["path"] for n in bundle["insights"]]


def test_note_created_after_indexing_is_found(tmp_vault, fresh_db):
    _build_vault(tmp_vault)
    fresh_db(tmp_vault)
    new = _insight(tmp_vault / INSIGHTS / "2026-10-22-new.md", sid=SID)
    bundle = _evidence(tmp_vault)
    assert str(new) in [n["path"] for n in bundle["insights"]]


def test_session_note_moved_into_project_after_indexing_is_found(tmp_vault, fresh_db):
    _build_vault(tmp_vault)
    moved = _session(tmp_vault / SESSIONS / "2026-10-23-other-project-zzzz.md",
                     sid="moved-sid", project="other-project", day="2026-10-23")
    fresh_db(tmp_vault)
    _session(moved, sid="moved-sid", project=PROJECT, day="2026-10-23")
    st = moved.stat()
    os.utime(moved, (st.st_atime, st.st_mtime + 10))
    # Newest by filename, so it is the brief's full session when it is seen.
    assert f"full_session_path: {moved}" in _brief(tmp_vault)


def test_malformed_note_still_reported(tmp_vault, fresh_db):
    _build_vault(tmp_vault)
    broken = tmp_vault / INSIGHTS / "2026-10-20-broken.md"
    broken.write_text("---\ntype: claude-insight\nsource_session: other\n\nno fence\n",
                      encoding="utf-8")
    fresh_db(tmp_vault)
    bundle = _evidence(tmp_vault)
    assert any(e.startswith("2026-10-20-broken.md:") for e in bundle["discovery_errors"])


def test_note_broken_after_indexing_still_reported(tmp_vault, fresh_db):
    vault = _build_vault(tmp_vault)
    fresh_db(tmp_vault)
    victim = vault["irrelevant"][1]
    victim.write_text("---\ntype: claude-insight\nsource_session: other\n\nno fence\n",
                      encoding="utf-8")
    st = victim.stat()
    os.utime(victim, (st.st_atime, st.st_mtime + 10))
    bundle = _evidence(tmp_vault)
    assert any(e.startswith(victim.name + ":") for e in bundle["discovery_errors"])


def test_oversized_note_is_not_skipped(tmp_vault, fresh_db, monkeypatch):
    """The capped prefix read can call a note's frontmatter too long while
    the index (whole-file read) parsed it. That note must still be read so
    /retro reports it, so a note at or over the cap never skips."""
    _build_vault(tmp_vault)
    big = _insight(tmp_vault / INSIGHTS / "2026-10-20-big.md", sid=OTHER_SID)
    text = big.read_text(encoding="utf-8")
    big.write_text(text.replace("tags:\n", "padding: " + "x" * 400 + "\ntags:\n", 1),
                   encoding="utf-8")
    fresh_db(tmp_vault)
    monkeypatch.setattr(obsidian_utils, "_FRONTMATTER_MAX_CHARS", 200)
    monkeypatch.setattr(obsidian_utils, "_FRONTMATTER_READ_BLOCK", 64)
    bundle = _evidence(tmp_vault)
    assert any(e.startswith("2026-10-20-big.md:") for e in bundle["discovery_errors"])


def test_project_null_row_is_compared_like_missing_frontmatter(tmp_vault, fresh_db, read_log):
    no_project = tmp_vault / SESSIONS / "2026-10-20-no-project.md"
    no_project.write_text("---\ntype: claude-session\nsession_id: np\n---\n\n# x\n",
                          encoding="utf-8")
    fresh_db(tmp_vault)
    _brief(tmp_vault)
    assert str(no_project) not in read_log


# --- growth ---------------------------------------------------------------


@pytest.mark.parametrize("n", [20, 40], ids=["n20", "n40"])
def test_reads_do_not_grow_with_irrelevant_notes(tmp_path, monkeypatch, n, read_log):
    vault = tmp_path / "vault"
    (vault / SESSIONS).mkdir(parents=True)
    (vault / INSIGHTS).mkdir()
    built = _build_vault(vault, n_irrelevant=n)
    db = tmp_path / "growth.db"
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", str(db))
    _index(vault, db)
    _evidence(vault)
    _brief(vault)
    irrelevant = {str(p) for p in built["irrelevant"] + built["other_sessions"]}
    assert not irrelevant & set(read_log)
    # The relevant notes are what is left: a fixed count however large n is.
    insight_reads = [p for p in read_log if f"{os.sep}{INSIGHTS}{os.sep}" in p]
    assert len(insight_reads) == len(built["relevant"])


def test_bulk_scan_writes_cache_once(tmp_vault, fresh_db, monkeypatch):
    _build_vault(tmp_vault)
    real_write = obsidian_utils._cache_write
    writes: list[str] = []

    def spy(path, data):
        writes.append(path)
        return real_write(path, data)

    monkeypatch.setattr(obsidian_utils, "_cache_write", spy)
    _evidence(tmp_vault)
    assert len(writes) == 1


def test_brief_skips_snapshot_reads_through_the_index(tmp_vault, fresh_db, read_log):
    # Snapshots of a session the brief never lists: only the session-table
    # scan could read them, and the index proves each one is a snapshot.
    _build_vault(tmp_vault)
    orphans = [
        _session(tmp_vault / SESSIONS / f"2026-10-01-obsidian-brain-cccc-snapshot-09{i:04d}.md",
                 sid="orphan-sid", typ="claude-snapshot", day="2026-10-01")
        for i in range(3)
    ]
    fresh_db(tmp_vault)
    _brief(tmp_vault)
    assert not {str(p) for p in orphans} & set(read_log)


def test_note_gone_since_listing_gets_no_row(tmp_vault):
    """A note deleted between the listing and the stat must not skip on a
    row: it goes to the full read, which reports it as unreadable."""
    gone = tmp_vault / INSIGHTS / "2026-10-20-gone.md"
    row = {"path": str(gone), "mtime": 0.0, "type": "claude-insight",
           "project": PROJECT, "source_session": OTHER_SID}
    assert obsidian_utils._fresh_index_row({str(gone): row}, gone) is None


def test_brief_keeps_notes_whose_project_differs_by_case_or_slug(
        tmp_vault, fresh_db, monkeypatch, read_log):
    """The pre-filter must compare projects the way the full scan does: by
    case and by slug. A raw `!=` would skip these notes, and the indexed run
    would then differ from the full scan."""
    ses = tmp_vault / SESSIONS
    ses.mkdir(parents=True, exist_ok=True)
    cased = _session(ses / "2026-10-22-case-cccc.md", sid="c-sid",
                     project="Obsidian-Brain", day="2026-10-22")
    slugged = _session(ses / "2026-10-22-slug-dddd.md", sid="d-sid",
                       project="obsidian brain", day="2026-10-22")
    _build_vault(tmp_vault)
    fresh_db(tmp_vault)
    with_index = _brief(tmp_vault)
    read_with_index = set(read_log)
    del read_log[:]
    fresh_db(tmp_vault)
    with monkeypatch.context() as m:
        _no_index(m)
        without = _brief(tmp_vault)
    assert with_index == without
    # Not skipped: the indexed run still read both notes.
    assert str(cased) in read_with_index
    assert str(slugged) in read_with_index
