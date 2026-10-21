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
from pathlib import Path
import time as _time
from unittest.mock import MagicMock, patch

import pytest

import check_items_cli
import check_items_prefilter
import open_item_dedup as oid


# #339: exercise the cap with unrelated git evidence so the legacy
# notes-only shortcut cannot hide a bypass in per-verdict corroboration.
def _note_cap_bundle(**buckets):
    return {"p": {"note_completions": [{"text": "finish #318"}],
                  "commits": ["abcdef0 unrelated work"], **buckets}}


@pytest.mark.parametrize("citation,text,project", [
    (None, "finish #318", "p"), ("", "finish #318", "p"),
    ("  ", "finish #318", "p"), ("done #318", None, "p"),
    ("done #318", "", "p"), ("done #318", "  ", "p"),
    ("done #318", "finish #318", ""),
])
def test_note_cap_missing_citation_caps(citation, text, project):
    assert check_items_cli.note_cap_for(
        _note_cap_bundle(commits=["abcdef0 fixed #318"]), project, citation, text,
    )


@pytest.mark.parametrize("bucket", [
    {"merged_prs": "#318"}, {"merged_prs": ["#318"]},
    {"merged_prs": [None]}, {"merged_prs": [{}]},
    {"closed_issues": ["#318"]}, {"tags": "v3.4"},
    {"releases": [None]}, {"changelog_excerpt": ["#318"]},
])
def test_note_cap_malformed_bucket_caps(bucket):
    # A valid matching commit must not mask a malformed sibling bucket.
    assert check_items_cli.note_cap_for(
        _note_cap_bundle(commits=["abcdef0 fixed #318"], **bucket),
        "p", "done #318", "finish #318",
    )


@pytest.mark.parametrize("buckets", [{}, {
    "commits": [], "tags": [], "releases": [], "merged_prs": [],
    "closed_issues": [], "changelog_excerpt": "",
}])
def test_note_cap_empty_git_buckets_caps(buckets):
    evidence = {"p": {"note_completions": [{"text": "finish #318"}], **buckets}}
    assert check_items_cli.note_cap_for(evidence, "p", "done #318", "finish #318")


def test_note_cap_fts_mentions_never_corroborates():
    assert check_items_cli.note_cap_for(
        _note_cap_bundle(fts_mentions={"finish #318": 7}),
        "p", "done #318", "finish #318",
    )


def test_note_cap_note_text_never_corroborates():
    assert check_items_cli.note_cap_for(_note_cap_bundle(), "p", "done #318", "finish #318")


def test_note_cap_changed_paths_never_corroborates():
    assert check_items_cli.note_cap_for(
        _note_cap_bundle(changed_paths=["fix-#318.py"]), "p", "done #318", "finish #318",
    )


@pytest.mark.parametrize("git_ref,citation_ref,item_ref", [
    ("#31", "#318", "#318"), ("#318", "#31", "#31"),
    ("#318", "#318", "#31"), ("#318", "#31", "#318"),
])
def test_note_cap_issue_ref_boundary(git_ref, citation_ref, item_ref):
    assert check_items_cli.note_cap_for(
        _note_cap_bundle(commits=["abcdef0 fixed " + git_ref]),
        "p", "done " + citation_ref, "finish " + item_ref,
    )


@pytest.mark.parametrize("git_ref,citation_ref,item_ref", [
    ("v3.4", "v3.4.1", "v3.4.1"), ("v3.4.1", "v3.4", "v3.4"),
    ("v3.4.1", "v3.4.1", "v3.4"), ("v3.4.1.2", "v3.4.1", "v3.4.1"),
])
def test_note_cap_version_boundary(git_ref, citation_ref, item_ref):
    assert check_items_cli.note_cap_for(
        _note_cap_bundle(tags=[git_ref]), "p", "released " + citation_ref, "ship " + item_ref,
    )


@pytest.mark.parametrize("git_ref,citation_ref", [("123abc", "123abc7"), ("123abc7", "123abc")])
def test_note_cap_short_sha_rejected(git_ref, citation_ref):
    assert check_items_cli.note_cap_for(
        _note_cap_bundle(commits=[git_ref + " fixed"]),
        "p", "done " + citation_ref, "finish " + citation_ref,
    )


@pytest.mark.parametrize("citation", [
    "Session note says #318 is complete", "Reported complete in session (#318)",
    "Reported done in session 2026-02-01 (#318)",
])
def test_note_cap_paraphrase_repo_backed_is_med(citation):
    cap = check_items_cli.note_cap_for(_note_cap_bundle(), "p", citation, "finish #318")
    assert cap is True
    assert oid.assign_tier(citation, "finish #318", "DONE", "agent", cap) == "MED"


