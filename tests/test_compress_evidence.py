"""Tests for summarize_match_evidence() and topic_snippet() in compress_guard.py.

Covers:
- summarize_match_evidence: rank passthrough, runner_up_rank logic, band thresholds,
  shared_terms ordering and capping, empty/missing vector handling.
- topic_snippet: frontmatter stripping, first-body-paragraph extraction, heading/blank
  skipping, whitespace collapsing, truncation with ellipsis, edge cases.
"""

from compress_guard import summarize_match_evidence, topic_snippet


# ---------------------------------------------------------------------------
# summarize_match_evidence tests
# ---------------------------------------------------------------------------

class TestSummarizeMatchEvidenceRank:
    def test_rank_passthrough(self):
        results = [{"rank": -18.5, "tfidf_vector": None}]
        ev = summarize_match_evidence(results)
        assert ev["rank"] == -18.5

    def test_runner_up_rank_none_for_single_element(self):
        results = [{"rank": -20.0, "tfidf_vector": None}]
        ev = summarize_match_evidence(results)
        assert ev["runner_up_rank"] is None

    def test_runner_up_rank_from_second_element(self):
        results = [{"rank": -20.0, "tfidf_vector": None}, {"rank": -14.5}]
        ev = summarize_match_evidence(results)
        assert ev["runner_up_rank"] == -14.5

    def test_runner_up_rank_uses_index_1_only(self):
        """Even if there are 3+ results, runner_up_rank is always results[1]."""
        results = [
            {"rank": -20.0, "tfidf_vector": None},
            {"rank": -14.5},
            {"rank": -9.0},
        ]
        ev = summarize_match_evidence(results)
        assert ev["runner_up_rank"] == -14.5


class TestSummarizeMatchEvidenceBand:
    """Band thresholds: >=25→very strong, >=15→strong, >=8→moderate, else→borderline."""

    def test_band_borderline(self):
        # |rank| = 7.9 < 8 → borderline
        results = [{"rank": -7.9, "tfidf_vector": None}]
        ev = summarize_match_evidence(results)
        assert ev["rank_note"] == "borderline"

    def test_band_moderate_exact_boundary(self):
        # |rank| = 8.0 exactly → moderate (inclusive lower edge)
        results = [{"rank": -8.0, "tfidf_vector": None}]
        ev = summarize_match_evidence(results)
        assert ev["rank_note"] == "moderate"

    def test_band_moderate_interior(self):
        # |rank| = 12.0, 8 <= 12 < 15 → moderate
        results = [{"rank": -12.0, "tfidf_vector": None}]
        ev = summarize_match_evidence(results)
        assert ev["rank_note"] == "moderate"

    def test_band_strong_exact_boundary(self):
        # |rank| = 15.0 exactly → strong (inclusive lower edge)
        results = [{"rank": -15.0, "tfidf_vector": None}]
        ev = summarize_match_evidence(results)
        assert ev["rank_note"] == "strong"

    def test_band_strong_interior(self):
        # |rank| = 20.0, 15 <= 20 < 25 → strong
        results = [{"rank": -20.0, "tfidf_vector": None}]
        ev = summarize_match_evidence(results)
        assert ev["rank_note"] == "strong"

    def test_band_very_strong_exact_boundary(self):
        # |rank| = 25.0 exactly → very strong (inclusive lower edge)
        results = [{"rank": -25.0, "tfidf_vector": None}]
        ev = summarize_match_evidence(results)
        assert ev["rank_note"] == "very strong"

    def test_band_very_strong_high(self):
        # |rank| = 30.0 >> 25 → very strong
        results = [{"rank": -30.0, "tfidf_vector": None}]
        ev = summarize_match_evidence(results)
        assert ev["rank_note"] == "very strong"


