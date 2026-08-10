"""Regression test for issue #297 Task 6: SKILL.md Step 7's preselect logic.

SKILL.md Step 7 is prose (a bash/python heredoc), not importable code, so it
cannot be exercised directly. This file pins the decision logic the heredoc
implements — assign_tier(citation, canonical_text, classification,
classifier_source) followed by the preselect predicate
`classification == "DONE" and tier == "HIGH"` — against the actual
open_item_dedup functions, so a future edit to Step 7 (or to assign_tier's
allowlist) that regresses the fail-closed behaviour is caught here even
though the prose itself is untestable.

Production instance (obsidian-brain #297): the item below received a
heuristic citation that reached tier HIGH under the pre-fix denylist and was
preselected [x] despite the validation never having been performed. Task 4
closed that with a classifier_source allowlist; this test is the guard that
Step 7's wiring (the 4th positional arg to assign_tier) does not regress it.
"""

from __future__ import annotations

import open_item_dedup as oid

# Exact production item text — do not paraphrase (memory
# feedback_adversarial_fixture_with_heuristic: production-failure case, not
# sanitized vocab).
_PRODUCTION_TEXT = (
    "Validate that 9 stranded board items (v3.4.0) and 1 current item "
    "(v3.4.1) are correctly categorized by release tag post-promotion."
)


def _step7_mark(item: dict) -> str:
    """Reproduce SKILL.md Step 7's exact preselect predicate."""
    return "[x]" if item["classification"] == "DONE" and item["tier"] == "HIGH" else "[ ]"


def test_heuristic_done_is_never_preselected():
    merged_groups = [
        {
            "group_id": "g1",
            "project": "obsidian-brain",
            "representative": _PRODUCTION_TEXT,
            "members": [{"file": "a.md", "line": 1, "text": _PRODUCTION_TEXT}],
        },
    ]

    out = oid.classify_groups_heuristic(merged_groups, {})
    assert len(out) == 1
    record = out[0]
    assert record["classification"] == "DONE"
    assert record["classifier_source"] == "heuristic"
    assert record["confidence"] == "MED"

    # Step 7's exact call shape.
    record["tier"] = oid.assign_tier(
        record.get("evidence_citation"),
        record.get("canonical_text"),
        record.get("classification"),
        record.get("classifier_source"),
    )

    assert _step7_mark(record) == "[ ]"


def test_agent_done_with_literal_ref_is_preselected():
    """Regression guard for the fail-closed change: without this test, a
    wiring mistake that caps everything at MED (e.g. dropping the 4th
    assign_tier argument, or misspelling classifier_source) would silently
    make the skill useless — nothing would ever preselect — and no test
    would notice."""
    citation = "PR #297 merged"
    text = "Fix classifier degradation handling (PR #297 merged)"
    record = {
        "classification": "DONE",
        "canonical_text": text,
        "evidence_citation": citation,
        "classifier_source": "agent",
    }

    record["tier"] = oid.assign_tier(
        record.get("evidence_citation"),
        record.get("canonical_text"),
        record.get("classification"),
        record.get("classifier_source"),
    )

    assert record["tier"] == "HIGH"
    assert _step7_mark(record) == "[x]"


def test_legacy_cache_entry_without_provenance_is_not_preselected():
    """#297 legacy-cache gap: a group cached before #297 existed has no
    classifier_source at all, and some of those entries are demonstrably
    heuristic-derived (production: citation 'heuristic: token 'v0.4.0' near
    completion phrase 'release'' on an item whose text contains 'v0.4.0').
    SKILL.md Step 6 must stamp such a replay 'cache-legacy', not 'cache' —
    otherwise it launders an untrusted verdict into the high-trust set and
    the item gets auto-checked off unverified, exactly the #297 failure
    mode, just sourced from the pre-existing cache instead of a live
    degraded run."""
    citation = "heuristic: token 'v0.4.0' near completion phrase 'release'"
    text = "Bump project to v0.4.0 and release."
    record = {
        "classification": "DONE",
        "canonical_text": text,
        "evidence_citation": citation,
        "classifier_source": "cache-legacy",
    }

    record["tier"] = oid.assign_tier(
        record.get("evidence_citation"),
        record.get("canonical_text"),
        record.get("classification"),
        record.get("classifier_source"),
    )

    assert _step7_mark(record) == "[ ]"

    # Contrast: the same record, but with classifier_source="cache" (i.e.
    # the cached entry DID record trusted provenance) DOES preselect. This
    # proves the outcome hinges on the provenance value, not on the citation
    # shape or item text — without it, the assertion above could pass for
    # unrelated reasons (e.g. assign_tier always returning MED for this
    # citation shape regardless of source).
    trusted = dict(record, classifier_source="cache")
    trusted["tier"] = oid.assign_tier(
        trusted.get("evidence_citation"),
        trusted.get("canonical_text"),
        trusted.get("classification"),
        trusted.get("classifier_source"),
    )
    assert _step7_mark(trusted) == "[x]"


def test_cache_sourced_done_is_preselected():
    """Step 6 stamps replayed cache hits classifier_source='cache' — a
    high-trust source per _HIGH_TRUST_SOURCES, so a cached DONE verdict with
    a literal-ref citation must still preselect."""
    citation = "PR #297 merged"
    text = "Fix classifier degradation handling (PR #297 merged)"
    record = {
        "classification": "DONE",
        "canonical_text": text,
        "evidence_citation": citation,
        "classifier_source": "cache",
    }

    record["tier"] = oid.assign_tier(
        record.get("evidence_citation"),
        record.get("canonical_text"),
        record.get("classification"),
        record.get("classifier_source"),
    )

    assert record["tier"] == "HIGH"
    assert _step7_mark(record) == "[x]"
