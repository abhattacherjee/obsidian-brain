"""Tests for hooks/compress_guard.py — /compress Step 3.5 rank-gap predicate.

Covers boundary cases for `is_high_confidence_match()`, the pure predicate
extracted from /compress SKILL.md Step 3.5. The helper is imported
directly — no FTS5 fixture needed since the predicate takes a pre-sorted
results list.
"""

from compress_guard import (
    MIN_COSINE,
    MIN_RANK_DELTA,
    MIN_RANK_STRENGTH,
    MIN_TERM_OVERLAP,
    is_high_confidence_match,
)


def test_empty_results_rejects():
    assert is_high_confidence_match([]) is False


def test_equal_ranks_rejects():
    """Delta = 0 when top and runner-up have identical ranks.

    FTS5 can produce identical rank values for near-identical notes. The
    strict '>' delta gate rejects (0 is never > any positive MIN_RANK_DELTA),
    so the guard correctly asks the user to choose rather than auto-picking.
    """
    results = [{"rank": -10.0}, {"rank": -10.0}]
    assert is_high_confidence_match(results) is False


def test_single_result_matches_when_rank_strong():
    # rank -10 is stronger than min_strength -5.0
    assert is_high_confidence_match([{"rank": -10.0}]) is True


def test_single_result_rejects_when_rank_weak():
    # rank -3 is weaker than min_strength -5.0
    assert is_high_confidence_match([{"rank": -3.0}]) is False


def test_delta_above_threshold_matches():
    # delta = |−29| − |−20| = 9, above default MIN_RANK_DELTA
    results = [{"rank": -29.0}, {"rank": -20.0}]
    assert is_high_confidence_match(results) is True


def test_delta_below_threshold_rejects():
    # delta ≈ 0.1 (|-29| - |-28.9| = 0.1000...142 due to IEEE 754);
    # below the current THRESHOLD_GRID minimum of 0.25, so this case
    # rejects at every grid-selectable MIN_RANK_DELTA.
    results = [{"rank": -29.0}, {"rank": -28.9}]
    assert is_high_confidence_match(results) is False


def test_issue_45_repro_case():
    """Executable form of the issue #45 bug definition.

    Real ranks from the vault: top -29.46, runner-up -24.71, delta 4.75.
    Must resolve True under the shipped MIN_RANK_DELTA — a failing
    assertion here means the fix did not land. The shipped constant
    must be strictly less than 4.75.
    """
    results = [{"rank": -29.46}, {"rank": -24.71}]
    assert is_high_confidence_match(results) is True


def test_weak_top_rank_rejects():
    # List is sorted most-negative first (predicate's documented contract):
    # -4.0 is the "top" at index 0, -3.0 is the runner-up at index 1. Top
    # rank -4 still fails the strength gate (-4 > -5). The strength gate
    # fires before the delta gate is evaluated — no strong #2 is needed
    # to prove the point.
    results = [{"rank": -4.0}, {"rank": -3.0}]
    assert is_high_confidence_match(results) is False


def test_delta_at_threshold_boundary_rejects():
    """Strict > comparison: delta exactly at threshold must reject.

    Predicate uses `delta > min_delta`, not `>=`. This test locks that
    semantic so a future tuning change to a different MIN_RANK_DELTA
    value still correctly rejects the at-threshold case.
    Guards against an off-by-one drift toward `>=`.
    """
    # delta = |−29| − |−28.75| = 0.25, exactly equals default MIN_RANK_DELTA (0.25) → reject.
    # Input values are multiples of 0.25, so they're exact binary fractions (no IEEE 754
    # rounding); this makes the at-threshold equality check reliable. Future boundary tests
    # should pick similarly-exact inputs rather than (say) 0.1, which is not exactly representable.
    results = [{"rank": -29.0}, {"rank": -28.75}]
    assert is_high_confidence_match(results) is False
    # With a custom min_delta=0.15, same input has delta 0.25 > 0.15 → accept
    assert is_high_confidence_match(results, min_delta=0.15) is True


def test_custom_thresholds_respected():
    # Sub-test 1: strength gate only (single result, delta gate bypassed)
    results_single = [{"rank": -8.0}]
    assert is_high_confidence_match(results_single) is True  # -8 <= default -5.0
    assert (
        is_high_confidence_match(results_single, min_strength=-10.0) is False
    )  # -8 > -10 fails custom strength gate

    # Sub-test 2: delta gate (two results; strength gate passes for both calls)
    results_pair = [{"rank": -8.0}, {"rank": -7.9}]
    assert is_high_confidence_match(results_pair) is False  # delta 0.1 below default 0.25
    assert (
        is_high_confidence_match(results_pair, min_delta=0.05) is True
    )  # delta 0.1 above custom min_delta=0.05


