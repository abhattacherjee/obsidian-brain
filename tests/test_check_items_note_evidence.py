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
from unittest.mock import MagicMock, patch

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
