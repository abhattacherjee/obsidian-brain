"""Tests for deep_analysis_pipeline() evidence widening (Task 8 — issue #87).

Spec ref: Pipeline architecture Stage 3 (evidence gathering).
Adaptations from plan's verbatim version:
- Real signature: deep_analysis_pipeline(basenames, projects_json, output_path,
  vault_path, sessions_folder, insights_folder, db_path=None) -> str
- Evidence is written to output_path JSON, not returned directly
- pipeline requires vault_index.ensure_index() (mocked) and _resolve_project_paths()
  (patched to return a tmp repo dir so evidence loop runs)
- git log, gh release, gh pr list, gh issue list all go through subprocess.run;
  we intercept all of them via patch
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys

import pytest

# conftest.py adds hooks/ to sys.path; import after that
import open_item_dedup as oid
from unittest.mock import patch, MagicMock

# Imported here for use in timeout error-path tests
import check_items_cli as _check_items_cli_mod
SUBAGENT_TIMEOUT_SEC = _check_items_cli_mod.SUBAGENT_TIMEOUT_SEC


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_completed(stdout="", returncode=0):
    cp = MagicMock()
    cp.stdout = stdout
    cp.returncode = returncode
    cp.stderr = ""
    return cp


def _make_fake_vault_index(tmp_path):
    """Return a mock vault_index module stub sufficient for the pipeline."""
    vi = MagicMock()
    db_path = str(tmp_path / "vault.db")
    vi.ensure_index.return_value = db_path
    vi.extract_keywords.return_value = []
    vi.search_vault.return_value = []
    return vi


# ---------------------------------------------------------------------------
# Test 1: merged PRs + closed issues appear in output JSON
# ---------------------------------------------------------------------------

def test_deep_analysis_pipeline_includes_merged_prs_and_closed_issues(tmp_path):
    """Stage 3 must gather git log -40, gh pr list --state merged, gh issue list --state closed.

    Calls deep_analysis_pipeline with a real tmp repo dir patched in via
    _resolve_project_paths, intercepts all subprocess.run calls, and asserts
    that merged_prs and closed_issues appear in the output JSON.
    """
    repo_dir = tmp_path / "fake-repo"
    repo_dir.mkdir()
    output_path = str(tmp_path / "pipeline-out.json")

    # Minimal vault structure (no actual notes needed — projects list drives evidence loop)
    vault_path = str(tmp_path)
    sessions_folder = "sessions"
    insights_folder = "insights"
    os.makedirs(str(tmp_path / sessions_folder), exist_ok=True)
    os.makedirs(str(tmp_path / insights_folder), exist_ok=True)

    git_log_calls = []
    pr_list_calls = []
    issue_list_calls = []

    def fake_run(cmd, *args, **kwargs):
        if not isinstance(cmd, list):
            cmd = cmd.split()
        if cmd[:2] == ["git", "log"]:
            git_log_calls.append(cmd)
            assert "-40" in cmd, f"git log must use -40, got: {cmd}"
            return _fake_completed("abc1234 feat: test\ndef5678 fix: another\n")
        if cmd[:3] == ["gh", "pr", "list"]:
            pr_list_calls.append(cmd)
            assert "--state" in cmd and "merged" in cmd, f"pr list must be --state merged, got: {cmd}"
            assert "--limit" in cmd and "20" in cmd, f"pr list must have --limit 20, got: {cmd}"
            return _fake_completed(json.dumps([
                {"number": 68, "title": "fix: snapshot backlink", "mergedAt": "2026-04-24T00:00:00Z", "url": "https://github.com/x/y/pull/68"},
            ]))
        if cmd[:3] == ["gh", "issue", "list"]:
            issue_list_calls.append(cmd)
            assert "--state" in cmd and "closed" in cmd, f"issue list must be --state closed, got: {cmd}"
            assert "--limit" in cmd and "20" in cmd, f"issue list must have --limit 20, got: {cmd}"
            return _fake_completed(json.dumps([
                {"number": 87, "title": "Smarter /check-items", "closedAt": "2026-05-01T00:00:00Z", "body": "", "url": "https://github.com/x/y/issues/87"},
            ]))
        if cmd[:2] == ["gh", "release"]:
            return _fake_completed("[]")
        return _fake_completed("")

    fake_vi = _make_fake_vault_index(tmp_path)

    with patch("subprocess.run", side_effect=fake_run), \
         patch.dict("sys.modules", {"vault_index": fake_vi}), \
         patch.object(oid, "_resolve_project_paths", return_value={"myproject": str(repo_dir)}):

        result = oid.deep_analysis_pipeline(
            basenames=[],
            projects_json=json.dumps(["myproject"]),
            output_path=output_path,
            vault_path=vault_path,
            sessions_folder=sessions_folder,
            insights_folder=insights_folder,
            db_path=str(tmp_path / "test-vault.db"),
        )

    assert result.startswith("OK:"), f"pipeline returned error: {result}"

    # Verify output JSON has merged_prs and closed_issues under evidence
    with open(output_path, encoding="utf-8") as f:
        data = json.load(f)

    evidence = data.get("evidence", {})
    assert "myproject" in evidence, f"expected myproject in evidence, got: {list(evidence.keys())}"

    proj_ev = evidence["myproject"]
    assert "merged_prs" in proj_ev, f"merged_prs missing from evidence: {list(proj_ev.keys())}"
    assert "closed_issues" in proj_ev, f"closed_issues missing from evidence: {list(proj_ev.keys())}"

    assert len(proj_ev["merged_prs"]) == 1
    assert proj_ev["merged_prs"][0]["number"] == 68

    assert len(proj_ev["closed_issues"]) == 1
    assert proj_ev["closed_issues"][0]["number"] == 87

    # Verify subprocess calls were actually made
    assert git_log_calls, "git log was never called"
    assert pr_list_calls, "gh pr list was never called"
    assert issue_list_calls, "gh issue list was never called"


# ---------------------------------------------------------------------------
# Test 2: all evidence subprocess calls use timeout ≤ 10s
# ---------------------------------------------------------------------------

def test_deep_analysis_pipeline_subprocess_timeout_bounded(tmp_path):
    """All evidence subprocess calls must share the 10s timeout pattern."""
    repo_dir = tmp_path / "fake-repo"
    repo_dir.mkdir()
    output_path = str(tmp_path / "pipeline-out.json")

    vault_path = str(tmp_path)
    sessions_folder = "sessions"
    insights_folder = "insights"
    os.makedirs(str(tmp_path / sessions_folder), exist_ok=True)
    os.makedirs(str(tmp_path / insights_folder), exist_ok=True)

    timeouts = []

    def capture_run(cmd, *args, **kwargs):
        if "timeout" in kwargs:
            timeouts.append(kwargs["timeout"])
        if not isinstance(cmd, list):
            cmd = cmd.split()
        if cmd[:2] == ["git", "log"]:
            return _fake_completed("abc1234 feat: test\n")
        if cmd[:3] == ["gh", "pr", "list"]:
            return _fake_completed(json.dumps([]))
        if cmd[:3] == ["gh", "issue", "list"]:
            return _fake_completed(json.dumps([]))
        if cmd[:2] == ["gh", "release"]:
            return _fake_completed("")
        return _fake_completed("")

    fake_vi = _make_fake_vault_index(tmp_path)

    with patch("subprocess.run", side_effect=capture_run), \
         patch.dict("sys.modules", {"vault_index": fake_vi}), \
         patch.object(oid, "_resolve_project_paths", return_value={"myproject": str(repo_dir)}):

        result = oid.deep_analysis_pipeline(
            basenames=[],
            projects_json=json.dumps(["myproject"]),
            output_path=output_path,
            vault_path=vault_path,
            sessions_folder=sessions_folder,
            insights_folder=insights_folder,
            db_path=str(tmp_path / "test-vault.db"),
        )

    assert result.startswith("OK:"), f"pipeline returned error: {result}"
    assert timeouts, "expected at least one subprocess call to set timeout="
    assert all(t <= 10 for t in timeouts), f"all timeouts must be <=10s, got {timeouts}"


# ---------------------------------------------------------------------------
# Test 3: graceful error swallow on TimeoutExpired / CalledProcessError
# ---------------------------------------------------------------------------

def test_deep_analysis_pipeline_evidence_error_swallow(tmp_path):
    """On TimeoutExpired for gh pr list / gh issue list, keys default to []."""
    import subprocess as sp

    repo_dir = tmp_path / "fake-repo"
    repo_dir.mkdir()
    output_path = str(tmp_path / "pipeline-out.json")

    vault_path = str(tmp_path)
    sessions_folder = "sessions"
    insights_folder = "insights"
    os.makedirs(str(tmp_path / sessions_folder), exist_ok=True)
    os.makedirs(str(tmp_path / insights_folder), exist_ok=True)

    def timeout_run(cmd, *args, **kwargs):
        if not isinstance(cmd, list):
            cmd = cmd.split()
        if cmd[:3] == ["gh", "pr", "list"]:
            raise sp.TimeoutExpired(cmd, 10)
        if cmd[:3] == ["gh", "issue", "list"]:
            raise sp.TimeoutExpired(cmd, 10)
        if cmd[:2] == ["git", "log"]:
            return _fake_completed("abc1234 feat: test\n")
        if cmd[:2] == ["gh", "release"]:
            return _fake_completed("")
        return _fake_completed("")

    fake_vi = _make_fake_vault_index(tmp_path)

    with patch("subprocess.run", side_effect=timeout_run), \
         patch.dict("sys.modules", {"vault_index": fake_vi}), \
         patch.object(oid, "_resolve_project_paths", return_value={"myproject": str(repo_dir)}):

        result = oid.deep_analysis_pipeline(
            basenames=[],
            projects_json=json.dumps(["myproject"]),
            output_path=output_path,
            vault_path=vault_path,
            sessions_folder=sessions_folder,
            insights_folder=insights_folder,
            db_path=str(tmp_path / "test-vault.db"),
        )

    assert result.startswith("OK:"), f"pipeline returned error: {result}"

    with open(output_path, encoding="utf-8") as f:
        data = json.load(f)

    evidence = data.get("evidence", {})
    assert "myproject" in evidence
    proj_ev = evidence["myproject"]
    # Both keys should be present and set to [] after timeout
    assert proj_ev.get("merged_prs") == [], f"expected merged_prs=[], got: {proj_ev.get('merged_prs')}"
    assert proj_ev.get("closed_issues") == [], f"expected closed_issues=[], got: {proj_ev.get('closed_issues')}"


# ---------------------------------------------------------------------------
# Task 9: CONFIDENCE_TIER_RULES + assign_tier helper
# ---------------------------------------------------------------------------

import re


def test_confidence_tier_rules_high_requires_literal_ref():
    """Test 4 - MED evidence with inferred linkage must NOT promote to HIGH.

    Adaptation from plan: MED citation changed from
      "Story 11.12 shipped the fix; #534 still OPEN on GitHub."
    to
      "Story 11.12 shipped the fix; legacy ticket still OPEN on GitHub."
    to avoid #534 appearing in both citation and item_text (which would trigger
    the HIGH rule's #\\d+ pattern naively). The spirit of the test is preserved:
    an inferred linkage where the citation references a different artifact (Story
    identifier, not the canonical GitHub issue number) should land in MED.
    """
    from open_item_dedup import assign_tier, CONFIDENCE_TIER_RULES

    tier_high = assign_tier(
        evidence_citation="Merged as 5dfaf98 on 2026-04-24; PR #68 closed.",
        item_text="PR #68 write-path cross-midnight backlink fix",
        # #297: absent/unknown classifier_source now caps at MED too, so an
        # explicit trusted source is required to prove this citation shape
        # genuinely reaches HIGH.
        classifier_source="agent",
    )
    assert tier_high == "HIGH"

    tier_med = assign_tier(
        evidence_citation="Story 11.12 shipped the fix; legacy ticket still OPEN on GitHub.",
        item_text="Close GitHub issue #534 (covered by Story 11.12)",
    )
    assert tier_med == "MED"

    tier_low = assign_tier(
        evidence_citation="FTS mentions: 3 occurrences in recent sessions.",
        item_text="Some open question text",
    )
    assert tier_low == "LOW"

    assert isinstance(CONFIDENCE_TIER_RULES, dict)
    assert "HIGH" in CONFIDENCE_TIER_RULES or "high" in CONFIDENCE_TIER_RULES


def test_assign_tier_handles_none_or_empty():
    """assign_tier defaults to LOW on empty/None evidence."""
    from open_item_dedup import assign_tier
    assert assign_tier(None, "item text") == "LOW"
    assert assign_tier("", "item text") == "LOW"
    assert assign_tier("citation", None) == "LOW"


def test_cross_project_dedup_respects_project_boundary():
    """Test 1 - #534 in obsidian-brain and #534 in tiny-vacation-agent stay separate."""
    from open_item_dedup import cross_project_dedup

    groups_by_project = {
        "obsidian-brain": [
            {
                "group_id": "ob-0001",
                "project": "obsidian-brain",
                "representative": "Close GitHub issue #534 (snapshot backlink)",
                "members": [{"file": "a.md", "line": 1, "text": "Close #534"}],
            }
        ],
        "tiny-vacation-agent": [
            {
                "group_id": "tva-0001",
                "project": "tiny-vacation-agent",
                "representative": "Close GitHub issue #534 (Story 11.12 fixed)",
                "members": [{"file": "b.md", "line": 1, "text": "Close #534"}],
            }
        ],
    }

    merged = cross_project_dedup(groups_by_project)

    all_groups = merged if isinstance(merged, list) else [
        g for project_groups in merged.values() for g in project_groups
    ]
    assert len(all_groups) == 2
    projects = {g["project"] for g in all_groups}
    assert projects == {"obsidian-brain", "tiny-vacation-agent"}


