"""Tests for _backfill_missing_tfidf_vectors (GH #255).

Covers:
- (a) core: NULLed vector is backfilled, count returned == 1
- (b) value oracle: backfilled vector equals the original (byte-identical after json round-trip)
- (c) idempotency: second call returns 0 and changes no vectors
- (d) non-NULL rows untouched: other vectors unchanged by backfill
- (e) empty note stays NULL: stopword/punct-only content keeps tfidf_vector IS NULL
- (f) term_df invariant: SELECT term, df FROM term_df is identical before and after
- (g) integration via rebuild_index(full=False): vector repopulated, stats include 'backfilled'
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import vault_index
from vault_index import (
    _connect,
    _backfill_missing_tfidf_vectors,
    ensure_index,
    rebuild_index,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FOLDERS = ["claude-sessions", "claude-insights"]


def _write_note(path: Path, frontmatter: dict, body: str = "") -> None:
    lines = ["---"]
    for k, v in frontmatter.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    if body:
        lines.append(body)
    path.write_text("\n".join(lines), encoding="utf-8")


def _build_index(tmp_vault: Path, notes: list[tuple]) -> str:
    """Write notes to tmp_vault, build the index, return db_path."""
    for rel_path, fm, body in notes:
        p = tmp_vault / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_note(p, fm, body)
    db_path = str(tmp_vault / "backfill-test.db")
    ensure_index(str(tmp_vault), FOLDERS, db_path=db_path)
    return db_path


def _sample_notes():
    return [
        (
            "claude-sessions/note-alpha.md",
            {
                "type": "claude-session",
                "date": "2026-06-21",
                "project": "alphaproj",
                "tags": ["claude/session"],
            },
            "# Alpha Session\n\nImplemented the authentication middleware pipeline "
            "using token validation and refresh strategies for secure access control.",
        ),
        (
            "claude-sessions/note-beta.md",
            {
                "type": "claude-session",
                "date": "2026-06-21",
                "project": "betaproj",
                "tags": ["claude/session"],
            },
            "# Beta Session\n\nDebugged the database connection pooling issue "
            "with transaction isolation levels and deadlock prevention techniques.",
        ),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBackfillMissingTfidfVectors:
    """_backfill_missing_tfidf_vectors correctness tests."""

    def test_null_vector_gets_backfilled(self, tmp_vault):
        """(a) NULLed vector is repopulated; return count == 1."""
        db_path = _build_index(tmp_vault, _sample_notes())
        conn = _connect(db_path)
        try:
            # Verify both notes have vectors after fresh index
            rows = conn.execute(
                "SELECT path, tfidf_vector FROM notes ORDER BY path"
            ).fetchall()
            assert len(rows) == 2, "expected exactly 2 notes in index"
            for row in rows:
                assert row["tfidf_vector"] is not None, (
                    f"fresh index should produce non-NULL tfidf_vector for {row['path']}"
                )

            # NULL out exactly one note's vector
            target_path = rows[0]["path"]
            conn.execute(
                "UPDATE notes SET tfidf_vector = NULL WHERE path = ?", (target_path,)
            )
            conn.commit()

            # Confirm it is NULL
            null_check = conn.execute(
                "SELECT tfidf_vector FROM notes WHERE path = ?", (target_path,)
            ).fetchone()
            assert null_check["tfidf_vector"] is None, "should be NULL before backfill"

            # Call the function under test
            count = _backfill_missing_tfidf_vectors(conn)
            conn.commit()

            assert count == 1, f"expected backfill count 1, got {count}"

            # Verify the vector is now non-NULL
            after = conn.execute(
                "SELECT tfidf_vector FROM notes WHERE path = ?", (target_path,)
            ).fetchone()
            assert after["tfidf_vector"] is not None, "tfidf_vector must be non-NULL after backfill"
            vec = json.loads(after["tfidf_vector"])
            assert isinstance(vec, dict) and len(vec) > 0, (
                "backfilled tfidf_vector must be a non-empty dict"
            )
        finally:
            conn.close()

    def test_backfilled_vector_equals_fresh_index_vector(self, tmp_vault):
        """(b) Backfilled vector is byte-identical to the original (freshly indexed) value."""
        db_path = _build_index(tmp_vault, _sample_notes())
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                "SELECT path, tfidf_vector FROM notes ORDER BY path"
            ).fetchall()
            target_path = rows[0]["path"]
            original_vector_json = rows[0]["tfidf_vector"]
            assert original_vector_json is not None, "fresh index must produce a vector"

            # NULL it out
            conn.execute(
                "UPDATE notes SET tfidf_vector = NULL WHERE path = ?", (target_path,)
            )
            conn.commit()

            # Backfill
            _backfill_missing_tfidf_vectors(conn)
            conn.commit()

            # Compare JSON strings (byte-identical serialization)
            after = conn.execute(
                "SELECT tfidf_vector FROM notes WHERE path = ?", (target_path,)
            ).fetchone()
            assert after["tfidf_vector"] == original_vector_json, (
                f"backfilled vector differs from original.\n"
                f"original: {original_vector_json}\n"
                f"backfilled: {after['tfidf_vector']}"
            )
        finally:
            conn.close()

    def test_second_backfill_is_noop(self, tmp_vault):
        """(c) Second call returns 0 and changes no vectors."""
        db_path = _build_index(tmp_vault, _sample_notes())
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                "SELECT path FROM notes ORDER BY path"
            ).fetchall()
            target_path = rows[0]["path"]

            # NULL one vector
            conn.execute(
                "UPDATE notes SET tfidf_vector = NULL WHERE path = ?", (target_path,)
            )
            conn.commit()

            # First backfill
            count1 = _backfill_missing_tfidf_vectors(conn)
            conn.commit()
            assert count1 == 1

            # Capture state after first backfill
            vecs_after_first = {
                r["path"]: r["tfidf_vector"]
                for r in conn.execute("SELECT path, tfidf_vector FROM notes").fetchall()
            }

            # Second backfill — must return 0 and not change anything
            count2 = _backfill_missing_tfidf_vectors(conn)
            conn.commit()
            assert count2 == 0, f"expected 0 on second call, got {count2}"

            vecs_after_second = {
                r["path"]: r["tfidf_vector"]
                for r in conn.execute("SELECT path, tfidf_vector FROM notes").fetchall()
            }
            assert vecs_after_first == vecs_after_second, (
                "second backfill must not change any vectors"
            )
        finally:
            conn.close()

    def test_existing_vectors_untouched(self, tmp_vault):
        """(d) Rows that already have a tfidf_vector are not changed by backfill."""
        db_path = _build_index(tmp_vault, _sample_notes())
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                "SELECT path, tfidf_vector FROM notes ORDER BY path"
            ).fetchall()
            assert len(rows) == 2
            # Save the untouched row (index 1)
            untouched_path = rows[1]["path"]
            untouched_vector = rows[1]["tfidf_vector"]
            assert untouched_vector is not None

            # NULL only row 0
            target_path = rows[0]["path"]
            conn.execute(
                "UPDATE notes SET tfidf_vector = NULL WHERE path = ?", (target_path,)
            )
            conn.commit()

            _backfill_missing_tfidf_vectors(conn)
            conn.commit()

            # Check untouched row is unchanged
            after = conn.execute(
                "SELECT tfidf_vector FROM notes WHERE path = ?", (untouched_path,)
            ).fetchone()
            assert after["tfidf_vector"] == untouched_vector, (
                "backfill must not modify a row that already has a tfidf_vector"
            )
        finally:
            conn.close()

    def test_empty_note_stays_null(self, tmp_vault):
        """(e) A note whose content tokenizes to empty keeps tfidf_vector IS NULL.

        The tokenization source is title + tags + body joined with spaces.
        To guarantee an empty token list, we use a note with:
        - no title (empty string)
        - no tags (empty string)
        - a body consisting solely of single-character tokens and stopwords

        We insert this note directly into the DB (bypassing the parser) so we
        have precise control over the stored title/tags/body fields.
        """
        db_path = _build_index(tmp_vault, _sample_notes())
        conn = _connect(db_path)
        try:
            # Insert a sentinel note directly with known-empty tokenization.
            # title="", tags="", body="a b c" — all single-char tokens (dropped by
            # _tokenize_for_tfidf which requires len(t) > 1).
            empty_path = str(tmp_vault / "claude-sessions" / "note-empty.md")
            conn.execute(
                "INSERT INTO notes (path, type, date, project, title, tags, body, "
                "status, mtime, size, importance, tfidf_vector) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    empty_path, "claude-session", "2026-06-21", "emptyproj",
                    "",    # title — empty
                    "",    # tags — empty
                    "a b c",  # body — single-char tokens only, all dropped
                    "auto-logged", time.time(), 10, 5,
                ),
            )
            conn.commit()

            # NULL out ALL vectors so backfill processes every row (including
            # the two real notes from _build_index + the sentinel empty note)
            conn.execute("UPDATE notes SET tfidf_vector = NULL")
            conn.commit()

            # Verify empty note starts NULL
            initial = conn.execute(
                "SELECT tfidf_vector FROM notes WHERE path = ?", (empty_path,)
            ).fetchone()
            assert initial["tfidf_vector"] is None

            count = _backfill_missing_tfidf_vectors(conn)
            conn.commit()

            # The empty note should remain NULL and NOT be counted
            empty_after = conn.execute(
                "SELECT tfidf_vector FROM notes WHERE path = ?", (empty_path,)
            ).fetchone()
            assert empty_after["tfidf_vector"] is None, (
                "note with empty tokenization must keep tfidf_vector IS NULL"
            )

            # Count should equal the number of notes that got a non-NULL vector
            # (i.e. total notes minus the empty one)
            non_null_count = conn.execute(
                "SELECT COUNT(*) FROM notes WHERE tfidf_vector IS NOT NULL"
            ).fetchone()[0]
            total_notes = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            # Exactly the non-empty notes (total - 1 empty sentinel) were backfilled
            assert count == non_null_count, (
                f"backfill count ({count}) must equal actual non-NULL rows ({non_null_count})"
            )
            assert count == total_notes - 1, (
                f"expected count == total_notes - 1 = {total_notes - 1}, got {count}"
            )
        finally:
            conn.close()

    def test_term_df_unchanged_by_backfill(self, tmp_vault):
        """(f) term_df table is completely unchanged by backfill."""
        db_path = _build_index(tmp_vault, _sample_notes())
        conn = _connect(db_path)
        try:
            # Capture term_df before
            term_df_before = conn.execute(
                "SELECT term, df FROM term_df ORDER BY term"
            ).fetchall()

            # NULL one vector
            rows = conn.execute("SELECT path FROM notes LIMIT 1").fetchall()
            conn.execute(
                "UPDATE notes SET tfidf_vector = NULL WHERE path = ?",
                (rows[0]["path"],),
            )
            conn.commit()

            _backfill_missing_tfidf_vectors(conn)
            conn.commit()

            # Capture term_df after
            term_df_after = conn.execute(
                "SELECT term, df FROM term_df ORDER BY term"
            ).fetchall()

            assert term_df_before == term_df_after, (
                "backfill must NOT modify term_df table (over-count guard)"
            )
        finally:
            conn.close()

    def test_rebuild_index_full_false_backfills(self, tmp_vault):
        """(g) rebuild_index(full=False) repopulates NULL vectors and reports 'backfilled' in stats."""
        notes = _sample_notes()
        db_path = _build_index(tmp_vault, notes)

        # NULL one vector directly
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                "SELECT path FROM notes ORDER BY path LIMIT 1"
            ).fetchall()
            target_path = rows[0]["path"]
            conn.execute(
                "UPDATE notes SET tfidf_vector = NULL WHERE path = ?", (target_path,)
            )
            conn.commit()
        finally:
            conn.close()

        # Run non-destructive rebuild
        stats = rebuild_index(str(tmp_vault), FOLDERS, db_path=db_path, full=False)

        # Stats must include a 'backfilled' key with count >= 1
        assert "backfilled" in stats, (
            f"rebuild_index(full=False) stats must include 'backfilled' key; got: {stats}"
        )
        assert stats["backfilled"] >= 1, (
            f"expected backfilled >= 1, got {stats['backfilled']}"
        )

        # Verify the vector is actually repopulated
        conn = _connect(db_path)
        try:
            after = conn.execute(
                "SELECT tfidf_vector FROM notes WHERE path = ?", (target_path,)
            ).fetchone()
            assert after["tfidf_vector"] is not None, (
                "vector must be non-NULL after rebuild_index(full=False)"
            )
        finally:
            conn.close()

    def test_full_false_no_nulls_reports_zero(self, tmp_vault):
        """(h) rebuild_index(full=False) on a fully-indexed corpus returns stats['backfilled'] == 0.

        Guards against a future refactor that gates the key on count > 0
        — the key must always be present, even when there is nothing to backfill.
        """
        db_path = _build_index(tmp_vault, _sample_notes())

        # Confirm all vectors are non-NULL (fresh index)
        conn = _connect(db_path)
        try:
            null_count = conn.execute(
                "SELECT COUNT(*) FROM notes WHERE tfidf_vector IS NULL"
            ).fetchone()[0]
            assert null_count == 0, "fresh index must have no NULL vectors"
        finally:
            conn.close()

        stats = rebuild_index(str(tmp_vault), FOLDERS, db_path=db_path, full=False)

        assert "backfilled" in stats, (
            f"'backfilled' key must be present even when nothing was backfilled; got: {stats}"
        )
        assert stats["backfilled"] == 0, (
            f"expected stats['backfilled'] == 0 on a fully-indexed corpus, got {stats['backfilled']}"
        )

    def test_full_true_reports_zero_and_no_nulls(self, tmp_vault):
        """(i) rebuild_index(full=True) returns stats with 'backfilled' == 0.

        A full wipe rebuilds all vectors from scratch via _sync, so no NULLs
        can remain — verified directly against the DB. Fix-1 contract: the key
        must always be present. backfilled==0 is causally meaningful here: the
        DB check confirms there are no NULLs left to backfill.
        """
        db_path = _build_index(tmp_vault, _sample_notes())

        stats = rebuild_index(str(tmp_vault), FOLDERS, db_path=db_path, full=True)

        assert "backfilled" in stats, (
            f"'backfilled' key must be present after full=True rebuild; got: {stats}"
        )
        assert stats["backfilled"] == 0, (
            f"expected stats['backfilled'] == 0 after full rebuild, got {stats['backfilled']}"
        )
        conn = _connect(db_path)
        try:
            null_after = conn.execute(
                "SELECT COUNT(*) FROM notes WHERE tfidf_vector IS NULL"
            ).fetchone()[0]
            assert null_after == 0, "full rebuild must leave no NULL vectors"
        finally:
            conn.close()
