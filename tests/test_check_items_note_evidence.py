"""Tests for #318 Task 4: a non-git evidence source for notes-only projects.

Every evidence source deep_analysis_pipeline gathers today is git-derived
(commits, tags, changed_paths, gh releases/PRs/issues, CHANGELOG excerpt,
FTS mentions). A notes-only project (no local git repo, by design) can
therefore never surface a citable DONE signal, even when the answer is
sitting in the vault: a strictly newer session's own `## Summary` reporting
the item done. `/recall` already computes exactly this signal as
`contradicted_by` (obsidian_utils.py, BH-001-guarded) --
gather_note_completion_evidence() is the same signal, restated as a
git-free /check-items evidence source.

Fixture conventions follow tests/test_recall_stale_openitem_flag.py (the
`contradicted_by` reference implementation) and
tests/test_check_items_evidence_gaps.py (the deep_analysis_pipeline /
subprocess.run-mocking pattern for a repo-less project).
"""

from __future__ import annotations

import json as _json
import os
import time as _time
from unittest.mock import MagicMock, patch

import pytest

import check_items_cli
import check_items_prefilter
import open_item_dedup as oid


def _session(path, date, project, summary="Did some work.", open_items=None):
    """Write a minimal claude-session note: optional `- [ ]` items under
    `## Open Questions / Next Steps`, and a `## Summary` body. Mirrors
    tests/test_recall_stale_openitem_flag.py's `_session()` helper so the
    frontmatter shape (type/project/date, first 20 lines) matches what
    collect_open_items() and gather_note_completion_evidence() both scan."""
    body = f"## Summary\n{summary}\n"
    if open_items:
        items_block = "\n".join(f"- [ ] {t}" for t in open_items)
        body += f"\n## Open Questions / Next Steps\n{items_block}\n"
    path.write_text(
        f"---\ntype: claude-session\ndate: {date}\nproject: {project}\n"
        f"status: summarized\n---\n\n# Session: {project}\n\n{body}",
        encoding="utf-8",
    )


def _vault(tmp_path):
    vault = tmp_path / "v"
    sessions = vault / "claude-sessions"
    sessions.mkdir(parents=True)
    return vault, sessions


def test_newer_session_reporting_done_is_detected(tmp_path):
    vault, sessions = _vault(tmp_path)
    _session(
        sessions / "2026-01-01-notes-only-aaaa.md", "2026-01-01", "notes-only",
        open_items=["wire up the foo exporter service"],
    )
    _session(
        sessions / "2026-02-01-notes-only-bbbb.md", "2026-02-01", "notes-only",
        summary="Shipped the foo exporter service end to end.",
    )

    results = oid.gather_note_completion_evidence(
        str(vault), "claude-sessions", "notes-only",
    )

    assert len(results) == 1, results
    assert results[0]["contradicted_by"] == "2026-02-01"


def test_same_date_session_never_contradicts(tmp_path):
    vault, sessions = _vault(tmp_path)
    _session(
        sessions / "2026-01-01-notes-only-aaaa.md", "2026-01-01", "notes-only",
        open_items=["wire up the foo exporter service"],
    )
    _session(
        sessions / "2026-01-01-notes-only-bbbb.md", "2026-01-01", "notes-only",
        summary="Shipped the foo exporter service end to end.",
    )

    results = oid.gather_note_completion_evidence(
        str(vault), "claude-sessions", "notes-only",
    )

    assert results == []


def test_older_session_never_contradicts(tmp_path):
    vault, sessions = _vault(tmp_path)
    _session(
        sessions / "2026-01-01-notes-only-aaaa.md", "2026-01-01", "notes-only",
        open_items=["wire up the foo exporter service"],
    )
    _session(
        sessions / "2025-12-01-notes-only-bbbb.md", "2025-12-01", "notes-only",
        summary="Shipped the foo exporter service end to end.",
    )

    results = oid.gather_note_completion_evidence(
        str(vault), "claude-sessions", "notes-only",
    )

    assert results == []


