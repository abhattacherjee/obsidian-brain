"""Task 4: Connection-count bound for the upgrade_note_with_summary pipeline.

Asserts that upgrade_note_with_summary opens at most 2 sqlite connections
when running the full index + theme-assign + surprise pipeline.
"""
import json
import sys
import os
from datetime import date

# Ensure hooks/ is on the path for direct imports.
_hooks_dir = os.path.join(os.path.dirname(__file__), "..", "hooks")
if _hooks_dir not in sys.path:
    sys.path.insert(0, _hooks_dir)

import obsidian_utils
import vault_index
import themes


def _seed_theme(db: str, project: str = "p") -> None:
    """Insert a theme whose centroid overlaps the test note's summarized content."""
    conn = vault_index._connect(db)
    try:
        today = date.today().isoformat()
        # Use terms that will appear in both the raw note body AND the summary
        # text so the note's tfidf_vector (after re-indexing the summarized
        # version) still overlaps with the centroid -> assign_to_theme matches.
        centroid = json.dumps(
            {"python": 0.9, "work": 0.7, "changes": 0.5},
            separators=(",", ":"),
        )
        conn.execute(
            "INSERT INTO themes "
            "(name, summary, centroid, note_count, activation, "
            "created_date, updated_date, project) "
            "VALUES (?, '', ?, 1, 0.0, ?, ?, ?)",
            ("test-theme", centroid, today, today, project),
        )
        conn.commit()
    finally:
        conn.close()


def test_upgrade_opens_at_most_two_connections(tmp_path, monkeypatch):
    """upgrade_note_with_summary must open <=2 sqlite connections for the
    index+theme+surprise pipeline (importance + upsert share conn A;
    assign_to_theme including surprise is conn B).

    A theme that overlaps the note's summarized terms is seeded so the full
    path (assign_to_theme match + surprise write) is exercised: pre-fix opens
    4 connections, post-fix must open <=2."""
    # Minimal vault + DB setup.
    vault = tmp_path / "vault"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    note = sess / "s.md"
    # Raw note body uses terms that will survive into the summary.
    note.write_text(
        "---\ntype: session\nproject: p\ndate: 2026-06-15\n"
        "title: s\nstatus: auto-logged\n---\n\n"
        "python work changes cannot avoid broken\n"
    )
    db = str(tmp_path / "idx.db")
    vault_index.rebuild_index(str(vault), ["claude-sessions"], db_path=db, full=True)

    # Seed a theme whose centroid overlaps the summarized note content.
    _seed_theme(db)

    # Patch _default_db_path so obsidian_utils resolves to our test DB.
    monkeypatch.setattr(vault_index, "_default_db_path", lambda *a, **k: db)

    # Count _connect calls made AFTER the setup rebuild + theme seed.
    # assign_to_theme does `from vault_index import _connect` at call time,
    # so patching vault_index._connect is honored (the import re-executes on
    # each assign_to_theme call, reading the current module attribute).
    opens = {"n": 0}
    real_connect = vault_index._connect

    def counting_connect(*a, **k):
        opens["n"] += 1
        return real_connect(*a, **k)

    monkeypatch.setattr(vault_index, "_connect", counting_connect)

    # Summary contains "python", "work", "changes" so the re-indexed
    # tfidf_vector will overlap with the theme centroid.
    summary = (
        "## Summary\nDid python work on changes.\n\n"
        "## Key Decisions\nNone noted.\n\n"
        "## Changes Made\nPython changes landed.\n\n"
        "## Errors Encountered\nNone.\n\n"
        "## Open Questions / Next Steps\nNone.\n\nIMPORTANCE: 6\n"
    )
    result = obsidian_utils.upgrade_note_with_summary(
        str(note), summary, str(vault), "claude-sessions", "p",
    )
    # Must have succeeded (connection count is moot on early-exit failure)
    assert result.startswith("Upgraded "), f"upgrade failed: {result}"
    assert opens["n"] <= 2, f"opened {opens['n']} connections, expected <=2"


def _build_upgrade_setup(tmp_path):
    """Shared setup for surprise-value tests: vault + note + DB + theme."""
    vault = tmp_path / "vault"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    note = sess / "s.md"
    note.write_text(
        "---\ntype: session\nproject: p\ndate: 2026-06-15\n"
        "title: s\nstatus: auto-logged\n---\n\n"
        "python work changes cannot avoid broken\n"
    )
    db = str(tmp_path / "idx.db")
    vault_index.rebuild_index(str(vault), ["claude-sessions"], db_path=db, full=True)
    _seed_theme(db)
    return vault, note, db