def test_constants_are_exposed():
    # Import-level contract: callers can reference the defaults.
    assert isinstance(MIN_RANK_STRENGTH, float)
    assert isinstance(MIN_RANK_DELTA, float)
    assert MIN_RANK_STRENGTH <= 0.0
    assert MIN_RANK_DELTA > 0.0
    # Hard constraint from issue #45 — fix is not complete without this.
    assert MIN_RANK_DELTA < 4.75


# ---------------------------------------------------------------------------
# Cosine + high-IDF gate tests (#252 / #108)
# ---------------------------------------------------------------------------

# Helper vectors used across multiple cosine tests.
# ON-TOPIC: query and note share "notebook" and "obsidian" (high-specificity
# terms); cosine is clearly above MIN_COSINE.
_QUERY_ON_TOPIC = {"notebook": 4.0, "obsidian": 4.0, "plugin": 2.0}
_NOTE_ON_TOPIC = {"notebook": 4.0, "obsidian": 4.0, "vault": 2.0}

# CROSS-TOPIC: query is about "obsidian notebook" but note is about "python
# code debug" — no shared terms → cosine = 0.0 < MIN_COSINE.
_NOTE_CROSS_TOPIC = {"python": 4.0, "code": 4.0, "debug": 2.0}

# GENERIC: query and note share only "plugin" (low-weight generic term).
# Actual cosine depends on weights; see test_single_or_fallback_generic_overlap_rejects.
_NOTE_GENERIC_OVERLAP = {"plugin": 2.0, "other": 8.0}

# Rank that passes the strength gate (single result branch, the #252 bug path).
_SINGLE_RESULT_STRONG_RANK = -29.46


def test_query_vec_none_is_backward_compatible():
    """When query_vec=None the new parameters must have zero effect on behavior.

    Deliberately mirrors the pre-existing rank-only test cases (empty list,
    single strong, two-result large delta, two-result tight delta) as an
    explicit backward-compat contract guard — not accidental duplication.
    Exercises the new optional-parameter signature to confirm it does not
    alter outcomes when cosine data is absent.
    """
    # empty → False (unchanged)
    assert is_high_confidence_match([], query_vec=None) is False

    # single result, strong rank → True (the #252 bug path, unchanged without cosine)
    assert is_high_confidence_match(
        [{"rank": _SINGLE_RESULT_STRONG_RANK}], query_vec=None
    ) is True

    # two results, large delta → True
    results_strong = [{"rank": -29.46}, {"rank": -24.71}]
    assert is_high_confidence_match(results_strong, query_vec=None) is True

    # two results, delta below threshold → False
    results_tight = [{"rank": -29.0}, {"rank": -28.9}]
    assert is_high_confidence_match(results_tight, query_vec=None) is False

    # explicit min_cosine with query_vec=None must still be a no-op
    assert is_high_confidence_match(
        [{"rank": _SINGLE_RESULT_STRONG_RANK}], query_vec=None, min_cosine=0.99
    ) is True


def test_cosine_below_floor_rejects_strong_rank():
    """Top result passes rank gates but cosine(query, note) < MIN_COSINE → False.

    Models the #108 cross-topic peer scenario: two notes have very similar FTS5
    rank values (large delta) but the query is semantically unrelated to the
    top result (different domain entirely).
    """
    results = [
        {"rank": -29.46, "tfidf_vector": _NOTE_CROSS_TOPIC},
        {"rank": -24.71},
    ]
    # Sanity: without cosine gate (query_vec=None) the rank gates would accept.
    assert is_high_confidence_match(results, query_vec=None) is True

    # With cosine gate: cross-topic → cosine = 0.0 < MIN_COSINE → reject.
    assert is_high_confidence_match(results, query_vec=_QUERY_ON_TOPIC) is False


def test_single_or_fallback_generic_overlap_rejects():
    """Single result, strong rank, but query/note share only a low-weight generic term.

    Models #252: the single-result fast-path (no runner-up) previously returned
    True unconditionally.  With the cosine gate a generic-only overlap must be
    rejected when query_vec is supplied.
    """
    # Only "plugin" is shared; its weight in both vectors is 2.0.
    # query = {"notebook": 4, "obsidian": 4, "plugin": 2}
    # note  = {"plugin": 2, "other": 8}
    # dot = 2*2 = 4
    # |q| = sqrt(16+16+4) = sqrt(36) = 6
    # |n| = sqrt(4+64) = sqrt(68) ≈ 8.246
    # cosine ≈ 4/(6*8.246) ≈ 0.081 < MIN_COSINE (0.12)
    results = [
        {"rank": _SINGLE_RESULT_STRONG_RANK, "tfidf_vector": _NOTE_GENERIC_OVERLAP},
    ]
    # Rank-only (the pre-fix behavior) would accept this.
    assert is_high_confidence_match(results, query_vec=None) is True

    # Cosine gate with the supplied query_vec rejects the generic-only overlap.
    assert is_high_confidence_match(results, query_vec=_QUERY_ON_TOPIC) is False


