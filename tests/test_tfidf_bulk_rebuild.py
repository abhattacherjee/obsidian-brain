"""Test: two-pass tfidf recompute on bulk rebuild ensures order-invariance.

Parity oracle: inserting notes in forward vs reverse order must yield identical
tfidf_vector values after _recompute_all_tfidf_vectors() corrects the insertion-
time IDF. The underlying bug: early notes inserted in a _sync batch use a smaller
total_docs (N) so their IDF values are inflated relative to later notes.

This file also covers the bulk-DELETION / combined-churn recompute trigger
(deletions and insertions counted together against the corpus), the exact-half
`>` boundary (strict, not `>=`), and the empty-corpus (total_after == 0) edge.
"""
import json
from pathlib import Path

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
        Path(p).name: (json.loads(v) if v else {}) for p, v in rows
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
        return {Path(p).name: (json.loads(v) if v else {}) for p, v in rows}

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
    vecs = {Path(p).name: (json.loads(v) if v else {}) for p, v in rows}

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
        name = Path(path).name
        assert actual == expected, (
            f"{name}: stored vector doesn't match final-corpus recompute: "
            f"{actual} != {expected}"
        )


def test_sync_incremental_skip_no_recompute(tmp_path):
    """Incremental _sync (1 new note added to a 10-note corpus) skips recompute.

    With _BULK_RECOMPUTE_MIN_FRACTION = 0.5, inserting 1 note into a 10-note
    corpus gives inserted=1 and total_after=11. The guard (1 > 11 * 0.5 = 5.5)
    is False, so _recompute_all_tfidf_vectors is NOT called.

    Also verifies that an existing note's tfidf_vector is bit-for-bit unchanged
    between the two _sync calls.
    """
    vault = tmp_path / "vault"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)

    # Seed 10 notes so the corpus is already large enough that 1 new note
    # does not cross the 50% bulk-recompute threshold.
    _EXTRA_BODIES = {f"note{i:02d}.md": f"term{i} common extra" for i in range(10)}
    for name, body in _EXTRA_BODIES.items():
        (sess / name).write_text(
            f"---\ntype: session\nproject: p\ndate: 2026-06-15\n"
            f"title: {name}\n---\n\n{body}\n"
        )

    db = str(tmp_path / "v.db")
    conn = vault_index._connect(db)
    vault_index._init_schema(conn)
    stats1 = vault_index._sync(conn, str(vault), ["claude-sessions"])
    conn.close()

    assert stats1["inserted"] == 10, f"expected 10 inserted: {stats1}"

    # Capture vector for the first note before adding a new one.
    first_note_path = str(sess / "note00.md")
    conn = vault_index._connect(db)
    row = conn.execute(
        "SELECT tfidf_vector FROM notes WHERE path = ?", (first_note_path,)
    ).fetchone()
    conn.close()
    assert row is not None, "note00.md not indexed after first sync"
    vec_before = row[0]

    # Add one new note and re-sync.
    (sess / "new_note.md").write_text(
        "---\ntype: session\nproject: p\ndate: 2026-06-15\n"
        "title: new_note\n---\n\nnewnote unique term\n"
    )

    conn = vault_index._connect(db)
    stats2 = vault_index._sync(conn, str(vault), ["claude-sessions"])
    conn.close()

    assert stats2["inserted"] == 1, f"expected 1 inserted: {stats2}"
    assert stats2["recomputed"] == 0, (
        f"Expected recomputed=0 (1 of 11 notes < 50% threshold), got: {stats2}"
    )

    # Existing note vector must be unchanged (no recompute pass ran).
    conn = vault_index._connect(db)
    row2 = conn.execute(
        "SELECT tfidf_vector FROM notes WHERE path = ?", (first_note_path,)
    ).fetchone()
    conn.close()
    assert row2 is not None
    assert row2[0] == vec_before, (
        "note00.md tfidf_vector changed between syncs despite recomputed=0"
    )