@pytest.mark.parametrize("bucket", [
    {"commits": ["abcdef0 fixed #318"]}, {"tags": ["v3.4"]},
    {"releases": ["v3.4 release"]}, {"merged_prs": [{"number": 318, "title": "fixed"}]},
    {"closed_issues": [{"number": 318, "title": "fixed"}]},
    {"changelog_excerpt": "Fixed #318"},
])
def test_note_cap_corroborated_ref_reaches_high(bucket):
    ref = "v3.4" if "tags" in bucket or "releases" in bucket else "#318"
    citation, text = "shipped " + ref, "finish " + ref
    cap = check_items_cli.note_cap_for(_note_cap_bundle(**bucket), "p", citation, text)
    assert cap is False
    assert oid.assign_tier(citation, text, "DONE", "agent", cap) == "HIGH"


@pytest.mark.parametrize("git_sha,citation_sha", [
    ("123abcd", "123abcd" + "0" * 33), ("123abcd" + "0" * 33, "123abcd"),
])
def test_note_cap_sha_prefix_both_directions(git_sha, citation_sha):
    assert check_items_cli.note_cap_for(
        _note_cap_bundle(commits=[git_sha + " fixed"]),
        "p", "done " + citation_sha, "finish " + citation_sha,
    ) is False


@pytest.mark.parametrize("notes", [None, [], ""])
def test_note_cap_no_notes_unchanged(notes):
    evidence = {"p": {"commits": ["abcdef0 fixed #318"], "note_completions": notes}}
    assert check_items_cli.note_cap_for(evidence, "p", None, None) is False
    cap = check_items_cli.note_cap_for(evidence, "p", "done #318", "finish #318")
    assert oid.assign_tier("done #318", "finish #318", "DONE", "agent", cap) == "HIGH"


def test_cache_merge_uses_note_cap_for():
    skill = (Path(__file__).resolve().parents[1] / "skills/check-items/SKILL.md").read_text()
    cache = skill.split("# Merge cached classifications", 1)[1].split("out =", 1)[0]
    assert "from check_items_cli import note_cap_for" in skill
    assert '"note_evidence_only": note_cap_for(' in cache
    assert 'g.get("_cached_evidence_citation"), g.get("representative", "")' in cache


def test_note_cap_classifier_stamps_each_verdict(tmp_path):
    """#339: two verdicts in one project must not share a memoised cap."""
    groups = [{"group_id": str(n), "project": "p", "representative": f"finish #{n}"}
              for n in (318, 339)]
    results = [{"group_id": str(n), "classification": "DONE", "confidence": "HIGH",
                "canonical_text": f"finish #{n}", "evidence_citation": f"fixed #{n}",
                "action_required": None, "note_evidence_only": False}
               for n in (318, 339)]
    payload = {"groups": groups, "evidence": _note_cap_bundle(
        commits=["abcdef0 fixed #318"],
    )}
    out = tmp_path / "verdicts.json"
    with patch.object(check_items_cli, "_dispatch_classifier_chunk", return_value=(0, results)):
        # Disable only the evidence-presence prefilter so both mocked agent
        # records reach the real merge/stamp path.
        with patch.dict(os.environ, {"CHECK_ITEMS_PREFILTER": "off"}):
            assert check_items_cli.run_classifier(_json.dumps(payload), str(out)) == 0
    records = _json.loads(out.read_text())
    assert [r["note_evidence_only"] for r in records] == [False, True]


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


