"""Tests for Task 2: search_vault vector exposure + compute_query_vector.

Covers:
- include_vectors=True exposes decoded tfidf_vector on each result dict
- Default include_vectors=False leaves the key absent (no behavior change)
- or_fallback flag is set on rows from the OR-fallback branch
- compute_query_vector returns the same sparse dict as direct _compute_tfidf_vector
- Empty/stopword-only query returns {}
"""

from __future__ import annotations

import sqlite3

import pytest

import vault_index
from vault_index import (
    _connect,
    _tokenize_for_tfidf,
    _compute_tfidf_vector,
    ensure_index,
    search_vault,
)


# ---------------------------------------------------------------------------
# Scratch-index helper (mirrors TestSearchVault pattern in test_vault_index.py)
# ---------------------------------------------------------------------------


def _write_note(path, frontmatter: dict, body: str = "") -> None:
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


def _build_db(tmp_vault, notes: list[tuple]) -> str:
    """Write notes to tmp_vault, build the index, return db_path."""
    for rel_path, fm, body in notes:
        path = tmp_vault / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_note(path, fm, body)
    db_path = str(tmp_vault / "test.db")
    ensure_index(
        str(tmp_vault),
        ["claude-sessions", "claude-insights"],
        db_path=db_path,
    )
    return db_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIncludeVectors:
    """search_vault include_vectors parameter."""

    def _notes(self):
        return [
            (
                "claude-sessions/vec-sess.md",
                {
                    "type": "claude-session",
                    "date": "2026-06-21",
                    "project": "vecproj",
                    "tags": ["claude/session"],
                },
                "# Session: Vectors\n\nTesting tfidf vector extraction from vault index.",
            ),
        ]

    def test_search_vault_include_vectors_adds_tfidf_vector(self, tmp_vault):
        """include_vectors=True adds decoded tfidf_vector dict (or None) to results."""
        db_path = _build_db(tmp_vault, self._notes())

        results = search_vault(db_path, "tfidf vector extraction", include_vectors=True)
        assert len(results) >= 1

        for r in results:
            assert "tfidf_vector" in r, "include_vectors=True must set tfidf_vector key"
            # value must be a dict (decoded) or None (null in db) — not a raw JSON string
            v = r["tfidf_vector"]
            assert v is None or isinstance(v, dict), (
                f"tfidf_vector must be dict or None, got {type(v)}: {v!r}"
            )

    def test_search_vault_default_omits_tfidf_vector(self, tmp_vault):
        """Default call (include_vectors=False) must NOT include tfidf_vector key."""
        db_path = _build_db(tmp_vault, self._notes())

        results = search_vault(db_path, "tfidf vector extraction")
        assert len(results) >= 1

        for r in results:
            assert "tfidf_vector" not in r, (
                "Default search_vault must not expose tfidf_vector (backward-compat)"
            )

    def test_body_still_absent_when_include_vectors_true(self, tmp_vault):
        """body is stripped even when include_vectors=True."""
        db_path = _build_db(tmp_vault, self._notes())

        results = search_vault(db_path, "tfidf vector extraction", include_vectors=True)
        assert len(results) >= 1
        for r in results:
            assert "body" not in r, "body must still be popped when include_vectors=True"