def test_cosine_above_floor_matches():
    """Strong rank + cosine clearly above MIN_COSINE → True.

    On-topic query and note share high-weight specific terms; the cosine gate
    should not spuriously reject a genuine match.
    """
    results = [
        {"rank": -29.46, "tfidf_vector": _NOTE_ON_TOPIC},
        {"rank": -24.71},
    ]
    assert is_high_confidence_match(results, query_vec=_QUERY_ON_TOPIC) is True


def test_cosine_at_floor_boundary():
    """Boundary: cosine == min_cosine must be accepted (>= semantics).

    Constructs sparse vectors with power-of-two weights where cosine can be
    computed exactly in IEEE 754, then passes that exact value as min_cosine.

    Vectors chosen:
        q = {"a": 1.0, "b": 2.0, "c": 2.0}   |q| = sqrt(1+4+4) = 3
        n = {"a": 2.0, "d": 4.0, "e": 4.0}   |n| = sqrt(4+16+16) = 6
        dot = 1*2 = 2
        cosine = 2 / (3*6) = 2/18 = 1/9

    1/9 is not exactly representable in IEEE 754, but Python's float division
    2.0 / (3.0 * 6.0) produces the same bit pattern as the cosine computation
    inside _cosine_similarity — so the exact-equality test is reliable.
    """
    q_vec = {"a": 1.0, "b": 2.0, "c": 2.0}   # |q| = 3.0 (exact)
    n_vec = {"a": 2.0, "d": 4.0, "e": 4.0}   # |n| = 6.0 (exact)
    # cosine = dot / (|q| * |n|) = 2.0 / 18.0 = 1/9 (same float ops as _cosine_similarity)
    exact_cosine = 2.0 / (3.0 * 6.0)

    results = [
        {"rank": _SINGLE_RESULT_STRONG_RANK, "tfidf_vector": n_vec},
    ]

    # At boundary (min_cosine == cosine): >= passes, > would reject.
    assert is_high_confidence_match(
        results, query_vec=q_vec, min_cosine=exact_cosine
    ) is True  # cosine == min_cosine → accepted (>= semantics)

    # Strictly above boundary: also passes.
    assert is_high_confidence_match(
        results, query_vec=q_vec, min_cosine=exact_cosine - 1e-10
    ) is True

    # Strictly below boundary: rejects.
    assert is_high_confidence_match(
        results, query_vec=q_vec, min_cosine=exact_cosine + 1e-10
    ) is False


def test_issue_45_true_positive_still_matches():
    """The #45 repro case (top -29.46 / runner-up -24.71) must stay True with cosine gate.

    On-topic vectors confirm cosine >> MIN_COSINE so the gate does not regress
    a real match.
    """
    results = [
        {"rank": -29.46, "tfidf_vector": _NOTE_ON_TOPIC},
        {"rank": -24.71},
    ]
    assert is_high_confidence_match(results, query_vec=_QUERY_ON_TOPIC) is True


def test_missing_tfidf_vector_defaults_safe():
    """When tfidf_vector is missing/None, the cosine gate is skipped (fail-open).

    The caller may have been indexed before cosine vectors were computed, or the
    note may be too short to produce a non-empty TF-IDF vector.  In both cases
    we fall back to the rank-only verdict rather than spuriously rejecting.
    """
    # tfidf_vector key absent entirely — must not KeyError, must fall back.
    results_absent = [
        {"rank": _SINGLE_RESULT_STRONG_RANK},
        {"rank": -24.71},
    ]
    assert is_high_confidence_match(results_absent, query_vec=_QUERY_ON_TOPIC) is True

    # tfidf_vector key present but None.
    results_none = [
        {"rank": _SINGLE_RESULT_STRONG_RANK, "tfidf_vector": None},
        {"rank": -24.71},
    ]
    assert is_high_confidence_match(results_none, query_vec=_QUERY_ON_TOPIC) is True

    # tfidf_vector present but empty dict — treated as missing (cosine would be 0).
    # An empty vector means the note had no usable terms; skip gate.
    results_empty = [
        {"rank": _SINGLE_RESULT_STRONG_RANK, "tfidf_vector": {}},
        {"rank": -24.71},
    ]
    assert is_high_confidence_match(results_empty, query_vec=_QUERY_ON_TOPIC) is True