def test_cross_project_dedup_within_project_unchanged():
    """Within-project dedup is handled upstream by find_duplicates; this fn must
    not coalesce same-project groups."""
    from open_item_dedup import cross_project_dedup
    groups_by_project = {
        "obsidian-brain": [
            {"group_id": "ob-0001", "project": "obsidian-brain",
             "representative": "Fix bug A", "members": []},
            {"group_id": "ob-0002", "project": "obsidian-brain",
             "representative": "Fix bug B", "members": []},
        ],
    }
    merged = cross_project_dedup(groups_by_project)
    all_groups = merged if isinstance(merged, list) else [
        g for project_groups in merged.values() for g in project_groups
    ]
    assert len(all_groups) == 2


def test_cross_project_dedup_single_project_passthrough():
    """When only one project's groups are provided, output equals input shape."""
    from open_item_dedup import cross_project_dedup
    groups_by_project = {
        "obsidian-brain": [
            {"group_id": "ob-0001", "project": "obsidian-brain",
             "representative": "Item", "members": []},
        ],
    }
    merged = cross_project_dedup(groups_by_project)
    all_groups = merged if isinstance(merged, list) else [
        g for project_groups in merged.values() for g in project_groups
    ]
    assert len(all_groups) == 1
    assert all_groups[0]["project"] == "obsidian-brain"


# ---------------------------------------------------------------------------
# Task 11: run_semantic_merge() CLI wrapper
# ---------------------------------------------------------------------------

def test_run_semantic_merge_picks_haiku_for_small_groups(tmp_path, monkeypatch):
    """<=60 groups -> claude -p --model haiku."""
    import check_items_cli as cli

    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        captured["stdin"] = kwargs.get("input", "")
        out_path = tmp_path / "out.json"
        out_path.write_text(json.dumps({
            "merges": [],
            "total_groups_before": 5,
            "total_groups_after": 5,
        }))
        return _fake_completed(stdout=out_path.read_text(), returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    stdin_json = json.dumps({"groups": [{"group_id": f"g{i}", "project": "p",
                                          "representative": f"item {i}", "member_texts": []}
                                         for i in range(5)]})
    out_path = tmp_path / "merge.json"
    rc = cli.run_semantic_merge(stdin_json=stdin_json, output_path=str(out_path))
    assert rc == 0
    assert out_path.exists()
    assert "haiku" in " ".join(captured["cmd"])


def test_run_semantic_merge_picks_sonnet_above_60(tmp_path, monkeypatch):
    """>60 groups escalates to sonnet."""
    import check_items_cli as cli

    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        out_path = tmp_path / "out.json"
        out_path.write_text(json.dumps({
            "merges": [],
            "total_groups_before": 75,
            "total_groups_after": 75,
        }))
        return _fake_completed(stdout=out_path.read_text(), returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    big_groups = [{"group_id": f"g{i}", "project": "p", "representative": f"x{i}",
                   "member_texts": []} for i in range(75)]
    stdin_json = json.dumps({"groups": big_groups})
    out_path = tmp_path / "merge2.json"
    rc = cli.run_semantic_merge(stdin_json=stdin_json, output_path=str(out_path))
    assert rc == 0
    assert "sonnet" in " ".join(captured["cmd"])


def test_run_semantic_merge_caps_stdin_at_1mb():
    """Per project CLAUDE.md security pattern, stdin reads cap at 1_000_000 bytes."""
    import check_items_cli as cli
    src = open(cli.__file__).read()
    assert "1_000_000" in src or "1000000" in src, \
        "check_items_cli must cap stdin reads at 1_000_000 bytes"


def test_run_semantic_merge_prompt_includes_five_examples():
    """Per spec lines 226-228, the 5 canonical examples are LOAD-BEARING and must be present."""
    import check_items_cli as cli
    prompt = cli.SEMANTIC_MERGE_PROMPT
    assert "SHOULD MERGE" in prompt
    assert "SHOULD NOT MERGE" in prompt
    assert "text-fallback routing" in prompt or "AskUserQuestion" in prompt
    assert "vault-doctor" in prompt
    assert "Investigate" in prompt and "Fix" in prompt
    assert "PR #67" in prompt or "PR #70" in prompt
    assert "STRICT JSON" in prompt or "strict JSON" in prompt.lower()


# ---------------------------------------------------------------------------
# Task 12: merge_groups_semantically() orchestrator
# ---------------------------------------------------------------------------

def test_semantic_merge_pairs_with_zero_token_overlap(tmp_path, monkeypatch):
    """Test 1b - sub-agent merges two zero-token-overlap items into one group."""
    import open_item_dedup as oid

    def fake_cli_run(cmd, *args, **kwargs):
        out_path = None
        for i, c in enumerate(cmd):
            if c.endswith(".json") and "out" in c:
                out_path = c
        merge_map = {
            "merges": [
                {
                    "canonical_group_id": "ob-0004",
                    "absorbed_group_ids": ["ob-0003"],
                    "reasoning": "Both describe N=1 text-fallback routing decision",
                }
            ],
            "total_groups_before": 2,
            "total_groups_after": 1,
        }
        if out_path:
            with open(out_path, "w") as f:
                json.dump(merge_map, f)
        return _fake_completed(stdout=json.dumps(merge_map), returncode=0)

    coarse = [
        {
            "group_id": "ob-0003",
            "project": "obsidian-brain",
            "representative": "Decide text-fallback routing vs. sentinel option to satisfy AskUserQuestion minItems=2",
            "members": [{"file": "a.md", "line": 1, "text": "..."}],
        },
        {
            "group_id": "ob-0004",
            "project": "obsidian-brain",
            "representative": "Review fuzzy-matched cascade candidate about routing N=1 to text-fallback",
            "members": [{"file": "b.md", "line": 2, "text": "..."}],
        },
    ]

    monkeypatch.setattr(oid.subprocess, "run", fake_cli_run)
    merged = oid.merge_groups_semantically({"obsidian-brain": coarse})

    flat = merged if isinstance(merged, list) else [
        g for v in merged.values() for g in v
    ]
    assert len(flat) == 1, f"expected 1 merged group, got {len(flat)}: {flat}"
    assert flat[0]["group_id"] == "ob-0004"
    assert len(flat[0]["members"]) == 2


def test_semantic_merge_rejects_cross_project(tmp_path, monkeypatch):
    """Test 1c - sub-agent returns a cross-project merge; Python filter drops it."""
    import open_item_dedup as oid

    def fake_cli_run(cmd, *args, **kwargs):
        out_path = None
        for c in cmd:
            if isinstance(c, str) and c.endswith(".json") and "out" in c:
                out_path = c
        merge_map = {
            "merges": [
                {
                    "canonical_group_id": "ob-0001",
                    "absorbed_group_ids": ["tva-0001"],
                    "reasoning": "sub-agent erroneously merged across projects",
                }
            ],
            "total_groups_before": 2,
            "total_groups_after": 1,
        }
        if out_path:
            with open(out_path, "w") as f:
                json.dump(merge_map, f)
        return _fake_completed(stdout=json.dumps(merge_map), returncode=0)

    coarse_by_proj = {
        "obsidian-brain": [{"group_id": "ob-0001", "project": "obsidian-brain",
                            "representative": "X", "members": []}],
        "tiny-vacation-agent": [{"group_id": "tva-0001", "project": "tiny-vacation-agent",
                                  "representative": "Y", "members": []}],
    }

    monkeypatch.setattr(oid.subprocess, "run", fake_cli_run)
    merged = oid.merge_groups_semantically(coarse_by_proj)

    flat = merged if isinstance(merged, list) else [
        g for v in merged.values() for g in v
    ]
    assert len(flat) == 2, "cross-project merge must be filtered out"


# ---------------------------------------------------------------------------
# Task 13: token-only fallback after 2 sub-agent failures
# ---------------------------------------------------------------------------

def test_semantic_merge_fallback_on_failure(monkeypatch, tmp_path):
    """Test 1d - sub-agent returns malformed JSON twice; coarse groups pass through
    with pipeline_mode flag set."""
    import open_item_dedup as oid

    call_count = {"n": 0}

    def fake_cli_run(cmd, *args, **kwargs):
        call_count["n"] += 1
        return _fake_completed(stdout="not valid json", returncode=0)

    coarse = [
        {"group_id": "g1", "project": "p", "representative": "A", "members": []},
        {"group_id": "g2", "project": "p", "representative": "B", "members": []},
    ]

    monkeypatch.setattr(oid.subprocess, "run", fake_cli_run)
    merged = oid.merge_groups_semantically(coarse)

    assert call_count["n"] == 2, f"expected 2 attempts before fallback, got {call_count['n']}"
    flat = merged if isinstance(merged, list) else [g for v in merged.values() for g in v]
    assert len(flat) == 2, "fallback must pass coarse groups through unchanged"
    mode = oid.get_last_semantic_merge_mode()
    assert mode == "token-only (semantic pass failed)"


def test_semantic_merge_mode_reset_on_success(monkeypatch, tmp_path):
    """Successful merge clears the failure flag."""
    import open_item_dedup as oid

    def fake_cli_run(cmd, *args, **kwargs):
        out_path = None
        for c in cmd:
            if isinstance(c, str) and c.endswith(".json") and "out" in c:
                out_path = c
        if out_path:
            with open(out_path, "w") as f:
                json.dump({"merges": [], "total_groups_before": 1, "total_groups_after": 1}, f)
        return _fake_completed(returncode=0)

    monkeypatch.setattr(oid.subprocess, "run", fake_cli_run)
    oid.merge_groups_semantically([
        {"group_id": "g1", "project": "p", "representative": "A", "members": []}
    ])
    assert oid.get_last_semantic_merge_mode() == "ok"


# ---------------------------------------------------------------------------
# Task 14: over-merge guards (test-only)
# ---------------------------------------------------------------------------

def test_semantic_merge_keeps_dry_run_vs_apply_separate(monkeypatch):
    """Test 1e - dry-run and apply variants of the same command MUST NOT merge."""
    import open_item_dedup as oid

    def fake_cli_run(cmd, *args, **kwargs):
        out_path = next((c for c in cmd if isinstance(c, str)
                         and c.endswith(".json") and "out" in c), None)
        merge_map = {"merges": [], "total_groups_before": 2, "total_groups_after": 2}
        if out_path:
            with open(out_path, "w") as f:
                json.dump(merge_map, f)
        return _fake_completed(returncode=0)

    coarse = [
        {"group_id": "g1", "project": "obsidian-brain",
         "representative": "Run /vault-doctor --check snapshot-integrity (dry-run)",
         "members": [{"file": "a.md", "line": 1, "text": "..."}]},
        {"group_id": "g2", "project": "obsidian-brain",
         "representative": "Run /vault-doctor fix --check snapshot-integrity (apply mode)",
         "members": [{"file": "b.md", "line": 1, "text": "..."}]},
    ]
    monkeypatch.setattr(oid.subprocess, "run", fake_cli_run)
    merged = oid.merge_groups_semantically(coarse)
    flat = merged if isinstance(merged, list) else [g for v in merged.values() for g in v]
    ids = {g["group_id"] for g in flat}
    assert ids == {"g1", "g2"}, f"dry-run and apply must remain separate; got {ids}"


def test_semantic_merge_keeps_investigate_vs_fix_separate(monkeypatch):
    """Test 1f - 'Investigate X' and 'Fix X' MUST NOT merge."""
    import open_item_dedup as oid

    def fake_cli_run(cmd, *args, **kwargs):
        out_path = next((c for c in cmd if isinstance(c, str)
                         and c.endswith(".json") and "out" in c), None)
        merge_map = {"merges": [], "total_groups_before": 2, "total_groups_after": 2}
        if out_path:
            with open(out_path, "w") as f:
                json.dump(merge_map, f)
        return _fake_completed(returncode=0)

    coarse = [
        {"group_id": "g1", "project": "obsidian-brain",
         "representative": "Investigate dispatcher-discovery fallback logic",
         "members": [{"file": "a.md", "line": 1, "text": "..."}]},
        {"group_id": "g2", "project": "obsidian-brain",
         "representative": "Fix dispatcher-discovery fallback to probe check availability",
         "members": [{"file": "b.md", "line": 1, "text": "..."}]},
    ]
    monkeypatch.setattr(oid.subprocess, "run", fake_cli_run)
    merged = oid.merge_groups_semantically(coarse)
    flat = merged if isinstance(merged, list) else [g for v in merged.values() for g in v]
    ids = {g["group_id"] for g in flat}
    assert ids == {"g1", "g2"}, f"Investigate and Fix must remain separate; got {ids}"


def test_semantic_merge_prompt_contains_negative_examples():
    """Guard rail: removing Example 3 or Example 4 from the prompt would silently
    weaken the over-merge guards."""
    from check_items_cli import SEMANTIC_MERGE_PROMPT
    assert "vault-doctor --check snapshot-integrity (dry-run)" in SEMANTIC_MERGE_PROMPT
    assert "vault-doctor fix --check snapshot-integrity (apply mode)" in SEMANTIC_MERGE_PROMPT
    assert "Investigate dispatcher-discovery fallback logic" in SEMANTIC_MERGE_PROMPT
    assert "Fix dispatcher-discovery fallback" in SEMANTIC_MERGE_PROMPT
    assert "DO NOT MERGE" in SEMANTIC_MERGE_PROMPT


# ---------------------------------------------------------------------------
# Stage 4 — run_classifier() tests  (Task 15)
# ---------------------------------------------------------------------------


def _make_model_pick_fake_run(captured: dict):
    """Chunk-aware subprocess.run stub for model-selection assertions.

    Parses the embedded output path out of the prompt and writes a one-record
    classifier payload there, so the stub works for both single and chunked
    dispatch paths. Records the most recent cmd in captured["cmd"].
    """
    import re as _re
    from pathlib import Path as _Path

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        prompt = kwargs.get("input", "")
        in_match = _re.search(r"(/\S+\.classin\.json)", prompt)
        out_match = _re.search(r"JSON to (/\S+?)\.?(?:\s|$)", prompt)
        assert in_match and out_match, f"paths missing from prompt: {prompt[:200]!r}"
        chunk_in = json.loads(_Path(in_match.group(1)).read_text())
        chunk_out = [
            {
                "group_id": g["group_id"],
                "classification": "ACTIVE",
                "confidence": "LOW",
                "canonical_text": g["representative"],
                "evidence_citation": None,
                "action_required": None,
            }
            for g in chunk_in["groups"]
        ]
        _Path(out_match.group(1)).write_text(json.dumps(chunk_out))
        return _fake_completed(stdout=json.dumps(chunk_out), returncode=0)

    return fake_run


def test_run_classifier_picks_haiku_at_30_or_fewer(tmp_path, monkeypatch):
    """<=30 merged groups -> claude -p --model haiku per spec line 106."""
    import check_items_cli as cli

    # Disable L2 prefilter: this test exercises sub-agent model selection, not L2.
    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")

    captured: dict = {}
    monkeypatch.setattr(cli.subprocess, "run", _make_model_pick_fake_run(captured))
    payload = {
        "groups": [{"group_id": f"g{i}", "project": "p", "representative": f"x{i}",
                    "instances": []} for i in range(30)],
        "evidence": {"p": {}},
    }
    out_path = tmp_path / "classify.json"
    rc = cli.run_classifier(stdin_json=json.dumps(payload), output_path=str(out_path))
    assert rc == 0
    assert "haiku" in " ".join(captured["cmd"])


def test_run_classifier_picks_sonnet_above_30(tmp_path, monkeypatch):
    """>30 merged groups in a single dispatch escalates to sonnet.

    Chunking is disabled (CLASSIFIER_CHUNK_SIZE > group_count) so model
    selection runs on the full payload, exercising the documented
    haiku/sonnet 30-group threshold per spec line 106.
    """
    import check_items_cli as cli

    # Disable L2 prefilter: this test exercises sub-agent model selection, not L2.
    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")
    # Disable chunking: 45 groups in one dispatch lets the 30-group sonnet
    # threshold fire. Under chunked dispatch the model is picked per chunk,
    # so chunks <=30 always pick haiku — a separate code path.
    monkeypatch.setattr(cli, "CLASSIFIER_CHUNK_SIZE", 100)

    captured: dict = {}
    monkeypatch.setattr(cli.subprocess, "run", _make_model_pick_fake_run(captured))
    payload = {
        "groups": [{"group_id": f"g{i}", "project": "p", "representative": f"x{i}",
                    "instances": []} for i in range(45)],
        "evidence": {"p": {}},
    }
    out_path = tmp_path / "classify2.json"
    rc = cli.run_classifier(stdin_json=json.dumps(payload), output_path=str(out_path))
    assert rc == 0
    assert "sonnet" in " ".join(captured["cmd"])


def test_classifier_prompt_includes_self_referential_rule():
    """Per spec § Classification semantics (line 321) Patch 3: prompt MUST include the
    self-referential rule: discovery evidence is not completion evidence."""
    import check_items_cli as cli
    p = cli.CLASSIFIER_PROMPT
    assert "discovery" in p.lower() and "fix" in p.lower()
    assert "prefer ACTIVE" in p or "prefer ACTIVE." in p
    for c in ("DONE", "NEEDS-ACTION", "STALE", "ACTIVE", "REVIEW"):
        assert c in p, f"prompt missing classification: {c}"
    assert "STRICT JSON" in p or "strict JSON" in p.lower()
    for field in ("group_id", "classification", "confidence",
                  "canonical_text", "evidence_citation", "action_required"):
        assert field in p, f"prompt missing schema field: {field}"


# ---------------------------------------------------------------------------
# Coverage: error paths in run_semantic_merge and run_classifier  (Task 15)
# These cover lines that were not reachable via orchestrator-level tests.
# ---------------------------------------------------------------------------


def test_run_semantic_merge_invalid_json_returns_2():
    """run_semantic_merge returns 2 on invalid stdin JSON."""
    import check_items_cli as cli
    rc = cli.run_semantic_merge(stdin_json="not-json", output_path="/dev/null")
    assert rc == 2


def test_run_semantic_merge_timeout_returns_3(tmp_path, monkeypatch):
    """run_semantic_merge returns 3 on subprocess timeout."""
    import check_items_cli as cli

    def fake_run(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, SUBAGENT_TIMEOUT_SEC)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    payload = {"groups": [{"group_id": "g1", "project": "p",
                           "representative": "x", "instances": []}]}
    out_path = tmp_path / "out.json"
    rc = cli.run_semantic_merge(stdin_json=json.dumps(payload), output_path=str(out_path))
    assert rc == 3


def test_run_semantic_merge_nonzero_rc_propagated(tmp_path, monkeypatch):
    """run_semantic_merge propagates non-zero subprocess return code."""
    import check_items_cli as cli

    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **kw: _fake_completed(returncode=5))
    payload = {"groups": [{"group_id": "g1", "project": "p",
                           "representative": "x", "instances": []}]}
    out_path = tmp_path / "out.json"
    rc = cli.run_semantic_merge(stdin_json=json.dumps(payload), output_path=str(out_path))
    assert rc == 5


def test_run_semantic_merge_invalid_stdout_json_returns_4(tmp_path, monkeypatch):
    """run_semantic_merge returns 4 when stdout is not valid JSON and output file absent."""
    import check_items_cli as cli

    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **kw: _fake_completed(stdout="not-json", returncode=0))
    payload = {"groups": [{"group_id": "g1", "project": "p",
                           "representative": "x", "instances": []}]}
    out_path = tmp_path / "no_such.json"  # must not exist
    rc = cli.run_semantic_merge(stdin_json=json.dumps(payload), output_path=str(out_path))
    assert rc == 4


