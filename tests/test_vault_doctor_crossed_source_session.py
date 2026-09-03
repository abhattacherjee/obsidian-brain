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

import os
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


def test_matching_pair_no_issue(tmp_path, capsys):
    """Case 2: source_session == target's session_id -> no issues.

    Also pins the three previously-unasserted summary counters (#354 review
    item 5c): n_clean, n_dangling, n_indexed. `issues == []` alone cannot
    tell "1 clean pair" apart from "nothing was scanned at all" — both
    produce an empty issues list — so a mutation that stopped counting a
    clean match (or silently dropped it into the wrong bucket) would pass
    unnoticed without asserting the summary line itself.
    """
    v = _vault(tmp_path)
    _write_session(v["sessions"] / "2026-08-26-demo-df46.md", session_id="session-A")
    _write_insight(
        v["insights"] / "2026-08-27-demo-retro.md",
        source_session="session-A",
        source_session_note_stem="2026-08-26-demo-df46",
    )

    capsys.readouterr()  # drain
    issues = css.scan(str(v["root"]), SESSIONS, INSIGHTS, 30)
    err = capsys.readouterr().err

    assert issues == []
    # n_scanned=2 (the session note + the insight), n_indexed=1 (the one
    # session note), 0 crossed, n_clean=1 (the matching pair), n_no_stamp=1
    # (the session note itself has no source_session), n_dangling=0,
    # n_target_unreadable=0, n_skipped=0.
    assert (
        "scanned 2 note(s) (1 session note(s) indexed as link targets): "
        "0 crossed, 1 clean, 1 no source_session/link to check, 0 dangling "
        "link (not reported), 0 target unreadable, 0 source note unreadable"
        in err
    ), err


def test_dangling_link_no_issue(tmp_path, capsys):
    """Case 3 (negative control): the wikilink target does not exist on
    disk at all. This is the scoping decision this check makes on purpose
    — dangling links belong to snapshot-integrity/#214, not here. A
    mutation that made scan() ALSO report dangling links must turn this
    test red (see the mutation-proof step in the task verification, not
    reproduced in this file).

    Also pins the summary counters (#354 review item 5c): n_dangling and
    n_indexed, alongside n_clean staying 0 — `issues == []` alone cannot
    distinguish "1 dangling link, correctly excluded" from "nothing was
    scanned".
    """
    v = _vault(tmp_path)
    # No session note written at all for this stem.
    _write_insight(
        v["insights"] / "2026-08-27-demo-retro.md",
        source_session="session-A",
        source_session_note_stem="2026-08-26-demo-nonexistent",
    )

    capsys.readouterr()  # drain
    issues = css.scan(str(v["root"]), SESSIONS, INSIGHTS, 30)
    err = capsys.readouterr().err

    assert issues == []
    # n_scanned=1 (only the insight; sessions folder is empty), n_indexed=0
    # (no session notes exist to index), 0 crossed, 0 clean, 0 no-stamp,
    # n_dangling=1 (the unresolvable link, excluded by design), 0 target
    # unreadable, 0 source note unreadable.
    assert (
        "scanned 1 note(s) (0 session note(s) indexed as link targets): "
        "0 crossed, 0 clean, 0 no source_session/link to check, 1 dangling "
        "link (not reported), 0 target unreadable, 0 source note unreadable"
        in err
    ), err


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


def test_confidence_pinned_at_0_95(tmp_path):
    """#330 review item 12c: pin the actual confidence value on a crossed
    finding. A confidence of 0.0 would still make every assertion above
    pass, silently defeating the DEFAULT_MIN_CONFIDENCE filter this check
    exists to survive — this test fails if that regresses."""
    v = _vault(tmp_path)
    _write_session(v["sessions"] / "2026-08-26-demo-df46.md", session_id="session-B")
    _write_insight(
        v["insights"] / "2026-08-27-demo-retro.md",
        source_session="session-A",
        source_session_note_stem="2026-08-26-demo-df46",
    )

    issues = css.scan(str(v["root"]), SESSIONS, INSIGHTS, 30)

    assert len(issues) == 1
    assert issues[0].confidence == 0.95


