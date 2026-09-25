"""Tests for #318 Task 3: deep_analysis_pipeline names repo-less projects
instead of silently `continue`-ing past them.

Reuses tests/test_open_item_dedup.py's vault-fixture conventions (session
notes under `claude-sessions/`, project frontmatter) and the
subprocess.run-mocking pattern from that file's #264 Task 2 tests
(`_run_pipeline_with_fake_git`-style: patch open_item_dedup.subprocess.run
directly, never a real git/gh invocation).
"""

from __future__ import annotations

import json as _json
import os
import re
from unittest.mock import MagicMock, patch

import open_item_dedup as oid


def _create_session_note(sessions_dir, filename, project, open_items):
    items_text = "\n".join(f"- [ ] {item}" for item in open_items)
    note = sessions_dir / filename
    note.write_text(
        f"---\ntype: claude-session\nproject: {project}\nstatus: summarized\n---\n\n"
        f"# Session\n\n## Summary\nDid stuff.\n\n"
        f"## Open Questions / Next Steps\n{items_text}\n",
        encoding="utf-8",
    )
    return note


def _fake_completed(stdout="", returncode=0):
    cp = MagicMock()
    cp.stdout = stdout
    cp.returncode = returncode
    cp.stderr = ""
    return cp


def _run_pipeline(tmp_path, project_paths, fake_run=None):
    """Drive deep_analysis_pipeline() for a single project "notes-only" with
    one session note providing an open item, mocking _resolve_project_paths
    and (when repo-backed) subprocess.run per test-3 brief and preflight
    ruling R2 (no real git init, no real gh call)."""
    vault_path = str(tmp_path)
    sessions_folder = "claude-sessions"
    insights_folder = "insights"
    sessions_dir = tmp_path / sessions_folder
    sessions_dir.mkdir(exist_ok=True)
    os.makedirs(str(tmp_path / insights_folder), exist_ok=True)
    _create_session_note(
        sessions_dir, "2026-08-15-notes-only-abcd.md", "notes-only",
        ["Ship the widget"],
    )

    output_path = str(tmp_path / "pipeline-out.json")

    fake_vi = MagicMock()
    fake_vi.ensure_index.return_value = str(tmp_path / "vault.db")
    fake_vi.extract_keywords.return_value = []
    fake_vi.search_vault.return_value = []

    run_side_effect = fake_run if fake_run is not None else (lambda *a, **k: _fake_completed(""))

    with patch("subprocess.run", side_effect=run_side_effect), \
         patch.dict("sys.modules", {"vault_index": fake_vi}), \
         patch.object(oid, "_resolve_project_paths", return_value=project_paths):

        result = oid.deep_analysis_pipeline(
            basenames=[],
            projects_json=_json.dumps(["notes-only"]),
            output_path=output_path,
            vault_path=vault_path,
            sessions_folder=sessions_folder,
            insights_folder=insights_folder,
            db_path=str(tmp_path / "test-vault.db"),
        )

    with open(output_path, encoding="utf-8") as f:
        data = _json.load(f)
    return result, data


def test_repo_less_project_is_named_in_output_json(tmp_path):
    _result, data = _run_pipeline(tmp_path, project_paths={})
    assert data["evidence_gaps"]["projects_without_repo"] == ["notes-only"]


def test_repo_less_project_warns_on_stderr(tmp_path, capsys):
    _run_pipeline(tmp_path, project_paths={})
    stderr = capsys.readouterr().err
    assert "notes-only" in stderr
    assert "no local git repo" in stderr
    assert "DONE" in stderr
    assert "reach tier HIGH or classification DONE" in stderr


def test_all_projects_without_repo_sets_zero_evidence_flag(tmp_path):
    _result, data = _run_pipeline(tmp_path, project_paths={})
    gaps = data["evidence_gaps"]
    assert gaps["projects_with_evidence"] == 0
    assert gaps["all_projects_gapped"] is True


def test_project_with_repo_reports_no_gap(tmp_path):
    """Positive control: a repo-backed project must NOT be named as a gap.
    subprocess.run is stubbed (returncode=0, stdout="") rather than a real
    git repo — the guard under test is `if not repo_path`, which never
    inspects the directory (Preflight ruling R2). Without this test, a
    check hardcoded to "always gapped" would still pass tests 1-3.
    """
    repo_dir = tmp_path / "fake-repo"
    repo_dir.mkdir()

    def fake_run(cmd, *args, **kwargs):
        return _fake_completed(stdout="", returncode=0)

    _result, data = _run_pipeline(
        tmp_path, project_paths={"notes-only": str(repo_dir)}, fake_run=fake_run,
    )
    gaps = data["evidence_gaps"]
    assert gaps["projects_without_repo"] == []
    assert gaps["all_projects_gapped"] is False


def test_status_string_keeps_ok_prefix_and_gains_gap_count(tmp_path):
    result, data = _run_pipeline(tmp_path, project_paths={})
    assert re.match(r"^OK:\d+:\d+:\d+:\d+$", result), f"unexpected status format: {result}"
    gap_count = int(result.split(":")[4])
    assert gap_count == len(data["evidence_gaps"]["projects_without_repo"])
