"""Tests for the crossed-source-session vault_doctor check (#330 task 6).

A note is "crossed" when its stamped ``source_session`` and the
``session_id`` of the note its own ``source_session_note`` wikilink
resolves to disagree — evidence that two different ``get_session_context()``
resolutions were used to fill in the two fields of a single note write
(see ``docs/plans/330-session-id-crossing.md``).

The detection is deliberately narrow: it must NOT report a dangling
``source_session_note`` link (target missing on disk). That is a separate,
already-owned problem space (``snapshot-integrity`` / #214) and a
deliberate scoping decision — case 3 below is the negative control that
proves the check actually excludes it, not merely never triggers it.

``apply()`` never repairs anything (deciding which id is right needs
evidence the check does not have) — case 6 proves that end to end: the
note is byte-identical after ``apply()`` runs.

Every test writes under ``tmp_path``. The live vault is never touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import vault_doctor_checks  # noqa: E402
from vault_doctor_checks import crossed_source_session as css  # noqa: E402

SESSIONS = "claude-sessions"
INSIGHTS = "claude-insights"


def _write_insight(
    path: Path,
    source_session: str,
    source_session_note_stem: str | None,
    project: str = "demo",
) -> None:
    fm = (
        "---\n"
        "type: claude-insight\n"
        "date: 2026-08-27\n"
        f"source_session: {source_session}\n"
    )
    if source_session_note_stem is not None:
        fm += f'source_session_note: "[[{source_session_note_stem}]]"\n'
    fm += f"project: {project}\n---\n\n# Insight\n\nBody text.\n"
    path.write_text(fm, encoding="utf-8")


def _write_session(
    path: Path,
    session_id: str | None,
    project: str = "demo",
) -> None:
    fm = "---\ntype: claude-session\ndate: 2026-08-26\n"
    if session_id is not None:
        fm += f"session_id: {session_id}\n"
    fm += f"project: {project}\n---\n\n# Session\n\nBody text.\n"
    path.write_text(fm, encoding="utf-8")


def _vault(tmp_path: Path) -> dict:
    vault = tmp_path / "vault"
    (vault / SESSIONS).mkdir(parents=True)
    (vault / INSIGHTS).mkdir(parents=True)
    return {"root": vault, "sessions": vault / SESSIONS, "insights": vault / INSIGHTS}


def test_crossed_pair_detected(tmp_path):
    """Case 1: source_session != target's session_id -> exactly one Issue,
    naming both ids."""
    v = _vault(tmp_path)
    _write_session(v["sessions"] / "2026-08-26-demo-df46.md", session_id="session-B")
    _write_insight(
        v["insights"] / "2026-08-27-demo-retro.md",
        source_session="session-A",
        source_session_note_stem="2026-08-26-demo-df46",
    )

    issues = css.scan(str(v["root"]), SESSIONS, INSIGHTS, 30)

    assert len(issues) == 1
    issue = issues[0]
    assert issue.check == "crossed-source-session"
    assert "session-A" in issue.current_source or "session-A" in issue.reason
    assert "session-B" in issue.proposed_source or "session-B" in issue.reason
    assert issue.extra["source_session"] == "session-A"
    assert issue.extra["target_session_id"] == "session-B"


def test_matching_pair_no_issue(tmp_path):
    """Case 2: source_session == target's session_id -> no issues."""
    v = _vault(tmp_path)
    _write_session(v["sessions"] / "2026-08-26-demo-df46.md", session_id="session-A")
    _write_insight(
        v["insights"] / "2026-08-27-demo-retro.md",
        source_session="session-A",
        source_session_note_stem="2026-08-26-demo-df46",
    )

    issues = css.scan(str(v["root"]), SESSIONS, INSIGHTS, 30)

    assert issues == []


