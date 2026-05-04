"""Tests for obsidian_utils.gather_session_evidence().

Spec: docs/superpowers/specs/2026-05-03-issue-122-retro-evidence-base-design.md
Issue: https://github.com/abhattacherjee/obsidian-brain/issues/122
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

import obsidian_utils


def _write(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def test_gather_session_evidence_unknown_sid_returns_empty(tmp_vault: Path) -> None:
    """session_id == 'unknown' must return all-empty lists with no I/O attempted."""
    bundle = obsidian_utils.gather_session_evidence(
        vault_path=str(tmp_vault),
        sessions_folder="claude-sessions",
        insights_folder="claude-insights",
        session_id="unknown",
        date="2026-05-03",
        project="obsidian-brain",
    )
    assert bundle["session_id"] == "unknown"
    assert bundle["snapshots"] == []
    assert bundle["insights"] == []
    assert bundle["decisions"] == []
    assert bundle["error_fixes"] == []
    assert bundle["discovery_errors"] == []


SNAPSHOT_TEMPLATE = """\
---
type: claude-snapshot
session_id: {sid}
project: {project}
date: 2026-05-03
trigger: {trigger}
---

# Context Snapshot: {project}

## Summary
Snapshot body for testing — hhmmss {hhmmss}.
"""


def _make_snapshot(
    sessions_dir: Path,
    *,
    sid: str,
    project: str = "obsidian-brain",
    hhmmss: str = "100000",
    trigger: str = "auto",
    project_slug: str | None = None,
) -> Path:
    slug = project_slug or project
    path = sessions_dir / f"2026-05-03-{slug}-aaaa-snapshot-{hhmmss}.md"
    _write(path, SNAPSHOT_TEMPLATE.format(sid=sid, project=project, hhmmss=hhmmss, trigger=trigger))
    return path


def test_gather_session_evidence_snapshots_happy_path(tmp_vault: Path) -> None:
    sessions_dir = tmp_vault / "claude-sessions"
    snap_a = _make_snapshot(sessions_dir, sid="SID-A", hhmmss="100000")
    snap_b = _make_snapshot(sessions_dir, sid="SID-A", hhmmss="200000")

    bundle = obsidian_utils.gather_session_evidence(
        vault_path=str(tmp_vault),
        sessions_folder="claude-sessions",
        insights_folder="claude-insights",
        session_id="SID-A",
        date="2026-05-03",
        project="obsidian-brain",
    )

    paths = [s["path"] for s in bundle["snapshots"]]
    assert paths == [str(snap_a), str(snap_b)]
    assert bundle["snapshots"][0]["hhmmss"] == "100000"
    assert bundle["snapshots"][0]["trigger"] == "auto"
    assert "Snapshot body for testing" in bundle["snapshots"][0]["body"]
    assert bundle["discovery_errors"] == []


def test_gather_session_evidence_snapshots_sorted_ascending_by_hhmmss(tmp_vault: Path) -> None:
    """Even if the filesystem returns snapshots in reverse order, helper sorts ascending."""
    sessions_dir = tmp_vault / "claude-sessions"
    _make_snapshot(sessions_dir, sid="SID-A", hhmmss="200000")
    _make_snapshot(sessions_dir, sid="SID-A", hhmmss="100000")

    bundle = obsidian_utils.gather_session_evidence(
        vault_path=str(tmp_vault),
        sessions_folder="claude-sessions",
        insights_folder="claude-insights",
        session_id="SID-A",
        date="2026-05-03",
        project="obsidian-brain",
    )

    hhmmss_seq = [s["hhmmss"] for s in bundle["snapshots"]]
    assert hhmmss_seq == ["100000", "200000"]


def test_gather_session_evidence_snapshots_filter_other_project(tmp_vault: Path) -> None:
    """Snapshots whose frontmatter project differs are excluded."""
    sessions_dir = tmp_vault / "claude-sessions"
    own = _make_snapshot(sessions_dir, sid="SID-A", hhmmss="100000", project="obsidian-brain")
    _make_snapshot(
        sessions_dir,
        sid="SID-A",
        hhmmss="200000",
        project="other-project",
        project_slug="other-project",
    )

    bundle = obsidian_utils.gather_session_evidence(
        vault_path=str(tmp_vault),
        sessions_folder="claude-sessions",
        insights_folder="claude-insights",
        session_id="SID-A",
        date="2026-05-03",
        project="obsidian-brain",
    )

    paths = [s["path"] for s in bundle["snapshots"]]
    assert paths == [str(own)]
