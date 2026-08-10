"""Tests for issue #297 Task 4: provenance stamping + heuristic tier cap.

A heuristic citation is BUILT FROM a token lifted out of the item text, so
assign_tier's "literal ref appears in both citation and text" HIGH condition
is trivially satisfied for heuristic verdicts — citation and text are not
independent evidence. HIGH + DONE is what SKILL.md Step 7 preselects for
auto-checkoff, so a heuristic verdict must never reach HIGH.

Production instance (obsidian-brain #297): the item
  "Validate that 9 stranded board items (v3.4.0) and 1 current item
  (v3.4.1) are correctly categorized by release tag post-promotion."
received citation "heuristic: token 'v3.4.0' near completion phrase
'release'" -> tier HIGH -> preselected [x], despite the validation never
having been performed.
"""

from __future__ import annotations

import json

import open_item_dedup as oid

# Exact production strings — do not paraphrase. Test 1 exists to prove this
# shape genuinely reaches HIGH before the guard is applied, so test 2's cap
# is testing something real.
_CITATION = "heuristic: token 'v3.4.0' near completion phrase 'release'"
_TEXT = (
    "Validate that 9 stranded board items (v3.4.0) and 1 current item "
    "(v3.4.1) are correctly categorized by release tag post-promotion."
)


def _fake_completed(stdout="", returncode=0):
    from unittest.mock import MagicMock
    cp = MagicMock()
    cp.stdout = stdout
    cp.returncode = returncode
    cp.stderr = ""
    return cp


# ---------------------------------------------------------------------------
# assign_tier
# ---------------------------------------------------------------------------

def test_heuristic_citation_scores_high_without_the_guard():
    """The discriminating assertion: the production citation/text shape
    reaches HIGH when no classifier_source is passed. If this ever fails,
    the cap in test_heuristic_source_capped_at_med is testing a shape that
    was never HIGH to begin with, and this task's premise must be
    re-derived."""
    assert oid.assign_tier(_CITATION, _TEXT) == "HIGH"


def test_heuristic_source_capped_at_med():
    assert oid.assign_tier(_CITATION, _TEXT, "DONE", "heuristic") == "MED"


def test_agent_source_still_reaches_high():
    """Guards against capping everything — only classifier_source=='heuristic'
    is capped."""
    assert oid.assign_tier(_CITATION, _TEXT, "DONE", "agent") == "HIGH"


def test_review_cap_still_applies():
    """Existing #264 behaviour is unchanged by the new parameter."""
    assert oid.assign_tier(_CITATION, _TEXT, "REVIEW") == "MED"


# ---------------------------------------------------------------------------
# classify_groups_heuristic stamping
# ---------------------------------------------------------------------------

def test_classify_groups_heuristic_stamps_source():
    merged_groups = [
        {
            "group_id": "g1",
            "project": "p",
            "representative": _TEXT,
            "members": [{"file": "a.md", "line": 1, "text": _TEXT}],
        },
        {
            "group_id": "g2",
            "project": "p",
            "representative": "Return to feature/x worktree",
            "members": [{"file": "a.md", "line": 2, "text": "Return to feature/x worktree"}],
        },
    ]
    out = oid.classify_groups_heuristic(merged_groups, {})
    assert len(out) == 2
    for record in out:
        assert record["classifier_source"] == "heuristic"


# ---------------------------------------------------------------------------
# classify_groups_with_agent stamping
# ---------------------------------------------------------------------------

def test_agent_results_stamped_agent_or_prefilter(monkeypatch):
    def fake_cli_run(cmd, *args, **kwargs):
        out_path = next((c for c in cmd if isinstance(c, str)
                         and c.endswith(".json") and "out" in c), None)
        if out_path:
            with open(out_path, "w") as f:
                json.dump([
                    {
                        "group_id": "g1",
                        "classification": "DONE",
                        "confidence": "HIGH",
                        "canonical_text": "ship it",
                        "evidence_citation": "PR #87 merged",
                        "action_required": None,
                    },
                    {
                        "group_id": "g2",
                        "classification": "DONE",
                        "confidence": "HIGH",
                        "canonical_text": "prefiltered item",
                        "evidence_citation": "exact match",
                        "action_required": None,
                        "prefiltered": True,
                    },
                ], f)
        return _fake_completed(returncode=0)

    monkeypatch.setattr(oid.subprocess, "run", fake_cli_run)

    merged_groups = [
        {"group_id": "g1", "project": "p", "representative": "ship",
         "members": [{"file": "a.md", "line": 1, "text": "ship"}]},
        {"group_id": "g2", "project": "p", "representative": "prefiltered",
         "members": [{"file": "a.md", "line": 2, "text": "prefiltered"}]},
    ]
    evidence = {"p": {"commits": [], "merged_prs": [], "closed_issues": []}}

    out = oid.classify_groups_with_agent(merged_groups, evidence)
    by_id = {r["group_id"]: r for r in out}
    assert by_id["g1"]["classifier_source"] == "agent"
    assert by_id["g2"]["classifier_source"] == "prefilter"
