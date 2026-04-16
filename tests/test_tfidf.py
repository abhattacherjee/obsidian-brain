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
