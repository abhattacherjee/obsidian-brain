# Plan — #264: unanchored next-step checkboxes escape the audit as false-open

**Issue:** #264 (bug, milestone v3.3). A `/check-items` run silently leaves a genuinely-completed open item in the `ACTIVE` bucket (false-open) when the checkbox text has **no issue/PR anchor**, because the deterministic L2 pre-filter can't ground it and `synthetic_classification()` defaults to a hidden `ACTIVE`. Separately, `/recall` re-lists such an item forever even when a newer session summary in the *same* brief already reports it done.

## Acceptance (verbatim from the issue)

- **A.** A completed item whose checkbox text lacks an issue/PR number but names a shipped component/branch is either **auto-closed (with git-derived evidence)** or **surfaced in a visible REVIEW tier** — not silently left `ACTIVE`.
- **B.** `/recall` **flags (or suppresses)** an open item that a **newer session summary in the same brief** reports as done.

The issue's third "proposed direction" (an authoring lint on unanchored checkboxes) is optional and **not** in acceptance — deferred to a follow-up issue.

## Design decisions (grounded in code, reviewed before build)

1. **Auto-close stays conservative (sub-agent only).** Deterministic paths never emit `DONE`. A no-anchor item with a distinctive component/branch token that is NOT confidently done routes to a new **`REVIEW`** classification (always visible, never auto-checks-off). Genuine auto-close still happens only through the careful sub-agent classifier with a citation.
2. **`REVIEW` is a net-new classification value**, distinct from `ACTIVE`/`STALE`/`DONE`/`NEEDS-ACTION`. It is always surfaced, never silent, always short-TTL cached (a nudge, not a durable verdict).
3. **Widen git ground truth** (`git tag --list`, changed-paths via `git log --name-only`, both bounded) so genuinely-shipped no-anchor items reach the sub-agent with enough evidence to auto-close as `DONE` — mirroring how the reporter verified by hand (`git tag`, `git ls-tree`).
4. **Fix B is self-contained in `build_context_brief()`** — both the narrative summaries and the open-items scan already run in that one Python pass; the match result is currently computed then discarded. Reuse `match_items_against_evidence`, widen the evidence pool to all newer session summaries, and flag (not suppress) contradicted items. No coupling to the check-items cache.

## Tasks

### Task 1 — REVIEW classification tier (fix A, surfacing half)
Files: hooks/open_item_dedup.py, hooks/check_items_prefilter.py, hooks/check_items_cli.py, hooks/check_items_cache.py, skills/check-items/SKILL.md.
- _VALID_CLASSIFICATIONS add "REVIEW"; partition_for_review routes REVIEW to the visible review bucket (not dashboard_only), keeping its evidence citation.
- synthetic_classification: no #N/sha anchor AND a distinctive component/branch token present AND not confidently done -> REVIEW (LOW), else preserve existing behavior.
- CLASSIFIER_PROMPT + schema enum + _validate_classifier_payload accept REVIEW.
- _ttl_for("REVIEW") short (ACTIVE-like).
- check-items/SKILL.md Step 7 + output-format render a visible REVIEW section/count.
- Tests fail-first: test_check_items_prefilter.py (adversarial techno-design-system fixture, no #N -> REVIEW not silent ACTIVE), test_open_item_dedup.py (partition visibility), test_check_items_cli.py (validator), test_check_items_cache.py (ttl).

### Task 2 — Widen git ground truth (fix A, auto-close half)
File: hooks/open_item_dedup.py deep_analysis_pipeline.
- Add bounded `git tag --list` and changed-paths (`git log --name-only`, deduped+capped) to proj_evidence; fold into the evidence the L2 completion-zone check and the sub-agent see. Guard git/gh absence like existing calls.
- Tests fail-first: test_open_item_dedup.py — no-anchor item naming a component present only in a tag/changed-path becomes DONE-eligible; assert bounded.

### Task 3 — recall cross-reference (fix B)
Files: hooks/obsidian_utils.py build_context_brief, skills/recall/SKILL.md.
- Widen evidence from most-recent session to all session summaries newer than each candidate item's source note; stop discarding the match; attach the contradicting session date/title to OB_OPEN_ITEM_CANDIDATES.
- recall/SKILL.md Step 3/4 surface the flag (read-only, never check off).
- Tests fail-first: test_recall_no_checkoff.py (or new test) — earlier session `- [ ]` X + newer summary says X shipped -> flagged.

### Task 4 — docs + architecture + changelog
- Update docs/architecture/architecture.json for the REVIEW classification + recall cross-reference flow; re-render + smoke-test.
- CHANGELOG.md [Unreleased] Fixed entry for #264.

## Out of scope / deferred
- Authoring lint on unanchored checkboxes -> follow-up issue.
- Coupling recall to the check-items persistence cache (rejected: self-contained match is simpler).

## Verification
- python3 -m pytest tests/ -q green (the 2 pre-existing test_emerge_v2 date-rot failures are fixed as commit 1 on this branch).
- scripts/commit-preflight.sh per commit; Phase 6 /deep-review; CI green-gate.