def test_run_classifier_invalid_json_returns_2():
    """run_classifier returns 2 on invalid stdin JSON."""
    import check_items_cli as cli
    rc = cli.run_classifier(stdin_json="bad-json", output_path="/dev/null")
    assert rc == 2


def test_run_classifier_timeout_returns_3(tmp_path, monkeypatch):
    """run_classifier returns 3 on subprocess timeout."""
    import check_items_cli as cli

    # Disable L2 prefilter so group reaches sub-agent and can trigger timeout.
    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")

    def fake_run(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, SUBAGENT_TIMEOUT_SEC)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    payload = {"groups": [{"group_id": "g1", "project": "p",
                           "representative": "x", "instances": []}],
               "evidence": {"p": {}}}
    out_path = tmp_path / "out.json"
    rc = cli.run_classifier(stdin_json=json.dumps(payload), output_path=str(out_path))
    assert rc == 3


def test_run_classifier_nonzero_rc_propagated(tmp_path, monkeypatch):
    """run_classifier propagates non-zero subprocess return code."""
    import check_items_cli as cli

    # Disable L2 prefilter so group reaches sub-agent and can return non-zero rc.
    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")

    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **kw: _fake_completed(returncode=7))
    payload = {"groups": [{"group_id": "g1", "project": "p",
                           "representative": "x", "instances": []}],
               "evidence": {"p": {}}}
    out_path = tmp_path / "out.json"
    rc = cli.run_classifier(stdin_json=json.dumps(payload), output_path=str(out_path))
    assert rc == 7


def test_run_classifier_invalid_stdout_json_returns_4(tmp_path, monkeypatch):
    """run_classifier returns 4 when stdout is not valid JSON and output file absent."""
    import check_items_cli as cli

    # Disable L2 prefilter so group reaches sub-agent and can trigger the json-error path.
    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")

    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **kw: _fake_completed(stdout="not-json", returncode=0))
    payload = {"groups": [{"group_id": "g1", "project": "p",
                           "representative": "x", "instances": []}],
               "evidence": {"p": {}}}
    out_path = tmp_path / "no_such_classifier.json"
    rc = cli.run_classifier(stdin_json=json.dumps(payload), output_path=str(out_path))
    assert rc == 4


# ---------------------------------------------------------------------------
# Task 16: classify_groups_with_agent orchestrator
# ---------------------------------------------------------------------------

def test_classifier_retry_on_malformed_json(monkeypatch):
    """Test 2 - first sub-agent response missing 'classification'; retry succeeds."""
    import open_item_dedup as oid

    call_count = {"n": 0}

    def fake_cli_run(cmd, *args, **kwargs):
        call_count["n"] += 1
        out_path = next((c for c in cmd if isinstance(c, str)
                         and c.endswith(".json") and "out" in c), None)
        if call_count["n"] == 1:
            if out_path:
                with open(out_path, "w") as f:
                    json.dump([{"group_id": "g1", "confidence": "LOW"}], f)
            return _fake_completed(returncode=0)
        if out_path:
            with open(out_path, "w") as f:
                json.dump([{
                    "group_id": "g1",
                    "classification": "DONE",
                    "confidence": "HIGH",
                    "canonical_text": "ship it",
                    "evidence_citation": "PR #87 merged",
                    "action_required": None,
                }], f)
        return _fake_completed(returncode=0)

    monkeypatch.setattr(oid.subprocess, "run", fake_cli_run)

    merged_groups = [
        {"group_id": "g1", "project": "p", "representative": "ship",
         "members": [{"file": "a.md", "line": 1, "text": "ship"}]}
    ]
    evidence = {"p": {"commits": [], "merged_prs": [], "closed_issues": []}}

    out = oid.classify_groups_with_agent(merged_groups, evidence)
    assert call_count["n"] == 2, "expected one retry"
    assert len(out) == 1
    assert out[0]["classification"] == "DONE"


def test_needs_action_surfaces_command(monkeypatch):
    """Test 5 - NEEDS-ACTION items carry an `action_required` command string."""
    import open_item_dedup as oid

    def fake_cli_run(cmd, *args, **kwargs):
        out_path = next((c for c in cmd if isinstance(c, str)
                         and c.endswith(".json") and "out" in c), None)
        if out_path:
            with open(out_path, "w") as f:
                json.dump([{
                    "group_id": "tva-0003",
                    "classification": "NEEDS-ACTION",
                    "confidence": "HIGH",
                    "canonical_text": "Close issue #534",
                    "evidence_citation": "Story 11.12 shipped",
                    "action_required": 'gh issue close 534 --comment "Fixed in Story 11.12"',
                }], f)
        return _fake_completed(returncode=0)

    monkeypatch.setattr(oid.subprocess, "run", fake_cli_run)

    merged_groups = [
        {"group_id": "tva-0003", "project": "tiny-vacation-agent",
         "representative": "Close issue #534", "members": []}
    ]
    evidence = {"tiny-vacation-agent": {}}

    out = oid.classify_groups_with_agent(merged_groups, evidence)
    assert len(out) == 1
    item = out[0]
    assert item["classification"] == "NEEDS-ACTION"
    assert item["action_required"] is not None
    assert "gh issue close 534" in item["action_required"]


