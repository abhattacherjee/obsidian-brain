"""Test: two-pass tfidf recompute on bulk rebuild ensures order-invariance.

Parity oracle: inserting notes in forward vs reverse order must yield identical
tfidf_vector values after _recompute_all_tfidf_vectors() corrects the insertion-
time IDF. The underlying bug: early notes inserted in a _sync batch use a smaller
total_docs (N) so their IDF values are inflated relative to later notes.
"""
import json
import vault_index


_BODIES = {
    "a.md": "alpha alpha beta common common",
    "b.md": "beta gamma common rare",
    "c.md": "gamma delta common common common",
    "d.md": "epsilon common alpha",
}

_ORDER_FORWARD = ["a.md", "b.md", "c.md", "d.md"]
_ORDER_REVERSE = ["d.md", "c.md", "b.md", "a.md"]


def _parsed(name):
    return {
        "type": "session",
        "project": "p",
        "date": "2026-06-15",
        "title": name,
        "tags": "",
        "body": _BODIES[name],
    }


def _build_with_order(tmp_path, order, db_name="v.db"):
    """Insert notes directly via _upsert_note in the given order.

    Bypasses rglob (which always returns alphabetical order on macOS/APFS),
    so we can control insertion order and observe the IDF skew.
    Returns (db_path, {name: vec}).
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = str(tmp_path / db_name)
    conn = vault_index._connect(db)
    vault_index._init_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    for name in order:
        path = f"/vault/sessions/{name}"
        vault_index._upsert_note(conn, path, _parsed(name), mtime=1.0, size=10)
    conn.commit()

    rows = conn.execute("SELECT path, tfidf_vector FROM notes").fetchall()
    conn.close()
    return db, {
        p.split("/")[-1]: (json.loads(v) if v else {}) for p, v in rows
    }


def test_tfidf_skew_exists_before_recompute(tmp_path):
    """Verify the raw insertion-order bug is observable.

    The first note inserted sees N=1 (inflated IDF); the last sees N=4.
    Without the recompute pass, vectors differ between forward and reverse
    insertion orders.
    """
    _db_f, forward = _build_with_order(tmp_path / "f", _ORDER_FORWARD)
    _db_r, reverse = _build_with_order(tmp_path / "r", _ORDER_REVERSE)
    # At least one note must differ — if they're all equal the corpus is
    # too homogeneous to distinguish IDF skew.
    any_differ = any(forward[n] != reverse[n] for n in forward)
    assert any_differ, (
        "No insertion-order skew detected — corpus may be too small or all "
        "terms appear in every note. The test is vacuous; enlarge the corpus."
    )


def test_tfidf_order_invariant_after_recompute(tmp_path):
    """After _recompute_all_tfidf_vectors, vectors are order-invariant."""
    db_f, _ = _build_with_order(tmp_path / "f", _ORDER_FORWARD, "fwd.db")
    db_r, _ = _build_with_order(tmp_path / "r", _ORDER_REVERSE, "rev.db")

    # Apply the recompute pass to both DBs.
    for db in (db_f, db_r):
        conn = vault_index._connect(db)
        vault_index._recompute_all_tfidf_vectors(conn)
        conn.commit()
        conn.close()

    # Now collect results.
    def _vecs(db):
        conn = vault_index._connect(db)
        rows = conn.execute("SELECT path, tfidf_vector FROM notes").fetchall()
        conn.close()
        return {p.split("/")[-1]: (json.loads(v) if v else {}) for p, v in rows}

    forward = _vecs(db_f)
    reverse = _vecs(db_r)

    assert set(forward) == set(reverse)
    for name in forward:
        assert forward[name] == reverse[name], (
            f"{name} differs after recompute: {forward[name]} != {reverse[name]}"
        )


def test_recompute_helper_matches_final_corpus(tmp_path):
    """After recompute, each vector equals a from-scratch compute with final N/df."""
    db, _ = _build_with_order(tmp_path, _ORDER_FORWARD)

    conn = vault_index._connect(db)
    vault_index._recompute_all_tfidf_vectors(conn)
    conn.commit()
    rows = conn.execute("SELECT path, tfidf_vector FROM notes").fetchall()
    vecs = {p.split("/")[-1]: (json.loads(v) if v else {}) for p, v in rows}

    # Independently recompute a.md's vector with the final corpus stats.
    total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    full_df = dict(conn.execute("SELECT term, df FROM term_df").fetchall())
    row = conn.execute(
        "SELECT title, tags, body FROM notes WHERE path LIKE '%a.md'"
    ).fetchone()
    conn.close()

    tokens = vault_index._tokenize_for_tfidf(
        " ".join([row[0] or "", row[1] or "", row[2] or ""])
    )
    expected = vault_index._compute_tfidf_vector(tokens, full_df, total, top_k=50)
    assert vecs["a.md"] == expected


def test_sync_triggers_recompute_on_bulk_insert(tmp_path):
    """_sync calls _recompute_all_tfidf_vectors when inserted > 50% of final corpus.

    We verify this by checking that the stats dict returned has 'recomputed' > 0
    for a full-corpus bulk load (all notes new), and that the resulting vectors
    are consistent with final-corpus N/df.
    """
    import os
    from pathlib import Path

    vault = tmp_path / "vault"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    for name, body in _BODIES.items():
        (sess / name).write_text(
            f"---\ntype: session\nproject: p\ndate: 2026-06-15\n"
            f"title: {name}\n---\n\n{body}\n"
        )

    db = str(tmp_path / "v.db")
    conn = vault_index._connect(db)
    vault_index._init_schema(conn)
    stats = vault_index._sync(conn, str(vault), ["claude-sessions"])
    conn.close()

    assert "recomputed" in stats, (
        f"stats dict missing 'recomputed' key: {stats}"
    )
    assert stats["recomputed"] > 0, (
        f"Expected recomputed > 0 for bulk insert (all 4 notes new), got: {stats}"
    )
    # Verify all vectors are consistent with final corpus.
    conn = vault_index._connect(db)
    total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    full_df = dict(conn.execute("SELECT term, df FROM term_df").fetchall())
    rows = conn.execute("SELECT path, title, tags, body, tfidf_vector FROM notes").fetchall()
    conn.close()

    for path, title, tags, body, vec_json in rows:
        tokens = vault_index._tokenize_for_tfidf(
            " ".join([title or "", tags or "", body or ""])
        )
        expected = vault_index._compute_tfidf_vector(tokens, full_df, total, top_k=50)
        actual = json.loads(vec_json) if vec_json else {}
        name = path.split("/")[-1]
        assert actual == expected, (
            f"{name}: stored vector doesn't match final-corpus recompute: "
            f"{actual} != {expected}"
        )
