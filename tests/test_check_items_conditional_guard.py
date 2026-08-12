"""Corpus tests for the #299 conditional / forward-looking guard on
classify_groups_heuristic's DONE verdict.

Pre-#299 the heuristic assigned DONE whenever a distinctive token (#N, sha,
vX.Y.Z) and a completion phrase (done|merged|shipped|closed|fixed|complete|
resolved|released) landed within 120 chars of each other in one member text.
That cannot tell a claim of past completion ("Fixed in #51") from a reference
to a completion that has not happened yet ("Blocked until #64 is resolved") —
both shapes satisfy the rule. The guard rejects DONE when the co-occurrence is
governed by a pending-intent cue, or by a time/condition subordinator together
with a forward-looking verb form.

The corpus below is asserted as a whole rather than case by case: this is a
classifier, so the trade between the false DONEs removed and the true DONEs
kept is only meaningful over both directions at once (memory
feedback_dogfood_include_success_path — a fixture per category, including the
success path). The FALSE_DONE entries marked "production" are the phrasings
reported on issue #299, not synthesized vocabulary.
"""

from __future__ import annotations

import open_item_dedup as oid


# ---------------------------------------------------------------------------
# Labelled corpus: (case_id, item_text, expected_classification)
# ---------------------------------------------------------------------------

# Direction 1 — false DONEs the guard must eliminate. The first six are the
# production-observed phrasings from issue #299.
FALSE_DONE_CORPUS = [
    ("fp-issue-original",
     "Confirm whether a fresh /plugin update on adversarial-review will pull "
     "the new version once #51 is resolved"),
    ("fp-waiting-on", "Waiting on #51 to be closed before we can ship"),
    ("fp-blocked-until", "Blocked until #64 is resolved"),
    ("fp-check-back", "Check back after v1.2.0 is released"),
    ("fp-if-fixed", "If #99 is fixed, revisit the cache design"),
    ("fp-track-whether", "Track whether abc1234 gets merged upstream"),
    # Near-boundary: pending-intent cues reject regardless of the phrase's
    # tense, because they describe the item's own outstanding action.
    ("bd-confirm-was-merged", "Confirm #51 was merged"),
    ("bd-pending", "Pending #64 release"),
    ("bd-depends-on", "Depends on #77 being closed"),
    ("bd-awaiting", "Awaiting the v2.0.0 release"),
    ("bd-verify", "Verify v1.2.0 is released to the marketplace"),
    # The #297 production item: an unperformed validation task.
    ("bd-297-production",
     "Validate that 9 stranded board items (v3.4.0) and 1 current item "
     "(v3.4.1) are correctly categorized by release tag post-promotion."),
    # Live vault items (replayed from ~/.claude/obsidian-brain/check-items-*/
    # merged.json) that the pre-#299 heuristic called DONE. These are what set
    # the tier-1 cue scope: 'Monitor' governs across the ';' in the first two.
    ("live-monitor-crossclause",
     "Monitor when PR #228 merges to main; auto-promote #227 on next release"),
    ("live-monitor-upstream",
     "Monitor github.com/abhattacherjee/claude-code-skills#3 for upstream "
     "durable fix to worktree hook cwd detection; skill becomes obsolete "
     "once merged."),
    ("live-check-epic",
     "Check epic #66 checklist for sub-phases shipped vs outstanding"),
    ("live-confirm-plugin-update",
     "Confirm whether a fresh `/plugin update` on adversarial-review will "
     "pull the new version once #51 is resolved (it should not currently, "
     "given identical version string)."),
]

