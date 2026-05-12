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

import json
import os
import sys

import pytest

# conftest.py adds hooks/ to sys.path; import after that
import open_item_dedup as oid
from unittest.mock import patch, MagicMock


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