def test_mention_without_completion_phrase_is_not_evidence(tmp_path):
    """BH-001: a strictly-newer session merely co-mentioning the item's
    subject, without a completion phrase, must not fabricate DONE evidence."""
    vault, sessions = _vault(tmp_path)
    _session(
        sessions / "2026-01-01-notes-only-aaaa.md", "2026-01-01", "notes-only",
        open_items=["wire up the foo exporter service"],
    )
    _session(
        sessions / "2026-02-01-notes-only-bbbb.md", "2026-02-01", "notes-only",
        summary="Still blocked on the foo exporter service.",
    )

    results = oid.gather_note_completion_evidence(
        str(vault), "claude-sessions", "notes-only",
    )

    assert results == []


def _fake_completed(stdout="", returncode=0):
    cp = MagicMock()
    cp.stdout = stdout
    cp.returncode = returncode
    cp.stderr = ""
    return cp


def test_pipeline_attaches_note_completions_for_repo_less_project(tmp_path):
    """End-to-end through deep_analysis_pipeline for a project with no local
    git repo (_resolve_project_paths -> {}). The gap is still named in
    evidence_gaps, but the project is no longer evidence-less: it has a
    note_completions entry."""
    vault, sessions = _vault(tmp_path)
    insights_dir = vault / "insights"
    insights_dir.mkdir()
    _session(
        sessions / "2026-01-01-notes-only-aaaa.md", "2026-01-01", "notes-only",
        open_items=["wire up the foo exporter service"],
    )
    _session(
        sessions / "2026-02-01-notes-only-bbbb.md", "2026-02-01", "notes-only",
        summary="Shipped the foo exporter service end to end.",
    )

    output_path = str(tmp_path / "pipeline-out.json")

    fake_vi = MagicMock()
    fake_vi.ensure_index.return_value = str(tmp_path / "vault.db")
    fake_vi.extract_keywords.return_value = []
    fake_vi.search_vault.return_value = []

    with patch("subprocess.run", side_effect=lambda *a, **k: _fake_completed("")), \
         patch.dict("sys.modules", {"vault_index": fake_vi}), \
         patch.object(oid, "_resolve_project_paths", return_value={}):

        result = oid.deep_analysis_pipeline(
            basenames=[],
            projects_json=_json.dumps(["notes-only"]),
            output_path=output_path,
            vault_path=str(vault),
            sessions_folder="claude-sessions",
            insights_folder="insights",
            db_path=str(tmp_path / "test-vault.db"),
        )

    with open(output_path, encoding="utf-8") as f:
        data = _json.load(f)

    assert result.startswith("OK:"), result
    assert data["evidence"]["notes-only"]["note_completions"], data["evidence"]
    assert data["evidence_gaps"]["projects_with_evidence"] == 1
    assert data["evidence_gaps"]["projects_without_repo"] == ["notes-only"]


def _bridged_notes_only_evidence():
    """The evidence/bridged pair shared by test 6 and test 7 (positive
    control) -- both must reason about the SAME bridged payload."""
    evidence = {
        "notes-only": {
            "note_completions": [
                {
                    "text": "wire up the foo exporter",
                    "contradicted_by": "2026-02-01",
                    "contradicted_by_title": "Shipped the foo exporter end to end.",
                },
            ],
        },
    }
    return check_items_cli._bridge_project_evidence(evidence, "notes-only")


def test_bridge_exposes_note_completions_to_prefilter():
    bridged = _bridged_notes_only_evidence()

    assert "note_completion_items" in bridged
    assert check_items_prefilter.has_classifiable_evidence(
        {"canonical_text": "wire up the foo exporter"}, bridged,
    ) is True


def test_prefilter_rule_zero_requires_an_actual_match():
    """POSITIVE CONTROL: without this test, a Rule 0 that returns True
    unconditionally whenever note_completion_items is non-empty would still
    pass the previous test."""
    bridged = _bridged_notes_only_evidence()

    assert check_items_prefilter.has_classifiable_evidence(
        {"canonical_text": "rotate the deploy key"}, bridged,
    ) is False


def test_note_completion_citation_reaches_med_not_high():
    """Pin MED explicitly: the tier lift is from LOW, and the HIGH-trust cap
    on _HIGH_TRUST_SOURCES still applies -- it never reaches HIGH.

    Strengthened per fix-round-1 F1: the citation and item text now SHARE a
    literal #N ref (the exact shape that used to slip past the note-
    completion check and reach the HIGH literal-ref loop). Without embedding
    a shared ref, this test could never observe that path at all -- which is
    why the original CRITICAL defect survived it.
    """
    tier = oid.assign_tier(
        "reported done in session 2026-02-01 (fixed the foo exporter #318)",
        "wire up the foo exporter #318",
        "DONE",
        "agent",
    )
    assert tier == "MED"