def test_unreadable_sessions_dir_warns_instead_of_reporting_silent_clean(
    tmp_path, capsys
):
    """#330 review item 11: Path.glob() silently swallows OSError while
    walking a directory, so an unreadable sessions_folder used to look
    EMPTY rather than unreadable — every source_session_note link then
    looked dangling (excluded by design) and the check reported CLEAN
    having actually scanned nothing. iterdir() raises instead, and that
    must now surface as a stderr WARNING naming the failure, so a scan
    that silently found nothing is distinguishable from a scan that never
    ran."""
    v = _vault(tmp_path)
    _write_session(v["sessions"] / "2026-08-26-demo-df46.md", session_id="session-B")
    _write_insight(
        v["insights"] / "2026-08-27-demo-retro.md",
        source_session="session-A",
        source_session_note_stem="2026-08-26-demo-df46",
    )

    os.chmod(v["sessions"], 0o000)
    try:
        capsys.readouterr()  # drain
        issues = css.scan(str(v["root"]), SESSIONS, INSIGHTS, 30)
        err = capsys.readouterr().err
    finally:
        os.chmod(v["sessions"], 0o700)  # restore so tmp_path cleanup works

    # The crossing is invisible this run (its target is unreadable) — that
    # part of the behavior is unavoidable — but the run must NOT look like
    # an ordinary clean scan: it must say so on stderr.
    assert issues == []
    assert "WARNING" in err
    assert "could not list" in err
    assert str(v["sessions"]) in err

    # #354 review item 5b: an unreadable sessions_folder trips BOTH
    # "could not list" branches at once — _index_sessions_by_stem's (used to
    # resolve wikilinks) and the main scan loop's (sessions_folder is one of
    # the two folders scanned) — so a bare "could not list" substring check
    # cannot tell whether EITHER branch alone was deleted; the other would
    # still fire and the assertion above would stay green. Assert each
    # branch's OWN distinguishing trailing phrase so deleting either one
    # alone is independently detectable.
    assert "not reported — dangling links are excluded by design" in err, (
        "the _index_sessions_by_stem WARNING (stem-index branch) did not fire"
    )
    assert "NOT scanned this run" in err, (
        "the main scan-loop WARNING (per-folder listing branch) did not fire"
    )


def test_unreadable_insights_dir_only_warns_scan_loop_not_stem_index(
    tmp_path, capsys
):
    """#354 review item 5b, part 2: isolate the scan-loop "could not list"
    branch from the stem-index branch. Only insights_folder is made
    unreadable — sessions_folder (what _index_sessions_by_stem lists) stays
    readable — so ONLY the main scan-loop WARNING can fire here. This
    catches a deletion of that branch that the sessions-unreadable test
    above could miss (there, the stem-index branch's own WARNING would
    still make a bare "could not list" assertion pass)."""
    v = _vault(tmp_path)
    _write_session(v["sessions"] / "2026-08-26-demo-df46.md", session_id="session-B")

    os.chmod(v["insights"], 0o000)
    try:
        capsys.readouterr()  # drain
        issues = css.scan(str(v["root"]), SESSIONS, INSIGHTS, 30)
        err = capsys.readouterr().err
    finally:
        os.chmod(v["insights"], 0o700)  # restore so tmp_path cleanup works

    assert issues == []
    assert "WARNING" in err
    assert "could not list" in err
    assert str(v["insights"]) in err
    assert "NOT scanned this run" in err
    # The stem-index branch must NOT have fired: sessions_folder (what it
    # lists) was never made unreadable.
    assert "not reported — dangling links are excluded by design" not in err


def test_unparsable_source_note_is_counted_and_warned_not_silently_skipped(
    tmp_path, capsys
):
    """#330 review item 11: a source-side note (insight/session) that fails
    to parse must be visible on stderr and reflected in the scan summary —
    not a silent `continue` indistinguishable from 'nothing to check here'."""
    v = _vault(tmp_path)
    _write_session(v["sessions"] / "2026-08-26-demo-df46.md", session_id="session-B")
    # No closing '---' fence -> read_note_metadata() cannot parse this.
    (v["insights"] / "2026-08-27-broken.md").write_text(
        "---\nsource_session: session-A\n# no closing fence\n", encoding="utf-8"
    )

    capsys.readouterr()  # drain
    issues = css.scan(str(v["root"]), SESSIONS, INSIGHTS, 30)
    err = capsys.readouterr().err

    assert issues == []
    assert "WARNING" in err
    assert "could not parse" in err
    assert "2026-08-27-broken.md" in err
    # 2 notes scanned: the broken insight AND the sessions-folder note
    # (session-B, no source_session — both folders are scanned for the
    # STAMPED fields, per the module docstring).
    assert "scanned 2 note" in err
    assert "1 source note unreadable" in err


def test_unparsable_link_target_is_counted_and_warned(tmp_path, capsys):
    """#330 review item 11: the link TARGET failing to parse (present on
    disk, broken frontmatter) is a distinct, worth-reporting case from a
    dangling link — the note exists, so 'cannot tell' is a scan gap, not
    the normal pre-SessionEnd shape."""
    v = _vault(tmp_path)
    # Target exists but is unparsable.
    (v["sessions"] / "2026-08-26-demo-df46.md").write_text(
        "---\nsession_id: session-B\n# no closing fence\n", encoding="utf-8"
    )
    _write_insight(
        v["insights"] / "2026-08-27-demo-retro.md",
        source_session="session-A",
        source_session_note_stem="2026-08-26-demo-df46",
    )

    capsys.readouterr()  # drain
    issues = css.scan(str(v["root"]), SESSIONS, INSIGHTS, 30)
    err = capsys.readouterr().err

    assert issues == []
    assert "WARNING" in err
    assert "could not parse link target" in err
    assert "2026-08-26-demo-df46.md" in err
    assert "1 target unreadable" in err
