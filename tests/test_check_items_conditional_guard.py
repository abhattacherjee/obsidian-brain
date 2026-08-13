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

import re

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
    # `(?![-\w])` anchor no qualifying pair reaches the pair loop. Without that
    # anchor the all-pairs sweep paired it with '#101' and flipped a live
    # ACTIVE item to DONE. Unlike the two cases above it does NOT get a None
    # citation: the anchor suppressed a phrase that the pre-#299 code used, so
    # the downgrade has to be explained — see _COMPOUND_REPORTED_CASES.
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


# Cases in NO_COOCCURRENCE_CORPUS whose ACTIVE verdict IS explained, because
# the phrase gate — not an absence of completion language — is what removed
# the pair. Everything else in that corpus has no completion word or no token
# at all, so there is genuinely nothing to report.
_COMPOUND_REPORTED_CASES = {"live-compound-post-release"}


def test_no_cooccurrence_corpus_unchanged():
    by_id = _classify(NO_COOCCURRENCE_CORPUS)
    for cid, _ in NO_COOCCURRENCE_CORPUS:
        assert by_id[cid]["classification"] == "ACTIVE"
        citation = by_id[cid]["evidence_citation"]
        if cid in _COMPOUND_REPORTED_CASES:
            # A phrase the compound anchor swallowed is reported, not silent.
            assert citation is not None, cid
            assert "hyphenated compound modifier" in citation, cid
        else:
            # No pair was ever a DONE candidate: nothing to explain.
            assert citation is None, cid


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


def test_loose_and_strict_phrase_regexes_differ_only_by_the_anchor():
    """The axiom _compound_phrase_rejection's whole explanation rests on.

    It reports every loose match the strict regex misses as "part of a
    hyphenated compound modifier". That is sound only because the two regexes
    are generated from ONE word list and differ ONLY by the trailing compound
    anchor — so the sole thing that can remove a loose match is a following
    non-particle hyphen. Everything downstream of that premise is proven and
    the premise itself was not: adding words to the loose regex alone
    (`|verified|landed`) passed the entire suite while making "QA #51 verified
    by the team" ACTIVE with a citation calling 'verified' a hyphenated
    compound, which is simply false.

    Pinned structurally as well as behaviourally: a probe string can only
    catch drift in words it happens to contain, while the pattern identity
    catches ANY word added to or dropped from either side."""
    # Structural: loose IS the shared word list, and strict is loose + anchor.
    shared = r"\b(" + oid._COMPLETION_WORDS + r")\b"
    anchor = (r"(?!-(?!(?:" + oid._COMPLETION_PARTICLES + r")(?![\w-])))")
    assert oid._LOOSE_COMPLETION_PHRASE_RE.pattern == shared
    assert oid._COMPLETION_PHRASE_RE.pattern == shared + anchor
    assert oid._COMPLETION_PHRASE_RE.flags == oid._LOOSE_COMPLETION_PHRASE_RE.flags

    # Behavioural: over a probe carrying every completion word, the loose
    # regex matches exactly what a regex rebuilt from _COMPLETION_WORDS does.
    rebuilt = re.compile(shared, re.IGNORECASE)
    probe = ("done merged shipped closed fixed complete completed resolved "
             "release released Done MERGED verified landed reopened "
             "release-notes closed-out post-release-verification undone "
             "doneness #51 v1.2.0 abc1234")
    spans = lambda rx: [(m.start(), m.group(0)) for m in rx.finditer(probe)]
    assert spans(oid._LOOSE_COMPLETION_PHRASE_RE) == spans(rebuilt)

    # ... and where no hyphen is in play the strict regex matches at every
    # span the loose one does, so the anchor is the ONLY difference between
    # them. Each loose-only span must be a completion word followed by '-'.
    loose_only = [m for m in oid._LOOSE_COMPLETION_PHRASE_RE.finditer(probe)
                  if not any(k.start() == m.start()
                             for k in oid._COMPLETION_PHRASE_RE.finditer(probe))]
    assert loose_only, "probe must exercise the anchor"
    for m in loose_only:
        assert probe[m.end():m.end() + 1] == "-", (m.group(0), m.start())

    no_hyphen = "done merged shipped closed fixed completed resolved released"
    strict_spans = [(m.start(), m.group(0))
                    for m in oid._COMPLETION_PHRASE_RE.finditer(no_hyphen)]
    loose_spans = [(m.start(), m.group(0))
                   for m in oid._LOOSE_COMPLETION_PHRASE_RE.finditer(no_hyphen)]
    assert strict_spans == loose_spans
    assert len(strict_spans) == len(no_hyphen.split())