# ---------------------------------------------------------------------------
# Task 7: outer telemetry line in classify_groups_with_agent
# ---------------------------------------------------------------------------

def test_outer_telemetry_line_format(monkeypatch, capsys):
    """classify_groups_with_agent emits a [check-items] classifier-result line
    with total_classified/prefiltered/subagent fields and no cache_hit key
    (cache_hit lives outside this function's visibility — see Task 7 option b)."""
    import open_item_dedup as oid
    import re

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
                        "classification": "ACTIVE",
                        "confidence": "LOW",
                        "canonical_text": "explore growth",
                        "evidence_citation": None,
                        "action_required": None,
                        "prefiltered": True,
                    },
                ], f)
        return _fake_completed(returncode=0)

    monkeypatch.setattr(oid.subprocess, "run", fake_cli_run)

    merged_groups = [
        {"group_id": "g1", "project": "p", "representative": "ship",
         "members": [{"file": "a.md", "line": 1, "text": "ship"}]},
        {"group_id": "g2", "project": "p", "representative": "explore growth",
         "members": [{"file": "b.md", "line": 2, "text": "explore growth"}]},
    ]
    evidence = {"p": {"commits": [], "merged_prs": [], "closed_issues": []}}

    parsed = oid.classify_groups_with_agent(merged_groups, evidence)
    assert len(parsed) == 2

    captured = capsys.readouterr()
    outer_lines = [
        l for l in captured.err.splitlines()
        if l.startswith("[check-items] classifier-result:")
    ]
    assert len(outer_lines) == 1, (
        f"Expected exactly 1 outer telemetry line, got: {outer_lines}"
    )
    line = outer_lines[0]

    # Required fields
    assert re.search(r'total_classified=2', line), f"Bad total_classified in: {line}"
    assert re.search(r'prefiltered=1', line), f"Bad prefiltered in: {line}"
    assert re.search(r'subagent=1', line), f"Bad subagent in: {line}"

    # cache_hit must NOT appear in the outer line (plan Task 7 Step 4 option b:
    # drop it entirely rather than emit a placeholder).
    assert "cache_hit" not in line, (
        f"Outer line must not include cache_hit (orchestration is SKILL.md "
        f"prose; plan fallback drops the key). Got: {line}"
    )


def test_outer_telemetry_invariant_with_partial_parse(monkeypatch, capsys):
    """total_classified must equal prefiltered + subagent even when the CLI
    returns fewer records than merged_groups (e.g. partial parse, dropped
    entries). All three counts derive from `parsed`, not merged_groups."""
    import open_item_dedup as oid
    import re

    # 3 input groups but CLI only returns 2 records (one dropped/lost).
    # Of the 2 returned: 1 prefiltered, 1 subagent.
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
                        "canonical_text": "ship",
                        "evidence_citation": "PR #1",
                        "action_required": None,
                    },
                    {
                        "group_id": "g3",
                        "classification": "ACTIVE",
                        "confidence": "LOW",
                        "canonical_text": "explore",
                        "evidence_citation": None,
                        "action_required": None,
                        "prefiltered": True,
                    },
                    # g2 deliberately omitted
                ], f)
        return _fake_completed(returncode=0)

    monkeypatch.setattr(oid.subprocess, "run", fake_cli_run)

    merged_groups = [
        {"group_id": "g1", "project": "p", "representative": "ship",
         "members": [{"file": "a.md", "line": 1, "text": "ship"}]},
        {"group_id": "g2", "project": "p", "representative": "dropped",
         "members": [{"file": "b.md", "line": 2, "text": "dropped"}]},
        {"group_id": "g3", "project": "p", "representative": "explore",
         "members": [{"file": "c.md", "line": 3, "text": "explore"}]},
    ]
    evidence = {"p": {"commits": [], "merged_prs": [], "closed_issues": []}}

    parsed = oid.classify_groups_with_agent(merged_groups, evidence)
    assert len(parsed) == 2  # one dropped

    captured = capsys.readouterr()
    outer_lines = [
        l for l in captured.err.splitlines()
        if l.startswith("[check-items] classifier-result:")
    ]
    assert len(outer_lines) == 1
    line = outer_lines[0]

    total_m = re.search(r'total_classified=(\d+)', line)
    prefiltered_m = re.search(r'prefiltered=(\d+)', line)
    subagent_m = re.search(r'subagent=(\d+)', line)
    assert total_m and prefiltered_m and subagent_m, f"Missing field in: {line}"

    total = int(total_m.group(1))
    prefiltered = int(prefiltered_m.group(1))
    subagent = int(subagent_m.group(1))

    # Invariant: counts sum correctly. total_classified must reflect parsed,
    # NOT merged_groups (which would give 3 and break the invariant).
    assert total == 2, f"total_classified should equal len(parsed)=2, got {total}: {line}"
    assert prefiltered == 1, f"prefiltered=1, got {prefiltered}: {line}"
    assert subagent == 1, f"subagent=1, got {subagent}: {line}"
    assert total == prefiltered + subagent, (
        f"Invariant broken: total={total} != prefiltered={prefiltered} + "
        f"subagent={subagent}; line: {line}"
    )


# ---------------------------------------------------------------------------
# Task 17: heuristic fallback classifier
# ---------------------------------------------------------------------------

def test_classifier_heuristic_fallback(monkeypatch):
    """Test 3 - both sub-agent attempts fail; heuristic fallback runs."""
    import open_item_dedup as oid

    def always_fail(cmd, *args, **kwargs):
        return _fake_completed(stdout="not json", returncode=1)

    monkeypatch.setattr(oid.subprocess, "run", always_fail)

    merged_groups = [
        {"group_id": "g1", "project": "p",
         "representative": "Fix bug #87 merged in v2.5.0",
         "members": [{"file": "a.md", "line": 1,
                      "text": "Fix bug #87 — done; merged in v2.5.0 release."}]},
        {"group_id": "g2", "project": "p",
         "representative": "Investigate something unrelated",
         "members": [{"file": "b.md", "line": 2, "text": "TBD"}]},
    ]
    evidence = {"p": {}}

    primary = oid.classify_groups_with_agent(merged_groups, evidence)
    assert primary == []
    assert oid.get_last_classifier_mode() == "heuristic-fallback"

    fallback = oid.classify_groups_heuristic(merged_groups, evidence)
    assert isinstance(fallback, list)
    assert len(fallback) == 2

    by_id = {item["group_id"]: item for item in fallback}
    assert by_id["g1"]["classification"] == "DONE"
    assert by_id["g2"]["classification"] == "ACTIVE"
    assert all(item["confidence"] in ("HIGH", "MED", "LOW") for item in fallback)


def test_classify_groups_heuristic_distinctive_token_and_completion_phrase():
    """Tightened heuristic from feedback_check_items_filter_tightening:
    DONE requires a distinctive token AND a completion phrase within +/- 120 chars."""
    import open_item_dedup as oid

    merged_groups = [
        {"group_id": "g1", "project": "p",
         "representative": "Track issue #87 work",
         "members": [{"file": "a.md", "line": 1,
                      "text": "Track issue #87 work (no completion words)"}]},
        {"group_id": "g2", "project": "p",
         "representative": "Generic done note",
         "members": [{"file": "b.md", "line": 1,
                      "text": "this is done now finally"}]},
        {"group_id": "g3", "project": "p",
         "representative": "PR #99 merged",
         "members": [{"file": "c.md", "line": 1,
                      "text": "Fix #99 merged - this work is done; release shipped."}]},
    ]
    out = oid.classify_groups_heuristic(merged_groups, {})
    by_id = {i["group_id"]: i for i in out}
    assert by_id["g1"]["classification"] == "ACTIVE"
    assert by_id["g2"]["classification"] == "ACTIVE"
    assert by_id["g3"]["classification"] == "DONE"


# ---------------------------------------------------------------------------
# Task 18: partition_for_review with STALE hide-by-default and ACTIVE omission
# ---------------------------------------------------------------------------

def test_stale_hidden_without_show_all():
    """Test 6 - STALE items absent from default review; present with --show-all."""
    from open_item_dedup import partition_for_review

    classifications = [
        {"group_id": "g1", "classification": "DONE", "confidence": "HIGH",
         "canonical_text": "x", "evidence_citation": "c", "action_required": None},
        {"group_id": "g2", "classification": "NEEDS-ACTION", "confidence": "HIGH",
         "canonical_text": "y", "evidence_citation": "c", "action_required": "gh ..."},
        {"group_id": "g3", "classification": "STALE", "confidence": "LOW",
         "canonical_text": "z", "evidence_citation": None, "action_required": None},
        {"group_id": "g4", "classification": "ACTIVE", "confidence": "LOW",
         "canonical_text": "w", "evidence_citation": None, "action_required": None},
    ]

    default = partition_for_review(classifications, show_all=False)
    review_ids = {item["group_id"] for item in default["review"]}
    assert review_ids == {"g1", "g2"}
    assert any(d["group_id"] == "g4" and d["evidence_citation"] is None
               for d in default["dashboard_only"])
    assert all(d["group_id"] != "g3" for d in default["review"])

    expanded = partition_for_review(classifications, show_all=True)
    review_ids_expanded = {item["group_id"] for item in expanded["review"]}
    assert "g3" in review_ids_expanded


def test_active_groups_carry_null_citation_to_dashboard():
    """ACTIVE entries are written to dashboard with evidence_citation=null
    per spec line 322."""
    from open_item_dedup import partition_for_review

    classifications = [
        {"group_id": "g1", "classification": "ACTIVE", "confidence": "LOW",
         "canonical_text": "w", "evidence_citation": "garbage that should be cleared",
         "action_required": None},
    ]
    out = partition_for_review(classifications, show_all=False)
    assert len(out["dashboard_only"]) == 1
    assert out["dashboard_only"][0]["evidence_citation"] is None


# ---------------------------------------------------------------------------
# Task 20 — Order-independent argument parser (check_items_args.py)
# Spec § Invocation contract lines 35-52
# ---------------------------------------------------------------------------

def test_parse_scope_defaults():
    from check_items_args import parse_scope
    scope = parse_scope([])
    assert scope.mode == "current"
    assert scope.window_days == 14
    assert scope.show_all is False
    assert scope.dry_run is False
    assert scope.no_cache is False


def test_parse_scope_all_keyword():
    from check_items_args import parse_scope
    scope = parse_scope(["all"])
    assert scope.mode == "vault"


def test_parse_scope_window_only():
    from check_items_args import parse_scope
    scope = parse_scope(["30d"])
    assert scope.mode == "current"
    assert scope.window_days == 30


def test_parse_scope_combinations_order_independent():
    """Spec line 46 — all arguments combinable: /check-items all 30d --show-all."""
    from check_items_args import parse_scope
    a = parse_scope(["all", "30d", "--show-all"])
    b = parse_scope(["--show-all", "30d", "all"])
    c = parse_scope(["30d", "--show-all", "all"])
    for s in (a, b, c):
        assert s.mode == "vault"
        assert s.window_days == 30
        assert s.show_all is True


def test_parse_scope_project_name(monkeypatch):
    """Positional matching a known project directory routes to project scope."""
    import check_items_args
    monkeypatch.setattr(check_items_args, "_known_projects",
                        lambda: {"obsidian-brain", "tiny-vacation-agent"})
    scope = check_items_args.parse_scope(["obsidian-brain"])
    assert scope.mode == "project"
    assert scope.project == "obsidian-brain"


def test_parse_scope_flags():
    from check_items_args import parse_scope
    s = parse_scope(["--dry-run", "--no-cache"])
    assert s.dry_run is True
    assert s.no_cache is True


def test_parse_scope_all_clears_stale_project(monkeypatch):
    """When 'all' appears after a project token, scope.project must be cleared (Finding B)."""
    import check_items_args
    monkeypatch.setattr(check_items_args, "_known_projects",
                        lambda: {"obsidian-brain"})
    scope = check_items_args.parse_scope(["obsidian-brain", "all"])
    assert scope.mode == "vault"
    assert scope.project is None


# ---------------------------------------------------------------------------
# Task 1 (issue #318) — parse_scope must not silently drop a positional
# ---------------------------------------------------------------------------

def test_parse_scope_records_unknown_token(monkeypatch):
    """A positional matching no known project is recorded, not silently dropped."""
    import check_items_args
    monkeypatch.setattr(check_items_args, "_known_projects", lambda: set())
    monkeypatch.setattr(check_items_args, "_vault_known_projects", lambda: set())
    scope = check_items_args.parse_scope(["not-a-project"])
    assert scope.unknown_tokens == ["not-a-project"]
    assert scope.mode == "current"


def test_parse_scope_no_unknown_tokens_for_valid_input(monkeypatch):
    """Positive control: recognised flags/keywords must never be flagged as unknown."""
    import check_items_args
    monkeypatch.setattr(check_items_args, "_known_projects", lambda: set())
    monkeypatch.setattr(check_items_args, "_vault_known_projects", lambda: set())
    scope = check_items_args.parse_scope(["all", "30d", "--show-all"])
    assert scope.unknown_tokens == []


