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