class TestOrFallbackFlag:
    """or_fallback is set correctly on AND-path and OR-path rows."""

    def _notes(self):
        # Note A has "obsidian" but NOT "notebook"
        # Note B has "notebook" but NOT "obsidian"
        # A query for "obsidian notebook" via AND returns 0 results;
        # the OR fallback returns both A and B.
        return [
            (
                "claude-sessions/or-sess-a.md",
                {
                    "type": "claude-session",
                    "date": "2026-06-21",
                    "project": "orproj",
                    "tags": ["claude/session"],
                },
                "# Session: Obsidian Plugin\n\nWorking on obsidian vault plugin integration.",
            ),
            (
                "claude-sessions/or-sess-b.md",
                {
                    "type": "claude-session",
                    "date": "2026-06-21",
                    "project": "orproj",
                    "tags": ["claude/session"],
                },
                "# Session: Notebook Design\n\nDesigning the notebook layout structure.",
            ),
            (
                "claude-sessions/and-sess.md",
                {
                    "type": "claude-session",
                    "date": "2026-06-21",
                    "project": "andproj",
                    "tags": ["claude/session"],
                },
                "# Session: Vault Sync\n\nImplemented vault sync with FTS index.",
            ),
        ]

    def test_or_fallback_flag_set_on_or_path(self, tmp_vault):
        """Rows from the OR-fallback branch carry or_fallback=True; AND rows carry False."""
        db_path = _build_db(tmp_vault, self._notes())

        # "obsidian notebook" AND → 0 hits (no note has both);
        # OR fallback → at least one of or-sess-a or or-sess-b
        or_results = search_vault(db_path, "obsidian notebook")
        assert len(or_results) >= 1, (
            "OR fallback should return at least one result for 'obsidian notebook'"
        )
        for r in or_results:
            assert r.get("or_fallback") is True, (
                f"OR-path row must have or_fallback=True, got {r.get('or_fallback')!r}"
            )

    def test_and_path_flag_is_false(self, tmp_vault):
        """AND-matching rows carry or_fallback=False."""
        db_path = _build_db(tmp_vault, self._notes())

        # "vault sync" AND → and-sess has both terms → hits without fallback
        and_results = search_vault(db_path, "vault sync")
        assert len(and_results) >= 1, "AND query must return at least one result"
        for r in and_results:
            assert r.get("or_fallback") is False, (
                f"AND-path row must have or_fallback=False, got {r.get('or_fallback')!r}"
            )


class TestComputeQueryVector:
    """compute_query_vector helper."""

    def _notes(self):
        return [
            (
                "claude-sessions/qvec-sess.md",
                {
                    "type": "claude-session",
                    "date": "2026-06-21",
                    "project": "qvproj",
                    "tags": ["claude/session"],
                },
                "# Session: Cosine Gate\n\n"
                "Testing cosine similarity gate for compress confidence matching.",
            ),
        ]

    def test_compute_query_vector_matches_note_vectorization(self, tmp_vault):
        """compute_query_vector returns the same dict as _compute_tfidf_vector called
        with the live term_df + total_docs from the same connection.

        Anchored to caller-side behavior: runs the same computation that
        _upsert_note would use (minus the +1 that accounts for a note-about-to-insert).
        """
        db_path = _build_db(tmp_vault, self._notes())
        text = "cosine similarity gate compress confidence matching"

        # Get result from the helper under test
        conn = _connect(db_path)
        try:
            result = vault_index.compute_query_vector(conn, text)

            # Independent recomputation using the same connection
            tokens = _tokenize_for_tfidf(text)
            total_docs = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            unique_terms = list(set(tokens))
            term_df: dict[str, int] = {}
            for i in range(0, len(unique_terms), 900):
                chunk = unique_terms[i : i + 900]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT term, df FROM term_df WHERE term IN ({placeholders})",
                    chunk,
                ).fetchall()
                term_df.update(dict(rows))
            expected = _compute_tfidf_vector(tokens, term_df, total_docs, top_k=50)
        finally:
            conn.close()

        assert result == expected, (
            f"compute_query_vector output does not match independent recomputation.\n"
            f"got:      {result}\n"
            f"expected: {expected}"
        )

    def test_compute_query_vector_empty_query(self, tmp_vault):
        """Empty or stopword-only query returns {} (cosine skip signal)."""
        db_path = _build_db(tmp_vault, self._notes())
        conn = _connect(db_path)
        try:
            # pure empty string
            assert vault_index.compute_query_vector(conn, "") == {}
            # stopwords only — _tokenize_for_tfidf returns []
            assert vault_index.compute_query_vector(conn, "the a and or") == {}
        finally:
            conn.close()