class TestSummarizeMatchEvidenceSharedTerms:
    """shared_terms: intersection of query_vec (weight>0) and tfidf_vector."""

    def _make_result(self, tfidf_vector):
        return [{"rank": -20.0, "tfidf_vector": tfidf_vector}]

    def test_shared_terms_empty_when_query_vec_none(self):
        results = self._make_result({"topic": 3.0, "auth": 2.0})
        ev = summarize_match_evidence(results, query_vec=None)
        assert ev["shared_terms"] == []

    def test_shared_terms_empty_when_query_vec_empty_dict(self):
        results = self._make_result({"topic": 3.0})
        ev = summarize_match_evidence(results, query_vec={})
        assert ev["shared_terms"] == []

    def test_shared_terms_empty_when_tfidf_vector_none(self):
        query_vec = {"topic": 4.0, "auth": 2.0}
        results = self._make_result(None)
        ev = summarize_match_evidence(results, query_vec=query_vec)
        assert ev["shared_terms"] == []

    def test_shared_terms_empty_when_tfidf_vector_missing(self):
        """If the result dict has no tfidf_vector key at all."""
        results = [{"rank": -20.0}]
        query_vec = {"topic": 4.0, "auth": 2.0}
        ev = summarize_match_evidence(results, query_vec=query_vec)
        assert ev["shared_terms"] == []

    def test_shared_terms_empty_when_tfidf_vector_empty(self):
        query_vec = {"topic": 4.0, "auth": 2.0}
        results = self._make_result({})
        ev = summarize_match_evidence(results, query_vec=query_vec)
        assert ev["shared_terms"] == []

    def test_shared_terms_only_terms_present_in_both(self):
        """Terms in query but not note, and in note but not query, are excluded."""
        query_vec = {"topic": 4.0, "auth": 2.0, "cache": 1.5}
        note_vec = {"topic": 3.0, "cache": 5.0, "redis": 7.0}
        results = self._make_result(note_vec)
        ev = summarize_match_evidence(results, query_vec=query_vec)
        # "auth" in query but not note; "redis" in note but not query
        assert set(ev["shared_terms"]) == {"topic", "cache"}

    def test_shared_terms_sorted_by_descending_query_weight(self):
        """Terms sorted by descending query weight (not note weight)."""
        query_vec = {"topic": 4.0, "auth": 2.0, "cache": 1.5}
        note_vec = {"topic": 1.0, "auth": 9.0, "cache": 0.5}
        results = self._make_result(note_vec)
        ev = summarize_match_evidence(results, query_vec=query_vec)
        # Sorted by query weight: topic(4.0) > auth(2.0) > cache(1.5)
        assert ev["shared_terms"] == ["topic", "auth", "cache"]

    def test_shared_terms_tie_broken_alphabetically(self):
        """Terms with equal query weight are sorted alphabetically for determinism."""
        query_vec = {"bravo": 3.0, "alpha": 3.0, "charlie": 3.0}
        note_vec = {"bravo": 2.0, "alpha": 5.0, "charlie": 1.0}
        results = self._make_result(note_vec)
        ev = summarize_match_evidence(results, query_vec=query_vec)
        # All tied at 3.0, alphabetical tiebreak
        assert ev["shared_terms"] == ["alpha", "bravo", "charlie"]

    def test_shared_terms_capped_at_max_terms(self):
        """max_terms=3 caps the result even if more terms are shared."""
        query_vec = {f"term{i}": float(10 - i) for i in range(6)}  # 6 terms
        note_vec = {f"term{i}": 1.0 for i in range(6)}  # all 6 shared
        results = self._make_result(note_vec)
        ev = summarize_match_evidence(results, query_vec=query_vec, max_terms=3)
        assert len(ev["shared_terms"]) == 3
        # Should be the top 3 by query weight: term0(10.0), term1(9.0), term2(8.0)
        assert ev["shared_terms"] == ["term0", "term1", "term2"]

    def test_shared_terms_excludes_zero_weight_query_terms(self):
        """query_vec terms with weight == 0 must NOT be counted as shared."""
        query_vec = {"topic": 4.0, "zero_weight": 0.0, "auth": 2.0}
        note_vec = {"topic": 3.0, "zero_weight": 5.0, "auth": 1.0}
        results = self._make_result(note_vec)
        ev = summarize_match_evidence(results, query_vec=query_vec)
        assert "zero_weight" not in ev["shared_terms"]
        assert "topic" in ev["shared_terms"]
        assert "auth" in ev["shared_terms"]

    def test_default_max_terms_8(self):
        """Default max_terms is 8."""
        query_vec = {f"term{i}": float(20 - i) for i in range(10)}
        note_vec = {f"term{i}": 1.0 for i in range(10)}
        results = self._make_result(note_vec)
        ev = summarize_match_evidence(results, query_vec=query_vec)
        assert len(ev["shared_terms"]) == 8


class TestSummarizeMatchEvidenceDefensive:
    def test_empty_results_returns_rank_none(self):
        """Guard: empty results list returns safe dict with rank=None."""
        ev = summarize_match_evidence([])
        assert ev["rank"] is None
        assert ev["runner_up_rank"] is None
        assert ev["shared_terms"] == []
        assert ev["rank_note"] == ""


# ---------------------------------------------------------------------------
# topic_snippet tests
# ---------------------------------------------------------------------------

