"""Tests for hooks/compress_guard.py — /compress Step 3.5 rank-gap predicate.

Covers boundary cases for `is_high_confidence_match()`, the pure predicate
extracted from /compress SKILL.md Step 3.5. The helper is imported
directly — no FTS5 fixture needed since the predicate takes a pre-sorted
results list.
"""

import pytest

from compress_guard import (
    MIN_RANK_DELTA,
    MIN_RANK_STRENGTH,
    is_high_confidence_match,
)


def test_empty_results_rejects():
    assert is_high_confidence_match([]) is False


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
    # delta = |−29| − |−26| = 3, below default MIN_RANK_DELTA
    results = [{"rank": -29.0}, {"rank": -26.0}]
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


def test_weak_top_rank_rejects_even_with_large_delta():
    # Top rank -3 fails strength gate despite enormous delta to -30
    results = [{"rank": -3.0}, {"rank": -30.0}]
    assert is_high_confidence_match(results) is False


def test_custom_thresholds_respected():
    # Default accepts (rank -8 <= -5), custom min_strength=-10 rejects
    results = [{"rank": -8.0}, {"rank": -6.0}]
    assert is_high_confidence_match(results) is True
    assert (
        is_high_confidence_match(results, min_strength=-10.0, min_delta=1.0)
        is False
    )
    # Default rejects (delta 2 < 5), custom min_delta=1 accepts
    results2 = [{"rank": -8.0}, {"rank": -6.0}]
    assert is_high_confidence_match(results2) is False
    assert (
        is_high_confidence_match(results2, min_delta=1.0) is True
    )


def test_constants_are_exposed():
    # Import-level contract: callers can reference the defaults.
    assert isinstance(MIN_RANK_STRENGTH, float)
    assert isinstance(MIN_RANK_DELTA, float)
    assert MIN_RANK_STRENGTH <= 0.0
    assert MIN_RANK_DELTA > 0.0
    # Hard constraint from issue #45 — fix is not complete without this.
    assert MIN_RANK_DELTA < 4.75