def test_note_completion_citation_with_shared_version_still_caps_at_med():
    """Same defect, different literal-ref shape (vX.Y.Z instead of #N)."""
    tier = oid.assign_tier(
        "reported done in session 2026-02-01 (shipped v3.4.0)",
        "wire up the foo exporter v3.4.0",
        "DONE",
        "agent",
    )
    assert tier == "MED"


def test_ordinary_high_trust_citation_with_shared_ref_still_reaches_high():
    """POSITIVE CONTROL for F1: a genuine merged-PR/commit citation (NOT the
    note-completion shape) that shares a literal #N with the item text must
    still reach HIGH for a high-trust source. Without this test, capping the
    note-completion shape at MED could be implemented as capping
    EVERYTHING at MED, and no test here would notice."""
    tier = oid.assign_tier(
        "PR #318 merged as abc1234 on 2026-04-24.",
        "close issue #318",
        "DONE",
        "agent",
    )
    assert tier == "HIGH"


def test_note_completion_citation_without_the_shape_stays_low():
    """Guards the new MED regex from being widened into a catch-all.

    Strengthened per fix-round-1 F4: the original fixture ("some prose with
    no anchor") shared no words with the MED pattern at all, so a regex
    widened to e.g. `\\breported done\\b` (dropping the date requirement)
    would still leave this test green. This near-miss carries the phrase's
    prefix but not its `\\d{4}-\\d{2}-\\d{2}` anchor.
    """
    tier = oid.assign_tier(
        "reported done in session recently", "wire up the foo exporter service",
    )
    assert tier == "LOW"


def test_sentence_cased_note_completion_citation_still_reaches_med():
    """F8 (#318 fix round 2 addendum): the citation-shape fallback regex
    must be case-insensitive. A model writing "Reported done in session..."
    (capital R, sentence-cased -- the most natural way to start a sentence)
    used to walk straight past the case-sensitive regex into the HIGH loop.
    note_evidence_only is left at its default (False) here on purpose: this
    test is specifically about the fallback layer (F1's regex), not F7's
    provenance flag."""
    tier = oid.assign_tier(
        "Reported done in session 2026-02-01 (fixed the foo exporter #318)",
        "wire up the foo exporter #318",
        "DONE",
        "agent",
    )
    assert tier == "MED"


def test_datetime_dated_evidence_note_still_reaches_med(tmp_path):
    """F2: a note dated with a full datetime (not just YYYY-MM-DD) must not
    silently drop the resulting citation to LOW. gather_note_completion_evidence
    must store the date-ONLY prefix in contradicted_by, and the citation built
    from it (per the CLASSIFIER_PROMPT shape) must still hit the MED regex."""
    vault, sessions = _vault(tmp_path)
    _session(
        sessions / "2026-01-01-notes-only-aaaa.md", "2026-01-01", "notes-only",
        open_items=["wire up the foo exporter service"],
    )
    _session(
        sessions / "2026-02-01-notes-only-bbbb.md", "2026-02-01T09:30:00Z", "notes-only",
        summary="Shipped the foo exporter service end to end.",
    )

    results = oid.gather_note_completion_evidence(
        str(vault), "claude-sessions", "notes-only",
    )

    assert len(results) == 1, results
    assert results[0]["contradicted_by"] == "2026-02-01", results[0]["contradicted_by"]

    citation = (
        f"reported done in session {results[0]['contradicted_by']} "
        f"({results[0]['contradicted_by_title']})"
    )
    tier = oid.assign_tier(citation, "wire up the foo exporter service", "DONE", "agent")
    assert tier == "MED"