def test_phrase_gate_suppression_is_reported_not_silent(capsys, monkeypatch):
    """Inverse of the silence this file used to characterize.

    _COMPLETION_PHRASE_RE sits UPSTREAM of the pair loop, so a narrowing there
    that suppressed the only phrase in a text built no pair at all: the #299
    reporting machinery never ran and the record was an ACTIVE with
    evidence_citation None and no log line, indistinguishable from a text that
    never mentioned completion. That is how the trailing compound anchor
    swallowed 'This was merged-in with #51' until a cross-model review found
    it, and a live sweep of 3671 open-item texts still found 2 downgrades
    reaching production through that channel.

    _compound_phrase_rejection closes it: the loose (pre-#299) phrase shape
    recovers whatever the anchor removed and reports it through BOTH channels.
    The verdict is unchanged — this is a reporting fix, not a behaviour change.
    """
    # A text whose only completion word is inside a compound modifier.
    text = "The post-release-verification item for #101 is still open"
    assert oid._COMPLETION_PHRASE_RE.search(text) is None
    tok, phr, reason = oid._heuristic_member_verdict(text)
    assert (tok.group(0), phr.group(0)) == ("#101", "release")
    assert reason == oid._COMPOUND_PHRASE_REASON
    record = _classify([("gate", text)])["gate"]
    assert record["classification"] == "ACTIVE"     # verdict unchanged...
    assert "hyphenated compound modifier" in record["evidence_citation"]
    assert "heuristic-guard DONE-REJECTED" in capsys.readouterr().err

    # Both live vault texts that reached production silently. Each stays
    # ACTIVE (the right verdict) and now says why.
    for cid, live in (
            ("live-1", "**Pre-tick post-merge tracking flip on PR creation.** "
                       "Note `(merged-on-PR-close)` in tracking row. PR #859 "
                       "was avoidable."),
            ("live-2", "First-time publish: After #73 fix, next real sync "
                       "publishes `github-release-board-promote` for the first "
                       "time—confirm this is intended behavior."),
    ):
        record = _classify([(cid, live)])[cid]
        assert record["classification"] == "ACTIVE", cid
        assert "hyphenated compound modifier" in record["evidence_citation"], cid
        assert "heuristic-guard DONE-REJECTED" in capsys.readouterr().err, cid

    # A rejection INSIDE the pair loop still reports through both channels,
    # and outranks the compound reason when both are available.
    record = _classify([("pair", "Blocked until #64 is resolved")])["pair"]
    assert record["classification"] == "ACTIVE"
    assert "DONE rejected" in record["evidence_citation"]
    assert "pending-intent cue" in record["evidence_citation"]
    assert "heuristic-guard" in capsys.readouterr().err

    # The TOKEN gate has no loose counterpart, so its suppression is STILL
    # silent — the same hazard, on the side this test's subject did not close.
    # Pinned by actually NARROWING the gate and watching a live verdict vanish
    # without a trace. The earlier version of this pin asserted silence over a
    # text carrying no distinctive token at all, which holds under any token
    # regex — narrower, wider, or deleted — and so characterized "a text with
    # no token is silent" rather than the hazard named above it. The mutation
    # it missed is the one applied here: restricting versions to three parts
    # passes the whole suite while sending "Fixed in v1.2" from DONE to ACTIVE
    # with no citation and no log line.
    #
    # DELETE ME DELIBERATELY if the token gate ever gains a loose counterpart:
    # this failing would mean the silence CLOSED, which is the good outcome and
    # has to be an explicit decision rather than a quietly passing assertion.
    live = "Fixed in v1.2"
    assert _classify([("tok-wide", live)])["tok-wide"]["classification"] == "DONE"
    capsys.readouterr()
    monkeypatch.setattr(oid, "_DISTINCTIVE_TOKEN_RE", re.compile(
        r"(#\d+|\b[0-9a-f]{7,40}\b|\bv\d+\.\d+\.\d+\b)"))  # 3-part versions only
    assert oid._DISTINCTIVE_TOKEN_RE.search(live) is None
    record = _classify([("tok-gate", live)])["tok-gate"]
    assert record["classification"] == "ACTIVE"    # the verdict MOVED...
    assert record["evidence_citation"] is None     # ...and explained nothing,
    assert capsys.readouterr().err == ""           # ...through either channel.


