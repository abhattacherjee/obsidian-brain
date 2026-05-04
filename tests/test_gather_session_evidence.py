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


INSIGHT_TEMPLATE = """\
---
type: {note_type}
source_session: {sid}
project: obsidian-brain
date: 2026-05-03
---

# {title}

{body}
"""


def _make_insight(
    insights_dir: Path,
    *,
    filename: str,
    note_type: str,
    sid: str,
    title: str = "Test Title",
    body: str = "Test body content.",
) -> Path:
    path = insights_dir / filename
    _write(path, INSIGHT_TEMPLATE.format(note_type=note_type, sid=sid, title=title, body=body))
    return path


def test_gather_session_evidence_partitions_by_type(tmp_vault: Path) -> None:
    insights_dir = tmp_vault / "claude-insights"
    ins = _make_insight(
        insights_dir,
        filename="2026-05-03-finding-aaaa.md",
        note_type="claude-insight",
        sid="SID-A",
        title="An insight",
    )
    dec = _make_insight(
        insights_dir,
        filename="2026-05-03-decision-bbbb-decision.md",
        note_type="claude-decision",
        sid="SID-A",
        title="A decision",
    )
    err = _make_insight(
        insights_dir,
        filename="2026-05-03-bug-cccc-error-fix.md",
        note_type="claude-error-fix",
        sid="SID-A",
        title="An error fix",
    )

    bundle = obsidian_utils.gather_session_evidence(
        vault_path=str(tmp_vault),
        sessions_folder="claude-sessions",
        insights_folder="claude-insights",
        session_id="SID-A",
        date="2026-05-03",
        project="obsidian-brain",
    )

    assert [i["path"] for i in bundle["insights"]] == [str(ins)]
    assert [d["path"] for d in bundle["decisions"]] == [str(dec)]
    assert [e["path"] for e in bundle["error_fixes"]] == [str(err)]
    assert bundle["insights"][0]["title"] == "An insight"
    assert "Test body content." in bundle["insights"][0]["body"]


def test_gather_session_evidence_filters_decoys(tmp_vault: Path) -> None:
    """Notes belonging to other sessions or types must not leak into the bundle."""
    insights_dir = tmp_vault / "claude-insights"
    own = _make_insight(
        insights_dir,
        filename="2026-05-03-mine-aaaa.md",
        note_type="claude-insight",
        sid="SID-A",
    )
    _make_insight(  # other session — must be excluded
        insights_dir,
        filename="2026-05-03-theirs-bbbb.md",
        note_type="claude-insight",
        sid="SID-B",
    )
    _make_insight(  # ignored type (e.g. retro from prior session) — must be excluded
        insights_dir,
        filename="2026-05-03-old-retro-cccc.md",
        note_type="claude-retro",
        sid="SID-A",
    )

    bundle = obsidian_utils.gather_session_evidence(
        vault_path=str(tmp_vault),
        sessions_folder="claude-sessions",
        insights_folder="claude-insights",
        session_id="SID-A",
        date="2026-05-03",
        project="obsidian-brain",
    )

    assert [i["path"] for i in bundle["insights"]] == [str(own)]
    assert bundle["decisions"] == []
    assert bundle["error_fixes"] == []


def test_gather_session_evidence_unreadable_file_in_discovery_errors(
    tmp_vault: Path,
) -> None:
    """A file we can't read appears in discovery_errors but doesn't break the bundle."""
    insights_dir = tmp_vault / "claude-insights"
    good = _make_insight(
        insights_dir,
        filename="2026-05-03-good-aaaa.md",
        note_type="claude-insight",
        sid="SID-A",
    )
    bad = _make_insight(
        insights_dir,
        filename="2026-05-03-bad-bbbb.md",
        note_type="claude-insight",
        sid="SID-A",
    )

    # Make the body read fail by chmod'ing the file unreadable. The frontmatter
    # read in read_note_metadata() will also fail, which routes through the
    # OSError branch in the discovery loop. We use 0o000 rather than removing
    # the file so the glob still finds it.
    if os.geteuid() == 0:
        pytest.skip("chmod-based unreadable test does not work for root")
    os.chmod(bad, 0o000)
    try:
        bundle = obsidian_utils.gather_session_evidence(
            vault_path=str(tmp_vault),
            sessions_folder="claude-sessions",
            insights_folder="claude-insights",
            session_id="SID-A",
            date="2026-05-03",
            project="obsidian-brain",
        )
    finally:
        os.chmod(bad, 0o600)  # restore so pytest tmp_path teardown can delete

    assert [i["path"] for i in bundle["insights"]] == [str(good)]
    assert any("bad-bbbb" in err for err in bundle["discovery_errors"])
