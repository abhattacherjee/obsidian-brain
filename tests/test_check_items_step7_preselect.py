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

#299 added a second, earlier line of defence for that same production item:
'Validate ...' is a pending-intent cue, so the heuristic no longer even calls
it DONE. Both layers are asserted below — the guard on the production text,
and the assign_tier cap on a heuristic DONE that survives the guard.
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

# A completion CLAIM the #299 guard must leave alone, so the assign_tier cap
# below is still exercised on a real heuristic DONE rather than vacuously
# passing because nothing reaches DONE any more.
_UNGOVERNED_DONE_TEXT = "Fix bug #87 — done; merged in v2.5.0 release."


def _step7_mark(item: dict) -> str:
    """Reproduce SKILL.md Step 7's exact preselect predicate."""
    return "[x]" if item["classification"] == "DONE" and item["tier"] == "HIGH" else "[ ]"


def test_production_validation_item_no_longer_reaches_done():
    """#299: the #297 production item is an unperformed validation task.
    'Validate' is a pending-intent cue governing the v3.4.0/'release'
    co-occurrence, so the heuristic must not call it DONE at all — and the
    rejection reason must survive on the record."""
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
    assert record["classification"] == "ACTIVE"
    assert record["classifier_source"] == "heuristic"
    assert "DONE rejected" in (record["evidence_citation"] or "")
    assert "Validate" in record["evidence_citation"]

    record["tier"] = oid.assign_tier(
        record.get("evidence_citation"),
        record.get("canonical_text"),
        record.get("classification"),
        record.get("classifier_source"),
    )
    assert _step7_mark(record) == "[ ]"


def test_heuristic_done_is_never_preselected():
    """A heuristic DONE that the #299 guard leaves standing still must not
    preselect: its citation is built from a token lifted out of the item text,
    so assign_tier's literal-ref rule would otherwise hand it HIGH."""
    merged_groups = [
        {
            "group_id": "g1",
            "project": "obsidian-brain",
            "representative": _UNGOVERNED_DONE_TEXT,
            "members": [{"file": "a.md", "line": 1,
                         "text": _UNGOVERNED_DONE_TEXT}],
        },
    ]

    out = oid.classify_groups_heuristic(merged_groups, {})
    assert len(out) == 1
    record = out[0]
    assert record["classification"] == "DONE"
    assert record["classifier_source"] == "heuristic"
    assert record["confidence"] == "MED"
    # The citation really does carry a literal ref that appears in the item
    # text — without the #297 cap this record would tier HIGH.
    assert "#87" in record["evidence_citation"]
    assert "#87" in record["canonical_text"]

    # Step 7's exact call shape.
    record["tier"] = oid.assign_tier(
        record.get("evidence_citation"),
        record.get("canonical_text"),
        record.get("classification"),
        record.get("classifier_source"),
    )

    assert record["tier"] == "MED"
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