def test_compound_reason_states_the_condition_that_reached_it():
    """A reason a downgrade cites has to be TRUE of the text it explains.

    _compound_phrase_rejection is reached when the pair LOOP produced nothing,
    and that means no phrase SURVIVING the gate sat within range of a token —
    not that no phrase survived the gate at all. The reason string used to
    claim the latter, and the text below falsifies it: 'Fixed' survives the
    anchor untouched and is merely too far from '#101' to pair with it. One of
    the two compound citations on the live WIDE corpus has this shape, so the
    false clause was printed on real data, not only constructible."""
    text = ("Fixed the flaky parser here. " + "x" * 140
            + " the post-release-verification for #101")
    # A strict phrase demonstrably survived the gate...
    assert [m.group(0) for m in oid._COMPLETION_PHRASE_RE.finditer(text)] == ["Fixed"]
    # ... it is simply out of range of the only token, which is what actually
    # routes this text to the compound path.
    assert (abs(text.index("#101") - text.index("Fixed"))
            > oid._HEURISTIC_PROXIMITY_CHARS)
    tok, phr, reason = oid._heuristic_member_verdict(text)
    assert (tok.group(0), phr.group(0)) == ("#101", "release")
    assert reason == oid._COMPOUND_PHRASE_REASON

    citation = _classify([("far", text)])["far"]["evidence_citation"]
    assert "hyphenated compound modifier" in citation
    assert "no phrase survived the phrase gate" not in citation, citation
    assert "within range of a distinctive token" in citation


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


def test_sentence_final_etc_is_a_known_residual_of_the_allowlist():
    """The allowlist does NOT remove the cue-reaches-across failure for `etc.`,
    and the shortfall is documented on the regex rather than fixed.

    `etc.` is the one allowlisted spelling that is commonly SENTENCE-FINAL, so
    the lookbehind stops it terminating its own sentence and a tier-1 cue
    reaches into the next one — the same mechanism as the `A.B.` initials case
    the allowlist was introduced to remove. Kept deliberately: mid-sentence and
    sentence-final `etc.` need opposite treatment from one regex, and dropping
    `etc.` from the allowlist trades this safe-direction error for a false DONE.

    Pinned so the residual is a measured, reported fact rather than a claim in
    a comment — and so that anyone who does fix it sees this go red."""
    text = "Waiting on infra, DBs, etc. Fixed #51."
    # The dot does not end the sentence, so 'Waiting' still governs 'Fixed'.
    assert oid._sentence_start(text, text.index("#51")) == 0
    record = _classify([("etc-final", text)])["etc-final"]
    assert record["classification"] == "ACTIVE"
    # Safe direction, and NOT silent: the item stays open WITH a reason.
    assert "pending-intent cue" in record["evidence_citation"]

    # Contrast, and the reason the allowlist is still worth having: the other
    # three spellings are not normally sentence-final, and an initial — the
    # case the allowlist was introduced for — still ends its sentence.
    initials = "Awaiting update from A.B. Fixed #51."
    assert oid._sentence_start(initials, initials.index("#51")) > 0
    assert _classify([("ab", initials)])["ab"]["classification"] == "DONE"


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

    # `on` is excluded from _COMPLETION_PARTICLES, and THESE are what the
    # exclusion buys — a branch/date qualifier, not a phrasal verb. The
    # "Development-Complete-on-board" case asserted above does NOT discriminate
    # it: that one is blocked either way by the segment-exactness lookahead,
    # since its `on` is followed by `-board`. Adding `on` to the particle list
    # passes every other assertion in this file and fails only these two.
    assert oid._COMPLETION_PHRASE_RE.search("merged-on main") is None
    assert oid._COMPLETION_PHRASE_RE.search("closed-on friday") is None


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
    # `go.get.it` is blocked by BOTH dot anchors independently, so it pins
    # neither one on its own and says nothing about `/`. The slash in
    # _FORWARD_FORM_RE's classes is pinned by
    # test_forward_form_is_anchored_against_path_segments; the slash's ABSENCE
    # from _CONDITIONAL_CUE_RE below by test_slash_joined_conditional_cues_still_fire.
    assert oid._CONDITIONAL_CUE_RE.search("see docs/when.md for details") is None
    assert oid._CONDITIONAL_CUE_RE.search("when CI is green") is not None
    # A bare path segment DOES match here, by design: tier 2 cannot reject on a
    # cue alone, and _FORWARD_FORM_RE — which keeps `/` — has to corroborate.
    assert oid._CONDITIONAL_CUE_RE.search("see docs/when/index for details") is not None
    by_id = _classify([("dotted", "After `go.get.it`, #123 was closed.")])
    assert by_id["dotted"]["classification"] == "DONE"


