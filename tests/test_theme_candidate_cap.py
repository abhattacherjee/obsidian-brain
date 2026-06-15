import json
import vault_index
import themes


def _mk_theme(conn, tid, centroid, project="p"):
    conn.execute(
        "INSERT INTO themes (id, name, summary, centroid, note_count, "
        "activation, created_date, updated_date, project) "
        "VALUES (?, ?, '', ?, 1, 0.0, '2026-06-15', '2026-06-15', ?)",
        (tid, f"t{tid}", json.dumps(centroid), project),
    )


def _mk_note(conn, path, vec, project="p"):
    conn.execute(
        "INSERT INTO notes (path, type, project, title, body, mtime, "
        "tfidf_vector) VALUES (?, 'session', ?, 't', 'b', 1.0, ?)",
        (path, project, json.dumps(vec)),
    )


def _setup(tmp_path):
    db = str(tmp_path / "v.db")
    conn = vault_index._connect(db)
    vault_index._init_schema(conn)
    # Disjoint and overlapping themes.
    _mk_theme(conn, 1, {"alpha": 1.0, "beta": 1.0})
    _mk_theme(conn, 2, {"gamma": 1.0, "delta": 1.0})
    _mk_theme(conn, 3, {"alpha": 0.9, "zeta": 0.2})
    conn.commit()
    conn.close()
    return db


def test_prefilter_skips_zero_overlap_cosine(tmp_path, monkeypatch):
    db = _setup(tmp_path)
    conn = vault_index._connect(db)
    _mk_note(conn, "/n.md", {"alpha": 1.0, "beta": 0.5})
    conn.commit()
    conn.close()

    calls = {"n": 0}
    real = themes._cosine_similarity

    def counting(a, b):
        calls["n"] += 1
        return real(a, b)

    monkeypatch.setattr(themes, "_cosine_similarity", counting)
    res = themes.assign_to_theme(db, "/n.md", project="p")
    # Theme 2 (gamma/delta) shares no term with the note -> cosine skipped.
    # Only themes 1 and 3 share >=1 term.
    assert calls["n"] <= 2, f"cosine computed {calls['n']} times, expected <=2"
    assert res is not None and res["theme_id"] in (1, 3)


def test_prefilter_parity_with_full_scan(tmp_path):
    """Prefiltered assignment equals a brute-force full-scan assignment."""
    db = _setup(tmp_path)
    conn = vault_index._connect(db)
    _mk_note(conn, "/n.md", {"alpha": 1.0, "zeta": 0.3})
    conn.commit()
    # Brute-force expected: max cosine over ALL themes.
    rows = conn.execute("SELECT id, centroid FROM themes").fetchall()
    note_vec = {"alpha": 1.0, "zeta": 0.3}
    best_id, best_sim = None, -1.0
    for tid, cj in rows:
        sim = themes._cosine_similarity(note_vec, json.loads(cj))
        if sim > best_sim:
            best_id, best_sim = tid, sim
    conn.close()

    res = themes.assign_to_theme(db, "/n.md", project="p")
    if best_sim >= themes._THEME_SIMILARITY_THRESHOLD:
        assert res is not None and res["theme_id"] == best_id
    else:
        assert res is None