# Direction 2 — genuine completion claims the guard must NOT touch. A guard
# that only ever says ACTIVE is a silent failure of its own; these are what
# make the over-correcting mutation fail.
TRUE_DONE_CORPUS = [
    ("tp-fixed-in", "Fixed in #51"),
    ("tp-merged-shipped", "Merged #64 and shipped v1.2.0"),
    ("tp-sha-closed", "abc1234 closed this out"),
    # Near-boundary: a cue AFTER the co-occurrence does not govern it.
    ("bd-cue-after-1", "Fixed #51 after the merge conflict resolution"),
    ("bd-cue-after-2", "Fixed in #51 - confirm with the team"),
    # Near-boundary: a cue in a PRIOR sentence does not govern it.
    ("bd-cue-prior-sentence", "Waiting on CI. Fixed in #51."),
    # Near-boundary: bare past tense under a time subordinator is a claim.
    # This is why the guard's tense test excludes was/were/has been.
    ("bd-after-past", "After the outage we finally fixed #51"),
    ("bd-was-fixed", "After review #51 was fixed"),
    ("bd-has-been", "Once merged, it has been released as v1.2.0"),
    # Near-boundary: a copula with no governing cue is still a claim.
    ("bd-is-merged-no-cue", "The fix is merged in #51"),
    ("bd-bare-is-resolved", "#51 is resolved"),
    # Known limit, asserted so it is visible rather than assumed: forward
    # modality with no cue in front of it is not rejected.
    ("bd-will-be-no-cue", "#51 will be closed by Friday"),
    # One governed pair plus one genuine claim in the same text: the guard
    # scans every (token, phrase) pair, so the claim still wins.
    ("bd-mixed", "Blocked until #64 is resolved. Fixed #12."),
    # Pre-#299 fixtures from test_check_items_smart.py that must not move.
    ("reg-smart-g3", "Fix #99 merged - this work is done; release shipped."),
    ("reg-smart-fallback", "Fix bug #87 — done; merged in v2.5.0 release."),
]

# Direction 3 — never had a qualifying co-occurrence at all; unchanged by the
# guard and must carry no citation.
NO_COOCCURRENCE_CORPUS = [
    ("reg-no-phrase", "Track issue #87 work (no completion words)"),
    ("reg-no-token", "this is done now finally"),
]


def _classify(corpus):
    groups = [
        {"group_id": case_id, "project": "p", "representative": text,
         "members": [{"file": "a.md", "line": 1, "text": text}]}
        for case_id, text in corpus
    ]
    return {r["group_id"]: r for r in oid.classify_groups_heuristic(groups, {})}


def test_false_done_corpus_is_rejected():
    """Every governed co-occurrence must fail to reach DONE."""
    by_id = _classify(FALSE_DONE_CORPUS)
    wrong = {cid: by_id[cid]["classification"]
             for cid, _ in FALSE_DONE_CORPUS
             if by_id[cid]["classification"] != "ACTIVE"}
    assert wrong == {}, f"guard failed to reject: {wrong}"


def test_true_done_corpus_survives():
    """Every genuine completion claim must still reach DONE. This is the
    assertion an over-corrected guard (reject everything) fails."""
    by_id = _classify(TRUE_DONE_CORPUS)
    wrong = {cid: by_id[cid]["classification"]
             for cid, _ in TRUE_DONE_CORPUS
             if by_id[cid]["classification"] != "DONE"}
    assert wrong == {}, f"guard over-rejected: {wrong}"


def test_no_cooccurrence_corpus_unchanged():
    by_id = _classify(NO_COOCCURRENCE_CORPUS)
    for cid, _ in NO_COOCCURRENCE_CORPUS:
        assert by_id[cid]["classification"] == "ACTIVE"
        # No pair was ever a DONE candidate, so there is nothing to explain.
        assert by_id[cid]["evidence_citation"] is None


def test_confusion_matrix_over_full_corpus():
    """Whole-corpus counts, so a change that fixes one direction by breaking
    the other cannot pass by adding a single new case."""
    by_id = _classify(FALSE_DONE_CORPUS + TRUE_DONE_CORPUS
                      + NO_COOCCURRENCE_CORPUS)
    false_done_eliminated = sum(
        1 for cid, _ in FALSE_DONE_CORPUS
        if by_id[cid]["classification"] == "ACTIVE")
    true_done_preserved = sum(
        1 for cid, _ in TRUE_DONE_CORPUS
        if by_id[cid]["classification"] == "DONE")
    assert false_done_eliminated == len(FALSE_DONE_CORPUS) == 16
    assert true_done_preserved == len(TRUE_DONE_CORPUS) == 15


