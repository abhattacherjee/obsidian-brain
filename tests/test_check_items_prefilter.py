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


# ---------------------------------------------------------------------------
# Tests for has_classifiable_evidence
# ---------------------------------------------------------------------------

def _make_group(canonical_text: str, mtime: float = 0.0) -> dict:
    """Minimal group dict for has_classifiable_evidence tests."""
    return {
        "group_id": "test-01",
        "project": "obsidian-brain",
        "representative": canonical_text,
        "instances": [{"file": "note.md", "line": 1, "text": canonical_text, "mtime": mtime}],
    }


def _make_evidence(**kwargs) -> dict:
    """Build an evidence dict with all keys; missing keys default to empty string."""
    base = {
        "commits_text": "",
        "merged_prs_text": "",
        "closed_issues_text": "",
        "releases_text": "",
        "changelog_excerpt": "",
        "fts_mentions_text": "",
    }
    base.update(kwargs)
    return base


def test_has_evidence_ref_match_wins_immediately():
    """A group with '#42' in canonical_text returns True regardless of evidence bundle."""
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("Fix issue #42 once shipped")
    evidence = _make_evidence()  # empty evidence
    assert has_classifiable_evidence(group, evidence) is True


def test_has_evidence_commit_sha_wins_immediately():
    """A group with a 7-char hex token triggers the ref pattern."""
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("Patch landed in commit 1b1557b")
    evidence = _make_evidence()
    assert has_classifiable_evidence(group, evidence) is True


def test_has_evidence_token_overlap_with_commits():
    """Token overlap with commits_text returns True."""
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("Fix session_log race condition")
    evidence = _make_evidence(commits_text="fix session_log race condition in hook abc1234")
    assert has_classifiable_evidence(group, evidence) is True


def test_has_evidence_no_overlap_returns_false():
    """No ref and no token overlap returns False."""
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("Investigate dispatcher discovery")
    evidence = _make_evidence(
        commits_text="update vault index schema",
        merged_prs_text="add snapshot recall feature",
    )
    assert has_classifiable_evidence(group, evidence) is False


def test_has_evidence_stopwords_only_item():
    """All-stopword canonical_text produces zero tokens → no overlap → False."""
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("the and or for")
    evidence = _make_evidence(commits_text="the and or for is a an")
    assert has_classifiable_evidence(group, evidence) is False


def test_has_evidence_empty_evidence_dict():
    """Entirely missing evidence keys (empty dict) → no overlap → False (no ref)."""
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("Investigate dispatcher discovery")
    assert has_classifiable_evidence(group, {}) is False
