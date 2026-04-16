"""Tests for TF-IDF primitives: tokenization, vector math, cosine similarity."""

import json
import math

import pytest

import vault_index


class TestTokenize:
    def test_strips_punctuation_and_lowercases(self):
        toks = vault_index._tokenize_for_tfidf("Hello, World! Python-3.9.")
        assert toks == ["hello", "world", "python", "3", "9"]

    def test_drops_stopwords(self):
        toks = vault_index._tokenize_for_tfidf("the quick brown fox is here")
        assert "the" not in toks
        assert "is" not in toks
        assert "here" not in toks
        assert "quick" in toks
        assert "brown" in toks

    def test_drops_single_char_tokens(self):
        toks = vault_index._tokenize_for_tfidf("a b c debugging")
        assert toks == ["debugging"]

    def test_empty_input(self):
        assert vault_index._tokenize_for_tfidf("") == []
        assert vault_index._tokenize_for_tfidf("the a an") == []

    def test_is_deterministic_and_preserves_order(self):
        text = "retrieval scoring retrieval activation scoring"
        toks = vault_index._tokenize_for_tfidf(text)
        assert toks == ["retrieval", "scoring", "retrieval", "activation", "scoring"]


class TestComputeTfidf:
    def test_sparse_vector_top_k(self):
        """TF×IDF keeps only the top_k heaviest terms."""
        tokens = ["retrieval"] * 5 + ["scoring"] * 3 + ["noise"] * 1
        df = {"retrieval": 1, "scoring": 2, "noise": 50}
        total_docs = 100
        vec = vault_index._compute_tfidf_vector(tokens, df, total_docs, top_k=2)
        assert set(vec.keys()) == {"retrieval", "scoring"}
        # retrieval has the lowest df → highest IDF AND highest TF → should win
        assert vec["retrieval"] > vec["scoring"] > 0

    def test_rare_term_outranks_common_term_at_equal_tf(self):
        tokens = ["obsidian", "python"]
        df = {"obsidian": 1, "python": 80}
        total_docs = 100
        vec = vault_index._compute_tfidf_vector(tokens, df, total_docs, top_k=2)
        assert vec["obsidian"] > vec["python"]

    def test_empty_tokens_returns_empty_dict(self):
        assert vault_index._compute_tfidf_vector([], {}, 10) == {}

    def test_missing_df_treats_term_as_brand_new(self):
        """A term absent from term_df should score as if df=0 (max IDF)."""
        tokens = ["mystery"]
        vec = vault_index._compute_tfidf_vector(tokens, {}, total_docs=100, top_k=5)
        assert "mystery" in vec
        assert vec["mystery"] > 0

    def test_single_term_single_doc_corpus(self):
        """Smoothing must keep IDF strictly positive even when df = total_docs."""
        tokens = ["alpha"]
        vec = vault_index._compute_tfidf_vector(
            tokens, {"alpha": 1}, total_docs=1, top_k=5,
        )
        assert vec["alpha"] > 0


class TestCosineSimilarity:
    def test_identical_vectors_score_one(self):
        v = {"a": 2.0, "b": 1.0}
        assert vault_index._cosine_similarity(v, dict(v)) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self):
        v1 = {"a": 1.0, "b": 1.0}
        v2 = {"c": 1.0, "d": 1.0}
        assert vault_index._cosine_similarity(v1, v2) == 0.0

    def test_partial_overlap(self):
        v1 = {"a": 1.0, "b": 1.0}
        v2 = {"a": 1.0, "c": 1.0}
        assert vault_index._cosine_similarity(v1, v2) == pytest.approx(0.5)

    def test_empty_vector_returns_zero(self):
        assert vault_index._cosine_similarity({}, {"a": 1.0}) == 0.0
        assert vault_index._cosine_similarity({"a": 1.0}, {}) == 0.0
        assert vault_index._cosine_similarity({}, {}) == 0.0

    def test_order_independent(self):
        v1 = {"a": 3.0, "b": 4.0}
        v2 = {"b": 4.0, "a": 3.0}
        assert vault_index._cosine_similarity(v1, v2) == pytest.approx(1.0)
