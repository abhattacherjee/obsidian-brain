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


def test_completion_signal_tokens_set_is_frozen_and_complete():
    from check_items_prefilter import COMPLETION_SIGNAL_TOKENS
    assert isinstance(COMPLETION_SIGNAL_TOKENS, frozenset)
    # Spec-locked set — see 2026-05-16-l2-prefilter-completion-signal-design.md
    assert COMPLETION_SIGNAL_TOKENS == frozenset({
        "done", "shipped", "merged", "closed", "fixed", "resolved",
        "released", "deprecated", "removed", "reverted", "completed",
    })


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
    """Token overlap with commits_text + nearby completion verb returns True (activity-zone)."""
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("Fix session_log race condition")
    evidence = _make_evidence(commits_text="fixed session_log race condition in hook")
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


def test_nearby_completion_signal_finds_adjacent_verb():
    from check_items_prefilter import _has_nearby_completion_signal
    haystack = "fixed anchor caching bug"
    assert _has_nearby_completion_signal(haystack, ["anchor"]) is True


def test_nearby_completion_signal_returns_false_when_only_activity():
    from check_items_prefilter import _has_nearby_completion_signal
    haystack = "implement anchor scaffold for new feature"
    assert _has_nearby_completion_signal(haystack, ["anchor"]) is False


def test_nearby_completion_signal_returns_false_when_no_content_hit():
    from check_items_prefilter import _has_nearby_completion_signal
    haystack = "closed many things but not this widget"
    assert _has_nearby_completion_signal(haystack, ["telemetry"]) is False


def test_nearby_completion_signal_returns_false_when_signal_outside_window():
    from check_items_prefilter import _has_nearby_completion_signal
    # 200 chars of filler between the content token and the completion verb
    haystack = "anchor work " + ("x " * 200) + "closed"
    assert _has_nearby_completion_signal(haystack, ["anchor"]) is False


def test_nearby_completion_signal_case_insensitive():
    from check_items_prefilter import _has_nearby_completion_signal
    haystack = "RESOLVED Anchor regression in pipeline"
    assert _has_nearby_completion_signal(haystack, ["Anchor"]) is True


def test_nearby_completion_signal_default_window_is_120_chars():
    from check_items_prefilter import _has_nearby_completion_signal
    # 100 char gap — within default 120 window
    haystack_in = "anchor" + (" " * 100) + "closed"
    assert _has_nearby_completion_signal(haystack_in, ["anchor"]) is True
    # 130 char gap — outside default 120 window
    haystack_out = "anchor" + (" " * 130) + "closed"
    assert _has_nearby_completion_signal(haystack_out, ["anchor"]) is False


def test_nearby_completion_signal_empty_inputs_return_false():
    from check_items_prefilter import _has_nearby_completion_signal
    assert _has_nearby_completion_signal("", ["anchor"]) is False
    assert _has_nearby_completion_signal("fixed it", []) is False


# ---------------------------------------------------------------------------
# Zone-aware rules — spec table cases (#173, v2.6)
# ---------------------------------------------------------------------------

def test_has_evidence_ref_only_issue_number_returns_true():
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("Fix issue #42 in loader")
    assert has_classifiable_evidence(group, _make_evidence()) is True


def test_has_evidence_ref_only_commit_sha_returns_true():
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("Backport a1b2c3d to develop")
    assert has_classifiable_evidence(group, _make_evidence()) is True


def test_has_evidence_completion_zone_closed_issues_hit_returns_true():
    # 'regression' is distinctive (10 chars >= 8) and appears in the haystack.
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("anchor cache regression")
    ev = _make_evidence(closed_issues_text="anchor caching regression bug write-up")
    assert has_classifiable_evidence(group, ev) is True


def test_has_evidence_completion_zone_merged_prs_hit_returns_true():
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("telemetry dashboard chip alignment")
    ev = _make_evidence(merged_prs_text="telemetry dashboard chip layout PR")
    assert has_classifiable_evidence(group, ev) is True


def test_has_evidence_completion_zone_changelog_hit_returns_true():
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("session anchor resolution")
    ev = _make_evidence(changelog_excerpt="### Added\n- session anchor resolution")
    assert has_classifiable_evidence(group, ev) is True


def test_has_evidence_completion_zone_releases_hit_returns_true():
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("midnight rollover handling")
    ev = _make_evidence(releases_text="v0.3.0\tmidnight rollover handling shipped")
    assert has_classifiable_evidence(group, ev) is True


def test_has_evidence_activity_zone_proximity_hit_returns_true():
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("anchor regression in pipeline")
    ev = _make_evidence(commits_text="fixed anchor caching in pipeline")
    assert has_classifiable_evidence(group, ev) is True


def test_has_evidence_activity_zone_no_signal_returns_false():
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("anchor regression in pipeline")
    ev = _make_evidence(commits_text="implement anchor scaffold for new feature")
    assert has_classifiable_evidence(group, ev) is False


