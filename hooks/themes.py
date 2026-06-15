"""Theme assignment + surprise detection extracted from vault_index.py
(#229 Slice A).

Depends on tfidf (one-directional). Reaches the core-index helper _connect via
a lazy function-local import to avoid a vault_index <-> themes import cycle.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date

from tfidf import _cosine_similarity, _TOKEN_RE

_THEME_SIMILARITY_THRESHOLD = 0.3

_CENTROID_EPSILON = 1e-6

# Half-life (days) for the ACT-R base-level activation decay (Slice D, #231).
# Referenced by name in tests so activation assertions carry no magic literal.
HALF_LIFE_DAYS = 30


def compute_centroid(vectors: list[dict[str, float]]) -> dict[str, float]:
    """Element-wise mean of sparse vectors, pruning terms below epsilon."""
    if not vectors:
        return {}
    n = len(vectors)
    acc: dict[str, float] = {}
    for vec in vectors:
        for term, w in vec.items():
            acc[term] = acc.get(term, 0.0) + w
    return {t: s / n for t, s in acc.items() if abs(s / n) >= _CENTROID_EPSILON}


def get_unassigned_notes(db_path: str, project: str | None = None) -> list[tuple[str, str | None, dict]]:
    """Notes with a non-empty tfidf_vector and NO theme_members row. If ``project``
    is given, restrict to that project. Returns [(path, project, vec), ...]."""
    from vault_index import _connect
    conn = _connect(db_path)
    try:
        sql = (
            "SELECT n.path, n.project, n.tfidf_vector FROM notes n "
            "LEFT JOIN theme_members m ON n.path = m.note_path "
            "WHERE m.note_path IS NULL AND n.tfidf_vector IS NOT NULL AND n.tfidf_vector != ''"
        )
        params: tuple = ()
        if project is not None:
            sql += " AND n.project IS ?"
            params = (project,)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    out = []
    skipped = 0
    for path, proj, vec_json in rows:
        try:
            vec = json.loads(vec_json)
        except (ValueError, TypeError):
            skipped += 1
            continue
        if vec:
            out.append((path, proj, vec))
    if skipped > 0:
        print(f"[consolidate] skipped {skipped} notes with unparseable tfidf_vector", file=sys.stderr)
    return out


def get_all_vectorized_notes(db_path: str, project: str | None = None) -> list[tuple[str, str | None, dict]]:
    """Notes with a non-empty tfidf_vector, regardless of theme membership. If
    ``project`` is given, restrict to that project. Returns [(path, project, vec), ...].
    Used by ``--full`` consolidation to read all notes after deferring the wipe."""
    from vault_index import _connect
    conn = _connect(db_path)
    try:
        sql = (
            "SELECT path, project, tfidf_vector FROM notes "
            "WHERE tfidf_vector IS NOT NULL AND tfidf_vector != ''"
        )
        params: tuple = ()
        if project is not None:
            sql += " AND project IS ?"
            params = (project,)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    out = []
    skipped = 0
    for path, proj, vec_json in rows:
        try:
            vec = json.loads(vec_json)
        except (ValueError, TypeError):
            skipped += 1
            continue
        if vec:
            out.append((path, proj, vec))
    if skipped > 0:
        print(f"[consolidate] skipped {skipped} notes with unparseable tfidf_vector", file=sys.stderr)
    return out


def create_theme(
    conn: sqlite3.Connection,
    name: str,
    summary: str,
    centroid: dict[str, float],
    members: list[tuple[str, dict[str, float]]],
    project: str | None,
    now_iso: str,
) -> int:
    """INSERT a themes row + its theme_members rows (similarity = cosine to the
    centroid). Caller owns the transaction (does not commit). Returns theme id."""
    cur = conn.execute(
        "INSERT INTO themes (name, summary, centroid, note_count, activation, "
        "created_date, updated_date, project) VALUES (?, ?, ?, ?, 0.0, ?, ?, ?)",
        (name, summary, json.dumps(centroid, separators=(",", ":")), len(members),
         now_iso, now_iso, project),
    )
    theme_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO theme_members (theme_id, note_path, similarity, surprise, added_date) "
        "VALUES (?, ?, ?, 0.0, ?) "
        "ON CONFLICT(theme_id, note_path) DO UPDATE SET similarity = excluded.similarity",
        [(theme_id, path, _cosine_similarity(vec, centroid), now_iso) for path, vec in members],
    )
    return theme_id


def assign_to_theme(
    db_path: str,
    note_path: str,
    project: str | None = None,
    similarity_threshold: float = _THEME_SIMILARITY_THRESHOLD,
    note_text: str | None = None,
) -> dict | None:
    """Incrementally assign a summarized note to its nearest theme (if any).

    Reads the note's tfidf_vector and compares against every project-scoped
    theme centroid plus any cross-project themes (project IS NULL). If the
    best similarity exceeds ``similarity_threshold``, adds a theme_members
    row and updates the theme's centroid via running average.

    When ``note_text`` is provided and a theme is matched, the surprise score
    is computed via ``detect_surprise`` and written to ``theme_members.surprise``
    inside the same ``BEGIN IMMEDIATE`` transaction — eliminating a separate
    fourth connection from the upgrade pipeline.

    The centroid passed to ``detect_surprise`` is the POST-update centroid
    (``new_centroid`` for new members, unchanged ``centroid`` for reassignments),
    matching the behavior of the previous separate-connection implementation
    which re-read the centroid after ``assign_to_theme`` committed.

    Returns {"theme_id": int, "similarity": float, "surprise": float,
    "note_vec": dict, "centroid": dict} on assignment, or None if no theme
    was close enough (or the note has no vector).
    """
    # Lazy/local import breaks the vault_index<->themes import cycle. It binds the
    # canonical *bare* `vault_index` module — the form every runtime caller uses
    # (obsidian_utils/open_item_dedup/vault_stats all `import vault_index`), so a
    # monkeypatch of vault_index._connect is honored. Do not switch to a package
    # (`hooks.vault_index`) import here.
    from vault_index import _connect
    if not os.path.isfile(db_path):
        return None

    try:
        conn = _connect(db_path)
    except sqlite3.Error as exc:
        print(f"[vault-index] assign_to_theme could not connect: {exc}",
              file=sys.stderr)
        return None

    try:
        # Take the write lock BEFORE reading candidates so the
        # read-modify-write cycle is atomic against concurrent writers.
        # Other callers block for up to the connection's timeout (5s in
        # _connect()) before sqlite3 raises OperationalError.
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT tfidf_vector FROM notes WHERE path = ?", (note_path,)
            ).fetchone()
            if not row or not row["tfidf_vector"]:
                conn.commit()
                return None
            try:
                note_vec = json.loads(row["tfidf_vector"])
            except json.JSONDecodeError:
                conn.commit()
                return None
            if not note_vec:
                conn.commit()
                return None

            # When project is None (global/unscoped recall), "project = ?"
            # binds to NULL and matches no rows because `NULL = NULL` is
            # false in SQL — only project-scoped themes with project IS
            # NULL would match. Branch the query so unscoped callers see
            # every theme, and scoped callers see their own + cross-project.
            if project is None:
                candidates = conn.execute(
                    "SELECT id, centroid, note_count FROM themes"
                ).fetchall()
            else:
                candidates = conn.execute(
                    "SELECT id, centroid, note_count FROM themes "
                    "WHERE project = ? OR project IS NULL",
                    (project,),
                ).fetchall()

            note_terms = set(note_vec)
            best: tuple[float, int, dict, int] | None = None
            for cand in candidates:
                if not cand["centroid"]:
                    continue
                try:
                    centroid = json.loads(cand["centroid"])
                except json.JSONDecodeError:
                    continue
                # Prefilter: cosine is 0 without a shared term, which is below
                # the assignment threshold, so skip the expensive cosine call.
                if not note_terms.intersection(centroid):
                    continue
                sim = _cosine_similarity(note_vec, centroid)
                if best is None or sim > best[0]:
                    best = (sim, cand["id"], centroid, cand["note_count"])

            if best is None or best[0] < similarity_threshold:
                conn.commit()
                return None

            sim, theme_id, centroid, count = best
            today = date.today().isoformat()

            # Detect reassignment: if the note is already a member, the
            # centroid already reflects its contribution — updating count
            # and re-averaging would double-count it and drift the centroid.
            already_member = conn.execute(
                "SELECT 1 FROM theme_members "
                "WHERE theme_id = ? AND note_path = ?",
                (theme_id, note_path),
            ).fetchone() is not None

            if not already_member:
                new_centroid: dict[str, float] = {}
                all_terms = set(centroid) | set(note_vec)
                for term in all_terms:
                    c_val = centroid.get(term, 0.0)
                    v_val = note_vec.get(term, 0.0)
                    new_centroid[term] = (c_val * count + v_val) / (count + 1)

                conn.execute(
                    "UPDATE themes "
                    "SET centroid = ?, note_count = ?, updated_date = ? "
                    "WHERE id = ?",
                    (json.dumps(new_centroid, separators=(",", ":")),
                     count + 1, today, theme_id),
                )
                # Use the post-update centroid for surprise so parity is
                # maintained with the previous implementation that re-read
                # themes.centroid after assign_to_theme committed.
                effective_centroid = new_centroid
            else:
                # Reassignment: bump updated_date but leave count/centroid
                # alone. The member's similarity is refreshed below.
                conn.execute(
                    "UPDATE themes SET updated_date = ? WHERE id = ?",
                    (today, theme_id),
                )
                # Centroid unchanged on reassignment — same as old impl.
                effective_centroid = centroid

            # Preserve surprise + added_date on reassignment — only
            # similarity is refreshed from the latest cosine computation.
            conn.execute(
                "INSERT INTO theme_members "
                "(theme_id, note_path, similarity, surprise, added_date) "
                "VALUES (?, ?, ?, 0.0, ?) "
                "ON CONFLICT(theme_id, note_path) DO UPDATE SET "
                "similarity = excluded.similarity",
                (theme_id, note_path, sim, today),
            )

            # Compute + persist surprise in-txn when note_text is supplied.
            # This folds what was previously a separate fourth connection in
            # upgrade_note_with_summary into this existing transaction.
            surprise = 0.0
            if note_text is not None:
                surprise = detect_surprise(note_text, note_vec, effective_centroid)
                conn.execute(
                    "UPDATE theme_members SET surprise = ? "
                    "WHERE theme_id = ? AND note_path = ?",
                    (surprise, theme_id, note_path),
                )

            conn.commit()
            return {
                "theme_id": theme_id,
                "similarity": sim,
                "surprise": surprise,
                "note_vec": note_vec,
                "centroid": effective_centroid,
            }
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


_NEGATION_TERMS = frozenset({
    # Simple negation / failure words
    "not", "never", "failed", "broken", "wrong", "mistake",
    "avoid", "no", "cannot",
    # Contractions — bare forms only. detect_surprise() strips apostrophes
    # from note_text before tokenizing, so "don't"/"won't"/"isn\u2019t"
    # in source text collapse to "dont"/"wont"/"isnt" and match here.
    # Apostrophed variants (e.g. "don't") would never match post-
    # tokenization and are intentionally omitted.
    "dont", "cant", "wont", "isnt", "arent", "wasnt", "werent",
    "didnt", "doesnt", "couldnt", "shouldnt", "wouldnt",
    "hasnt", "havent", "hadnt",
})


def detect_surprise(
    note_text: str,
    note_vec: dict[str, float],
    theme_centroid: dict[str, float],
    window: int = 8,
    top_shared: int = 10,
) -> float:
    """Heuristic Free-Energy surprise score for a note vs. its theme centroid.

    Returns the fraction of the top_shared shared TF-IDF terms that appear
    within ``window`` tokens of a negation word in ``note_text``. Clamped
    to [0.0, 1.0]. Zero on missing overlap or empty input.
    """
    if not note_text or not note_vec or not theme_centroid:
        return 0.0

    shared = [
        (t, min(note_vec[t], theme_centroid[t]))
        for t in set(note_vec) & set(theme_centroid)
    ]
    if not shared:
        return 0.0
    shared.sort(key=lambda kv: (-kv[1], kv[0]))
    shared_terms = [t for t, _ in shared[:top_shared]]

    # Strip apostrophes (straight + smart) before tokenizing so contractions
    # like "don't" / "can't" collapse to "dont"/"cant" and match the
    # negation set. Without this, _TOKEN_RE = [a-z0-9]+ would emit
    # "don", "t" and the negation check would silently miss every
    # apostrophed negation in the source text.
    normalized = (
        note_text.lower()
        .replace("\u2019", "")  # right single quote (smart apostrophe)
        .replace("\u2018", "")  # left single quote
        .replace("'", "")       # straight apostrophe
    )
    tokens = _TOKEN_RE.findall(normalized)
    if not tokens:
        return 0.0

    negation_positions = [
        i for i, t in enumerate(tokens) if t in _NEGATION_TERMS
    ]
    if not negation_positions:
        return 0.0

    hits = 0
    for term in shared_terms:
        term_positions = [i for i, t in enumerate(tokens) if t == term]
        for p in term_positions:
            if any(abs(p - n) <= window for n in negation_positions):
                hits += 1
                break

    score = hits / len(shared_terms)
    return max(0.0, min(1.0, score))


def theme_stats(db_path: str) -> dict:
    """Read-only counts for /consolidate stats."""
    from vault_index import _connect
    conn = _connect(db_path)
    try:
        themes_n = conn.execute("SELECT COUNT(*) FROM themes").fetchone()[0]
        members_n = conn.execute("SELECT COUNT(*) FROM theme_members").fetchone()[0]
        unassigned_n = conn.execute(
            "SELECT COUNT(*) FROM notes n LEFT JOIN theme_members m ON n.path = m.note_path "
            "WHERE m.note_path IS NULL AND n.tfidf_vector IS NOT NULL AND n.tfidf_vector != ''"
        ).fetchone()[0]
        largest = conn.execute(
            "SELECT id, name, note_count FROM themes ORDER BY note_count DESC, id LIMIT 5"
        ).fetchall()
    finally:
        conn.close()
    return {"themes": themes_n, "members": members_n, "unassigned": unassigned_n,
            "largest": [{"id": r[0], "name": r[1], "note_count": r[2]} for r in largest]}


def _theme_member_vectors(conn: sqlite3.Connection, theme_id: int) -> list[tuple[str, dict[str, float]]]:
    """Return [(note_path, tfidf_vec), ...] for a theme's members, skipping notes with unparseable/empty vectors."""
    rows = conn.execute(
        "SELECT m.note_path, n.tfidf_vector FROM theme_members m "
        "JOIN notes n ON n.path = m.note_path WHERE m.theme_id = ?", (theme_id,)
    ).fetchall()
    out = []
    skipped = 0
    for path, vec_json in rows:
        try:
            vec = json.loads(vec_json) if vec_json else {}
        except (ValueError, TypeError):
            skipped += 1
            vec = {}
        if vec:
            out.append((path, vec))
    if skipped > 0:
        print(f"[consolidate] skipped {skipped} notes with unparseable tfidf_vector", file=sys.stderr)
    return out