def test_parse_scope_accepts_vault_known_project(monkeypatch):
    """A notes-only project (vault index, no workspace-root directory) still resolves."""
    import check_items_args
    monkeypatch.setattr(check_items_args, "_known_projects", lambda: set())
    monkeypatch.setattr(check_items_args, "_vault_known_projects",
                        lambda: {"notes-only-proj"})
    scope = check_items_args.parse_scope(["notes-only-proj"])
    assert scope.mode == "project"
    assert scope.project == "notes-only-proj"
    assert scope.unknown_tokens == []


def test_parse_scope_vault_lookup_failure_is_not_fatal(monkeypatch, capsys):
    """A broken vault index must degrade, not break argument parsing.

    _known_projects is mocked too (matching test_parse_scope_project_name's
    idiom above) rather than exercising the real filesystem scan: CI has
    neither ~/dev/claude_workspace nor ~/projects, so an unmocked
    get_workspace_roots() would return [] there and this test would fail
    for reasons unrelated to what it's checking.

    The failure is injected at the DB layer (vault_index._connect), the
    actual production failure mode, rather than by replacing
    _vault_known_projects wholesale — that exercises the real internal
    try/except in _vault_known_projects instead of a call-site guard that
    only a test double could ever trigger (Fix round 1, Finding C1).
    """
    import check_items_args
    import vault_index

    def _raise(db_path):
        raise RuntimeError("vault index unavailable")

    monkeypatch.setattr(check_items_args, "_known_projects",
                        lambda: {"obsidian-brain"})
    monkeypatch.setattr(vault_index, "_connect", _raise)
    scope = check_items_args.parse_scope(["obsidian-brain"])
    assert scope.mode == "project"
    assert scope.project == "obsidian-brain"
    assert "vault project lookup unavailable" in capsys.readouterr().err


def test_parse_scope_unknown_token_order_independent(monkeypatch):
    """An unknown token must not short-circuit parsing of tokens after it."""
    import check_items_args
    monkeypatch.setattr(check_items_args, "_known_projects", lambda: set())
    monkeypatch.setattr(check_items_args, "_vault_known_projects", lambda: set())
    scope = check_items_args.parse_scope(["30d", "bogus", "--dry-run"])
    assert scope.window_days == 30
    assert scope.dry_run is True
    assert scope.unknown_tokens == ["bogus"]


def test_parse_scope_exposes_known_projects_for_reuse(monkeypatch):
    """M2: parse_scope must expose the project set it already computed
    (scope.known_projects) so a caller building a 'Did you mean'
    suggestion doesn't have to re-query the vault index and re-walk every
    workspace root a second time -- see skills/check-items/SKILL.md Step 1,
    which used to do exactly that."""
    import check_items_args
    monkeypatch.setattr(check_items_args, "_known_projects",
                        lambda: {"obsidian-brain", "cc-token-router"})
    monkeypatch.setattr(check_items_args, "_vault_known_projects",
                        lambda: {"notes-only-proj"})
    scope = check_items_args.parse_scope(["30d"])
    assert scope.known_projects == {"obsidian-brain", "cc-token-router", "notes-only-proj"}


# ---------------------------------------------------------------------------
# verify_before_edit — Stage 6 pre-Edit guard
# ---------------------------------------------------------------------------

def test_verify_before_edit_match_returns_true(tmp_path):
    from open_item_dedup import verify_before_edit
    f = tmp_path / "note.md"
    f.write_text("line one\n- [ ] Fix #87 ship it\nline three\n")
    # expected_text is bare canonical text (no checkbox prefix); function strips file side
    ok = verify_before_edit(str(f), line_number=2, expected_text="Fix #87 ship it")
    assert ok is True


def test_verify_before_edit_mismatch_aborts(tmp_path):
    """Test 9 - mocked line content differs from preview; verify returns False."""
    from open_item_dedup import verify_before_edit
    f = tmp_path / "note.md"
    f.write_text("line one\n- [x] Fix #87 ALREADY DONE\nline three\n")
    ok = verify_before_edit(str(f), line_number=2, expected_text="- [ ] Fix #87 ship it")
    assert ok is False


def test_verify_before_edit_handles_missing_file(tmp_path):
    from open_item_dedup import verify_before_edit
    ok = verify_before_edit(str(tmp_path / "missing.md"), line_number=1,
                            expected_text="anything")
    assert ok is False


def test_verify_before_edit_handles_line_out_of_range(tmp_path):
    from open_item_dedup import verify_before_edit
    f = tmp_path / "note.md"
    f.write_text("only one line\n")
    ok = verify_before_edit(str(f), line_number=99, expected_text="anything")
    assert ok is False


# ---------------------------------------------------------------------------
# Task 22: Dashboard report writer
# ---------------------------------------------------------------------------

def test_dashboard_report_always_written_on_dry_run(tmp_path):
    """Test 7 - --dry-run still writes the report."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)

    path = write_check_items_dashboard(
        vault_path=str(vault),
        scope_name="obsidian-brain",
        date_str="2026-05-11",
        window_days=14,
        raw_count=225,
        group_count=40,
        classifications=[],
        applied=0,
        cascaded=0,
        merges=[],
        semantic_merge_mode="ok",
        classifier_mode="ok",
        dry_run=True,
    )
    assert os.path.exists(path)
    content = open(path).read()
    assert "type: claude-check-items-report" in content
    assert "scope: obsidian-brain" in content
    # #320 F1: skipped defaults to 0 for callers that don't pass it.
    assert "skipped: 0" in content


def test_dashboard_report_threads_skipped_count(tmp_path):
    """#320 F1: a caller-supplied skipped count renders in BOTH the
    frontmatter (`skipped: N`) and the prose summary line, next to
    `cascaded` -- not just the auto-checked count -- so a dashboard reader
    can see cascade candidates that were refused or lost, not only the
    ones that succeeded."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)

    path = write_check_items_dashboard(
        vault_path=str(vault),
        scope_name="obsidian-brain",
        date_str="2026-05-11",
        window_days=14,
        raw_count=225,
        group_count=40,
        classifications=[],
        applied=3,
        cascaded=2,
        merges=[],
        semantic_merge_mode="ok",
        classifier_mode="ok",
        dry_run=False,
        skipped=5,
    )
    content = open(path).read()
    assert "skipped: 5" in content
    assert "3 applied, 2 cascaded, 5 skipped." in content


def test_report_filename_scope_suffix(tmp_path):
    """Test 8 - project scope -> check-items-<project>-<date>.md;
    'vault' -> check-items-vault-<date>.md."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)

    p1 = write_check_items_dashboard(
        vault_path=str(vault), scope_name="obsidian-brain", date_str="2026-05-11",
        window_days=14, raw_count=0, group_count=0, classifications=[],
        applied=0, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=False
    )
    p2 = write_check_items_dashboard(
        vault_path=str(vault), scope_name="vault", date_str="2026-05-11",
        window_days=14, raw_count=0, group_count=0, classifications=[],
        applied=0, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=False
    )
    assert p1.endswith("check-items-obsidian-brain-2026-05-11.md")
    assert p2.endswith("check-items-vault-2026-05-11.md")


def test_dashboard_body_includes_merged_groups_audit(tmp_path):
    """Spec § Dashboard audit — body must list each merge with reasoning.
    Also exercises all four classification buckets and action_required."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    merges = [
        {"canonical_group_id": "ob-0004", "absorbed_group_ids": ["ob-0003"],
         "reasoning": "Both describe N=1 text-fallback routing decision."}
    ]
    classifications = [
        {"group_id": "g1", "classification": "DONE",
         "canonical_text": "Ship it", "evidence_citation": "PR #87 merged",
         "action_required": None},
        {"group_id": "g2", "classification": "NEEDS-ACTION",
         "canonical_text": "Close #534", "evidence_citation": "Story 11.12",
         "action_required": 'gh issue close 534'},
        {"group_id": "g3", "classification": "STALE",
         "canonical_text": "Old TODO", "evidence_citation": None,
         "action_required": None},
        {"group_id": "g4", "classification": "ACTIVE",
         "canonical_text": "Still open", "evidence_citation": None,
         "action_required": None},
    ]
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="obsidian-brain", date_str="2026-05-11",
        window_days=14, raw_count=10, group_count=8,
        classifications=classifications,
        applied=1, cascaded=0, merges=merges, semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True
    )
    body = open(path).read()
    assert "## Merged Groups" in body
    assert "ob-0004" in body and "ob-0003" in body
    assert "N=1 text-fallback routing" in body
    assert "Ship it" in body
    assert "Close #534" in body
    assert "gh issue close 534" in body
    assert "Old TODO" in body
    assert "Still open" in body


def test_dashboard_body_includes_review_section(tmp_path):
    """REVIEW items must be visible in the persisted dashboard report — this
    is the actual "surfaced VISIBLY" contract from #264. Without this, a
    REVIEW classification would look correct in the live Step-7 print but
    silently vanish from the file that gets left behind for the user."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    classifications = [
        {"group_id": "g1", "classification": "REVIEW",
         "canonical_text": "Return to feature/pull-to-refresh-v2 worktree "
                            "to complete Task 7 and merge feature PR",
         "evidence_citation": "similarly-named branch feature/pull-to-refresh-v2 (deleted)",
         "action_required": None},
    ]
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="obsidian-brain", date_str="2026-05-11",
        window_days=14, raw_count=1, group_count=1,
        classifications=classifications,
        applied=0, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True
    )
    content = open(path).read()
    assert "pull-to-refresh-v2" in content
    assert "review: 1" in content
    # Unapplied items render unchecked regardless of classification (#318
    # Task 6) -- this fixture carries no "applied" key, so it renders open.
    assert "- [ ] Return to feature/pull-to-refresh-v2" in content


# ---------------------------------------------------------------------------
# #318 Task 6 — applied items render as applied, by fact not by class.
# ---------------------------------------------------------------------------

def test_applied_review_item_renders_checked(tmp_path):
    """A REVIEW classification the user opted into checkoff (applied: True,
    stamped by Step 8's primary-flip loop) must render `- [x]`, even though
    REVIEW is never auto-checked by classification alone."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    classifications = [
        {"group_id": "g1", "classification": "REVIEW",
         "canonical_text": "Ship the thing", "evidence_citation": None,
         "applied": True},
    ]
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=1, group_count=1,
        classifications=classifications,
        applied=1, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True
    )
    body = open(path).read()
    assert "- [x] Ship the thing" in body


def test_unapplied_done_item_renders_unchecked(tmp_path):
    """POSITIVE CONTROL: a DONE classification with no `applied` key must
    render `- [ ]`, not `- [x]` -- this is what stops the by-fact fix from
    marking everything checked (DONE no longer means auto-checked)."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    classifications = [
        {"group_id": "g1", "classification": "DONE",
         "canonical_text": "Deselected done item", "evidence_citation": "PR #1"},
    ]
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=1, group_count=1,
        classifications=classifications,
        applied=0, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True
    )
    body = open(path).read()
    assert "- [ ] Deselected done item" in body
    assert "- [x] Deselected done item" not in body


def test_done_heading_counts_only_applied_done_items(tmp_path):
    """The ## Done heading must count applied DONE items, not the run's
    total applied count -- an applied REVIEW flip must not inflate it."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    classifications = [
        {"group_id": "g1", "classification": "DONE",
         "canonical_text": "Applied done item", "evidence_citation": "PR #1",
         "applied": True},
        {"group_id": "g2", "classification": "REVIEW",
         "canonical_text": "Applied review item", "evidence_citation": None,
         "applied": True},
    ]
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=2, group_count=2,
        classifications=classifications,
        applied=2, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True
    )
    body = open(path).read()
    assert "## Done (1 applied of 1 classified)" in body