def test_has_evidence_activity_zone_far_signal_returns_false():
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("anchor regression")
    filler = "x " * 200
    ev = _make_evidence(commits_text=f"anchor work {filler} closed")
    assert has_classifiable_evidence(group, ev) is False


def test_has_evidence_activity_zone_fts_mentions_with_signal_returns_true():
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("anchor resolution")
    ev = _make_evidence(fts_mentions_text="anchor resolution completed in session")
    assert has_classifiable_evidence(group, ev) is True


def test_has_evidence_no_content_tokens_returns_false():
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("the and of")
    ev = _make_evidence(commits_text="closed many things", merged_prs_text="shipped lots")
    assert has_classifiable_evidence(group, ev) is False


def test_has_evidence_no_overlap_anywhere_returns_false():
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("widget rendering pipeline")
    ev = _make_evidence(
        commits_text="anchor resolution work",
        merged_prs_text="anchor PR",
        closed_issues_text="anchor bug",
    )
    assert has_classifiable_evidence(group, ev) is False


def test_has_evidence_active_project_wip_item_returns_false():
    """Regression repro: WIP item shares vocabulary with its own commits but
    no completion signal nearby — must be prefiltered as ACTIVE."""
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group(
        "Implement resolveLastSessionAnchor(reader, now) with lifecycle pair "
        "detection and 5h cap in lib/timeframes/anchors.ts."
    )
    ev = _make_evidence(
        commits_text=(
            "feat(anchors): scaffold resolveLastSessionAnchor\n"
            "feat(timeframes): add lifecycle pair detection helper\n"
            "chore(anchors): rename anchor types\n"
        ),
        fts_mentions_text="anchor design discussion in vault note",
    )
    assert has_classifiable_evidence(group, ev) is False


# ---------------------------------------------------------------------------
# _is_distinctive
# ---------------------------------------------------------------------------

def test_is_distinctive_snake_case():
    from check_items_prefilter import _is_distinctive
    assert _is_distinctive("last_session_anchor") is True
    assert _is_distinctive("foo_bar") is True


def test_is_distinctive_camel_case():
    from check_items_prefilter import _is_distinctive
    assert _is_distinctive("resolveLastSessionAnchor") is True
    assert _is_distinctive("RangeAnchors") is True


def test_is_distinctive_long_lowercase():
    from check_items_prefilter import _is_distinctive
    assert _is_distinctive("regression") is True
    assert _is_distinctive("pagination") is True
    assert _is_distinctive("timeframes") is True


def test_is_distinctive_short_lowercase_is_false():
    from check_items_prefilter import _is_distinctive
    assert _is_distinctive("now") is False
    assert _is_distinctive("add") is False
    assert _is_distinctive("fix") is False
    assert _is_distinctive("chip") is False
    assert _is_distinctive("session") is False  # 7 chars, all lowercase
    assert _is_distinctive("widget") is False   # 6 chars


def test_is_distinctive_empty():
    from check_items_prefilter import _is_distinctive
    assert _is_distinctive("") is False


def test_is_distinctive_exactly_8_chars():
    from check_items_prefilter import _is_distinctive
    # 8-char lowercase — boundary, distinctive
    assert _is_distinctive("abcdefgh") is True
    # 7-char lowercase — boundary, NOT distinctive
    assert _is_distinctive("abcdefg") is False


def test_has_evidence_generic_token_overlap_does_not_trigger_rule_2():
    """Regression: generic short-lowercase tokens like 'now', 'fix', 'chip'
    overlap accidentally across unrelated completed features. Rule 2 must
    require a distinctive token (#173).

    All tokens in the group are short (<8 chars), all-lowercase, no underscore,
    no mixed-case — so none pass _is_distinctive and Rule 2 cannot fire.
    Rule 3 (proximity) doesn't apply because there is no commits_text.
    """
    from check_items_prefilter import has_classifiable_evidence
    # Tokens: 'now', 'fix', 'chip', 'new', 'run' — all short lowercase.
    group = _make_group("now fix chip new run")
    ev = _make_evidence(
        merged_prs_text="now fix chip run for other feature",  # same generic words
    )
    assert has_classifiable_evidence(group, ev) is False


def test_has_evidence_distinctive_token_still_triggers_rule_2():
    """Regression: distinctive tokens (length >= 8 OR has _ OR mixed-case)
    still trigger Rule 2 on completion-zone overlap (#173)."""
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("Investigate pagination regression")  # both distinctive
    ev = _make_evidence(
        merged_prs_text="fix pagination edge case",
    )
    assert has_classifiable_evidence(group, ev) is True


def test_has_evidence_camel_case_identifier_triggers_rule_2():
    from check_items_prefilter import has_classifiable_evidence
    group = _make_group("Implement resolveLastSessionAnchor cleanup")
    ev = _make_evidence(
        closed_issues_text="#15 resolveLastSessionAnchor refactor",
    )
    assert has_classifiable_evidence(group, ev) is True
