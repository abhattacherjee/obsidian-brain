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

from __future__ import annotations

from tfidf import _cosine_similarity

MIN_RANK_STRENGTH = -5.0  # top result's FTS5 rank must be <= this (stronger = more negative)
MIN_RANK_DELTA = 0.25     # tuned against scripts/compress_rank_gap_corpus.json on 2026-04-23

# PROVISIONAL default — see module docstring for calibration note.
MIN_COSINE = 0.12         # minimum cosine(query_vec, top_note_tfidf_vector) to accept a match

# Strong-cosine threshold that rescues a borderline rank_delta near-miss (#254 / #45 class).
# A top match this semantically close to the query is the right home even when the BM25
# rank-gap (MIN_RANK_DELTA) is a near-miss.  Conservative default: above the cross-topic
# false-positive cosine band (~0.12–0.20), at/below genuine same-topic (~0.42+).
# Mechanism-proving: #254's own repro is closed by the note_type widening (Fix A); this
# rescue hardens the broader #45 class, which currently lacks a live repro.
MIN_COSINE_RESCUE = 0.40

# A rescued match must still clear the cosine gate, so the rescue threshold
# must be >= the cosine floor; otherwise a rescue would fire then be silently
# rejected. Fail loudly at import if a future tuning breaks this invariant.
assert MIN_COSINE_RESCUE >= MIN_COSINE, (
    "MIN_COSINE_RESCUE must be >= MIN_COSINE (a rescued match must pass the cosine gate)"
)

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
    min_cosine_rescue=None,
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
        min_cosine_rescue: optional override for MIN_COSINE_RESCUE. When
            query_vec is not None and the rank_delta gate fails, a top
            match with cosine >= min_cosine_rescue is rescued (rank_verdict
            flipped True and evaluation continues into the cosine gate).
            Ignored when query_vec is None. Default None uses the module
            constant MIN_COSINE_RESCUE. Callers overriding this parameter
            must keep min_cosine_rescue >= min_cosine; the function does
            not re-validate override params (the module-level assert only
            guards the default constants).

    Returns:
        bool. True only if:
          (a) results is non-empty,
          (b) top.rank passes the absolute-strength gate,
          (c) either there is no runner-up or |top.rank| - |runner_up.rank|
              exceeds the delta gate, OR (c-rescue) the delta gate fails but
              query_vec is not None, the top result has a non-empty
              tfidf_vector, and cosine >= min_cosine_rescue, AND
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
    if min_cosine_rescue is None:
        min_cosine_rescue = MIN_COSINE_RESCUE

    if not results:
        return False
    top = results[0]
    if top["rank"] > min_strength:
        return False
    if len(results) < 2:
        rank_verdict = True
    else:
        rank_verdict = (abs(top["rank"]) - abs(results[1]["rank"])) > min_delta

    # Cosine-rescue: when rank_delta is a near-miss, a strong top-match cosine
    # rescues it.  The cosine value is computed once here and reused in the
    # cosine gate below to avoid calling _cosine_similarity twice.
    # Rescue fires only when query_vec is available (None → legacy path unchanged).
    top_cosine = None  # computed once, shared with the cosine gate below
    if not rank_verdict:
        if query_vec is not None:
            top_vec = top.get("tfidf_vector")
            if top_vec:
                top_cosine = _cosine_similarity(query_vec, top_vec)
                if top_cosine >= min_cosine_rescue:
                    rank_verdict = True  # rescued — continue into cosine gate
        if not rank_verdict:
            return False

    # Cosine gate — only evaluated when caller supplies a query vector.
    if query_vec is not None:
        note_vec = top.get("tfidf_vector")
        is_or_fallback = bool(top.get("or_fallback"))
        if note_vec:
            # Vector present: apply cosine gate for both AND and OR-fallback paths.
            # Reuse the cosine computed during rescue (if available) to avoid a
            # second call to _cosine_similarity on the same pair of vectors.
            if top_cosine is None:
                top_cosine = _cosine_similarity(query_vec, note_vec)
            cosine = top_cosine
            if min_term_overlap > 0:
                shared = sum(
                    1
                    for term, w in query_vec.items()
                    if w > 0 and term in note_vec
                )
                if shared < min_term_overlap:
                    return False
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