def test_evidence_pool_covers_a_wider_max_sessions(tmp_path):
    """F3: the evidence pool must not be capped smaller than max_sessions.

    Layout (newest-first scan order): 15 padding notes (2026-01-20 down to
    2026-01-06), then the completion note (2026-01-05), then the item's own
    note (2026-01-01, oldest). The completion note is strictly newer than
    the item and DOES carry a completion phrase, so it must contradict the
    item -- but it is the 16th matching note encountered, so a pool
    hard-capped at 10 (the old _NOTE_EVIDENCE_WINDOW-only cap) would never
    even read its body, and the item would wrongly stay unflagged.
    """
    vault, sessions = _vault(tmp_path)
    _session(
        sessions / "2026-01-01-notes-only-aaaa.md", "2026-01-01", "notes-only",
        open_items=["wire up the foo exporter service"],
    )
    _session(
        sessions / "2026-01-05-notes-only-bbbb.md", "2026-01-05", "notes-only",
        summary="Shipped the foo exporter service end to end.",
    )
    for i in range(15):
        _session(
            sessions / f"2026-01-{6 + i:02d}-notes-only-pad{i:02d}.md",
            f"2026-01-{6 + i:02d}", "notes-only",
            summary=f"Unrelated padding session {i}.",
        )

    results = oid.gather_note_completion_evidence(
        str(vault), "claude-sessions", "notes-only", max_sessions=20,
    )

    assert len(results) == 1, results
    assert results[0]["contradicted_by"] == "2026-01-05"


# ---------------------------------------------------------------------------
# Fix round 2 (F7, CRITICAL): the fix-round-1 citation-shape regex is text
# pattern matching against a string the classifier composes freely -- a
# reviewer found five off-template phrasings (one differs from the exact
# template by a single word) that all skip the regex and reach the HIGH
# literal-ref loop. These are the reviewer's own observed bypasses, used
# verbatim. The real fix is `note_evidence_only`: DATA check_items_cli
# derives from the evidence bundle's own shape (which no model wording can
# influence), threaded into assign_tier as an unconditional MED cap.
# ---------------------------------------------------------------------------

_OFF_TEMPLATE_BYPASS_CITATIONS = [
    "session 2026-02-01 reports #318 done",
    "newer session note (2026-02-01) says #318 shipped",
    "reported done in session note 2026-02-01 (Shipped #318)",
    "reported complete in session 2026-02-01 (Shipped #318)",
    "per the 2026-02-01 session summary, #318 is done",
]


@pytest.mark.parametrize("citation", _OFF_TEMPLATE_BYPASS_CITATIONS)
def test_note_evidence_only_caps_every_off_template_bypass_at_med(citation):
    """F7 enforcement: none of the five observed bypasses reach HIGH when
    note_evidence_only=True, regardless of the exact wording the classifier
    used or whether it happens to share #318 with the item text."""
    tier = oid.assign_tier(
        citation, "wire up the foo exporter #318", "DONE", "agent",
        note_evidence_only=True,
    )
    assert tier == "MED", (citation, tier)


@pytest.mark.parametrize("citation", _OFF_TEMPLATE_BYPASS_CITATIONS)
def test_off_template_citation_still_reaches_high_with_git_evidence(citation):
    """POSITIVE CONTROL: for a project that DOES have git evidence
    (note_evidence_only=False), the pre-existing behaviour is unchanged --
    a citation sharing a literal #N with the item text still reaches HIGH
    for a high-trust source. Without this control, F7's fix could cap
    EVERY citation at MED and no test here would notice."""
    tier = oid.assign_tier(
        citation, "wire up the foo exporter #318", "DONE", "agent",
        note_evidence_only=False,
    )
    assert tier == "HIGH", (citation, tier)


def test_bundle_note_completions_only_flags_note_evidence_only(tmp_path):
    """F7 bundle-level test, via check_items_cli.run_classifier(): a project
    whose evidence bundle is note_completions-ONLY (no git-derived bucket)
    is stamped note_evidence_only=True on its output record; a project
    whose bundle ALSO carries a git-derived bucket (commits) is stamped
    False, even though it too carries note_completions. Both groups have no
    classifiable evidence for their own canonical text, so both take the L2
    synthetic path -- exercising the "stamp synthetic records too" half of
    the fix without needing to mock a claude -p subprocess dispatch."""
    stdin_payload = {
        "groups": [
            {
                "group_id": "ob-0001",
                "project": "notes-only",
                "representative": "clean up temp files",
                "instances": [{"mtime": _time.time()}],
            },
            {
                "group_id": "ob-0002",
                "project": "git-project",
                "representative": "rotate the deploy key",
                "instances": [{"mtime": _time.time()}],
            },
        ],
        "evidence": {
            "notes-only": {
                "note_completions": [
                    {"text": "wire up the foo exporter service"},
                ],
            },
            "git-project": {
                "commits": ["abc1234 unrelated commit"],
                "note_completions": [
                    {"text": "wire up the foo exporter service"},
                ],
            },
        },
    }
    output_path = str(tmp_path / "classout.json")

    rc = check_items_cli.run_classifier(_json.dumps(stdin_payload), output_path)
    assert rc == 0, rc

    with open(output_path, encoding="utf-8") as f:
        records = _json.load(f)
    by_id = {r["group_id"]: r for r in records}

    assert by_id["ob-0001"]["note_evidence_only"] is True, by_id["ob-0001"]
    assert by_id["ob-0002"]["note_evidence_only"] is False, by_id["ob-0002"]