def merge_themes(db_path: str, a: int, b: int, now_iso: str) -> bool:
    """Move b's members into a, recompute a's centroid + note_count, delete b.
    Returns True on success, False if either theme is missing or a==b."""
    if a == b:
        return False
    from vault_index import _connect
    conn = _connect(db_path)
    try:
        rows = {r[0] for r in conn.execute("SELECT id FROM themes WHERE id IN (?, ?)", (a, b))}
        if a not in rows or b not in rows:
            return False
        conn.execute("BEGIN IMMEDIATE")
        # OR IGNORE deduplicates overlapping members (a note in both themes);
        # the conflicting b-row is silently dropped, then b's remaining rows
        # are deleted to leave only a's membership intact.
        conn.execute(
            "UPDATE OR IGNORE theme_members SET theme_id = ? WHERE theme_id = ?", (a, b))
        conn.execute("DELETE FROM theme_members WHERE theme_id = ?", (b,))
        conn.execute("DELETE FROM themes WHERE id = ?", (b,))
        vecs = _theme_member_vectors(conn, a)
        centroid = compute_centroid([v for _, v in vecs])
        conn.execute(
            "UPDATE themes SET centroid = ?, note_count = ?, updated_date = ? WHERE id = ?",
            (json.dumps(centroid, separators=(",", ":")), len(vecs), now_iso, a))
        conn.commit()
        return True
    finally:
        conn.close()


