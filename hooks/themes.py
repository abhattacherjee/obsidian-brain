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


def assign_to_theme(
    db_path: str,
    note_path: str,
    project: str | None = None,
    similarity_threshold: float = _THEME_SIMILARITY_THRESHOLD,
) -> dict | None:
    """Incrementally assign a summarized note to its nearest theme (if any).

    Reads the note's tfidf_vector and compares against every project-scoped
    theme centroid plus any cross-project themes (project IS NULL). If the
    best similarity exceeds ``similarity_threshold``, adds a theme_members
    row and updates the theme's centroid via running average.

    Returns {"theme_id": int, "similarity": float} on assignment, or None
    if no theme was close enough (or the note has no vector).
    """
    from vault_index import _connect  # local import: breaks vault_index<->themes cycle
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

            best: tuple[float, int, dict, int] | None = None
            for cand in candidates:
                if not cand["centroid"]:
                    continue
                try:
                    centroid = json.loads(cand["centroid"])
                except json.JSONDecodeError:
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
            else:
                # Reassignment: bump updated_date but leave count/centroid
                # alone. The member's similarity is refreshed below.
                conn.execute(
                    "UPDATE themes SET updated_date = ? WHERE id = ?",
                    (today, theme_id),
                )

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
            conn.commit()
            return {"theme_id": theme_id, "similarity": sim}
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