def test_step7_wiring_threads_note_evidence_only_into_assign_tier():
    """Step 7 wiring test: a record carrying note_evidence_only=True with an
    off-template #N citation must NOT land in the HIGH+DONE preselected
    bucket. Reproduces SKILL.md Step 7's assign_tier call shape directly
    (item.get("note_evidence_only", False) as the 5th positional arg) rather
    than re-parsing the markdown, since the call is a plain Python snippet
    with no importable entry point of its own."""
    item = {
        "evidence_citation": "session 2026-02-01 reports #318 done",
        "canonical_text": "wire up the foo exporter #318",
        "classification": "DONE",
        "classifier_source": "agent",
        "note_evidence_only": True,
    }
    tier = oid.assign_tier(
        item.get("evidence_citation"),
        item.get("canonical_text"),
        item.get("classification"),
        item.get("classifier_source"),
        item.get("note_evidence_only", False),
    )
    item["tier"] = tier

    assert tier == "MED", tier
    is_preselected_high_done = (
        item["classification"] == "DONE" and item["tier"] == "HIGH"
    )
    assert not is_preselected_high_done


# ---------------------------------------------------------------------------
# Fix round 4 (F12, CRITICAL): a cached classification (skills/check-items/
# SKILL.md Step 6's "known_unchanged" merge, classifier_source="cache") was
# hand-constructed with NO note_evidence_only key. "cache" IS in
# _HIGH_TRUST_SOURCES, and item.get("note_evidence_only", False) silently
# defaults False on a missing key, so a cached DONE citation sharing a
# literal #N with the item text reached HIGH and got auto-checked -- on
# every re-run of the same command after the first, since the first run's
# fresh classification is what got cached. The fix extracts the shared
# predicate to check_items_cli.note_evidence_only_for() so both producers
# (run_classifier's per-project stamp, and SKILL.md Step 6's cache-merge)
# use the identical logic and cannot drift apart.
# ---------------------------------------------------------------------------


def test_note_evidence_only_for_note_completions_only_bundle():
    evidence = {"notes-only": {"note_completions": [{"text": "x"}]}}
    assert check_items_cli.note_evidence_only_for(evidence, "notes-only") is True


def test_note_evidence_only_for_bundle_with_git_evidence():
    evidence = {
        "git-project": {
            "commits": ["abc1234 unrelated"],
            "note_completions": [{"text": "x"}],
        },
    }
    assert check_items_cli.note_evidence_only_for(evidence, "git-project") is False


def test_note_evidence_only_for_empty_bundle():
    assert check_items_cli.note_evidence_only_for({"p": {}}, "p") is False


def test_note_evidence_only_for_project_absent():
    assert check_items_cli.note_evidence_only_for({}, "notes-only") is False
    assert check_items_cli.note_evidence_only_for(
        {"other-project": {"note_completions": [{"text": "x"}]}}, "notes-only",
    ) is False


# ---------------------------------------------------------------------------
# Fix round 4 addendum (F13, CRITICAL): note_evidence_only_for() must be
# VALUE-aware, not key-presence. deep_analysis_pipeline's git block sets
# tags/changed_paths/merged_prs/closed_issues to `[]` on EVERY failure
# branch (no tags, gh unauthenticated/offline -- the normal state on a
# fresh machine or in CI), so a repo-backed project with zero usable git
# evidence still carries those keys with empty-list values. The original
# key-presence form (`set(proj.keys()) <= {"note_completions"}`) read that
# shape as "has git evidence" (any key beyond note_completions disqualifies
# it) and left it uncapped -- reachable without anything exotic: a repo
# with no tags and no merged PRs, gh offline.
# ---------------------------------------------------------------------------


