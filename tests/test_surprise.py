"""Tests for surprise detection (Phase 2)."""

import pytest

import vault_index


class TestDetectSurprise:
    def test_negated_shared_term_scores_above_threshold(self):
        text = "retrieval scoring is not reliable at 10k notes. activation does not help."
        centroid = {"retrieval": 1.0, "scoring": 1.0, "activation": 1.0}
        note_vec = {"retrieval": 1.0, "scoring": 1.0, "activation": 1.0}
        score = vault_index.detect_surprise(text, note_vec, centroid)
        assert score > 0.5

    def test_agreement_text_scores_zero(self):
        text = "retrieval scoring is reliable. activation is useful."
        centroid = {"retrieval": 1.0, "scoring": 1.0, "activation": 1.0}
        note_vec = {"retrieval": 1.0, "scoring": 1.0, "activation": 1.0}
        score = vault_index.detect_surprise(text, note_vec, centroid)
        assert score == 0.0

    def test_no_shared_terms_returns_zero(self):
        text = "retrieval scoring is not reliable"
        centroid = {"pasta": 1.0, "garlic": 1.0}
        note_vec = {"retrieval": 1.0, "scoring": 1.0}
        score = vault_index.detect_surprise(text, note_vec, centroid)
        assert score == 0.0

    def test_negation_outside_window_does_not_count(self):
        words = ["not"] + ["filler"] * 30 + ["retrieval"]
        text = " ".join(words)
        score = vault_index.detect_surprise(
            text, {"retrieval": 1.0}, {"retrieval": 1.0},
        )
        assert score == 0.0

    def test_score_is_clamped_to_unit_interval(self):
        text = "retrieval never scoring never activation never importance never"
        centroid = {
            "retrieval": 1.0, "scoring": 1.0, "activation": 1.0, "importance": 1.0,
        }
        note_vec = dict(centroid)
        score = vault_index.detect_surprise(text, note_vec, centroid)
        assert 0.0 <= score <= 1.0