def test_forward_form_is_anchored_against_path_segments():
    r"""The `/` in _FORWARD_FORM_RE's two anchor classes, pinned by fixtures
    that actually discriminate it.

    "run hooks/get_thing.py now" does NOT: `get` there is blocked by the
    TRAILING anchor on the `_`, so dropping `/` from both classes leaves it
    passing — the assertion reads as a slash claim but is a dotted/underscore
    claim, which is also what the sibling test's `go.get.it` fixture proves.
    Each fixture below has the path segment as the WHOLE final segment, so the
    slash is the only thing keeping it out."""
    for probe in ("see docs/be for details", "run hooks/get now",
                  "land src/be tomorrow"):
        assert oid._FORWARD_FORM_RE.search(probe) is None, probe
    # ... while the same words in prose still match.
    assert oid._FORWARD_FORM_RE.search("it will be closed") is not None
    assert oid._FORWARD_FORM_RE.search("once we get there") is not None

    # Classifier level: a tier-2 cue is present in each, so a path segment
    # re-admitted as a forward form corroborates it and falsely downgrades a
    # genuine completion claim to ACTIVE.
    for case_id, text in (
            ("fwd-docs", "Bump the cache once docs/be #51 merged"),
            ("fwd-hooks", "Rerun once hooks/get #51 merged"),
            ("fwd-src", "Land it after src/be #51 shipped"),
    ):
        assert _classify([(case_id, text)])[case_id]["classification"] == "DONE", case_id


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
    # the two tiers is visible rather than looking like an oversight. Behaviour
    # only — an assertion on `.pattern` would break under a behaviour-preserving
    # reorder or a hoist of the anchor into a shared constant (the way
    # _ABBREV_LOOKBEHINDS already is) while proving nothing this does not.
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


def _multi_member_group(gid, texts, files=None):
    members = []
    for i, t in enumerate(texts):
        m = {"file": f"{gid}.md", "line": i + 1, "text": t}
        if files is not None:
            m.update(files[i])
        members.append(m)
    return {"group_id": gid, "project": "p", "representative": texts[0],
            "members": members}


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
    # Line of the REJECTED member within each group, which is what the cascade
    # is about to tick off on its sibling's evidence.
    rejected_line = {"reject-first": 1, "done-first": 2}

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
        # ... and it names the disputed member's OWN line, not the group
        # representative's: Step 8 cascades to (file, line), so a reason
        # without a location leaves the operator with nothing to go look at.
        where = f" at {gid}.md:{rejected_line[gid]}"
        assert f"sibling member rejected{where}:" in citation, gid
        # ... and it is logged, naming the group, the ITEM, and the
        # classification the objection rode in on.
        line = next(ln for ln in err.splitlines() if gid in ln)
        assert "DONE-DISPUTED:" in line, gid
        assert f"sibling member rejected{where} —" in line, gid
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