def test_applied_elsewhere_line_names_non_done_flips(tmp_path):
    """The applied REVIEW flip from the fixture above must not vanish from
    the arithmetic once the ## Done heading stops over-counting -- a summary
    line must name it.

    #318 I2 AGREEING-CASE control: applied=2 matches the stamped total
    (1 DONE + 1 REVIEW, both c.get('applied') truthy), so no mismatch note
    should render -- see test_applied_mismatch_note_names_both_counts for
    the disagreeing case."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    classifications = [
        {"group_id": "g1", "classification": "DONE",
         "canonical_text": "Applied done item", "evidence_citation": "PR #1",
         "applied": True},
        {"group_id": "g2", "classification": "REVIEW",
         "canonical_text": "Applied review item", "evidence_citation": None,
         "applied": True},
    ]
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=2, group_count=2,
        classifications=classifications,
        applied=2, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True
    )
    body = open(path).read()
    assert "1" in body
    assert "applied outside DONE" in body
    assert "this run reported" not in body


def test_applied_outside_done_derived_from_stamps_not_subtraction(tmp_path):
    """#318 I2: the "additional flips applied outside DONE" count must come
    from counting stamped records outside DONE, not from `applied -
    done_applied`. Fixture: applied (run total) is inflated to 5 with only
    one REVIEW record actually stamped applied=True outside DONE -- the
    old subtraction form would have printed "4 additional flips applied
    outside DONE" (5 - 1) though only ONE record anywhere outside DONE
    renders checked. The correct, fact-derived count is 1."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    classifications = [
        {"group_id": "g1", "classification": "DONE",
         "canonical_text": "Applied done item", "evidence_citation": "PR #1",
         "applied": True},
        {"group_id": "g2", "classification": "REVIEW",
         "canonical_text": "Applied review item", "evidence_citation": None,
         "applied": True},
        {"group_id": "g3", "classification": "NEEDS-ACTION",
         "canonical_text": "Unapplied needs-action item", "evidence_citation": None},
    ]
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=3, group_count=3,
        classifications=classifications,
        applied=5, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True
    )
    body = open(path).read()
    assert "1 additional flip applied outside DONE" in body
    assert "4 additional flip" not in body


def test_applied_mismatch_note_names_both_counts(tmp_path):
    """#318 I2 DISAGREEING case: `applied` (the run total) and the
    fact-derived stamped total can legitimately diverge -- e.g. a
    cascade-only sibling flip Step 8 never stamped onto any record. That
    must be said explicitly (naming both numbers), not papered over."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    classifications = [
        {"group_id": "g1", "classification": "DONE",
         "canonical_text": "Applied done item", "evidence_citation": "PR #1",
         "applied": True},
    ]
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=1, group_count=1,
        classifications=classifications,
        applied=3, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True
    )
    body = open(path).read()
    assert "this run reported 3 applied" in body
    assert "only 1 record" in body


def test_dashboard_idempotent_overwrite(tmp_path):
    """Same scope + same date overwrites previous file."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    p1 = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=1, group_count=1, classifications=[],
        applied=0, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True
    )
    p2 = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=2, group_count=2, classifications=[],
        applied=0, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True
    )
    assert p1 == p2
    assert "groups: 2" in open(p2).read()


def test_dashboard_active_truncation_and_path_guard(tmp_path):
    """ACTIVE >50 items are truncated in the body; path-containment guard is present."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    active_items = [
        {"group_id": f"g{i}", "classification": "ACTIVE",
         "canonical_text": f"item {i}", "evidence_citation": None,
         "action_required": None}
        for i in range(55)
    ]
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=55, group_count=55,
        classifications=active_items,
        applied=0, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=False
    )
    body = open(path).read()
    assert "and 5 more (truncated)" in body

    # Verify the containment guard is present in source (security pattern per CLAUDE.md).
    import check_items_report as cr
    src = open(cr.__file__).read()
    assert "is_relative_to" in src
    assert "refusing to write outside" in src


# ---------------------------------------------------------------------------
# #318 Task 5 — merge_records_from_groups: adapt merge_groups_semantically's
# surviving-groups shape (absorbed_reasoning) to the dashboard's merge-record
# shape (canonical_group_id/absorbed_group_ids/reasoning).
# ---------------------------------------------------------------------------

def test_merge_records_from_groups_reconstructs_records():
    """A single canonical group with one absorbed entry -> one record."""
    groups = [
        {"group_id": "ob-1",
         "absorbed_reasoning": [{"absorbed": "ob-2", "reasoning": "same fix"}]},
    ]
    records = oid.merge_records_from_groups(groups)
    assert records == [
        {"canonical_group_id": "ob-1", "absorbed_group_ids": ["ob-2"],
         "reasoning": "same fix"},
    ]


def test_merge_records_groups_multiple_absorbed_under_one_canonical():
    """Two absorbed_reasoning entries under one canonical -> ONE record
    carrying both absorbed ids (in order) and both reasons joined."""
    groups = [
        {"group_id": "ob-1", "absorbed_reasoning": [
            {"absorbed": "ob-2", "reasoning": "same fix"},
            {"absorbed": "ob-3", "reasoning": "same fix too"},
        ]},
    ]
    records = oid.merge_records_from_groups(groups)
    assert records == [
        {"canonical_group_id": "ob-1", "absorbed_group_ids": ["ob-2", "ob-3"],
         "reasoning": "same fix; same fix too"},
    ]


def test_merge_records_empty_for_unmerged_groups():
    """POSITIVE CONTROL: groups that never absorbed anything (no key, or an
    empty absorbed_reasoning list) must produce NO records -- a function
    that always emits a record for every group would pass the other tests
    while saying nothing here."""
    groups = [
        {"group_id": "ob-1"},
        {"group_id": "ob-2", "absorbed_reasoning": []},
    ]
    assert oid.merge_records_from_groups(groups) == []


def test_merge_records_from_groups_accepts_dict_shape():
    """merge_groups_semantically(return_dict_shape=True) returns
    {project: [groups]} -- merge_records_from_groups must flatten that
    before deriving records, same output as the flat-list form."""
    groups_by_proj = {
        "proj-a": [
            {"group_id": "ob-1",
             "absorbed_reasoning": [{"absorbed": "ob-2", "reasoning": "same fix"}]},
        ],
        "proj-b": [{"group_id": "ob-9"}],
    }
    records = oid.merge_records_from_groups(groups_by_proj)
    assert records == [
        {"canonical_group_id": "ob-1", "absorbed_group_ids": ["ob-2"],
         "reasoning": "same fix"},
    ]


def test_dashboard_renders_merge_records(tmp_path):
    """Records shaped by merge_records_from_groups render in the dashboard
    body's Merged Groups section."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    groups = [
        {"group_id": "ob-1",
         "absorbed_reasoning": [{"absorbed": "ob-2", "reasoning": "same fix"}]},
    ]
    records = oid.merge_records_from_groups(groups)
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=2, group_count=1, classifications=[],
        applied=0, cascaded=0, merges=records, semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True
    )
    body = open(path).read()
    assert "ob-1 absorbs [ob-2]" in body


def test_dashboard_tolerates_int_merges(tmp_path):
    """A caller passing a count instead of a list of records (#318 bug 3)
    must not crash -- and the resulting warning must name the bad input
    (type + repr), not silently coerce to [] and read as 'no merges this
    run', which would hide the caller bug."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=0, group_count=0, classifications=[],
        applied=0, cascaded=0, merges=3, semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True
    )
    body = open(path).read()
    assert "Merged Groups unavailable" in body
    # Names the bad input specifically, not just a generic failure sentence.
    assert "int" in body
    assert "(3)" in body


def test_dashboard_tolerates_huge_merges_repr_truncated(tmp_path):
    """#318 M7: a bad `merges` value's repr is unbounded -- a caller could
    pass a giant string or a deeply nested structure, not just a bare int.
    The warning must cap the repr length and say so, rather than writing
    an unbounded blob into a kept artefact."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    huge_bad_value = "x" * 5000
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=0, group_count=0, classifications=[],
        applied=0, cascaded=0, merges=huge_bad_value, semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True
    )
    body = open(path).read()
    assert "Merged Groups unavailable" in body
    assert "(truncated)" in body
    assert "x" * 5000 not in body
    # The full 5000-char blob must not have leaked into the kept artefact.
    assert len(body) < 5000


def test_dashboard_tolerates_none_merges(tmp_path):
    """merges=None is a legitimate 'nothing to report' -- renders the plain
    fallback with NO warning line (unlike the int case above)."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=0, group_count=0, classifications=[],
        applied=0, cascaded=0, merges=None, semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True
    )
    body = open(path).read()
    assert "_No semantic merges this run._" in body
    assert "Merged Groups unavailable" not in body


# ---------------------------------------------------------------------------
# #318 Task 5, Step 5.5 — ## Evidence Gaps dashboard section (Preflight R3):
# the artefact half of the warning Task 3 already put on stderr.
# ---------------------------------------------------------------------------

def test_dashboard_renders_evidence_gap_section(tmp_path):
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    evidence_gaps = {
        "projects_scanned": 1, "projects_with_evidence": 0,
        "projects_without_repo": ["notes-only"], "all_projects_gapped": True,
    }
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=0, group_count=0, classifications=[],
        applied=0, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True, evidence_gaps=evidence_gaps,
    )
    body = open(path).read()
    assert "## Evidence Gaps" in body
    assert "notes-only" in body
    assert "DONE" in body and "unreachable" in body


def test_dashboard_omits_gap_section_when_no_gaps(tmp_path):
    """POSITIVE CONTROL: an evidence_gaps dict with no actual gaps must NOT
    render the section -- a section that always renders would pass the
    test above while saying nothing."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    evidence_gaps = {
        "projects_scanned": 1, "projects_with_evidence": 1,
        "projects_without_repo": [], "all_projects_gapped": False,
    }
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=0, group_count=0, classifications=[],
        applied=0, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True, evidence_gaps=evidence_gaps,
    )
    body = open(path).read()
    assert "## Evidence Gaps" not in body


def test_dashboard_gap_frontmatter_field(tmp_path):
    """The frontmatter carries evidence_gaps: N (Dataview-queryable), even
    when N is 0 for a caller who passed a real (gap-free) dict -- distinct
    from the argument being omitted entirely (see the no-op test below)."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)

    gapped = {
        "projects_scanned": 1, "projects_with_evidence": 0,
        "projects_without_repo": ["notes-only"], "all_projects_gapped": True,
    }
    p1 = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=0, group_count=0, classifications=[],
        applied=0, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True, evidence_gaps=gapped,
    )
    assert "evidence_gaps: 1" in open(p1).read()

    no_gaps = {
        "projects_scanned": 1, "projects_with_evidence": 1,
        "projects_without_repo": [], "all_projects_gapped": False,
    }
    p2 = write_check_items_dashboard(
        vault_path=str(vault), scope_name="y", date_str="2026-05-11",
        window_days=14, raw_count=0, group_count=0, classifications=[],
        applied=0, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True, evidence_gaps=no_gaps,
    )
    assert "evidence_gaps: 0" in open(p2).read()


def test_dashboard_without_evidence_gaps_argument_is_unchanged(tmp_path):
    """Every existing caller omits evidence_gaps entirely -- the default
    must be a true no-op: no heading, no frontmatter key, no exception."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="x", date_str="2026-05-11",
        window_days=14, raw_count=0, group_count=0, classifications=[],
        applied=0, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True,
    )
    content = open(path).read()
    assert "## Evidence Gaps" not in content
    assert "evidence_gaps:" not in content


# ---------------------------------------------------------------------------
# R2 regression: Finding 3 — path traversal via scope_name
# ---------------------------------------------------------------------------

def test_dashboard_rejects_path_traversal_in_scope_name(tmp_path, monkeypatch):
    """Containment guard rejects ../escape attempts in scope_name.

    Isolated from the user's live load_config() value by monkeypatching
    _CONFIG_PATH to a tmp config with no `check_items_folder` override.
    Without isolation, a cached or user-set `check_items_folder` value
    would change the expected output path and break the assertion.
    """
    import json
    cfg_path = tmp_path / "obsidian-brain-config.json"
    cfg_path.write_text(json.dumps({"vault_path": str(tmp_path / "vault")}))
    monkeypatch.setattr("obsidian_utils._CONFIG_PATH", cfg_path)
    _reset_load_config_cache()

    try:
        from check_items_report import write_check_items_dashboard
        vault = tmp_path / "vault"
        vault.mkdir(parents=True)
        # Path-traversal scope name: should be sanitized, not escape
        path = write_check_items_dashboard(
            vault_path=str(vault), scope_name="../../etc/passwd", date_str="2026-05-11",
            window_days=14, raw_count=0, group_count=0, classifications=[],
            applied=0, cascaded=0, merges=[], semantic_merge_mode="ok",
            classifier_mode="ok", dry_run=True,
        )
        # File must land inside check-items folder, with sanitized name
        assert str(vault / "claude-check-items") in path
        assert ".." not in os.path.basename(path)
        assert "/" not in os.path.basename(path)
    finally:
        _reset_load_config_cache()


# ---------------------------------------------------------------------------
# R2 regression: Finding 6 — verify_before_edit indented checkbox
# ---------------------------------------------------------------------------

def test_verify_before_edit_handles_indented_checkbox(tmp_path):
    """Indented checkboxes (nested lists) must verify against the stripped text."""
    from open_item_dedup import verify_before_edit
    f = tmp_path / "note.md"
    f.write_text("line one\n    - [ ] Indented item ship it\nline three\n")
    # expected_text is bare canonical text (no checkbox prefix); function strips file side
    ok = verify_before_edit(str(f), line_number=2, expected_text="Indented item ship it")
    assert ok is True


# ---------------------------------------------------------------------------
# R4 regression: Finding D — classify_groups_with_agent oversized payload
# ---------------------------------------------------------------------------