def summarize_match_evidence(results, query_vec=None, min_strength=None, max_terms=8):
    """Summarize the evidence for why the top search result is a high-confidence match.

    Pure function — no file I/O. Designed to be called after is_high_confidence_match()
    confirms a match, but guarded defensively for the empty-results case.

    Arguments:
        results: same sorted-most-negative-first list passed to is_high_confidence_match.
            Each element may carry a "tfidf_vector" key (dict[str, float] | None).
        query_vec: sparse TF-IDF dict {term: float} for the search query, or None.
            When None (or empty dict), shared_terms is always [].
        min_strength: accepted for call-site symmetry with is_high_confidence_match;
            not used anywhere in this function (band thresholds are fixed). Default None.
        max_terms: maximum number of shared terms to return (default 8).

    Returns a dict with:
        "rank": float | None — results[0]["rank"], or None if results is empty.
        "runner_up_rank": float | None — results[1]["rank"] if len>=2, else None.
        "rank_note": str — calibration band based on abs(rank):
            >= 25 → "very strong"
            >= 15 → "strong"
            >= 8  → "moderate"
            else  → "borderline"
            Boundaries are inclusive at the lower edge (|rank|==25 → "very strong",
            ==15 → "strong", ==8 → "moderate"). Returns "" when rank is None.
        "shared_terms": list[str] — terms present in BOTH query_vec (query weight > 0)
            AND results[0]["tfidf_vector"] (any stored weight), sorted by descending query weight (ties
            broken alphabetically for determinism), capped to max_terms. Empty list
            when query_vec is None/empty or top result has no usable tfidf_vector
            (None/missing/empty).
    """
    if not results:
        return {
            "rank": None,
            "runner_up_rank": None,
            "rank_note": "",
            "shared_terms": [],
        }

    rank = results[0]["rank"]
    runner_up_rank = results[1]["rank"] if len(results) >= 2 else None

    abs_rank = abs(rank)
    if abs_rank >= 25:
        rank_note = "very strong"
    elif abs_rank >= 15:
        rank_note = "strong"
    elif abs_rank >= 8:
        rank_note = "moderate"
    else:
        rank_note = "borderline"

    shared_terms = []
    if query_vec:
        note_vec = results[0].get("tfidf_vector")
        if note_vec:
            # Find terms in both vectors where query weight > 0
            shared = [
                (term, w)
                for term, w in query_vec.items()
                if w > 0 and term in note_vec
            ]
            # Sort by descending query weight; alphabetical tiebreak for determinism
            shared.sort(key=lambda tv: (-tv[1], tv[0]))
            shared_terms = [term for term, _ in shared[:max_terms]]

    return {
        "rank": rank,
        "runner_up_rank": runner_up_rank,
        "rank_note": rank_note,
        "shared_terms": shared_terms,
    }


def topic_snippet(note_text, max_chars=200):
    """Extract the first substantive paragraph from a vault note.

    Pure function — caller passes already-read text. No file I/O.

    Behavior:
        1. Strip a leading YAML frontmatter block if present: when the first line is exactly "---", drop through the next line that is exactly "---" (CRLF and LF endings both handled). If there is no closing fence, nothing is stripped.
        2. Skip leading blank lines and ATX headings (lines starting with "#").
        3. Collect consecutive non-blank, non-heading lines as the first paragraph.
        4. Join collected lines with single spaces, collapsing internal whitespace.
        5. Truncate to max_chars; append "…" (single ellipsis char) if truncated.
        6. Return "" if no substantive content found.

    Arguments:
        note_text: full raw text of a note file (YAML frontmatter + markdown body)
            or just a body. May be empty.
        max_chars: maximum number of characters to return before truncation (default 200).

    Returns:
        str — first substantive paragraph, possibly truncated with "…", or "".
    """
    if not note_text:
        return ""

    # Normalize line endings so CRLF notes are handled identically to LF.
    lines = note_text.replace("\r\n", "\n").split("\n")

    # Strip a leading YAML frontmatter block: when the first line is exactly
    # "---", drop through the next line that is exactly "---". If there is no
    # closing fence, nothing is stripped (the text is treated as a bare body).
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                lines = lines[i + 1:]
                break

    # Collect first substantive paragraph: skip blank/heading lines until content,
    # then collect until the next blank line or heading.
    paragraph_lines = []
    in_paragraph = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            # Blank line: end paragraph if we've started one, otherwise skip
            if in_paragraph:
                break
            continue
        if stripped.startswith("#"):
            # Heading line: end paragraph if started, otherwise skip
            if in_paragraph:
                break
            continue
        # Substantive line
        in_paragraph = True
        paragraph_lines.append(" ".join(stripped.split()))

    if not paragraph_lines:
        return ""

    # Join with single spaces (internal whitespace already stripped per line)
    result = " ".join(paragraph_lines)

    # Truncate if needed
    if len(result) > max_chars:
        result = result[:max_chars] + "…"

    return result