def test_note_evidence_only_for_all_empty_git_buckets_is_true():
    """The exact shape a repo produces when every git/gh call fails or
    returns nothing: real note evidence, but every git-derived bucket
    present with an empty value. Must be True -- this is precisely the case
    the key-presence form got wrong (it read the empty tags/merged_prs/etc.
    keys as "has git evidence" and left the project uncapped)."""
    evidence = {
        "notes-only": {
            "tags": [],
            "changed_paths": [],
            "merged_prs": [],
            "closed_issues": [],
            "note_completions": [{"text": "wire up the foo exporter #318"}],
        },
    }
    assert check_items_cli.note_evidence_only_for(evidence, "notes-only") is True


def test_note_evidence_only_for_real_commits_is_false():
    """POSITIVE CONTROL: a project with genuine (non-empty) git evidence
    alongside note_completions must stay uncapped. Without this, F13's fix
    could treat every bundle as note-evidence-only and nothing here would
    notice."""
    evidence = {
        "git-project": {
            "commits": ["abc1234 fix"],
            "note_completions": [{"text": "wire up the foo exporter #318"}],
        },
    }
    assert check_items_cli.note_evidence_only_for(evidence, "git-project") is False


def test_note_evidence_only_for_empty_git_buckets_no_note_evidence_is_false():
    """A project with only empty git buckets and NO note_completions at all
    is not note-evidence-only -- there is no note evidence for the cap to
    protect. Such a project has NO real evidence of any kind, so its groups
    reach Step 7 through the synthetic/heuristic path, which the
    pre-existing _cap_at_med (#297: classifier_source not in
    _HIGH_TRUST_SOURCES) already caps regardless of this flag."""
    evidence = {"notes-only": {"tags": [], "commits": []}}
    assert check_items_cli.note_evidence_only_for(evidence, "notes-only") is False


def _cached_record(project):
    """Mirrors SKILL.md Step 6's cache-merge dict shape exactly (post-F12),
    including the note_evidence_only stamp a caller must compute via
    check_items_cli.note_evidence_only_for() the same way Step 6 does."""
    evidence = {
        "notes-only": {"note_completions": [{"text": "wire up the foo exporter #318"}]},
        "git-project": {"commits": ["abc1234 fixed #318"]},
    }
    return {
        "group_id": "ob-0001",
        "classification": "DONE",
        "confidence": "LOW",
        "canonical_text": "wire up the foo exporter #318",
        "evidence_citation": "PR #318 merged as abc1234 on 2026-04-24.",
        "action_required": None,
        "project": project,
        "classifier_source": "cache",
        "note_evidence_only": check_items_cli.note_evidence_only_for(evidence, project),
    }


def test_cached_record_for_note_only_project_caps_at_med():
    """F12 enforcement: a cached record for a note_completions-only project,
    with a citation carrying a literal #N shared with the item text (which
    WOULD be HIGH without the flag -- see the positive control below), must
    read MED once note_evidence_only is threaded through Step 7's
    assign_tier call the same way a fresh verdict is."""
    record = _cached_record("notes-only")
    assert record["note_evidence_only"] is True

    tier = oid.assign_tier(
        record["evidence_citation"],
        record["canonical_text"],
        record["classification"],
        record["classifier_source"],
        record["note_evidence_only"],
    )
    assert tier == "MED", tier


def test_cached_record_for_git_project_still_reaches_high():
    """POSITIVE CONTROL: the identical cached-record shape, same citation
    and item text, but for a project that HAS git evidence -- note_evidence_
    only is False and classifier_source="cache" is high-trust, so the
    pre-existing behaviour (HIGH, auto-checkable) is unchanged. Without this
    control, F12's fix could cap every cached record at MED and no test
    here would notice."""
    record = _cached_record("git-project")
    assert record["note_evidence_only"] is False

    tier = oid.assign_tier(
        record["evidence_citation"],
        record["canonical_text"],
        record["classification"],
        record["classifier_source"],
        record["note_evidence_only"],
    )
    assert tier == "HIGH", tier