class TestTopicSnippet:
    def test_empty_input_returns_empty(self):
        assert topic_snippet("") == ""

    def test_no_frontmatter_returns_first_paragraph(self):
        text = "This is the first paragraph.\nIt has two lines.\n\nSecond paragraph."
        result = topic_snippet(text)
        assert result == "This is the first paragraph. It has two lines."

    def test_strips_frontmatter_and_returns_body(self):
        text = "---\ntype: claude-insight\ndate: 2026-01-01\n---\n\nBody content here."
        result = topic_snippet(text)
        assert result == "Body content here."

    def test_skips_leading_headings(self):
        text = "---\ntitle: Foo\n---\n\n# Title Heading\n\nActual body paragraph."
        result = topic_snippet(text)
        assert result == "Actual body paragraph."

    def test_skips_leading_blank_lines(self):
        text = "\n\n\nFirst substantive line.\nSecond line."
        result = topic_snippet(text)
        assert result == "First substantive line. Second line."

    def test_collapses_internal_whitespace(self):
        text = "Line one.  \n  Line two.\nLine three."
        result = topic_snippet(text)
        # Multiple spaces and leading/trailing stripped per line → joined with single space
        assert "  " not in result  # no double spaces
        assert "Line one." in result
        assert "Line two." in result

    def test_truncates_at_max_chars_with_ellipsis(self):
        text = "A" * 300
        result = topic_snippet(text, max_chars=200)
        assert len(result) == 201  # 200 chars + ellipsis char
        assert result.endswith("…")

    def test_no_truncation_when_short(self):
        text = "Short paragraph."
        result = topic_snippet(text, max_chars=200)
        assert result == "Short paragraph."
        assert not result.endswith("…")

    def test_heading_only_returns_empty(self):
        text = "# Heading One\n## Heading Two\n### Heading Three"
        result = topic_snippet(text)
        assert result == ""

    def test_frontmatter_only_returns_empty(self):
        text = "---\ntype: claude-insight\n---\n"
        result = topic_snippet(text)
        assert result == ""

    def test_stops_at_blank_line_after_content(self):
        text = "First paragraph line one.\nFirst paragraph line two.\n\nSecond paragraph."
        result = topic_snippet(text)
        # Should only include first paragraph
        assert result == "First paragraph line one. First paragraph line two."
        assert "Second paragraph" not in result

    def test_stops_at_heading_after_content(self):
        text = "First line of content.\n# Heading After Content\nMore content."
        result = topic_snippet(text)
        assert result == "First line of content."
        assert "Heading After Content" not in result

    def test_frontmatter_with_headings_then_body(self):
        text = (
            "---\n"
            "type: claude-insight\n"
            "date: 2026-01-01\n"
            "---\n"
            "\n"
            "# Note Title\n"
            "\n"
            "The real body starts here.\n"
            "Second line of body.\n"
        )
        result = topic_snippet(text)
        assert result == "The real body starts here. Second line of body."

    def test_text_without_frontmatter_dash_line(self):
        """Text that doesn't start with --- should be treated as no frontmatter."""
        text = "Just a plain body with no frontmatter.\nSecond line."
        result = topic_snippet(text)
        assert result == "Just a plain body with no frontmatter. Second line."

    def test_max_chars_parameter_respected(self):
        text = "Hello world this is a test."
        result = topic_snippet(text, max_chars=10)
        assert len(result) == 11  # 10 + ellipsis
        assert result.endswith("…")
        assert result.startswith("Hello worl")

    def test_whitespace_only_lines_skipped(self):
        """Lines that are only whitespace are treated as blank."""
        text = "   \n   \nActual content here."
        result = topic_snippet(text)
        assert result == "Actual content here."

    def test_strips_crlf_frontmatter(self):
        """CRLF-terminated frontmatter must be stripped identically to LF (Fix 3)."""
        # "---\r\nt: x\r\n---\r\n\r\nBody text here"
        text = "---\r\nt: x\r\n---\r\n\r\nBody text here"
        result = topic_snippet(text)
        assert result == "Body text here"

    def test_no_truncation_at_exact_max_chars(self):
        """A string exactly max_chars long must NOT be truncated or get an ellipsis."""
        text = "A" * 200
        result = topic_snippet(text, max_chars=200)
        assert len(result) == 200
        assert not result.endswith("…")

    def test_whitespace_collapse_exact_output(self):
        """Multiple internal spaces/newlines across body lines collapse to exact string."""
        text = "Line one.  \n  Line two."
        result = topic_snippet(text)
        assert result == "Line one. Line two."

    def test_collapses_intra_line_whitespace(self):
        assert topic_snippet("Text    with    extra     spaces") == "Text with extra spaces"


class TestSummarizeMatchEvidenceBandJustBelowUpper:
    """Band boundary: values just below the upper edge of each band stay in that band."""

    def test_band_just_below_strong_upper(self):
        # |rank| = 14.9 — just below 15; still in moderate band
        results = [{"rank": -14.9, "tfidf_vector": None}]
        ev = summarize_match_evidence(results)
        assert ev["rank_note"] == "moderate"

    def test_band_just_below_very_strong_upper(self):
        # |rank| = 24.9 — just below 25; still in strong band
        results = [{"rank": -24.9, "tfidf_vector": None}]
        ev = summarize_match_evidence(results)
        assert ev["rank_note"] == "strong"
