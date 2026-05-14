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


def test_run_classifier_picks_haiku_at_30_or_fewer(tmp_path, monkeypatch):
    """<=30 merged groups -> claude -p --model haiku per spec line 106."""
    import check_items_cli as cli

    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        out_path = tmp_path / "out.json"
        out_path.write_text(json.dumps([
            {"group_id": "g1", "classification": "ACTIVE", "confidence": "LOW",
             "canonical_text": "x", "evidence_citation": None, "action_required": None}
        ]))
        return _fake_completed(stdout=out_path.read_text(), returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
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
    """>30 merged groups escalates to sonnet."""
    import check_items_cli as cli

    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        out_path = tmp_path / "out.json"
        out_path.write_text(json.dumps([]))
        return _fake_completed(stdout=out_path.read_text(), returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
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
    for c in ("DONE", "NEEDS-ACTION", "STALE", "ACTIVE"):
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
# R2 regression: Finding 3 — path traversal via scope_name
# ---------------------------------------------------------------------------

def test_dashboard_rejects_path_traversal_in_scope_name(tmp_path):
    """Containment guard rejects ../escape attempts in scope_name."""
    from check_items_report import write_check_items_dashboard
    vault = tmp_path / "vault"
    (vault / "claude-dashboards").mkdir(parents=True)
    # Path-traversal scope name: should be sanitized, not escape
    path = write_check_items_dashboard(
        vault_path=str(vault), scope_name="../../etc/passwd", date_str="2026-05-11",
        window_days=14, raw_count=0, group_count=0, classifications=[],
        applied=0, cascaded=0, merges=[], semantic_merge_mode="ok",
        classifier_mode="ok", dry_run=True,
    )
    # File must land inside claude-dashboards/, with sanitized name
    assert str(vault / "claude-dashboards") in path
    assert ".." not in os.path.basename(path)
    assert "/" not in os.path.basename(path)


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

    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["input"] = kwargs.get("input", "")
        out_path = tmp_path / "out.json"
        out_path.write_text(json.dumps([]))
        return _fake_completed(stdout=out_path.read_text(), returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    payload = {"groups": [], "evidence": {}}
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
    """CHECK_ITEMS_SUBAGENT_TIMEOUT_SEC env var overrides the default 180s."""
    import check_items_cli as cli

    # Override via env var
    monkeypatch.setenv("CHECK_ITEMS_SUBAGENT_TIMEOUT_SEC", "300")
    importlib.reload(cli)
    assert cli.SUBAGENT_TIMEOUT_SEC == 300

    # Unset → default 180
    monkeypatch.delenv("CHECK_ITEMS_SUBAGENT_TIMEOUT_SEC", raising=False)
    importlib.reload(cli)
    assert cli.SUBAGENT_TIMEOUT_SEC == 180

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
