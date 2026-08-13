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
    # Hyphen-prefixed tier-1 cues. Before the `(?:re|cross|double|self)-`
    # prefix these never fired at all: the leading `(?<![\w./-])` anchor
    # rejects the preceding '-', so only the hand-spelled `double-?check`
    # worked and re-verify/re-check/cross-check/self-check were dead.
    ("bd-re-verify", "Re-verify #51 is closed"),
    ("bd-cross-check", "Cross-check that #51 merged"),
    ("bd-self-check", "Self-check whether v1.2.0 is released"),
    # Abbreviation dot. "e.g. " ends in dot-space, so it used to terminate the
    # sentence and clip the tier-1 window off 'Waiting on'.
    ("bd-abbrev-dot", "Waiting on infra, e.g. #51 to be closed"),
    # Cross-sentence pairs. The guard will not attribute governance across a
    # sentence boundary, so it declines to call these DONE even though the
    # pre-#299 code did. That decline is conservative by design; what it must
    # never be is SILENT — see
    # test_cross_sentence_pair_is_reported_not_dropped.
    ("bd-cross-sentence-see", "Fixed the flaky parser. See #51."),
    ("bd-cross-sentence-docs", "The docs are done. Track #51 next week."),
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
    # A wrapped line is ONE sentence: a bare newline must not terminate it, or
    # these lose the DONE the pre-#299 code gave them, silently.
    ("bd-wrapped-lf", "Fixed in\n#51"),
    ("bd-wrapped-crlf", "Fixed in\r\n#51"),
    # Cross-model (Gemini) case: the forward-form regex must not match 'get'
    # inside a dotted identifier and corroborate 'After' into a false ACTIVE.
    ("bd-dotted-identifier", "After `go.get.it`, #123 was closed."),
    # Pre-#299 fixtures from test_check_items_smart.py that must not move.
    ("reg-smart-g3", "Fix #99 merged - this work is done; release shipped."),
    ("reg-smart-fallback", "Fix bug #87 — done; merged in v2.5.0 release."),
]