def test_repo_backed_project_carries_note_completions(tmp_path):
    """#339 restores notes on repos; per-verdict corroboration enforces MED."""
    vault, sessions = _vault(tmp_path)
    insights_dir = vault / "insights"
    insights_dir.mkdir()
    _session(
        sessions / "2026-01-01-git-proj-aaaa.md", "2026-01-01", "git-proj",
        open_items=["wire up the foo exporter service #318"],
    )
    _session(
        sessions / "2026-02-01-git-proj-bbbb.md", "2026-02-01", "git-proj",
        summary="Shipped the foo exporter service end to end (#318).",
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    output_path = str(tmp_path / "pipeline-out.json")

    fake_vi = MagicMock()
    fake_vi.ensure_index.return_value = str(tmp_path / "vault.db")
    fake_vi.extract_keywords.return_value = []
    fake_vi.search_vault.return_value = []

    with patch("subprocess.run", side_effect=lambda *a, **k: _fake_completed("")), \
         patch.dict("sys.modules", {"vault_index": fake_vi}), \
         patch.object(oid, "_resolve_project_paths", return_value={"git-proj": str(repo_dir)}):

        result = oid.deep_analysis_pipeline(
            basenames=[],
            projects_json=_json.dumps(["git-proj"]),
            output_path=output_path,
            vault_path=str(vault),
            sessions_folder="claude-sessions",
            insights_folder="insights",
            db_path=str(tmp_path / "test-vault.db"),
        )

    with open(output_path, encoding="utf-8") as f:
        data = _json.load(f)

    assert result.startswith("OK:"), result
    assert data["evidence"]["git-proj"]["note_completions"], data["evidence"]
    assert data["evidence_gaps"]["projects_without_repo"] == []


def test_bundle_note_completions_only_flags_note_evidence_only(tmp_path):
    """#339 synthetic records cap even when unrelated git evidence exists."""
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
    assert by_id["ob-0002"]["note_evidence_only"] is True, by_id["ob-0002"]


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


# ---------------------------------------------------------------------------
# Hardening pass (H1-H4): the predicate's default was unsafe in four ways,
# all sharing one theme -- an unfamiliar or degraded input shape should fail
# SAFE (cap more), not fail open (cap less). Adjudicated as a hardening pass
# rather than a sixth fix round: every item lives in the same ~10 lines of
# note_evidence_only_for, the only function standing between a note-derived
# verdict and an auto-checked item.
# ---------------------------------------------------------------------------


def test_h1_unallowlisted_key_does_not_count_as_git_evidence():
    """H1: an open-ended "any other key counts as git evidence" test was
    right for a future GIT-derived source and wrong for a future NON-git
    one -- and #318 exists to add non-git sources, so the next one is more
    likely non-git than git. insight_completions is not in
    _GIT_DERIVED_EVIDENCE_KEYS, so it must not disqualify the cap."""
    evidence = {
        "notes-only": {
            "insight_completions": ["ship it"],
            "note_completions": [{"text": "wire up the foo exporter #318"}],
        },
    }
    assert check_items_cli.note_evidence_only_for(evidence, "notes-only") is True


def test_h1_allowlisted_key_still_counts_as_git_evidence():
    """POSITIVE CONTROL: a genuine allowlisted key (commits) must still
    disqualify the cap. Without this, H1's allowlist could be emptied out
    entirely and nothing here would notice."""
    evidence = {
        "git-project": {
            "commits": ["abc1234"],
            "note_completions": [{"text": "wire up the foo exporter #318"}],
        },
    }
    assert check_items_cli.note_evidence_only_for(evidence, "git-project") is False


def test_h2_fts_mentions_all_zero_counts_as_empty():
    """H2: fts_mentions is a dict of hit COUNTS, so {"x": 0} is a truthy
    dict meaning "searched, found nothing" -- a bare `not v` would read
    that as real evidence and leave the project uncapped."""
    evidence = {
        "notes-only": {
            "fts_mentions": {"wire up the foo exporter": 0},
            "note_completions": [{"text": "wire up the foo exporter #318"}],
        },
    }
    assert check_items_cli.note_evidence_only_for(evidence, "notes-only") is True


def test_h2_fts_mentions_nonzero_counts_as_real_evidence():
    """POSITIVE CONTROL: a genuine non-zero hit count must still count as
    git-derived evidence and disqualify the cap."""
    evidence = {
        "git-project": {
            "fts_mentions": {"wire up the foo exporter": 3},
            "note_completions": [{"text": "wire up the foo exporter #318"}],
        },
    }
    assert check_items_cli.note_evidence_only_for(evidence, "git-project") is False


def test_present_but_empty_note_completions_is_not_note_evidence_only():
    """Characterisation pin, not an H3 guard: a prior round proved H3 (the
    presence-before-emptiness check) carries no behavioural difference for
    ANY input -- no mutation can turn this test red on that basis. It
    stays as a pin of current behaviour: {"note_completions": []} -- key
    present (the scan ran for this project) but empty (found nothing) --
    must read as False, same as before the hardening pass: no note
    evidence means nothing for the cap to protect. Do not read this as
    proof that H3's presence check is exercised."""
    evidence = {"notes-only": {"note_completions": []}}
    assert check_items_cli.note_evidence_only_for(evidence, "notes-only") is False


def test_h4_unresolvable_project_fails_safe_to_capped():
    """H4: a group with no project name at all (`g.get("project", "")`
    defaults to "" when a group carries no "project" field) cannot be
    looked up in `evidence` no matter what it contains -- we cannot
    identify which project's evidence would justify HIGH, so the default
    must be to CAP, not to uncap. Narrower than "the project name is valid
    but absent from evidence" (test_note_evidence_only_for_project_absent,
    still False, unaffected -- see the docstring on note_evidence_only_for
    for why that case is safe to leave uncapped: #297's _cap_at_med already
    covers it via the synthetic/heuristic path)."""
    assert check_items_cli.note_evidence_only_for(
        {"other-project": {"note_completions": [{"text": "x"}]}}, "",
    ) is True
    assert check_items_cli.note_evidence_only_for({}, "") is True
    # Regression guard: a NAMED-but-absent project must NOT be affected by
    # the H4 change -- it stays False (this is the exact assertion
    # test_note_evidence_only_for_project_absent already pins; restated
    # here as the negative control this test's own docstring promises).
    assert check_items_cli.note_evidence_only_for(
        {}, "notes-only",
    ) is False


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
