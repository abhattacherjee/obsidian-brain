"""TF-IDF primitives extracted from vault_index.py (#229 Slice A).

Leaf module — depends only on the stdlib. Imported by themes.py and
re-exported from vault_index.py for back-compat. Do not import vault_index
or themes from here (keeps the import graph acyclic).
"""
from __future__ import annotations

import math
import re
import sqlite3
from collections import defaultdict

_STOPWORDS = frozenset(
    "a an the and or but in on at to for of is it this that was were be been "
    "being have has had do does did will would shall should may might can could "
    "not no nor so if then than too very are am was were with from by about "
    "into through during before after above below between out off over under "
    "again further once here there when where why how all each every both few "
    "more most other some such only own same also just because as until while "
    "up down its they them their what which who whom he she we you i my your "
    "his her our me him us".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize_for_tfidf(text: str) -> list[str]:
    """Tokenize text for TF-IDF: lowercase alphanumerics, drop stopwords + single chars.

    Order is preserved (token occurrences count — duplicates are kept) so the
    caller can compute TF by counting. Returns [] for empty or all-stopword input.

    Single-character tokens (both letters like "a" and digits like "3" or "9"
    from expressions such as "Python-3.9") are dropped. Single digits appear
    in few documents initially, which inflates their IDF and lets them
    outrank real semantic terms in the sparse top-k — so letter and digit
    noise are both removed uniformly.
    """
    if not text:
        return []
    lowered = text.lower()
    return [
        t for t in _TOKEN_RE.findall(lowered)
        if len(t) > 1 and t not in _STOPWORDS
    ]


def _compute_tfidf_vector(
    tokens: list[str],
    term_df: dict[str, int],
    total_docs: int,
    top_k: int = 50,
) -> dict[str, float]:
    """Compute a sparse TF×IDF vector keeping the top_k heaviest terms.

    tokens:       output of _tokenize_for_tfidf() for the document
    term_df:      {term: document_frequency} for the corpus
    total_docs:   total indexed documents (including this one if it is new;
                  callers using incremental updates should increment total_docs
                  BEFORE calling this function for a newly-inserted note)
    top_k:        maximum number of terms to retain in the sparse vector

    Returns {} when tokens is empty. Uses smoothed IDF
    (1 + ln((N + 1) / (df + 1))) so new / rare terms never produce 0 or NaN.
    """
    if not tokens:
        return {}

    tf: dict[str, int] = defaultdict(int)
    for t in tokens:
        tf[t] += 1

    weights: dict[str, float] = {}
    n_plus_one = total_docs + 1
    for term, count in tf.items():
        df = term_df.get(term, 0)
        idf = 1.0 + math.log(n_plus_one / (df + 1))
        weights[term] = count * idf

    top = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
    return dict(top)


def _cosine_similarity(v1: dict[str, float], v2: dict[str, float]) -> float:
    """Cosine similarity between two sparse dict vectors. Returns 0.0 on empty input.

    Iterates the smaller dict to compute the dot product, which keeps the
    inner loop bounded even when one vector is much larger than the other.
    """
    if not v1 or not v2:
        return 0.0

    if len(v1) > len(v2):
        v1, v2 = v2, v1

    dot = 0.0
    for term, w in v1.items():
        other = v2.get(term)
        if other is not None:
            dot += w * other

    if dot == 0.0:
        return 0.0

    norm1 = math.sqrt(sum(w * w for w in v1.values()))
    norm2 = math.sqrt(sum(w * w for w in v2.values()))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return dot / (norm1 * norm2)


def _update_term_df(
    conn: sqlite3.Connection,
    old_terms: set[str],
    new_terms: set[str],
) -> None:
    """Adjust document-frequency counts for a note whose terms changed.

    Compares the two sets and applies a +1 / -1 per term via executemany.
    Terms whose df falls to zero are deleted so the IDF denominator stays
    clean. The caller is responsible for committing the transaction — this
    helper writes through ``conn`` but does not commit, so it can be
    batched atomically with a note upsert or delete.
    """
    removed = old_terms - new_terms
    added = new_terms - old_terms
    if not removed and not added:
        return

    cur = conn.cursor()

    if added:
        cur.executemany(
            "INSERT INTO term_df (term, df) VALUES (?, 1) "
            "ON CONFLICT(term) DO UPDATE SET df = df + 1",
            [(t,) for t in added],
        )

    if removed:
        cur.executemany(
            "UPDATE term_df SET df = df - 1 WHERE term = ?",
            [(t,) for t in removed],
        )
        cur.execute("DELETE FROM term_df WHERE df <= 0")


def _reverse_fold_centroid(
    centroid: dict[str, float],
    note_vec: dict[str, float],
    count: int,
) -> dict[str, float]:
    """Remove ``note_vec``'s contribution from a running-average ``centroid``.

    Given a centroid that is the mean of ``count`` member vectors, return the
    centroid of the remaining ``count - 1`` members after ``note_vec`` is
    removed: ``new = (centroid * count - note_vec) / (count - 1)`` per term,
    over the union of both term sets. Terms whose magnitude falls to
    ``<= 1e-9`` are pruned to keep the centroid sparse.

    Pure and DB-unaware. The caller guarantees ``count >= 2`` (the
    ``count <= 1`` "drop the theme" case is DB bookkeeping, not math).
    """
    new_count = count - 1
    new_centroid: dict[str, float] = {}
    for term in set(centroid) | set(note_vec):
        c_val = centroid.get(term, 0.0)
        v_val = note_vec.get(term, 0.0)
        new_val = (c_val * count - v_val) / new_count
        if abs(new_val) > 1e-9:
            new_centroid[term] = new_val
    return new_centroid