def test_rejected_done_keeps_the_reason_on_the_record():
    """A rejected DONE must not become an indistinguishable ACTIVE: the
    citation has to say which token, which phrase, and which cue killed it.
    Losing the reason is the silent-failure class this guard exists to fix."""
    by_id = _classify(FALSE_DONE_CORPUS)
    for cid, _ in FALSE_DONE_CORPUS:
        citation = by_id[cid]["evidence_citation"]
        assert citation, f"{cid} lost its rejection reason"
        assert citation.startswith("heuristic: DONE rejected")
        assert "near completion phrase" in citation


def test_rejection_is_logged_to_stderr(capsys):
    """partition_for_review scrubs evidence_citation on ACTIVE before the
    dashboard write, so the stderr line is where the reason survives the run."""
    _classify([("fp-blocked-until", "Blocked until #64 is resolved")])
    err = capsys.readouterr().err
    assert "heuristic-guard" in err
    assert "fp-blocked-until" in err
    assert "Blocked until" in err


def test_rejected_records_keep_the_297_contract():
    """#297 guarantees are unchanged by the guard: heuristic provenance on
    every record, and MED (never HIGH) on any DONE it does emit."""
    by_id = _classify(FALSE_DONE_CORPUS + TRUE_DONE_CORPUS)
    for record in by_id.values():
        assert record["classifier_source"] == "heuristic"
        assert record["confidence"] in ("MED", "LOW")
        if record["classification"] == "DONE":
            assert record["confidence"] == "MED"
        else:
            assert record["confidence"] == "LOW"


def test_guard_helpers_are_scoped_to_the_governing_side():
    """Unit-level pin on the two scoping decisions the corpus depends on:
    the cue must precede the co-occurrence, and it must be in the same
    sentence."""
    text = "Fixed in #51 - confirm with the team"
    tok = oid._DISTINCTIVE_TOKEN_RE.search(text)
    phr = oid._COMPLETION_PHRASE_RE.search(text)
    assert oid._conditional_rejection(text, tok, phr) is None

    text = "Waiting on CI. Fixed in #51."
    tok = oid._DISTINCTIVE_TOKEN_RE.search(text)
    phr = oid._COMPLETION_PHRASE_RE.search(text)
    # Boundary end is just past the ".", so the leading space stays in the
    # left-hand region — harmless, and it keeps the slice cheap.
    assert oid._sentence_start(text, tok.start()) == len("Waiting on CI.")
    assert oid._conditional_rejection(text, tok, phr) is None


def test_cue_does_not_match_inside_a_path_or_filename():
    """Live vault item: 'modify `check-pr-base.py`' must not count as the
    verification cue 'check'. The item is open for other reasons, but a guard
    that fires on a filename fragment is firing by coincidence, and the next
    filename that happens to contain 'track' or 'monitor' would silently start
    rejecting real completions."""
    text = ("Implement fix in git-flow#21: modify `check-pr-base.py` to allow "
            "`release/*` and `hotfix/*` back-merges")
    assert oid._PENDING_INTENT_CUE_RE.search(text) is None
    # ... while the same word in prose still fires.
    assert oid._PENDING_INTENT_CUE_RE.search("Check that #21 shipped") is not None


def test_clause_and_sentence_scopes_differ_per_tier():
    """Tier 1 crosses ';', tier 2 does not; neither crosses '.'."""
    text = "Monitor when PR #228 merges to main; auto-promote #227 on next release"
    assert oid._clause_start(text, text.index("#227")) == text.index(";") + 1
    assert oid._sentence_start(text, text.index("#227")) == 0


def test_sentence_split_does_not_break_version_tokens():
    """The `(?=\\s|$)` lookahead on the boundary regex is load-bearing: without
    it 'v3.4.0' would be read as three sentences and a cue before it would
    stop governing."""
    text = "Validate that v3.4.0 is released"
    assert oid._sentence_start(text, text.index("v3.4.0")) == 0