def test_sibling_objection_omits_an_unknown_member_location(capsys):
    """Members arrive from merged.json, so file/line can be absent or null.
    The suffix is dropped rather than printing "at None:None" — but the
    objection itself still has to survive, since that is the whole point of
    keeping it."""
    for label, override in (("no-file", {"file": None}),
                            ("no-line", {"line": None}),
                            ("empty-file", {"file": ""})):
        group = _multi_member_group(
            label, ["Fixed in #51", "Blocked until #64 is resolved"],
            files=[{}, override])
        record = oid.classify_groups_heuristic([group], {})[0]
        err = capsys.readouterr().err
        citation = record["evidence_citation"]
        assert record["classification"] == "DONE", label
        assert "None" not in citation, label
        assert "sibling member rejected: token" in citation, label
        assert "Blocked until" in citation, label
        assert "None" not in err, label
        assert "sibling member rejected — token" in err, label


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


def test_rejection_kinds_have_a_total_order_across_members(capsys):
    """Within a member the three rejection kinds are already ranked. ACROSS
    members the choice was first-come and ignored the kind, so the reported
    reason depended on member ORDER: the same group with the same two members
    cited the uninformative boundary reason or the governing cue purely by list
    position. Order-independence IS the contract.

    All three pairwise orders are pinned, not just cue-vs-boundary. The
    compound rejection is the weakest of the three — it says only that the
    phrase gate had nothing to offer, where a cue says WHY the item is open —
    so a group carrying both must report the cue. Ranking it first is the
    mutation this covers: it is the one that would let the new reporting path
    from #299 round 4 mask the reason the guard exists to surface."""
    # One member text per rejection kind.
    boundary = "Fixed the parser. See #51."        # pair straddles a sentence
    cue = "Blocked until #64 is resolved"          # governed by a tier-1 cue
    compound = "The post-release-verification item for #101 is still open"

    # Each member really is the kind it stands for, so the test cannot
    # tautologise if one of them stops rejecting or changes kind.
    kinds = {"cue": cue, "boundary": boundary, "compound": compound}
    reasons = {k: oid._heuristic_member_verdict(t)[2] for k, t in kinds.items()}
    assert reasons["boundary"] == oid._CROSS_SENTENCE_REASON
    assert reasons["compound"] == oid._COMPOUND_PHRASE_REASON
    assert reasons["cue"] not in (oid._CROSS_SENTENCE_REASON,
                                  oid._COMPOUND_PHRASE_REASON)
    assert "pending-intent cue" in reasons["cue"]

    # (stronger, weaker) — asserted in BOTH member orders.
    for stronger, weaker in (("cue", "boundary"), ("cue", "compound"),
                             ("boundary", "compound")):
        for order in ((stronger, weaker), (weaker, stronger)):
            gid = f"{stronger}-over-{weaker}-{order[0]}first"
            record = oid.classify_groups_heuristic(
                [_multi_member_group(gid, [kinds[k] for k in order])], {})[0]
            assert record["classification"] == "ACTIVE", gid
            citation = record["evidence_citation"]
            assert reasons[stronger] in citation, gid
            assert reasons[weaker] not in citation, gid