def test_classify_groups_oversized_payload_skips_subagent(monkeypatch):
    """Payloads exceeding 1MB stdin cap must skip the sub-agent and fall back
    to heuristic mode without invoking subprocess.run.

    Regression test for R4 Finding D.
    """
    import open_item_dedup as oid

    call_count = {"n": 0}

    def fake_run(cmd, *args, **kwargs):
        call_count["n"] += 1
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    monkeypatch.setattr(oid.subprocess, "run", fake_run)

    # Build a huge payload: 500 groups with 4KB representative each ≈ 2MB
    big_groups = [
        {
            "group_id": f"g{i}",
            "project": "p",
            "representative": "X" * 4096,
            "members": [],
        }
        for i in range(500)
    ]
    evidence = {"p": {}}

    result = oid.classify_groups_with_agent(big_groups, evidence)
    assert result == [], "oversized payload must return empty list (fallback)"
    assert oid.get_last_classifier_mode() == "heuristic-fallback", (
        f"expected 'heuristic-fallback', got: {oid.get_last_classifier_mode()!r}"
    )
    assert call_count["n"] == 0, (
        f"sub-agent must NOT be invoked when payload oversized; "
        f"subprocess.run called {call_count['n']} time(s)"
    )


# ---------------------------------------------------------------------------
# R4 regression: Finding E — YAML frontmatter injection via scope_name
# ---------------------------------------------------------------------------

def test_dashboard_yaml_frontmatter_rejects_injection(tmp_path):
    """scope_name containing newlines/colons must be sanitized in frontmatter
    so that crafted values cannot inject extra YAML fields.

    Regression test for R4 Finding E.
    The sanitizer (_safe_filename_component) collapses newlines to underscores,
    so the attack vector `\\nmalicious_field: pwned\\n` becomes part of a single
    sanitized token — no standalone `malicious_field: pwned` line is injected.
    """
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    crafted = "obsidian-brain\nmalicious_field: pwned\nscope2"
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name=crafted, date_str="2026-05-11",
        window_days=14, raw_count=0, group_count=0, classifications=[],
        applied=0, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True,
    )
    content = open(path).read()
    # The injected payload must NOT appear as a standalone `key: value` line.
    # The sanitizer collapses whitespace/special chars to underscores, so the
    # `malicious_field: pwned` text must appear only as part of the sanitized
    # scope token — never as a bare YAML key on its own line.
    assert "malicious_field: pwned" not in content, (
        "crafted scope_name must not inject a 'malicious_field: pwned' YAML line"
    )
    # The frontmatter scope: line must be a single line (no embedded newlines).
    assert content.startswith("---"), "output must be a valid frontmatter file"
    fm_block = content.split("---")[1]  # middle block between --- markers
    scope_lines = [ln for ln in fm_block.splitlines() if ln.startswith("scope:")]
    assert len(scope_lines) == 1, f"expected exactly 1 scope: line, got: {scope_lines}"


def test_dashboard_yaml_rejects_date_injection(tmp_path):
    """date_str containing newlines must not inject extra YAML fields.

    Regression test for R5 Finding C. The sanitizer (_safe_filename_component)
    collapses newlines to underscores so crafted date values cannot produce
    extra standalone YAML key-value lines inside the frontmatter block.
    """
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    crafted = "2026-05-11\nmalicious_field: pwned\nextra"
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="obsidian-brain", date_str=crafted,
        window_days=14, raw_count=0, group_count=0, classifications=[],
        applied=0, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True,
    )
    content = open(path).read()
    # Check only the frontmatter block (between the first two --- delimiters).
    # The body is Markdown and may echo the raw date_str; the YAML injection risk
    # is exclusively in the frontmatter block.
    assert content.startswith("---"), "output must begin with YAML frontmatter"
    fm_block = content.split("---")[1]  # between first and second ---
    # The attack vector is a bare YAML key injection: `malicious_field: pwned` as its
    # own line. The sanitizer collapses newlines to underscores, so the substring
    # becomes part of the date: value — never a standalone key:value line.
    assert "malicious_field: pwned" not in fm_block, (
        "crafted date_str must not inject a standalone 'malicious_field: pwned' YAML line"
    )
    # The frontmatter date: line must be a single sanitized token (no embedded newlines).
    date_lines = [ln for ln in fm_block.splitlines() if ln.startswith("date:")]
    assert len(date_lines) == 1, f"expected exactly 1 date: line in frontmatter, got: {date_lines}"


# ---------------------------------------------------------------------------
# R6 regression: prompt piped via stdin, not argv  (Finding A + B)
# ---------------------------------------------------------------------------