def test_upgrade_surprise_value_new_member(tmp_path, monkeypatch):
    """surprise stored in theme_members equals oracle computed from full file.

    Oracle: read the note file raw (frontmatter + body) — same input as Fix 1
    produces — then call detect_surprise with the post-update centroid from DB.
    Fails if surprise is a hardcoded constant or body-only text is used.
    """
    vault, note, db = _build_upgrade_setup(tmp_path)
    monkeypatch.setattr(vault_index, "_default_db_path", lambda *a, **k: db)

    summary = (
        "## Summary\nDid python work on changes.\n\n"
        "## Key Decisions\nNone noted.\n\n"
        "## Changes Made\nPython changes landed.\n\n"
        "## Errors Encountered\nNone.\n\n"
        "## Open Questions / Next Steps\nNone.\n\nIMPORTANCE: 6\n"
    )
    result = obsidian_utils.upgrade_note_with_summary(
        str(note), summary, str(vault), "claude-sessions", "p",
    )
    assert result.startswith("Upgraded "), f"upgrade failed: {result}"

    # Read DB artifacts needed for oracle computation.
    conn = vault_index._connect(db)
    row = conn.execute(
        "SELECT surprise FROM theme_members WHERE note_path = ?",
        (str(note),),
    ).fetchone()
    assert row is not None, "no theme_members row found after upgrade"
    stored_surprise = row[0]

    note_vec_row = conn.execute(
        "SELECT tfidf_vector FROM notes WHERE path = ?",
        (str(note),),
    ).fetchone()
    assert note_vec_row is not None, "note not indexed after upgrade"
    note_vec = json.loads(note_vec_row[0])

    # After upgrade, the centroid has been updated (new_centroid branch).
    # Read the post-update centroid from themes table.
    centroid_row = conn.execute(
        "SELECT centroid FROM themes"
    ).fetchone()
    assert centroid_row is not None
    post_centroid = json.loads(centroid_row[0])
    conn.close()

    # Oracle: full file text (frontmatter + body) as Fix 1 now passes.
    with open(str(note), "r", encoding="utf-8") as fh:
        full_text = fh.read()
    oracle = themes.detect_surprise(full_text, note_vec, post_centroid)

    assert stored_surprise == oracle, (
        f"stored surprise {stored_surprise!r} != oracle {oracle!r}; "
        f"Fix 1 (note_text parity) may not be applied"
    )


def test_upgrade_surprise_value_reassignment(tmp_path, monkeypatch):
    """On reassignment, surprise is recomputed against the UNCHANGED centroid.

    Call assign_to_theme directly a second time with explicit note_text; assert
    the stored surprise matches oracle using the original (pre-first-call)
    centroid (effective_centroid = centroid branch in themes.py).
    Fails if surprise is a hardcoded constant or the wrong centroid is used.
    """
    vault, note, db = _build_upgrade_setup(tmp_path)
    monkeypatch.setattr(vault_index, "_default_db_path", lambda *a, **k: db)

    summary = (
        "## Summary\nDid python work on changes.\n\n"
        "## Key Decisions\nNone noted.\n\n"
        "## Changes Made\nPython changes landed.\n\n"
        "## Errors Encountered\nNone.\n\n"
        "## Open Questions / Next Steps\nNone.\n\nIMPORTANCE: 6\n"
    )
    # First upgrade: inserts theme_members row (new_member path).
    result = obsidian_utils.upgrade_note_with_summary(
        str(note), summary, str(vault), "claude-sessions", "p",
    )
    assert result.startswith("Upgraded "), f"first upgrade failed: {result}"

    # Snapshot the centroid BEFORE the second assign_to_theme call.
    # On reassignment, effective_centroid = centroid (unchanged) so the oracle
    # must use this pre-call snapshot, not any new_centroid.
    conn = vault_index._connect(db)
    centroid_row = conn.execute("SELECT centroid FROM themes").fetchone()
    pre_call_centroid = json.loads(centroid_row[0])
    note_vec_row = conn.execute(
        "SELECT tfidf_vector FROM notes WHERE path = ?", (str(note),)
    ).fetchone()
    note_vec = json.loads(note_vec_row[0])
    conn.close()

    # Read full file text to match Fix 1's note_text argument.
    with open(str(note), "r", encoding="utf-8") as fh:
        full_text = fh.read()

    # Second assign: reassignment path (already_member=True).
    result2 = themes.assign_to_theme(
        db, str(note), project="p", note_text=full_text
    )
    assert result2 is not None, "reassignment returned None"

    # Read updated surprise from DB.
    conn = vault_index._connect(db)
    row = conn.execute(
        "SELECT surprise FROM theme_members WHERE note_path = ?",
        (str(note),),
    ).fetchone()
    stored_surprise = row[0]
    conn.close()

    # Oracle: detect_surprise with the pre-call centroid (unchanged on reassign).
    oracle = themes.detect_surprise(full_text, note_vec, pre_call_centroid)

    assert stored_surprise == oracle, (
        f"stored surprise {stored_surprise!r} != oracle {oracle!r}; "
        f"reassignment must use the unchanged centroid (effective_centroid = centroid)"
    )
