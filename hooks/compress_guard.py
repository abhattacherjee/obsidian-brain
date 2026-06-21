"""Pure predicate for the /compress Step 3.5 high-confidence match filter.

Kept in its own module so the tuning harness (scripts/tune_compress_rank_gap.py)
and pytest (tests/test_compress_rank_gap.py) can import it directly rather
than shelling out through the SKILL.md embedded block.

MIN_RANK_DELTA is empirically tuned against scripts/compress_rank_gap_corpus.json.
Hard constraint: must be < 4.75 so the issue #45 repro case (top -29.46,
runner-up -24.71) resolves to match: True. The full tuning protocol lives
in the external spec-docs repository at
superpowers/specs/2026-04-23-compress-rank-gap-delta-guard-design.md
(github.com/abhattacherjee/spec-docs).

MIN_COSINE is a PROVISIONAL default validated by the unit-test mechanism here
(tests/test_compress_rank_gap.py cosine fixtures); final live-corpus calibration
against a representative set of vault notes is a follow-up (#252 / #108).
TF-IDF query-vs-note sparse cosine scores are inherently low (sparse overlap
between a short query vector and a pruned top-50 note vector), so 0.12 is
already conservative; lower values risk accepting cross-topic matches.

OR-fallback awareness (live-calibration fix, #252/#108):
  When the top result has or_fallback=True (retrieved via the FTS5 OR-path
  because AND found nothing), it may share only generic tokens with the query.
  Missing-vector policy is therefore split by path:
    - AND-path (or_fallback absent or False): fail-OPEN — all query terms were
      present, so high rank confidence is sufficient even without a stored vector.
    - OR-fallback (or_fallback=True) + no vector: fail-CLOSED — cannot confirm
      semantic closeness; this is exactly the #252/#108 false-positive class.
  When a vector IS present, both paths use the same cosine >= min_cosine gate
  (unchanged behavior).
"""

from tfidf import _cosine_similarity

MIN_RANK_STRENGTH = -5.0  # top result's FTS5 rank must be <= this (stronger = more negative)
MIN_RANK_DELTA = 0.25     # tuned against scripts/compress_rank_gap_corpus.json on 2026-04-23

# PROVISIONAL default — see module docstring for calibration note.
MIN_COSINE = 0.12         # minimum cosine(query_vec, top_note_tfidf_vector) to accept a match

# Minimum shared terms (query weight > 0) between query and top note; set to 0
# to disable the overlap check by default (cosine already captures generic-only
# overlap via low IDF weight; this param is available for future tightening).
MIN_TERM_OVERLAP = 0


def is_high_confidence_match(
    results,
    min_strength=None,
    min_delta=None,
    query_vec=None,
    min_cosine=None,
    min_term_overlap=None,
):
    """Return True if the top search result is a high-confidence match.

    Arguments:
        results: list of dicts with a "rank" field. Callers must pass the
            list sorted most-negative first (as SKILL.md Step 3.5 does).
            Results that carry a "tfidf_vector" field (dict or None) are
            used for the optional cosine gate when query_vec is supplied.
        min_strength: optional override for MIN_RANK_STRENGTH.
        min_delta: optional override for MIN_RANK_DELTA.
        query_vec: sparse TF-IDF dict {term: float} for the search query.
            When None the cosine gate is entirely bypassed (backward compat).
        min_cosine: optional override for MIN_COSINE. Ignored when query_vec
            is None.
        min_term_overlap: optional override for MIN_TERM_OVERLAP. When > 0
            the top result must share at least this many terms (with query
            weight > 0) with the query. Ignored when query_vec is None.
            Default 0 disables the check.

    Returns:
        bool. True only if:
          (a) results is non-empty,
          (b) top.rank passes the absolute-strength gate,
          (c) either there is no runner-up or |top.rank| - |runner_up.rank|
              exceeds the delta gate, AND
          (d) when query_vec is not None:
              - if top result carries a non-empty tfidf_vector:
                  cosine(query_vec, tfidf_vector) >= min_cosine (both paths).
              - if tfidf_vector is absent/None/empty:
                  - AND-path (or_fallback absent or False): fail-OPEN, rank
                    verdict stands (all query terms matched → high confidence).
                  - OR-fallback (or_fallback=True): fail-CLOSED, return False
                    (cannot confirm semantic closeness for a generic-token hit;
                    this is the #252/#108 false-positive class).
    """
    if min_strength is None:
        min_strength = MIN_RANK_STRENGTH
    if min_delta is None:
        min_delta = MIN_RANK_DELTA
    if min_cosine is None:
        min_cosine = MIN_COSINE
    if min_term_overlap is None:
        min_term_overlap = MIN_TERM_OVERLAP

    if not results:
        return False
    top = results[0]
    if top["rank"] > min_strength:
        return False
    if len(results) < 2:
        rank_verdict = True
    else:
        rank_verdict = (abs(top["rank"]) - abs(results[1]["rank"])) > min_delta

    if not rank_verdict:
        return False

    # Cosine gate — only evaluated when caller supplies a query vector.
    if query_vec is not None:
        note_vec = top.get("tfidf_vector")
        is_or_fallback = bool(top.get("or_fallback"))
        if note_vec:
            # Vector present: apply cosine gate for both AND and OR-fallback paths.
            if min_term_overlap > 0:
                shared = sum(
                    1
                    for term, w in query_vec.items()
                    if w > 0 and term in note_vec
                )
                if shared < min_term_overlap:
                    return False
            cosine = _cosine_similarity(query_vec, note_vec)
            if cosine < min_cosine:
                return False
        else:
            # No vector to confirm semantic closeness.
            # OR-fallback hit: fail-CLOSED — generic-token match, cannot verify
            # relatedness; this is the #252/#108 false-positive class.
            # AND-path (or_fallback absent/False): fail-OPEN — all query terms
            # were present, so rank confidence is sufficient.
            if is_or_fallback:
                return False

    return True