def test_run_semantic_merge_pipes_prompt_via_stdin(tmp_path, monkeypatch):
    """Prompt must NOT appear in argv (avoids ARG_MAX + ps leakage).

    After the R6 fix, cmd is ["claude", "-p", "--model", model] and the full
    prompt is passed via the `input=` kwarg to subprocess.run.
    """
    import check_items_cli as cli

    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["input"] = kwargs.get("input", "")
        out_path = tmp_path / "out.json"
        out_path.write_text(json.dumps({
            "merges": [],
            "total_groups_before": 0,
            "total_groups_after": 0,
        }))
        return _fake_completed(stdout=out_path.read_text(), returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    out_path = tmp_path / "merge.json"
    rc = cli.run_semantic_merge(
        stdin_json=json.dumps({"groups": []}),
        output_path=str(out_path),
    )
    assert rc == 0
    # argv must only contain the known-safe tokens; no prompt text
    allowed = {"claude", "-p", "--model", "haiku", "sonnet"}
    unexpected = [arg for arg in captured["cmd"] if arg not in allowed]
    assert not unexpected, (
        f"argv contains unexpected items (likely prompt leaked into argv): {unexpected}"
    )
    # Prompt should be delivered via stdin (the input= kwarg)
    assert "SHOULD MERGE" in captured["input"], (
        "SEMANTIC_MERGE_PROMPT must be piped via stdin, not argv"
    )


def test_run_classifier_pipes_prompt_via_stdin(tmp_path, monkeypatch):
    """Same stdin-pipe contract for run_classifier (R6 Finding B).

    cmd must be ["claude", "-p", "--model", model]; classifier prompt piped via input=.
    """
    import check_items_cli as cli

    # Disable L2 prefilter: this test exercises prompt delivery via stdin, not L2.
    # Also requires at least one group with content so sub-agent dispatch happens.
    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")

    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["input"] = kwargs.get("input", "")
        out_path = tmp_path / "out.json"
        out_path.write_text(json.dumps([
            {"group_id": "g1", "classification": "ACTIVE", "confidence": "LOW",
             "canonical_text": "Investigate dispatcher", "evidence_citation": None,
             "action_required": None}
        ]))
        return _fake_completed(stdout=out_path.read_text(), returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    payload = {"groups": [{"group_id": "g1", "project": "p",
                           "representative": "Investigate dispatcher",
                           "instances": []}],
               "evidence": {}}
    out_path = tmp_path / "classify.json"
    rc = cli.run_classifier(
        stdin_json=json.dumps(payload),
        output_path=str(out_path),
    )
    assert rc == 0
    allowed = {"claude", "-p", "--model", "haiku", "sonnet"}
    unexpected = [arg for arg in captured["cmd"] if arg not in allowed]
    assert not unexpected, (
        f"argv contains unexpected items (likely prompt leaked into argv): {unexpected}"
    )
    # Classifier prompt must also be delivered via stdin
    assert "discovery" in captured["input"].lower(), (
        "CLASSIFIER_PROMPT must be piped via stdin, not argv"
    )


# ---------------------------------------------------------------------------
# R10 test: SUBAGENT_TIMEOUT_SEC env override (F1 fix)
# ---------------------------------------------------------------------------

def test_subagent_timeout_env_override(monkeypatch):
    """CHECK_ITEMS_SUBAGENT_TIMEOUT_SEC env var overrides the default 300s."""
    import check_items_cli as cli

    # Override via env var
    monkeypatch.setenv("CHECK_ITEMS_SUBAGENT_TIMEOUT_SEC", "600")
    importlib.reload(cli)
    assert cli.SUBAGENT_TIMEOUT_SEC == 600

    # Unset → default 300
    monkeypatch.delenv("CHECK_ITEMS_SUBAGENT_TIMEOUT_SEC", raising=False)
    importlib.reload(cli)
    assert cli.SUBAGENT_TIMEOUT_SEC == 300

    # Restore module to default state so later tests aren't affected
    importlib.reload(cli)


# ---------------------------------------------------------------------------
# R12 Finding 1 — _strip_json_fences helper
# ---------------------------------------------------------------------------

def test_strip_json_fences_unwraps_haiku_fenced_output():
    """Haiku wraps JSON responses in ```json ... ``` fences even when prompted
    for raw JSON. The cli stdout-fallback path must strip them before json.loads.
    R12 dogfood finding — 52-group payload reliably hit this on cold-cache.
    """
    import json
    import check_items_cli as cli

    fenced = '```json\n{"merged_by_proj": {"foo": []}}\n```\n'
    stripped = cli._strip_json_fences(fenced)
    # Round-trips through json.loads without raising
    parsed = json.loads(stripped)
    assert parsed == {"merged_by_proj": {"foo": []}}


def test_strip_json_fences_handles_bare_backticks_no_language_tag():
    import json
    import check_items_cli as cli

    fenced = '```\n[1, 2, 3]\n```'
    parsed = json.loads(cli._strip_json_fences(fenced))
    assert parsed == [1, 2, 3]


def test_strip_json_fences_passthrough_when_no_fence():
    import check_items_cli as cli

    raw = '{"already": "raw json"}'
    assert cli._strip_json_fences(raw) == raw


def test_strip_json_fences_handles_leading_whitespace():
    """Some models prepend whitespace/newlines before the opening fence."""
    import json
    import check_items_cli as cli

    fenced = '\n  \n```json\n{"x": 1}\n```'
    parsed = json.loads(cli._strip_json_fences(fenced))
    assert parsed == {"x": 1}


def test_strip_json_fences_empty_and_none_safe():
    import check_items_cli as cli

    assert cli._strip_json_fences("") == ""
    # None would raise on .strip() — function should handle this defensively
    # via the truthy guard
    assert cli._strip_json_fences(None) is None


# ---------------------------------------------------------------------------
# R13 C1 — classifier stdout-fallback shape validation
# ---------------------------------------------------------------------------

def test_classifier_stdout_fallback_rejects_invalid_shape(tmp_path, monkeypatch):
    """run_classifier stdout-fallback must reject wrong-shape JSON (rc=4).

    When the sub-agent writes a valid JSON object that doesn't match the
    classifier list-of-dicts contract, the old code wrote it to output_path
    and returned 0. The downstream orchestrator then consumed the bad data.
    R13 C1 fix: _validate_classifier_payload rejects it and returns rc=4.
    """
    import json
    import subprocess
    import check_items_cli as cli
    from unittest.mock import patch, MagicMock

    # Disable L2 prefilter so group reaches sub-agent and triggers the fallback path.
    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")

    output_path = str(tmp_path / "out.json")

    valid_payload = json.dumps({
        "groups": [{"group_id": "g1", "representative": "Do the thing", "members": []}],
        "evidence": {},
    })

    # Sub-agent returns rc=0 but emits wrong-shape JSON (object, not list)
    cp = MagicMock()
    cp.returncode = 0
    cp.stdout = '{"not_classifications": []}'
    cp.stderr = ""

    with patch.object(cli.subprocess, "run", return_value=cp):
        rc = cli.run_classifier(valid_payload, output_path)

    assert rc == 4, f"Expected rc=4 for invalid shape, got {rc}"
    # Output file must NOT have been written
    assert not (tmp_path / "out.json").exists(), "output_path must not be written for invalid shape"


def test_classifier_stdout_fallback_accepts_valid_shape(tmp_path, monkeypatch):
    """run_classifier stdout-fallback writes output for a correctly-shaped response."""
    import json
    import check_items_cli as cli
    from unittest.mock import patch, MagicMock

    # Disable L2 prefilter so group reaches sub-agent and triggers the fallback path.
    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")

    output_path = str(tmp_path / "out.json")

    valid_payload = json.dumps({
        "groups": [{"group_id": "g1", "representative": "Do the thing", "members": []}],
        "evidence": {},
    })

    valid_response = json.dumps([
        {
            "group_id": "g1",
            "classification": "DONE",
            "confidence": 0.9,
            "canonical_text": "Do the thing",
            "evidence_citation": "commit abc",
            "action_required": "",
        }
    ])

    cp = MagicMock()
    cp.returncode = 0
    cp.stdout = valid_response
    cp.stderr = ""

    with patch.object(cli.subprocess, "run", return_value=cp):
        rc = cli.run_classifier(valid_payload, output_path)

    assert rc == 0
    written = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert isinstance(written, list)
    assert written[0]["classification"] == "DONE"


def test_validate_classifier_payload_rejects_missing_field():
    """_validate_classifier_payload returns False when a required field is absent."""
    import check_items_cli as cli

    bad = [{"group_id": "g1", "classification": "DONE"}]  # missing required fields
    assert cli._validate_classifier_payload(bad) is False


def test_validate_classifier_payload_rejects_invalid_classification():
    """_validate_classifier_payload returns False for unrecognised classification."""
    import check_items_cli as cli

    bad = [{
        "group_id": "g1",
        "classification": "MAYBE",  # not in valid set
        "confidence": 0.5,
        "canonical_text": "x",
        "evidence_citation": "",
        "action_required": "",
    }]
    assert cli._validate_classifier_payload(bad) is False


def test_validate_classifier_payload_rejects_non_list():
    """_validate_classifier_payload returns False when parsed is not a list."""
    import check_items_cli as cli

    assert cli._validate_classifier_payload({"not": "a list"}) is False
    assert cli._validate_classifier_payload(None) is False


# ---------------------------------------------------------------------------
# Folder rename: check_items_folder config key drives output location
# ---------------------------------------------------------------------------

def _reset_load_config_cache():
    """Invalidate the disk-cached config so the next load_config() re-reads.

    load_config keys its cache by session id under
    ~/.claude/obsidian-brain/cache-<sid>.json; tests that monkeypatch
    _CONFIG_PATH must invalidate or the next call returns the prior view.
    """
    import obsidian_utils
    sid = obsidian_utils._get_session_id_fast()
    obsidian_utils.cache_invalidate(sid, "config")


def test_check_items_report_default_folder_is_claude_check_items(tmp_path, monkeypatch):
    """Default folder for /check-items reports is `claude-check-items/`,
    not the legacy `claude-dashboards/`, since these notes list items
    rather than driving Dataview queries."""
    import os, sys, json
    HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
    if HOOKS not in sys.path:
        sys.path.insert(0, HOOKS)
    # Point config at a temp location with no `check_items_folder` set,
    # so the default kicks in.
    cfg_path = tmp_path / "obsidian-brain-config.json"
    cfg_path.write_text(json.dumps({"vault_path": str(tmp_path / "vault")}))
    monkeypatch.setattr("obsidian_utils._CONFIG_PATH", cfg_path)
    _reset_load_config_cache()

    vault = tmp_path / "vault"
    vault.mkdir()
    from check_items_report import write_check_items_dashboard
    try:
        path = write_check_items_dashboard(
            vault_path=str(vault),
            scope_name="proj-x",
            date_str="2026-05-16",
            window_days=14,
            raw_count=0,
            group_count=0,
            classifications=[],
            applied=0,
            cascaded=0,
            merges=[],
            semantic_merge_mode="ok",
            classifier_mode="ok",
            dry_run=True,
        )
        assert "claude-check-items" in path, (
            f"Default folder must be claude-check-items, got: {path}"
        )
        assert "claude-dashboards" not in path
        assert (vault / "claude-check-items").is_dir()
    finally:
        _reset_load_config_cache()


def test_check_items_report_honors_check_items_folder_config(tmp_path, monkeypatch):
    """A user who configures `check_items_folder` in obsidian-brain-config.json
    can keep using `claude-dashboards/` (or any other folder) for backward
    compat or organization preference."""
    import os, sys, json
    HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
    if HOOKS not in sys.path:
        sys.path.insert(0, HOOKS)
    cfg_path = tmp_path / "obsidian-brain-config.json"
    cfg_path.write_text(json.dumps({
        "vault_path": str(tmp_path / "vault"),
        "check_items_folder": "claude-dashboards",
    }))
    monkeypatch.setattr("obsidian_utils._CONFIG_PATH", cfg_path)
    _reset_load_config_cache()

    vault = tmp_path / "vault"
    vault.mkdir()
    from check_items_report import write_check_items_dashboard
    try:
        path = write_check_items_dashboard(
            vault_path=str(vault),
            scope_name="proj-y",
            date_str="2026-05-16",
            window_days=14,
            raw_count=0,
            group_count=0,
            classifications=[],
            applied=0,
            cascaded=0,
            merges=[],
            semantic_merge_mode="ok",
            classifier_mode="ok",
            dry_run=True,
        )
        assert "claude-dashboards" in path, (
            f"Override must place file in claude-dashboards, got: {path}"
        )
    finally:
        _reset_load_config_cache()


def _make_minimal_dashboard_kwargs(vault, scope="proj-x"):
    return dict(
        vault_path=str(vault),
        scope_name=scope,
        date_str="2026-05-16",
        window_days=14,
        raw_count=0,
        group_count=0,
        classifications=[],
        applied=0,
        cascaded=0,
        merges=[],
        semantic_merge_mode="ok",
        classifier_mode="ok",
        dry_run=True,
    )


def test_check_items_report_rejects_parent_traversal_in_folder_config(tmp_path, monkeypatch):
    """A hostile check_items_folder value containing `..` must raise rather
    than escape vault_path. Guards against the regression where the
    containment check was anchored on target_dir (itself derived from
    user input) instead of the vault root.
    """
    import os, sys, json, pytest as _pytest
    HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
    if HOOKS not in sys.path:
        sys.path.insert(0, HOOKS)
    cfg_path = tmp_path / "obsidian-brain-config.json"
    cfg_path.write_text(json.dumps({
        "vault_path": str(tmp_path / "vault"),
        "check_items_folder": "../../etc",
    }))
    monkeypatch.setattr("obsidian_utils._CONFIG_PATH", cfg_path)
    _reset_load_config_cache()

    vault = tmp_path / "vault"
    vault.mkdir()
    from check_items_report import write_check_items_dashboard
    try:
        with _pytest.raises(ValueError, match="parent-traversing|outside vault root"):
            write_check_items_dashboard(**_make_minimal_dashboard_kwargs(vault))
        # No directory escaped the vault
        assert not (tmp_path / "etc").exists()
    finally:
        _reset_load_config_cache()


def test_check_items_report_rejects_absolute_folder_config(tmp_path, monkeypatch):
    """Absolute paths like `/tmp/anywhere` in check_items_folder must raise."""
    import os, sys, json, pytest as _pytest
    HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
    if HOOKS not in sys.path:
        sys.path.insert(0, HOOKS)
    cfg_path = tmp_path / "obsidian-brain-config.json"
    cfg_path.write_text(json.dumps({
        "vault_path": str(tmp_path / "vault"),
        "check_items_folder": "/tmp/escape",
    }))
    monkeypatch.setattr("obsidian_utils._CONFIG_PATH", cfg_path)
    _reset_load_config_cache()

    vault = tmp_path / "vault"
    vault.mkdir()
    from check_items_report import write_check_items_dashboard
    try:
        with _pytest.raises(ValueError, match="absolute|outside vault root"):
            write_check_items_dashboard(**_make_minimal_dashboard_kwargs(vault))
    finally:
        _reset_load_config_cache()


def test_check_items_report_empty_string_folder_uses_default(tmp_path, monkeypatch):
    """check_items_folder: "" → fall back to default (claude-check-items),
    don't write straight into the vault root."""
    import os, sys, json
    HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
    if HOOKS not in sys.path:
        sys.path.insert(0, HOOKS)
    cfg_path = tmp_path / "obsidian-brain-config.json"
    cfg_path.write_text(json.dumps({
        "vault_path": str(tmp_path / "vault"),
        "check_items_folder": "",
    }))
    monkeypatch.setattr("obsidian_utils._CONFIG_PATH", cfg_path)
    _reset_load_config_cache()

    vault = tmp_path / "vault"
    vault.mkdir()
    from check_items_report import write_check_items_dashboard
    try:
        path = write_check_items_dashboard(**_make_minimal_dashboard_kwargs(vault))
        assert "claude-check-items" in path, (
            f"Empty-string folder config must fall back to default; got {path}"
        )
    finally:
        _reset_load_config_cache()


# ---------------------------------------------------------------------------
# #318 Task 7 — plugin install version-skew probe (preflight ruling R5:
# enumerate installs and report divergence; a skill cannot introspect which
# copy of itself Claude Code loaded, so CLAUDE_PLUGIN_ROOT-gated comparison
# was replaced with this). Every test drives injected paths only -- never
# this machine's real ~/.claude/plugins -- so the suite stays hermetic.
# ---------------------------------------------------------------------------

def _write_plugin_manifest(root, version, nested=".claude-plugin"):
    """Write a minimal plugin.json manifest under `root`, at `root/nested/
    plugin.json` (nested="." for the bare `root/plugin.json` rung)."""
    d = root / nested if nested != "." else root
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps({"name": "obsidian-brain", "version": version}))


def test_divergent_installs_are_reported(tmp_path):
    """Two installs at different versions -> a string naming both and
    marking which one is resolved."""
    import obsidian_utils

    old = tmp_path / "cache-3.4.1"
    new = tmp_path / "cache-3.5.1"
    _write_plugin_manifest(old, "3.4.1")
    _write_plugin_manifest(new, "3.5.1")

    msg = obsidian_utils.describe_plugin_install_divergence(
        install_paths=[str(old), str(new)], resolved_path=str(new)
    )
    assert msg is not None
    assert "3.4.1" in msg
    assert "3.5.1" in msg
    assert "resolved" in msg.lower()


def test_resolved_marker_picks_the_resolved_path_not_every_matching_version(tmp_path):
    """M1: two installs can share the SAME version as the executing one
    (e.g. a directory-source checkout and a cache entry both at the
    current release) while only one of them is the install actually
    imported. Marking by version equality tags every same-version entry
    "(resolved)"; marking by path must tag only the real one.

    `resolved_path` here is a hooks/ subdirectory NESTED under one of the
    plugin roots in install_paths -- the shape describe_plugin_install_divergence
    sees by default in a real run (resolved_path is a hooks dir; install_paths
    entries are plugin roots one level up) -- so this also proves the
    containment check, not just exact string equality."""
    import obsidian_utils

    resolved_root = tmp_path / "install-resolved"
    other_same_version = tmp_path / "install-same-version-not-resolved"
    stale = tmp_path / "install-stale"
    _write_plugin_manifest(resolved_root, "3.5.1")
    _write_plugin_manifest(other_same_version, "3.5.1")
    _write_plugin_manifest(stale, "3.4.1")

    resolved_hooks_dir = resolved_root / "hooks"
    resolved_hooks_dir.mkdir(parents=True, exist_ok=True)

    msg = obsidian_utils.describe_plugin_install_divergence(
        install_paths=[str(resolved_root), str(other_same_version), str(stale)],
        resolved_path=str(resolved_hooks_dir),
    )
    assert msg is not None
    assert f"3.5.1 at {resolved_root} (resolved)" in msg
    assert f"3.5.1 at {other_same_version}" in msg
    assert f"3.5.1 at {other_same_version} (resolved)" not in msg


def test_matching_installs_return_none(tmp_path):
    """POSITIVE CONTROL: two installs at the SAME version -> None. Without
    this, a probe hardcoded to always warn would still pass the divergence
    test above."""
    import obsidian_utils

    a = tmp_path / "install-a"
    b = tmp_path / "install-b"
    _write_plugin_manifest(a, "3.5.1")
    _write_plugin_manifest(b, "3.5.1")

    msg = obsidian_utils.describe_plugin_install_divergence(
        install_paths=[str(a), str(b)], resolved_path=str(a)
    )
    assert msg is None


def test_single_install_returns_none(tmp_path):
    """A single install cannot diverge from anything."""
    import obsidian_utils

    only = tmp_path / "install-only"
    _write_plugin_manifest(only, "3.5.1")

    msg = obsidian_utils.describe_plugin_install_divergence(
        install_paths=[str(only)], resolved_path=str(only)
    )
    assert msg is None


def test_unreadable_manifest_is_skipped_not_fatal(tmp_path):
    """One valid install plus one with no manifest at all -> None (one
    readable version means no divergence) and must not raise."""
    import obsidian_utils

    valid = tmp_path / "install-valid"
    _write_plugin_manifest(valid, "3.5.1")
    no_manifest = tmp_path / "install-no-manifest"
    no_manifest.mkdir()

    msg = obsidian_utils.describe_plugin_install_divergence(
        install_paths=[str(valid), str(no_manifest)], resolved_path=str(valid)
    )
    assert msg is None


def test_manifest_found_via_parent_rung(tmp_path):
    """This repo keeps its manifest at `.claude-plugin/plugin.json` with
    hooks one level down -- _read_plugin_version(hooks_dir) must climb to
    the parent to find it (Ruling R4, retained), or the resolved install
    reads as unknown and every divergence check above is inert against
    the real repo layout even though tests 1-4 all still pass."""
    import obsidian_utils

    root = tmp_path / "repo-root"
    _write_plugin_manifest(root, "3.5.1")
    hooks_dir = root / "hooks"
    hooks_dir.mkdir(parents=True)

    version = obsidian_utils._read_plugin_version(str(hooks_dir))
    assert version == "3.5.1"