# --- Slice D (#231): theme-query data layer + activation recompute --------


def recompute_activation(conn: sqlite3.Connection, now_iso: str,
                         half_life_days: int = HALF_LIFE_DAYS) -> int:
    """Recompute themes.activation for all themes from member added_date recency.

    activation = sum over members of 0.5 ** (age_days / half_life_days),
    age_days = max(0, (now - added_date).days). Caller owns the transaction
    (callers wrap this in BEGIN IMMEDIATE). Returns the number of themes updated.
    """
    from datetime import datetime
    now_d = datetime.fromisoformat(now_iso).date()
    acc: dict[int, float] = {}
    for r in conn.execute("SELECT theme_id, added_date FROM theme_members").fetchall():
        tid = r["theme_id"] if hasattr(r, "keys") else r[0]
        added = r["added_date"] if hasattr(r, "keys") else r[1]
        # A single member with a NULL or non-date added_date must be SKIPPED,
        # not crash the whole recompute. The bare/with-time parses are nested
        # so a None or garbage value raises (ValueError, TypeError) here.
        try:
            try:
                ad = datetime.fromisoformat(added).date()
            except ValueError:
                ad = datetime.fromisoformat(added + "T00:00:00").date()
        except (ValueError, TypeError):
            print(f"[themes] skipping member with bad added_date={added!r} theme_id={tid}",
                  file=sys.stderr)
            continue
        age = max(0, (now_d - ad).days)
        acc[tid] = acc.get(tid, 0.0) + 0.5 ** (age / half_life_days)
    n = 0
    theme_ids = [r["id"] if hasattr(r, "keys") else r[0]
                 for r in conn.execute("SELECT id FROM themes").fetchall()]
    for tid in theme_ids:
        conn.execute("UPDATE themes SET activation = ? WHERE id = ?",
                     (acc.get(tid, 0.0), tid))
        n += 1
    return n


