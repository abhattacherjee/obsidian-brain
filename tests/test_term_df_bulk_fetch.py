"""Tests for token-scoped term_df fetch in _upsert_note (Issue #42 Item 2).

Verifies that _upsert_note no longer performs a full-table scan of term_df,
and that the token-scoped fetch yields an identical tfidf_vector.
"""
import json
import sqlite3
import vault_index


def _seed(db, n_other_terms=2000):
    conn = vault_index._connect(db)
    conn.execute("INSERT OR IGNORE INTO term_df (term, df) VALUES (?, ?)",
                 ("alpha", 3))
    conn.executemany(
        "INSERT OR IGNORE INTO term_df (term, df) VALUES (?, 1)",
        [(f"filler{i}",) for i in range(n_other_terms)],
    )
    conn.commit()
    conn.close()


class SpyConnection(sqlite3.Connection):
    """sqlite3.Connection subclass that records all SQL strings executed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen_sql: list[str] = []

    def execute(self, sql, *args, **kwargs):
        self.seen_sql.append(sql)
        return super().execute(sql, *args, **kwargs)


def test_term_df_fetch_is_token_scoped(tmp_path):
    db = str(tmp_path / "v.db")
    # Bootstrap schema via normal connection.
    conn = vault_index._connect(db)
    vault_index._init_schema(conn)
    conn.close()
    _seed(db)

    # Open a spy connection and run the upsert through it.
    spy = sqlite3.connect(db, factory=SpyConnection)
    spy.row_factory = sqlite3.Row
    spy.execute("PRAGMA journal_mode=WAL")
    spy.execute("BEGIN IMMEDIATE")
    vault_index._upsert_note(
        spy, "/x/note.md",
        {"type": "session", "title": "alpha beta", "tags": "",
         "body": "alpha gamma", "date": "2026-06-15", "project": "p"},
        mtime=1.0, size=10,
    )
    spy.commit()
    seen_sql = spy.seen_sql[:]
    spy.close()

    # No full-table scan of term_df (SELECT ... FROM term_df with no WHERE clause).
    assert not any(
        "FROM term_df" in s and "WHERE" not in s and "term IN" not in s
        for s in seen_sql
    ), f"full term_df scan still present: {[s for s in seen_sql if 'term_df' in s]}"
    # The token-scoped fetch must be present.
    assert any("term_df" in s and ("term IN" in s or "WHERE term" in s)
               for s in seen_sql)


def test_tfidf_vector_parity_with_full_fetch(tmp_path):
    """Token-scoped fetch yields identical tfidf_vector to a full fetch."""
    db = str(tmp_path / "v.db")
    conn = vault_index._connect(db)
    vault_index._init_schema(conn)
    conn.close()
    _seed(db, n_other_terms=50)

    conn = vault_index._connect(db)
    conn.execute("BEGIN IMMEDIATE")
    vault_index._upsert_note(
        conn, "/x/n.md",
        {"type": "session", "title": "alpha beta", "tags": "",
         "body": "alpha gamma delta", "date": "2026-06-15", "project": "p"},
        mtime=1.0, size=10,
    )
    conn.commit()
    vec = json.loads(conn.execute(
        "SELECT tfidf_vector FROM notes WHERE path = ?", ("/x/n.md",)
    ).fetchone()[0])
    conn.close()

    # Recompute the expected vector with a full term_df fetch.
    conn = vault_index._connect(db)
    tokens = vault_index._tokenize_for_tfidf("alpha beta alpha gamma delta")
    full_df = dict(conn.execute("SELECT term, df FROM term_df").fetchall())
    total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    conn.close()
    expected = vault_index._compute_tfidf_vector(tokens, full_df, total, top_k=50)
    assert vec == expected
