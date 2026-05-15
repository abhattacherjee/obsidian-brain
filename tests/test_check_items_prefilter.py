"""Tests for check_items_prefilter.py — L2 evidence-presence pre-filter module."""
import os
import sys

HOOKS_DIR = os.path.join(os.path.dirname(__file__), "..", "hooks")
if HOOKS_DIR not in sys.path:
    sys.path.insert(0, HOOKS_DIR)


def test_content_tokens_drops_stopwords():
    from check_items_prefilter import _content_tokens
    out = _content_tokens("the fix is in the loader")
    assert out == ["fix", "loader"]


def test_content_tokens_drops_short_tokens():
    from check_items_prefilter import _content_tokens
    out = _content_tokens("Fix a 1 ab abc abcd")
    assert "Fix" in out
    assert "abc" in out
    assert "abcd" in out
    assert "1" not in out
    assert "ab" not in out
    assert "a" not in out


def test_ref_pattern_matches_issue_number():
    from check_items_prefilter import REF_PATTERN
    assert REF_PATTERN.search("Close #42 once shipped")
    assert REF_PATTERN.search("commit 1b1557b lands the fix")
    assert not REF_PATTERN.search("no refs here")
