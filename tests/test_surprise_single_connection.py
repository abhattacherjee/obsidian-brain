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
