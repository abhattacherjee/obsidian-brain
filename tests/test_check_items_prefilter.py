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


# ---------------------------------------------------------------------------
# Tests for synthetic_classification
# ---------------------------------------------------------------------------

import time as _time


def _make_group_with_mtime(canonical_text: str, mtime: float) -> dict:
    """Build a group with instances carrying a known mtime."""
    return {
        "group_id": "test-synth-01",
        "project": "obsidian-brain",
        "representative": canonical_text,
        "instances": [
            {"file": "note.md", "line": 1, "text": canonical_text, "mtime": mtime},
        ],
    }


def test_synthetic_young_item_is_active():
    """Item first seen 30 days ago (<=90d) → ACTIVE/LOW."""
    from check_items_prefilter import synthetic_classification
    mtime_30d_ago = _time.time() - (30 * 86400)
    group = _make_group_with_mtime("Investigate dispatcher discovery", mtime_30d_ago)
    result = synthetic_classification(group)
    assert result["classification"] == "ACTIVE"
    assert result["confidence"] == "LOW"
    assert result["prefiltered"] is True
    assert result["group_id"] == "test-synth-01"
    assert result["evidence_citation"] is None
    assert result["action_required"] is None


def test_synthetic_old_item_is_stale():
    """Item first seen >90 days ago → STALE/LOW."""
    from check_items_prefilter import synthetic_classification
    mtime_100d_ago = _time.time() - (100 * 86400)
    group = _make_group_with_mtime("Fix ancient configuration bug", mtime_100d_ago)
    result = synthetic_classification(group)
    assert result["classification"] == "STALE"
    assert result["confidence"] == "LOW"
    assert result["prefiltered"] is True


def test_synthetic_missing_mtime_treats_as_stale():
    """No mtime in instances → defaults to 0 → age computed as large number → STALE.

    Note: mtime=0 (epoch) means the file dates to 1970, which is >90 days ago.
    This is the safe conservative path: if we don't know the age, treat as STALE
    (older items are more likely to be stale). If the implementer finds 0→ACTIVE
    is preferable (treat unknown as young), this test must be updated to assert ACTIVE.
    The canonical decision: mtime=0 → age = now - 0 which is always > 90d → STALE.
    """
    from check_items_prefilter import synthetic_classification
    group = {
        "group_id": "test-synth-missing",
        "project": "obsidian-brain",
        "representative": "Some item text",
        "instances": [{"file": "note.md", "line": 1, "text": "Some item text"}],
        # no 'mtime' key in instances
    }
    result = synthetic_classification(group)
    # mtime defaults to 0 → epoch → age >> 90d → STALE
    assert result["classification"] == "STALE"
    assert result["confidence"] == "LOW"
    assert result["prefiltered"] is True


def test_synthetic_empty_instances_treats_as_stale():
    """Empty instances list → no mtimes → earliest_mtime = 0.0 → epoch → STALE.

    Fail-safe path for groups with no instances at all (defensive guard against
    malformed input). `min(mtimes) if mtimes else 0.0` yields 0.0, the epoch,
    so age > 90d → STALE.
    """
    from check_items_prefilter import synthetic_classification
    group = {
        "group_id": "test-synth-empty",
        "project": "obsidian-brain",
        "representative": "Some item text",
        "instances": [],
    }
    result = synthetic_classification(group)
    assert result["classification"] == "STALE"
    assert result["confidence"] == "LOW"
    assert result["prefiltered"] is True


def test_synthetic_record_has_canonical_text():
    """The synthetic record carries canonical_text matching the representative."""
    from check_items_prefilter import synthetic_classification
    mtime_recent = _time.time() - (10 * 86400)
    group = _make_group_with_mtime("Check vault integrity", mtime_recent)
    result = synthetic_classification(group)
    assert result["canonical_text"] == "Check vault integrity"


def test_synthetic_classification_now_injection_active():
    """Fixed clock — ACTIVE branch. Exercises the `now=` injection path."""
    from check_items_prefilter import synthetic_classification
    mtime = 1_000_000.0
    group = _make_group_with_mtime("Investigate dispatcher discovery", mtime)
    # 30 days after mtime → ACTIVE
    result = synthetic_classification(group, now=mtime + 30 * 86_400)
    assert result["classification"] == "ACTIVE"
    assert result["confidence"] == "LOW"
    assert result["prefiltered"] is True


def test_synthetic_classification_now_injection_stale():
    """Fixed clock — STALE branch. Exercises the `now=` injection path."""
    from check_items_prefilter import synthetic_classification
    mtime = 1_000_000.0
    group = _make_group_with_mtime("Fix ancient configuration bug", mtime)
    # 100 days after mtime → STALE
    result = synthetic_classification(group, now=mtime + 100 * 86_400)
    assert result["classification"] == "STALE"
    assert result["confidence"] == "LOW"
    assert result["prefiltered"] is True


# ---------------------------------------------------------------------------
# Tests for is_prefilter_enabled
# ---------------------------------------------------------------------------

def test_prefilter_enabled_by_default(monkeypatch):
    """CHECK_ITEMS_PREFILTER not set → prefilter is enabled."""
    import check_items_prefilter
    monkeypatch.delenv("CHECK_ITEMS_PREFILTER", raising=False)
    assert check_items_prefilter.is_prefilter_enabled() is True


def test_prefilter_disabled_by_off(monkeypatch):
    """CHECK_ITEMS_PREFILTER=off → prefilter disabled (case-insensitive)."""
    import check_items_prefilter
    for val in ("off", "OFF", "Off"):
        monkeypatch.setenv("CHECK_ITEMS_PREFILTER", val)
        assert check_items_prefilter.is_prefilter_enabled() is False, f"Expected False for {val!r}"


def test_prefilter_enabled_for_non_off_values(monkeypatch):
    """Any value other than 'off' (case-insensitive) leaves prefilter enabled."""
    import check_items_prefilter
    for val in ("on", "1", "true", "yes", ""):
        monkeypatch.setenv("CHECK_ITEMS_PREFILTER", val)
        assert check_items_prefilter.is_prefilter_enabled() is True, f"Expected True for {val!r}"