# Direction 3 — never had a qualifying co-occurrence at all; unchanged by the
# guard and must carry no citation.
NO_COOCCURRENCE_CORPUS = [
    ("reg-no-phrase", "Track issue #87 work (no completion words)"),
    ("reg-no-token", "this is done now finally"),
    # Live vault item. 'release' here is a fragment of the hyphenated compound
    # `post-release-verification`, not a completion word, so after the trailing
    # `(?![-\w])` anchor there is no qualifying pair at all — and therefore
    # nothing to explain in a citation. Without that anchor the all-pairs sweep
    # paired it with '#101' and flipped a live ACTIVE item to DONE.
    ("live-compound-post-release",
     "When next `/release` ships Epic 22 (or queued develop work) to main, "
     "the post-release-verification DoD item will flip and Dependabot alert "
     "#101 will auto-close."),
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
    assert false_done_eliminated == len(FALSE_DONE_CORPUS) == 22
    assert true_done_preserved == len(TRUE_DONE_CORPUS) == 18


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
    dashboard write, so the stderr line is where the reason survives the run.

    Every field a human would need to act on the line is pinned, and the
    logged reason is compared against the reason on the record. A weaker
    version of this test (substring 'heuristic-guard' + the group id) passed
    against a log body that named no token, no phrase and no cue at all — it
    proved a line was printed, not that the line said anything."""
    by_id = _classify([("fp-blocked-until", "Blocked until #64 is resolved")])
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if "heuristic-guard" in ln]
    assert len(lines) == 1, err
    line = lines[0]
    assert "fp-blocked-until" in line          # which group
    assert "#64" in line                       # which token
    assert "resolved" in line                  # which completion phrase
    assert "Blocked until" in line             # which cue, verbatim
    assert "DONE rejected" in line             # what was decided
    # The log and the record must not be able to drift apart: the reason the
    # line gives has to be the reason the record carries.
    citation = by_id["fp-blocked-until"]["evidence_citation"]
    reason = citation.split(", but ", 1)[1]
    assert reason in line


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


def test_compound_word_is_not_a_completion_phrase():
    """Live vault item, the #299 defect class reintroduced by the all-pairs
    sweep: 'release' inside the hyphenated compound `post-release-verification`
    is a modifier, not a claim, and pairing it with '#101' flipped a real
    ACTIVE item to DONE on production data.

    A LEADING hyphen still reads as a claim, so the anchor is deliberately
    one-sided — 'auto-merged' and 'back-merged' must keep matching."""
    text = ("When next `/release` ships Epic 22 (or queued develop work) to "
            "main, the post-release-verification DoD item will flip and "
            "Dependabot alert #101 will auto-close.")
    # The compound occurrence is gone. The bare `/release` earlier in the line
    # is still a phrase match — and it is 125 chars from '#101', outside the
    # proximity window — so the anchor is precisely what decides this item.
    assert oid._COMPLETION_PHRASE_RE.search(
        text, text.index("post-release")) is None
    matches = [m.start() for m in oid._COMPLETION_PHRASE_RE.finditer(text)]
    assert matches == [text.index("release", text.index("`/"))]
    assert abs(text.index("#101") - matches[0]) > oid._HEURISTIC_PROXIMITY_CHARS
    by_id = _classify([("live-compound", text)])
    assert by_id["live-compound"]["classification"] == "ACTIVE"

    # Trailing hyphen blocks; leading hyphen does not.
    assert oid._COMPLETION_PHRASE_RE.search("post-release-verification") is None
    assert oid._COMPLETION_PHRASE_RE.search("auto-merged to main") is not None
    assert oid._COMPLETION_PHRASE_RE.search("back-merged into develop") is not None
    # ... and the bare word in prose is untouched.
    assert oid._COMPLETION_PHRASE_RE.search("shipped in the release.") is not None


def test_phrase_gate_suppression_is_silent_by_construction(capsys):
    """CHARACTERIZATION of a known hazard, not an endorsement of it.

    _COMPLETION_PHRASE_RE and _DISTINCTIVE_TOKEN_RE are the gates UPSTREAM of
    the pair loop, so when a narrowing there suppresses the only phrase (or the
    only token) in a text, no pair is built, the #299 reporting machinery never
    runs, and the record is an ACTIVE with evidence_citation None and no log
    line — indistinguishable from a text that never mentioned completion at
    all. That is how the trailing compound anchor swallowed 'This was merged-in
    with #51' until a cross-model review found it, with no output to notice.

    This pins the property so a future narrowing of either regex has to reckon
    with it. If someone closes the hazard, this test SHOULD go red and be
    deleted deliberately — it is not asserting that silence is desirable."""
    # A text whose only completion word is inside a compound modifier.
    text = "The post-release-verification item for #101 is still open"
    assert oid._COMPLETION_PHRASE_RE.search(text) is None
    assert oid._heuristic_member_verdict(text) == (None, None, None)
    record = _classify([("gate", text)])["gate"]
    assert record["classification"] == "ACTIVE"
    assert record["evidence_citation"] is None      # nothing to explain...
    assert capsys.readouterr().err == ""            # ...and nothing logged

    # Contrast: a rejection INSIDE the pair loop is reported through both
    # channels. The gap is the placement of the gates, not the guard's intent.
    record = _classify([("pair", "Blocked until #64 is resolved")])["pair"]
    assert record["classification"] == "ACTIVE"
    assert "DONE rejected" in record["evidence_citation"]
    assert "heuristic-guard" in capsys.readouterr().err


def test_cross_sentence_pair_is_reported_not_dropped(capsys):
    """A pair the same-sentence gate rejects must flow through the SAME
    evidence_citation + stderr path as a cue rejection. Dropping it with a
    bare `continue` turned a DONE the pre-#299 code emitted into an ACTIVE
    carrying evidence_citation None and printing nothing — an unexplained
    downgrade, which is the exact silent-failure class this guard exists to
    remove."""
    by_id = _classify([("x-sent", "The docs are done. Track #51 next week.")])
    err = capsys.readouterr().err
    record = by_id["x-sent"]
    assert record["classification"] == "ACTIVE"
    citation = record["evidence_citation"]
    assert citation is not None, "cross-sentence pair was dropped silently"
    assert citation.startswith("heuristic: DONE rejected")
    assert "#51" in citation and "done" in citation
    assert "different sentences" in citation
    assert "x-sent" in err and "different sentences" in err


def test_cue_rejection_outranks_the_cross_sentence_rejection():
    """Precedence: an ungoverned pair wins (DONE), else a real cue rejection,
    else the boundary rejection. A text with both kinds of rejected pair must
    cite the cue, because the cue is the one that says why the item is open."""
    text = "Waiting on #51 to be closed. See #52."
    by_id = _classify([("prec", text)])
    citation = by_id["prec"]["evidence_citation"]
    assert by_id["prec"]["classification"] == "ACTIVE"
    assert "pending-intent cue" in citation
    assert "different sentences" not in citation


def test_wrapped_line_is_one_sentence_but_a_blank_line_is_not():
    r"""A bare newline is a wrap, not a sentence terminator; a BLANK line is.
    Treating `\n` as a terminator dropped "Fixed in\n#51" pairs outright."""
    assert oid._sentence_start("Fixed in\n#51", 9) == 0
    assert oid._sentence_start("Fixed in\r\n#51", 10) == 0
    assert oid._sentence_start("Fixed in\n\n#51", 10) == 10
    assert oid._sentence_start("Fixed in\r\n\r\n#51", 12) == 12
    by_id = _classify([("lf", "Fixed in\n#51"), ("crlf", "Fixed in\r\n#51")])
    assert by_id["lf"]["classification"] == "DONE"
    assert by_id["crlf"]["classification"] == "DONE"


def test_abbreviation_dot_is_not_a_sentence_boundary():
    """`e.g. ` / `i.e. ` end in dot-space and used to split, clipping the
    tier-1 window off the governing cue. The covered set is an ALLOWLIST of
    literal spellings — `e.g.`, `i.e.`, `etc.`, `vs.` — asserted here so the
    membership stays visible rather than assumed."""
    for case_id, text in (
            ("eg", "Waiting on infra, e.g. #51 to be closed"),
            ("ie", "Waiting on infra, i.e. #51 to be closed"),
            ("etc", "Waiting on infra etc. #51 to be closed"),
            ("vs", "Waiting on infra vs. #51 to be closed"),
    ):
        assert oid._sentence_start(text, text.index("#51")) == 0, case_id
        assert _classify([(case_id, text)])[case_id]["classification"] == "ACTIVE"

    # The `\b` inside the `etc.`/`vs.` lookbehinds is load-bearing and easy to
    # drop by accident: without it any word ENDING in those letters is read as
    # the abbreviation, and 'devs.'/'revs.' are ordinary vocabulary here. The
    # dot after such a word must still end the sentence, or a tier-1 cue reaches
    # across it exactly as it did for initials.
    text = "Waiting on the devs. The fix for #51 is merged."
    assert oid._sentence_start(text, text.index("#51")) == len(
        "Waiting on the devs.")
    assert _classify([("devs", text)])["devs"]["classification"] == "DONE"
    text = "Waiting on the revs. The fix for #51 is merged."
    assert oid._sentence_start(text, text.index("#51")) == len(
        "Waiting on the revs.")


def test_initials_end_a_sentence_so_a_cue_cannot_reach_across():
    """The allowlist replaced a letter-dot-letter SHAPE, which also matched
    INITIALS and merged two sentences into one. A tier-1 cue in the first half
    then governed a genuine completion in the second, and a real DONE read
    ACTIVE. Only the four allowlisted spellings suppress a boundary; an initial
    ends the sentence like any other dot."""
    text = "Awaiting update from A.B. The fix for #51 is merged."
    # The dot after the initial really is a boundary...
    assert oid._sentence_start(text, text.index("#51")) == len(
        "Awaiting update from A.B.")
    # ...so 'Awaiting' is in the sentence next door and cannot govern the pair.
    assert _classify([("initials", text)])["initials"]["classification"] == "DONE"

    text = "Waiting on J.R. The PR #12 was merged."
    assert oid._sentence_start(text, text.index("#12")) == len(
        "Waiting on J.R.")
    assert _classify([("initials2", text)])["initials2"]["classification"] == "DONE"

    # An allowlisted abbreviation in the same position still does NOT split,
    # so the two directions are asserted against each other.
    text = "Awaiting update from e.g. #51 is merged."
    assert oid._sentence_start(text, text.index("#51")) == 0


def test_verb_particle_survives_the_trailing_compound_anchor():
    """The trailing anchor blocks compound modifiers (`release-notes`), but it
    also blocked phrasal-verb completions — and SILENTLY, since no other pair
    in these texts qualifies, so the item became an ACTIVE with no rejection to
    cite. A particle that forms the whole final hyphen-segment is allowed
    through; every other following hyphen is still blocked."""
    for case_id, text in (
            ("merged-in", "This was merged-in with #51."),
            ("closed-out", "Issue #77 was closed-out last week."),
    ):
        assert _classify([(case_id, text)])[case_id]["classification"] == "DONE"

    assert oid._COMPLETION_PHRASE_RE.search("merged-into develop") is not None
    assert oid._COMPLETION_PHRASE_RE.search("fixed-up the parser") is not None
    assert oid._COMPLETION_PHRASE_RE.search("merged-back to develop") is not None

    # The compounds the anchor exists for are still blocked, including the ones
    # measured on the live vault.
    for compound in ("post-release-verification", "release-notes",
                     "closed-range", "release-version-sweep",
                     "github-release-board-promote",
                     "Development-Complete-on-board"):
        assert oid._COMPLETION_PHRASE_RE.search(compound) is None, compound

    # A particle PREFIX is not a particle: the segment must match exactly.
    assert oid._COMPLETION_PHRASE_RE.search("release-overhaul") is None
    assert oid._COMPLETION_PHRASE_RE.search("closed-out-of-scope") is None


def test_hyphen_prefixed_cues_fire_while_filenames_still_do_not():
    r"""Fix 5: the leading anchor was killing re-/cross-/double-/self- prefixed
    cues for nothing, since the TRAILING `(?![\w./-])` is what actually keeps
    'check' from matching inside `check-pr-base.py`."""
    for cue in ("re-verify", "re-check", "cross-check", "re-confirm",
                "self-check", "double-check", "doublecheck"):
        assert oid._PENDING_INTENT_CUE_RE.search(f"{cue} that #51 merged"), cue
    # The filename protection is unchanged.
    assert oid._PENDING_INTENT_CUE_RE.search(
        "modify `check-pr-base.py` to allow release/*") is None


def test_forward_form_and_conditional_cue_are_anchored_to_prose():
    r"""Cross-model (Gemini) finding: `_FORWARD_FORM_RE` on a bare \b matched
    'get' inside `go.get.it` and corroborated 'After' into a false ACTIVE on a
    genuine completion. `_CONDITIONAL_CUE_RE` had the same hole. Both now use
    the anchoring `_PENDING_INTENT_CUE_RE` already had."""
    assert oid._FORWARD_FORM_RE.search("After `go.get.it`, #123 was closed.") is None
    assert oid._FORWARD_FORM_RE.search("it will be closed") is not None
    # Blocked by the DOT, not by the slash — `/` is deliberately absent from
    # this regex's classes (see test_slash_joined_conditional_cues_still_fire).
    assert oid._CONDITIONAL_CUE_RE.search("see docs/when.md for details") is None
    assert oid._CONDITIONAL_CUE_RE.search("when CI is green") is not None
    # A bare path segment DOES match here, by design: tier 2 cannot reject on a
    # cue alone, and _FORWARD_FORM_RE — which keeps `/` — has to corroborate.
    assert oid._CONDITIONAL_CUE_RE.search("see docs/when/index for details") is not None
    assert oid._FORWARD_FORM_RE.search("run hooks/get_thing.py now") is None
    by_id = _classify([("dotted", "After `go.get.it`, #123 was closed.")])
    assert by_id["dotted"]["classification"] == "DONE"


def test_slash_joined_conditional_cues_still_fire():
    """Regression: putting `/` in _CONDITIONAL_CUE_RE's anchor classes silenced
    tier 2 on the most explicitly conditional phrasing there is. In "if/when",
    'if' failed the TRAILING anchor and 'when' failed the LEADING one, so
    neither cue matched and the item read DONE. A slash joins prose
    alternatives, not just path segments."""
    for case_id, text in (
            ("if-when", "Bump the cache if/when #51 is merged"),
            ("when-if", "Rerun the sweep when/if v1.2.0 is released"),
            ("before-after", "Ship the shim before/after #51 is merged"),
    ):
        assert oid._CONDITIONAL_CUE_RE.search(text) is not None, case_id
        assert _classify([(case_id, text)])[case_id]["classification"] == "ACTIVE"

    # The control this anchoring exists for is unaffected, because it is the
    # DOT that protects it, not the slash: 'get' still cannot match inside the
    # dotted filename and corroborate 'After'.
    assert oid._FORWARD_FORM_RE.search("After `go.get.it`, #123 was closed.") is None
    assert _classify([("dotted", "After `go.get.it`, #123 was closed.")]
                     )["dotted"]["classification"] == "DONE"

    # Tier 1 deliberately KEEPS `/`: it rejects on the cue alone, so a path
    # segment reading as a cue must not fire. Asserted so the asymmetry between
    # the two tiers is visible rather than looking like an oversight.
    assert "/" in oid._PENDING_INTENT_CUE_RE.pattern[:12]
    assert oid._PENDING_INTENT_CUE_RE.search("see docs/track for #51") is None


def test_pending_cue_does_not_match_inside_a_path_with_extension():
    r"""Companion to test_cue_does_not_match_inside_a_path_or_filename, pinning
    the LEADING `(?<![\w./-])` specifically.

    The fixture matters here. `scripts/verify.sh` does NOT discriminate: the
    trailing anchor already blocks it on the '.', so swapping the leading
    anchor for a bare \b passes anyway — the earlier version of this test
    asserted nothing about the anchor it names, and the mutation survived. The
    fixtures below end the cue at a word boundary, so ONLY the leading anchor
    can block them."""
    for text in ("rerun scripts/verify then #51 merged",
                 "see docs/track for the list; #51 merged",
                 "the pre-verify step ran; #51 merged"):
        assert oid._PENDING_INTENT_CUE_RE.search(text) is None, text
    # The dotted path stays pinned too — it is covered by the trailing anchor,
    # which is a different claim, not the same one.
    assert oid._PENDING_INTENT_CUE_RE.search(
        "rerun scripts/verify.sh then #51 merged") is None
    # ... and the same words in prose still fire.
    assert oid._PENDING_INTENT_CUE_RE.search("verify that #51 merged") is not None
    assert oid._PENDING_INTENT_CUE_RE.search("track whether #51 merged") is not None


def test_forward_form_lookback_window_is_bounded():
    """`_FORWARD_FORM_LOOKBACK_CHARS = 24` is a real decision, not a spare
    constant: widening it lets a copula from an unrelated part of the clause
    corroborate a tier-2 cue and reject a genuine completion claim."""
    text = "If the notes are unusually long we finally shipped #51"
    assert _classify([("lb", text)])["lb"]["classification"] == "DONE"

    original = oid._FORWARD_FORM_LOOKBACK_CHARS
    oid._FORWARD_FORM_LOOKBACK_CHARS = 1000
    try:
        assert _classify([("lb", text)])["lb"]["classification"] == "ACTIVE"
    finally:
        oid._FORWARD_FORM_LOOKBACK_CHARS = original


def test_tier2_is_clipped_to_the_clause_not_the_sentence():
    """Tier 2 scopes over its own clause. Widened to the sentence, a
    subordinator two clauses back starts rejecting real completion claims."""
    text = "Once CI passes we cut a build; #51 is merged"
    assert _classify([("t2", text)])["t2"]["classification"] == "DONE"

    original = oid._clause_start
    oid._clause_start = oid._sentence_start
    try:
        assert _classify([("t2", text)])["t2"]["classification"] == "ACTIVE"
    finally:
        oid._clause_start = original


def test_tier2_cue_search_is_bounded_on_the_right():
    """A cue AFTER the completion phrase does not govern it. If the search
    were unbounded on the right, "#51 is merged once CI is green" would pair
    the trailing 'once' with the leading 'is' and reject a real claim."""
    text = "#51 is merged once CI is green"
    assert _classify([("rb", text)])["rb"]["classification"] == "DONE"
    tok = oid._DISTINCTIVE_TOKEN_RE.search(text)
    phr = oid._COMPLETION_PHRASE_RE.search(text)
    assert oid._conditional_rejection(text, tok, phr) is None
    # The cue really is there — the bound, not its absence, is what saves this.
    assert oid._CONDITIONAL_CUE_RE.search(text) is not None


def test_segment_start_does_not_invent_a_boundary_at_pos():
    """Suggestion 7: passing `pos` as finditer's endpos makes `$` match there,
    so a '.' at pos-1 read as a sentence end even with no whitespace after it
    in the full text. It can only drop a pair today, but a dropped pair is now
    a reported rejection and the same-sentence gate was just reworked."""
    assert oid._sentence_start("Waiting on the fix.#51 is closed", 19) == 0
    # A real boundary is still found.
    assert oid._sentence_start("Waiting on the fix. #51 is closed", 20) == 19


def test_pairs_beyond_the_match_cap_are_not_seen():
    """`_HEURISTIC_MAX_MATCHES` bounds the pair sweep, and the documented cost
    is that a qualifying pair past the cap is invisible. Asserted rather than
    assumed: the cap can only lose a DONE, never invent one."""
    prefix = " ".join(f"#{i}" for i in range(1, 21))     # 20 tokens, far away
    filler = " ".join(["filler"] * 40)                   # pushes them >120 off
    text = f"{prefix} {filler} #21 fixed"
    assert len(list(oid._DISTINCTIVE_TOKEN_RE.finditer(text))) == 21
    assert _classify([("cap", text)])["cap"]["classification"] == "ACTIVE"

    original = oid._HEURISTIC_MAX_MATCHES
    oid._HEURISTIC_MAX_MATCHES = 100
    try:
        assert _classify([("cap", text)])["cap"]["classification"] == "DONE"
    finally:
        oid._HEURISTIC_MAX_MATCHES = original


def _multi_member_group(gid, texts):
    return {"group_id": gid, "project": "p", "representative": texts[0],
            "members": [{"file": "a.md", "line": i + 1, "text": t}
                        for i, t in enumerate(texts)]}


def test_sibling_done_keeps_the_rejected_member_s_objection(capsys):
    """A DONE on one member must not erase the guard's objection to another.

    SKILL.md Step 8 builds groups_to_cascade from ALL raw_members of a DONE
    group and cascade_group_members flips every (file, line) target — so the
    member the guard judged NOT done gets checked off on its sibling's
    evidence. Discarding the rejection deleted the only record that anyone
    had objected. Both member orders are asserted: the DONE-first order used
    to `break` out of the loop before the rejection was ever seen."""
    rejected = "Blocked until #64 is resolved"
    claim = "Fixed in #51"
    orders = {"reject-first": [rejected, claim], "done-first": [claim, rejected]}
    out = {r["group_id"]: r for r in oid.classify_groups_heuristic(
        [_multi_member_group(gid, texts) for gid, texts in orders.items()], {})}
    err = capsys.readouterr().err

    for gid, texts in orders.items():
        record = out[gid]
        # The verdict itself is unchanged.
        assert record["classification"] == "DONE", gid
        assert record["confidence"] == "MED", gid
        citation = record["evidence_citation"]
        assert citation.startswith("heuristic: token '#51'"), gid
        assert "sibling member rejected" in citation, gid
        assert "#64" in citation and "resolved" in citation, gid
        assert "Blocked until" in citation, gid
        # ... and it is logged, naming the group, the ITEM, and the
        # classification the objection rode in on.
        line = next(ln for ln in err.splitlines() if gid in ln)
        assert "DONE-DISPUTED:" in line, gid
        # The line quotes the GROUP's canonical text — the string an operator
        # maps back to the board — not the rejected member's own text, which
        # the rest of the line already names via its token/phrase/cue.
        assert f"[{texts[0]}]:" in line, gid   # which item, not just which id
        assert "classified DONE, but sibling member rejected" in line, gid
    assert err.count("sibling member rejected") == 2

    # The two log kinds must stay greppable apart: a sibling objection is not a
    # rejected verdict, so `grep "DONE rejected"` must not start matching it.
    assert "DONE rejected" not in err
    assert err.count("DONE-DISPUTED:") == 2
    assert "DONE-REJECTED:" not in err


def test_guard_log_lines_name_the_item_and_their_kind(capsys):
    """A bare group_id makes an operator map an opaque id back to an item by
    hand, and both line kinds used to open with the same 'heuristic-guard ...
    rejected' text. Each line now carries a distinct kind prefix and quotes the
    item — collapsed to one line, since the log contract is one line per
    event."""
    oid.classify_groups_heuristic(
        [_multi_member_group("rej", ["Blocked until #64 is resolved"])], {})
    line = capsys.readouterr().err.strip()
    assert line.startswith("[check-items] heuristic-guard DONE-REJECTED: ")
    assert "[Blocked until #64 is resolved]:" in line
    assert "DONE rejected" in line             # grep contract preserved

    # A representative with a newline must not split the line in two.
    oid.classify_groups_heuristic(
        [{"group_id": "wrapped",
          "project": "p",
          "representative": "Blocked until #64\nis resolved",
          "members": [{"file": "a.md", "line": 1,
                       "text": "Blocked until #64 is resolved"}]}], {})
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1, err
    assert "[Blocked until #64 is resolved]:" in err


def test_cue_rejection_outranks_a_boundary_rejection_across_members(capsys):
    """Within a member, a cue rejection already outranks a cross-sentence one.
    ACROSS members the choice was first-come and ignored the kind, so the
    reported reason depended on member ORDER: the same group with the same two
    members cited the uninformative boundary reason or the governing cue purely
    by list position. Order-independence IS the contract, so both orders are
    pinned."""
    boundary = "Fixed the parser. See #51."        # pair straddles a sentence
    cue = "Blocked until #64 is resolved"          # governed by a tier-1 cue

    # The two members really are the two rejection kinds, so the test cannot
    # tautologise if one of them stops rejecting.
    assert oid._heuristic_member_verdict(boundary)[2] == oid._CROSS_SENTENCE_REASON
    assert oid._heuristic_member_verdict(cue)[2] != oid._CROSS_SENTENCE_REASON

    for gid, texts in (("boundary-first", [boundary, cue]),
                       ("cue-first", [cue, boundary])):
        record = oid.classify_groups_heuristic(
            [_multi_member_group(gid, texts)], {})[0]
        assert record["classification"] == "ACTIVE", gid
        citation = record["evidence_citation"]
        assert "pending-intent cue 'Blocked until'" in citation, gid
        assert oid._CROSS_SENTENCE_REASON not in citation, gid


def test_group_with_only_rejected_members_is_still_active():
    """The sibling note must not leak into the single-member rejection path:
    with no DONE anywhere the record stays ACTIVE with the plain reason."""
    out = oid.classify_groups_heuristic(
        [_multi_member_group("all-rejected",
                             ["Blocked until #64 is resolved",
                              "Waiting on #51 to be closed"])], {})
    record = out[0]
    assert record["classification"] == "ACTIVE"
    assert "sibling member rejected" not in record["evidence_citation"]
    assert record["evidence_citation"].startswith("heuristic: DONE rejected")