def _write_note(folder, name, body):
    """Write a single session note file with varied-token body."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(
        f"---\ntype: session\nproject: p\ndate: 2026-06-15\n"
        f"title: {name}\n---\n\n{body}\n"
    )


def _assert_survivors_match_final_corpus(db):
    """Oracle: every stored tfidf_vector equals a fresh recompute against the
    FINAL corpus N and df. Proves survivors are not stale post-deletion."""
    conn = vault_index._connect(db)
    total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    full_df = dict(conn.execute("SELECT term, df FROM term_df").fetchall())
    rows = conn.execute(
        "SELECT path, title, tags, body, tfidf_vector FROM notes"
    ).fetchall()
    conn.close()

    for path, title, tags, body, vec_json in rows:
        tokens = vault_index._tokenize_for_tfidf(
            " ".join([title or "", tags or "", body or ""])
        )
        expected = vault_index._compute_tfidf_vector(tokens, full_df, total, top_k=50)
        actual = json.loads(vec_json) if vec_json else {}
        name = Path(path).name
        assert actual == expected, (
            f"{name}: stored vector doesn't match final-corpus recompute "
            f"(survivor is STALE): {actual} != {expected}"
        )
    return total


def test_sync_triggers_recompute_on_bulk_deletion(tmp_path):
    """_sync recomputes when deletions exceed 50% of the final corpus (#235).

    Deleting survivors' peers decrements df and shrinks N; without a recompute,
    survivors retain pre-deletion (stale) IDF. We build an 8-note corpus with
    varied tokens, index it, delete 5 files (>50%), re-sync, and assert both
    that recompute fired AND that survivors match a final-corpus oracle.
    """
    vault = tmp_path / "vault"
    sess = vault / "claude-sessions"
    # 8 notes; varied tokens so df differs across terms and IDF actually shifts
    # when half the corpus is removed.
    bodies = {
        "n0.md": "alpha alpha beta common shared",
        "n1.md": "beta gamma common shared rare1",
        "n2.md": "gamma delta common common shared",
        "n3.md": "delta epsilon common shared rare2",
        "n4.md": "epsilon zeta common shared",
        "n5.md": "zeta eta common shared rare3",
        "n6.md": "eta theta common common shared",
        "n7.md": "theta alpha common shared rare4",
    }
    for name, body in bodies.items():
        _write_note(sess, name, body)

    db = str(tmp_path / "v.db")
    conn = vault_index._connect(db)
    vault_index._init_schema(conn)
    # Baseline: index all 8 (may trip the insert recompute — fine, establishes
    # a clean state where stored vectors match the full 8-note corpus).
    stats1 = vault_index._sync(conn, str(vault), ["claude-sessions"])
    conn.close()
    assert stats1["inserted"] == 8, f"expected 8 inserted: {stats1}"

    # Delete 5 of 8 note files (>50% of the final corpus of 3).
    for name in ("n0.md", "n1.md", "n2.md", "n3.md", "n4.md"):
        (sess / name).unlink()

    conn = vault_index._connect(db)
    stats2 = vault_index._sync(conn, str(vault), ["claude-sessions"])
    conn.close()

    assert stats2["deleted"] == 5, f"expected 5 deleted: {stats2}"
    assert stats2["recomputed"] > 0, (
        f"Expected recomputed > 0 on bulk deletion (5 of 8 removed), got: {stats2}"
    )

    # Meaningful, non-vacuous oracle: survivors must match the FINAL 3-note
    # corpus N/df, proving they were not left with pre-deletion IDF.
    total = _assert_survivors_match_final_corpus(db)
    assert total == 3, f"expected 3 survivors, got {total}"


def test_sync_no_recompute_on_small_deletion(tmp_path):
    """Deleting 1 of 11 notes (<50%) does not trip the recompute."""
    vault = tmp_path / "vault"
    sess = vault / "claude-sessions"
    bodies = {f"note{i:02d}.md": f"term{i} common extra shared" for i in range(11)}
    for name, body in bodies.items():
        _write_note(sess, name, body)

    db = str(tmp_path / "v.db")
    conn = vault_index._connect(db)
    vault_index._init_schema(conn)
    stats1 = vault_index._sync(conn, str(vault), ["claude-sessions"])
    conn.close()
    assert stats1["inserted"] == 11, f"expected 11 inserted: {stats1}"

    # Delete just 1 note (1/11 ~= 9% < 50%).
    (sess / "note00.md").unlink()

    conn = vault_index._connect(db)
    stats2 = vault_index._sync(conn, str(vault), ["claude-sessions"])
    conn.close()

    assert stats2["deleted"] == 1, f"expected 1 deleted: {stats2}"
    assert stats2["inserted"] == 0, f"expected 0 inserted: {stats2}"
    assert stats2["recomputed"] == 0, (
        f"Expected recomputed=0 (1 of 11 deleted < 50% threshold), got: {stats2}"
    )


def test_sync_recompute_on_combined_churn_boundary(tmp_path):
    """Neither inserts nor deletes alone exceed 50%, but their SUM does.

    Final corpus = 10 notes. We delete 3 existing notes and add 3 new ones in
    one sync: inserted=3, deleted=3, total_after=10. 3 alone is < 5 (50% of 10),
    but 3+3=6 > 5 trips the combined-churn trigger. Locks in that the trigger
    keys off inserted+deleted, not either in isolation.
    """
    vault = tmp_path / "vault"
    sess = vault / "claude-sessions"
    # Seed 10 notes.
    for i in range(10):
        _write_note(sess, f"seed{i:02d}.md", f"seedterm{i} common shared body")

    db = str(tmp_path / "v.db")
    conn = vault_index._connect(db)
    vault_index._init_schema(conn)
    stats1 = vault_index._sync(conn, str(vault), ["claude-sessions"])
    conn.close()
    assert stats1["inserted"] == 10, f"expected 10 inserted: {stats1}"

    # Delete 3 seed notes, add 3 brand-new notes. Final corpus stays at 10.
    for i in range(3):
        (sess / f"seed{i:02d}.md").unlink()
    for i in range(3):
        _write_note(sess, f"fresh{i:02d}.md", f"freshterm{i} common shared body")

    conn = vault_index._connect(db)
    stats2 = vault_index._sync(conn, str(vault), ["claude-sessions"])
    conn.close()

    assert stats2["deleted"] == 3, f"expected 3 deleted: {stats2}"
    assert stats2["inserted"] == 3, f"expected 3 inserted: {stats2}"
    # 3 alone would NOT trip (3 < 10*0.5=5); combined 6 > 5 does.
    assert not (3 > 10 * vault_index._BULK_RECOMPUTE_MIN_FRACTION), (
        "precondition: inserts alone must be below threshold"
    )
    assert stats2["recomputed"] > 0, (
        f"Expected recomputed > 0 on combined churn (3 ins + 3 del of 10), "
        f"got: {stats2}"
    )


def test_sync_no_recompute_at_exact_half_threshold(tmp_path):
    """Landing EXACTLY on changed == total_after * 0.5 must NOT recompute.

    Pins the strict `>` operator (not `>=`). Deletion-only construction:
    index 9 notes, delete 3 -> total_after=6, deleted=3, inserted=0, so
    changed == 3 == 6 * 0.5 exactly. With `>`, 3 > 3.0 is False -> skip.
    If the guard were `>=`, this would fire and the assert below would fail.
    """
    vault = tmp_path / "vault"
    sess = vault / "claude-sessions"
    bodies = {f"note{i:02d}.md": f"term{i} common extra shared" for i in range(9)}
    for name, body in bodies.items():
        _write_note(sess, name, body)

    db = str(tmp_path / "v.db")
    conn = vault_index._connect(db)
    vault_index._init_schema(conn)
    stats1 = vault_index._sync(conn, str(vault), ["claude-sessions"])
    conn.close()
    assert stats1["inserted"] == 9, f"expected 9 inserted: {stats1}"

    # Delete exactly 3 -> total_after=6, changed=3, 3 == 6 * 0.5 exactly.
    for i in range(3):
        (sess / f"note{i:02d}.md").unlink()

    conn = vault_index._connect(db)
    stats2 = vault_index._sync(conn, str(vault), ["claude-sessions"])
    conn.close()

    assert stats2["deleted"] == 3, f"expected 3 deleted: {stats2}"
    assert stats2["inserted"] == 0, f"expected 0 inserted: {stats2}"
    # Confirm we landed exactly on the boundary (guards against drift).
    total_after = 6
    changed = stats2["inserted"] + stats2["deleted"]
    assert changed == total_after * vault_index._BULK_RECOMPUTE_MIN_FRACTION, (
        f"precondition: changed ({changed}) must equal exactly half of "
        f"total_after ({total_after})"
    )
    assert stats2["recomputed"] == 0, (
        f"Strict `>` must NOT fire at exactly half (changed == total_after*0.5); "
        f"got recomputed={stats2['recomputed']}. If this fails, the guard is "
        f"`>=` not `>`."
    )


def test_sync_recompute_just_over_half_threshold(tmp_path):
    """One unit past exact half DOES recompute (sibling to the boundary test).

    Index 10 notes, delete 4 -> total_after=6, changed=4, 4 > 6 * 0.5 = 3.0
    -> fires. This is the near-twin that proves the threshold is live just
    above the exact-half point pinned by the sibling test.
    """
    vault = tmp_path / "vault"
    sess = vault / "claude-sessions"
    bodies = {f"note{i:02d}.md": f"term{i} common extra shared" for i in range(10)}
    for name, body in bodies.items():
        _write_note(sess, name, body)

    db = str(tmp_path / "v.db")
    conn = vault_index._connect(db)
    vault_index._init_schema(conn)
    stats1 = vault_index._sync(conn, str(vault), ["claude-sessions"])
    conn.close()
    assert stats1["inserted"] == 10, f"expected 10 inserted: {stats1}"

    # Delete 4 -> total_after=6, changed=4 = 6*0.5 + 1, just over the boundary.
    for i in range(4):
        (sess / f"note{i:02d}.md").unlink()

    conn = vault_index._connect(db)
    stats2 = vault_index._sync(conn, str(vault), ["claude-sessions"])
    conn.close()

    assert stats2["deleted"] == 4, f"expected 4 deleted: {stats2}"
    total_after = 6
    changed = stats2["inserted"] + stats2["deleted"]
    assert changed == total_after * vault_index._BULK_RECOMPUTE_MIN_FRACTION + 1, (
        f"precondition: changed ({changed}) must be exactly one over half of "
        f"total_after ({total_after})"
    )
    assert stats2["recomputed"] > 0, (
        f"Expected recomputed > 0 just over the half threshold, got: {stats2}"
    )


def test_sync_delete_all_notes_no_recompute_no_error(tmp_path):
    """Deleting EVERY indexed note (total_after == 0) is a clean no-op recompute.

    The `total_after > 0` guard short-circuits the trigger, so no recompute
    runs (recomputing over an empty corpus would be meaningless/division-prone).
    Also confirms no orphan term_df rows survive a full wipe.
    """
    vault = tmp_path / "vault"
    sess = vault / "claude-sessions"
    n = 6
    bodies = {f"note{i:02d}.md": f"term{i} common extra shared" for i in range(n)}
    for name, body in bodies.items():
        _write_note(sess, name, body)

    db = str(tmp_path / "v.db")
    conn = vault_index._connect(db)
    vault_index._init_schema(conn)
    stats1 = vault_index._sync(conn, str(vault), ["claude-sessions"])
    conn.close()
    assert stats1["inserted"] == n, f"expected {n} inserted: {stats1}"

    # Delete ALL note files from disk.
    for name in bodies:
        (sess / name).unlink()

    conn = vault_index._connect(db)
    stats2 = vault_index._sync(conn, str(vault), ["claude-sessions"])
    # No exception means the empty-corpus path is safe.
    note_count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    df_count = conn.execute("SELECT COUNT(*) FROM term_df").fetchone()[0]
    conn.close()

    assert stats2["deleted"] == n, f"expected {n} deleted: {stats2}"
    assert stats2["recomputed"] == 0, (
        f"Expected recomputed=0 when total_after==0, got: {stats2}"
    )
    assert note_count == 0, f"expected 0 notes after full delete, got {note_count}"
    assert df_count == 0, (
        f"expected 0 term_df rows after full delete (orphan df), got {df_count}"
    )


def test_sync_small_mixed_churn_no_recompute(tmp_path):
    """Steady-state mixed churn (delete 1 + add 1 in a ~10-note corpus) skips.

    changed = inserted(1) + deleted(1) = 2, well under 50% of total_after=10.
    Locks in that counting deletions toward the trigger did not introduce a
    spurious recompute on the realistic mixed incremental path.
    """
    vault = tmp_path / "vault"
    sess = vault / "claude-sessions"
    for i in range(10):
        _write_note(sess, f"seed{i:02d}.md", f"seedterm{i} common shared body")

    db = str(tmp_path / "v.db")
    conn = vault_index._connect(db)
    vault_index._init_schema(conn)
    stats1 = vault_index._sync(conn, str(vault), ["claude-sessions"])
    conn.close()
    assert stats1["inserted"] == 10, f"expected 10 inserted: {stats1}"

    # Delete 1, add 1. Final corpus stays at 10; changed = 2.
    (sess / "seed00.md").unlink()
    _write_note(sess, "fresh00.md", "freshterm common shared body")

    conn = vault_index._connect(db)
    stats2 = vault_index._sync(conn, str(vault), ["claude-sessions"])
    conn.close()

    assert stats2["deleted"] == 1, f"expected 1 deleted: {stats2}"
    assert stats2["inserted"] == 1, f"expected 1 inserted: {stats2}"
    assert stats2["recomputed"] == 0, (
        f"Expected recomputed=0 on small mixed churn (changed=2 of 10), "
        f"got: {stats2}"
    )