def test_dangling_link_no_issue(tmp_path):
    """Case 3 (negative control): the wikilink target does not exist on
    disk at all. This is the scoping decision this check makes on purpose
    — dangling links belong to snapshot-integrity/#214, not here. A
    mutation that made scan() ALSO report dangling links must turn this
    test red (see the mutation-proof step in the task verification, not
    reproduced in this file)."""
    v = _vault(tmp_path)
    # No session note written at all for this stem.
    _write_insight(
        v["insights"] / "2026-08-27-demo-retro.md",
        source_session="session-A",
        source_session_note_stem="2026-08-26-demo-nonexistent",
    )

    issues = css.scan(str(v["root"]), SESSIONS, INSIGHTS, 30)

    assert issues == []


def test_target_missing_session_id_no_issue(tmp_path):
    """Case 4: target note exists but has no session_id frontmatter ->
    no issue (nothing to compare against)."""
    v = _vault(tmp_path)
    _write_session(v["sessions"] / "2026-08-26-demo-df46.md", session_id=None)
    _write_insight(
        v["insights"] / "2026-08-27-demo-retro.md",
        source_session="session-A",
        source_session_note_stem="2026-08-26-demo-df46",
    )

    issues = css.scan(str(v["root"]), SESSIONS, INSIGHTS, 30)

    assert issues == []


def test_source_session_unknown_no_issue(tmp_path):
    """Case 5: source_session: unknown -> no issue, even with a crossing
    target."""
    v = _vault(tmp_path)
    _write_session(v["sessions"] / "2026-08-26-demo-df46.md", session_id="session-B")
    _write_insight(
        v["insights"] / "2026-08-27-demo-retro.md",
        source_session="unknown",
        source_session_note_stem="2026-08-26-demo-df46",
    )

    issues = css.scan(str(v["root"]), SESSIONS, INSIGHTS, 30)

    assert issues == []


def test_apply_skips_and_writes_nothing(tmp_path):
    """Case 6: apply() returns status="skipped" for every issue and never
    writes — the note file is byte-identical before and after."""
    v = _vault(tmp_path)
    _write_session(v["sessions"] / "2026-08-26-demo-df46.md", session_id="session-B")
    note_path = v["insights"] / "2026-08-27-demo-retro.md"
    _write_insight(
        note_path,
        source_session="session-A",
        source_session_note_stem="2026-08-26-demo-df46",
    )

    before = note_path.read_bytes()
    issues = css.scan(str(v["root"]), SESSIONS, INSIGHTS, 30)
    assert len(issues) == 1

    results = css.apply(issues, str(tmp_path / "backup"))

    assert len(results) == 1
    assert results[0].status == "skipped"
    assert results[0].error  # explains why nothing happened
    after = note_path.read_bytes()
    assert after == before


def test_registered_and_discoverable():
    """Case 7: the check is registered and discoverable via
    list_checks() / all_checks()."""
    names = vault_doctor_checks.list_checks()
    assert "crossed-source-session" in names

    all_mods = vault_doctor_checks.all_checks()
    all_names = [getattr(m, "NAME", None) for m in all_mods]
    assert "crossed-source-session" in all_names


def test_realistic_live_fixture(tmp_path):
    """Case 8: realistic fixture using the REAL live vault values —
    the one genuine crossing found in production (#330 plan)."""
    v = _vault(tmp_path)
    real_source_session = "18785285-d99d-48ce-a2f7-5bc0aba14055"
    real_target_session_id = "504f461a-1881-4bc6-a262-0025f1420ea5"
    target_stem = "2026-08-26-openclaw-df46"

    _write_session(
        v["sessions"] / f"{target_stem}.md",
        session_id=real_target_session_id,
        project="openclaw",
    )
    _write_insight(
        v["insights"] / "2026-08-27-retro-d205.md",
        source_session=real_source_session,
        source_session_note_stem=target_stem,
        project="openclaw",
    )

    issues = css.scan(str(v["root"]), SESSIONS, INSIGHTS, 30)

    assert len(issues) == 1
    issue = issues[0]
    assert issue.extra["source_session"] == real_source_session
    assert issue.extra["target_session_id"] == real_target_session_id
    assert issue.note_path.endswith("2026-08-27-retro-d205.md")
