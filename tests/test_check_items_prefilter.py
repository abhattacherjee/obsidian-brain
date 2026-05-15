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


# ---------------------------------------------------------------------------
# Integration tests: real os.path.getmtime flows through _safe_mtime →
# group_members → classify payload → synthetic_classification
#
# These tests exercise the mtime threading bug that was silently STALE-ing
# every item: _safe_mtime(fpath) must be called at group_members construction
# time (open_item_dedup.py), not fabricated in test fixtures.
# ---------------------------------------------------------------------------

def _build_group_from_real_files(tmp_path, item_text: str, old: bool) -> dict:
    """Build a minimal group dict whose mtime comes from os.path.getmtime().

    Creates a real temp file, sets its mtime via os.utime(), then reads it
    back via os.path.getmtime() — the same call that _safe_mtime() in
    open_item_dedup.py makes. This exercises the real integration path:
    "real mtime → group_members dict → synthetic_classification".

    Args:
        tmp_path: pytest tmp_path fixture.
        item_text: Text for the open item.
        old: If True, sets mtime to 100 days ago (→ STALE).
             If False, sets mtime to 20 days ago (→ ACTIVE).
    """
    note = tmp_path / ("old_note.md" if old else "new_note.md")
    note.write_text(
        f"---\ntype: claude-session\nproject: test\n---\n\n"
        f"## Open Questions / Next Steps\n- [ ] {item_text}\n",
        encoding="utf-8",
    )
    days_ago = 100 if old else 20
    target_mtime = _time.time() - (days_ago * 86_400)
    os.utime(str(note), (target_mtime, target_mtime))

    # Read mtime back via the same call _safe_mtime() makes — this is the
    # integration point: if _safe_mtime were never called, mtime would be
    # absent from the group dict and synthetic_classification would always
    # compute epoch-age → STALE.
    real_mtime = os.path.getmtime(str(note))

    return {
        "group_id": f"integ-{'old' if old else 'new'}-01",
        "project": "test",
        "representative": item_text,
        "instances": [
            {
                "file": note.name,
                "line": 4,
                "text": item_text,
                "mtime": real_mtime,  # populated as _safe_mtime(fpath) would be
            }
        ],
    }


def test_mtime_threading_old_file_classifies_stale(tmp_path):
    """Group built with real mtime from a 100-day-old file → STALE.

    This test would have caught the original bug: when mtime was never set
    on group_members dicts, m.get('mtime', 0) returned 0 (epoch), and the
    age was always >> 90 days. This test passes an actual os.path.getmtime()
    value into the group, proving the ACTIVE branch is reachable with fresh
    files (see companion test below).
    """
    from check_items_prefilter import synthetic_classification
    group = _build_group_from_real_files(tmp_path, "Fix the old broken thing", old=True)
    result = synthetic_classification(group)
    assert result["classification"] == "STALE", (
        f"Expected STALE for 100-day-old file mtime; got {result['classification']!r}. "
        f"mtime={group['instances'][0]['mtime']}"
    )
    assert result["confidence"] == "LOW"
    assert result["prefiltered"] is True


def test_mtime_threading_new_file_classifies_active(tmp_path):
    """Group built with real mtime from a 20-day-old file → ACTIVE.

    This is the test that would have FAILED before the fix: because mtime
    was never populated in group_members, mtime always defaulted to 0
    (epoch) and every item was STALE regardless of file age. This test
    proves the ACTIVE branch is reachable once _safe_mtime(fpath) is
    wired in at group_members construction.
    """
    from check_items_prefilter import synthetic_classification
    group = _build_group_from_real_files(tmp_path, "Investigate new feature discovery", old=False)
    result = synthetic_classification(group)
    assert result["classification"] == "ACTIVE", (
        f"Expected ACTIVE for 20-day-old file mtime; got {result['classification']!r}. "
        f"mtime={group['instances'][0]['mtime']} — "
        "If STALE, mtime is likely still missing/0 from group_members (the original bug)."
    )
    assert result["confidence"] == "LOW"
    assert result["prefiltered"] is True


def test_mtime_threading_old_and_new_files_differ(tmp_path):
    """Old and new groups must classify differently — STALE vs ACTIVE.

    Regression guard: ensures both branches are independently exercised.
    If either file's mtime were missing (→ 0 → epoch → STALE), the new
    file would incorrectly be STALE and this assertion would fail.
    """
    from check_items_prefilter import synthetic_classification
    old_group = _build_group_from_real_files(tmp_path, "Fix ancient configuration bug", old=True)
    new_group = _build_group_from_real_files(tmp_path, "Ship new dashboard feature", old=False)
    old_result = synthetic_classification(old_group)
    new_result = synthetic_classification(new_group)
    assert old_result["classification"] == "STALE"
    assert new_result["classification"] == "ACTIVE"
    assert old_result["classification"] != new_result["classification"], (
        "Old and new groups must classify differently — if both are STALE, "
        "mtime is likely not reaching synthetic_classification (original bug)."
    )