def test_sibling_objection_needs_an_objection_not_an_absence(capsys):
    """DONE-DISPUTED must fire on a CONTESTED line, not on an absent one.

    Step 8 cascades a DONE group's checkoff to every member (file, line), and
    the objection exists so a human sees which line is being ticked off over
    the guard's head. A compound rejection objects to nothing: it is reached
    only when the member had no in-range (token, surviving-phrase) pair at all
    — the same evidentiary state as a member carrying no completion language
    whatsoever. Raising the alarm on one and not the other split two
    indistinguishable absences and diluted the signal on the lines that really
    are disputed. It stays on the ACTIVE citation, where it explains a real
    downgrade; it just no longer speaks for a sibling nobody objected to.

    A BOUNDARY rejection does belong here, and the distinction is checkable
    rather than a matter of taste: it is only reachable from inside the
    proximity check, so a real token and a phrase that survived the gate were
    within range and the guard declined to credit them — the guard refusing a
    verdict on evidence it examined, and the one path that turns a DONE the
    pre-#299 code emitted into an ACTIVE.

    Both member orders, since the DONE-first order is the one that used to
    short-circuit before a sibling was ever scanned."""
    claim = "Fixed in #51"
    absent = {
        # No completion phrase survives the anchor, so no pair is built and
        # the compound path reports what the gate removed.
        "compound": "Bump the release-notes for #77",
        # No token at all: nothing to report through any channel. The control
        # the compound member must now behave identically to.
        "no-token": "some prose with no token at all",
    }
    contested = {
        "boundary": "Fixed the parser. See #77.",
        "cue": "Blocked until #64 is resolved",
    }

    # Non-tautology: each member really is the kind it stands for.
    assert (oid._heuristic_member_verdict(absent["compound"])[2]
            == oid._COMPOUND_PHRASE_REASON)
    assert oid._heuristic_member_verdict(absent["no-token"]) == (None, None, None)
    assert (oid._heuristic_member_verdict(contested["boundary"])[2]
            == oid._CROSS_SENTENCE_REASON)
    assert "pending-intent cue" in oid._heuristic_member_verdict(contested["cue"])[2]

    citations = {}
    for kind, sibling in {**absent, **contested}.items():
        for order, texts in (("sibling-first", [sibling, claim]),
                             ("done-first", [claim, sibling])):
            gid = f"{kind}-{order}"
            record = oid.classify_groups_heuristic(
                [_multi_member_group(gid, texts)], {})[0]
            err = capsys.readouterr().err
            assert record["classification"] == "DONE", gid
            citation = record["evidence_citation"]
            citations[gid] = citation
            if kind in absent:
                assert "sibling member rejected" not in citation, gid
                assert "DONE-DISPUTED" not in err, gid
                assert err == "", gid
            else:
                assert "sibling member rejected" in citation, gid
                assert "DONE-DISPUTED" in err, gid

    # The compound sibling is now indistinguishable from the no-token control,
    # which is the point: two absences of evidence, one behaviour.
    for order in ("sibling-first", "done-first"):
        assert citations[f"compound-{order}"] == citations[f"no-token-{order}"]

    # ... while the compound reason still rides on a genuine downgrade, so
    # dropping it from the sibling path did not re-open the silent ACTIVE.
    record = _classify([("solo", absent["compound"])])["solo"]
    assert record["classification"] == "ACTIVE"
    assert "hyphenated compound modifier" in record["evidence_citation"]
    assert "heuristic-guard DONE-REJECTED" in capsys.readouterr().err


def test_reason_bucketing_compares_by_value_not_identity(monkeypatch):
    """`==`, not `is`, at the two bucketing sites — pinned on its own.

    The constants happen to be threaded through the return path unchanged, so
    `is` works today and a tidy-up back to it passes green. It is a latent
    inversion: any value-preserving reconstruction of a reason — a JSON
    round-trip, a `str()` copy, a reason assembled from parts — yields a
    different object, and `is` would then drop a boundary or compound
    rejection into the CUE bucket, where first-come wins and the precedence
    the buckets exist to establish inverts silently.

    The seam below reconstructs the reason exactly that way, so the mutation
    this kills is `==` -> `is` with nothing else changed."""
    real = oid._heuristic_member_verdict

    def rebuilt_reason(text):
        tok, phr, reason = real(text)
        if reason is not None:
            reason = "".join(reason)          # equal value, different object
        return tok, phr, reason

    # The seam must actually be a seam, or the test proves nothing.
    probe = rebuilt_reason("Blocked until #64 is resolved")[2]
    assert probe == real("Blocked until #64 is resolved")[2]
    assert probe is not real("Blocked until #64 is resolved")[2]
    monkeypatch.setattr(oid, "_heuristic_member_verdict", rebuilt_reason)

    cue = "Blocked until #64 is resolved"
    weaker = {"boundary": "Fixed the parser. See #51.",
              "compound": "The post-release-verification item for #101 is open"}
    for kind, text in weaker.items():
        for label, order in (("cue-first", [cue, text]),
                             ("cue-second", [text, cue])):
            gid = f"cue-over-{kind}-{label}"
            record = oid.classify_groups_heuristic(
                [_multi_member_group(gid, order)], {})[0]
            assert record["classification"] == "ACTIVE", gid
            citation = record["evidence_citation"]
            assert "pending-intent cue" in citation, gid
            assert oid._CROSS_SENTENCE_REASON not in citation, gid
            assert oid._COMPOUND_PHRASE_REASON not in citation, gid


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