def get_themes_in_window(db_path: str, window_start: str,
                         project: str | None = None) -> list[dict]:
    """Themes whose updated_date >= window_start, ranked by
    activation then size. project=None returns every theme (cross-project);
    a project name returns that project's themes plus cross-project (NULL) ones.
    """
    from vault_index import _connect
    sql = (
        "SELECT id, name, summary, note_count, activation, project, "
        "created_date, updated_date FROM themes WHERE updated_date >= ?"
    )
    params: list = [window_start]
    if project is not None:
        sql += " AND (project = ? OR project IS NULL)"
        params.append(project)
    sql += " ORDER BY activation DESC, note_count DESC"
    conn = _connect(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_theme_member_previews(conn: sqlite3.Connection, theme_id: int,
                              top_n: int = 3, body_chars: int = 600) -> list[dict]:
    """Top-n members of a theme by similarity DESC, joined to notes.

    Returns [{note_path, title, excerpt, similarity, surprise, project, date}].
    excerpt = notes.body truncated to body_chars; falls back to title when body
    is empty. Takes a live, caller-owned connection.
    """
    rows = conn.execute(
        "SELECT tm.note_path, tm.similarity, tm.surprise, "
        "n.title, n.body, n.project, n.date "
        "FROM theme_members tm JOIN notes n ON n.path = tm.note_path "
        "WHERE tm.theme_id = ? ORDER BY tm.similarity DESC LIMIT ?",
        (theme_id, top_n),
    ).fetchall()
    out = []
    for r in rows:
        body = r["body"] or ""
        title = r["title"] or ""
        excerpt = body[:body_chars] if body else title
        out.append({
            "note_path": r["note_path"],
            "title": title,
            "excerpt": excerpt,
            "similarity": r["similarity"],
            "surprise": r["surprise"],
            "project": r["project"],
            "date": r["date"],
        })
    return out


def get_top_themes_for_project(db_path: str, project: str | None,
                               top_n: int = 3) -> list[dict]:
    """Top themes for a project (plus cross-project NULL themes), ranked by
    activation then size. Returns [{id, name, summary, note_count, activation}].
    Used by /recall's Recurring Themes section.
    """
    from vault_index import _connect
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name, summary, note_count, activation FROM themes "
            "WHERE project = ? OR project IS NULL "
            "ORDER BY activation DESC, note_count DESC LIMIT ?",
            (project, top_n),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_unassigned_notes_in_window(db_path: str, window_start: str,
                                   limit: int = 30) -> list[dict]:
    """Vectorized notes with no theme_members row and date >= window_start,
    newest first, capped at ``limit``. These are /emerge's new candidates.
    Excludes transient ``claude-snapshot`` notes (parity with the retired
    ``collect_vault_corpus`` default). Returns [{note_path, title, excerpt,
    project, date}].
    """
    from vault_index import _connect
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT n.path, n.title, n.body, n.project, n.date FROM notes n "
            "WHERE n.tfidf_vector IS NOT NULL AND n.tfidf_vector != '' "
            "AND n.type != 'claude-snapshot' "
            "AND n.date >= ? "
            "AND NOT EXISTS (SELECT 1 FROM theme_members tm WHERE tm.note_path = n.path) "
            "ORDER BY n.date DESC LIMIT ?",
            (window_start, limit),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        body = r["body"] or ""
        title = r["title"] or ""
        out.append({
            "note_path": r["path"],
            "title": title,
            "excerpt": body[:600] if body else title,
            "project": r["project"],
            "date": r["date"],
        })
    return out
