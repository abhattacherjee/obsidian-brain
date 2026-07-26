"""Tests for the missing-frontmatter-fence vault_doctor check (#276).

Nine live notes lost their opening ``---`` fence to the leading-fence-eaten
failure mode, so their frontmatter is body text. This check inserts the fence
back. The detection preconditions carry the whole safety argument: a false
positive injects a stray ``---`` into a healthy note, so each precondition
has its own test proving that a note failing ONLY that one is not flagged.

Fixtures reproduce the real shape of the damaged notes (``type:`` first, then
``date``/``created_at``/``source_session``/``source_session_note``/``project``
and a block-style ``tags:`` list), not a sanitized two-key minimum.

Every test writes under ``tmp_path``. The live vault is never touched.
"""

from __future__ import annotations

import importlib
import os
import stat
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from vault_doctor_checks import missing_frontmatter_fence as mff  # noqa: E402

SESSIONS = "claude-sessions"
INSIGHTS = "claude-insights"

# The frontmatter block of a real /retro note, minus the opening fence.
# Keep this verbatim-shaped: the closing fence, the quoted wikilink, and the
# block-style tags list are all things detection has to walk over.
FENCELESS_FM = (
    "type: claude-retro\n"
    "date: 2026-07-22\n"
    "created_at: 2026-07-22T18:04:11+00:00\n"
    "source_session: 8b9b4c21-77f0-4a3e-9d2b-5f1e0c9a8b3d\n"
    'source_session_note: "[[2026-07-22-obsidian-brain-8b9b]]"\n'
    "project: obsidian-brain\n"
    "tags:\n"
    "  - claude/retro\n"
    "  - claude/project/obsidian-brain\n"
    "  - claude/auto\n"
    "---\n"
)

RETRO_BODY = (
    "\n"
    "# Retrospective — obsidian-brain\n"
    "\n"
    "## What Worked\n"
    "\n"
    "- The write-first pattern kept every session logged.\n"
    "\n"
    "## What Didn't\n"
    "\n"
    "- Sub-agent transcription ate the opening fence.\n"
    "\n"
    "## Process Improvements\n"
    "\n"
    "- Validate the fence at the write boundary.\n"
)

FENCELESS_NOTE = FENCELESS_FM + RETRO_BODY
HEALTHY_NOTE = "---\n" + FENCELESS_NOTE


def make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / SESSIONS).mkdir(parents=True)
    (vault / INSIGHTS).mkdir(parents=True)
    return vault


def write_note(vault: Path, folder: str, name: str, content: str) -> Path:
    p = vault / folder / name
    p.write_bytes(content.encode("utf-8"))
    return p


def scan(vault: Path, project=None):
    return mff.scan(str(vault), SESSIONS, INSIGHTS, 9999, project=project)


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


def test_check_is_discovered_by_registry():
    """list_checks() auto-discovers the module; a typo'd NAME would hide it."""
    import vault_doctor_checks
    importlib.reload(vault_doctor_checks)
    assert "missing-frontmatter-fence" in vault_doctor_checks.list_checks()


def test_module_exposes_check_interface():
    assert mff.NAME == "missing-frontmatter-fence"
    assert callable(mff.scan) and callable(mff.apply)
    assert isinstance(mff.DEFAULT_WINDOW_DAYS, int)
    # Not opt-in: this must run in the default all-checks sweep.
    assert getattr(mff, "OPT_IN", False) is False


# --------------------------------------------------------------------------
# Happy path: detect and repair
# --------------------------------------------------------------------------


def test_detects_and_repairs_fenceless_retro_note(tmp_path):
    vault = make_vault(tmp_path)
    note = write_note(
        vault, INSIGHTS, "2026-07-22-retro-8b9b.md", FENCELESS_NOTE
    )
    original = note.read_bytes()

    issues = scan(vault)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.check == "missing-frontmatter-fence"
    assert issue.note_path == str(note)
    assert issue.project == "obsidian-brain"
    assert issue.confidence == 0.99
    # The closing fence is the 11th line of the fenceless note.
    assert issue.extra["closing_fence_line"] == 11

    results = mff.apply(issues, str(tmp_path / "backups"))
    assert [r.status for r in results] == ["applied"]

    repaired = note.read_bytes()
    assert repaired == b"---\n" + original, (
        "repair must prepend exactly '---\\n' and change nothing else"
    )
    assert repaired.decode("utf-8") == HEALTHY_NOTE


def test_repaired_note_parses_as_frontmatter(tmp_path):
    """After repair the note is loadable by note_writer — the consumer that
    refuses these notes today with 'file does not open with a --- fence'."""
    import note_writer

    vault = make_vault(tmp_path)
    note = write_note(vault, INSIGHTS, "2026-06-12-retro-3a1c.md", FENCELESS_NOTE)

    before = note_writer._split_lines_lf_crlf(note.read_text(encoding="utf-8"))
    assert note_writer._split_frontmatter(before)[4] is not None, (
        "precondition: note_writer rejects the note before repair"
    )

    mff.apply(scan(vault), str(tmp_path / "backups"))

    after = note_writer._split_lines_lf_crlf(note.read_text(encoding="utf-8"))
    open_fence, fm_lines, close_fence, body, err = note_writer._split_frontmatter(after)
    assert err is None, f"repaired note still unparseable: {err}"
    assert open_fence.rstrip("\r\n") == "---"
    assert close_fence.rstrip("\r\n") == "---"
    assert "type: claude-retro\n" in fm_lines
    assert "  - claude/retro\n" in fm_lines
    assert "# Retrospective — obsidian-brain\n" in body


def test_scan_covers_both_sessions_and_insights_folders(tmp_path):
    vault = make_vault(tmp_path)
    write_note(vault, SESSIONS, "2026-05-08-sess-5f48.md", FENCELESS_NOTE)
    write_note(vault, INSIGHTS, "2026-05-08-insight-bf48.md", FENCELESS_NOTE)

    paths = {Path(i.note_path).parent.name for i in scan(vault)}
    assert paths == {SESSIONS, INSIGHTS}


def test_healthy_sibling_note_is_untouched_by_apply(tmp_path):
    vault = make_vault(tmp_path)
    broken = write_note(vault, INSIGHTS, "2026-06-13-retro-dc13.md", FENCELESS_NOTE)
    healthy = write_note(vault, INSIGHTS, "2026-06-13-retro-ok.md", HEALTHY_NOTE)
    healthy_before = healthy.read_bytes()

    mff.apply(scan(vault), str(tmp_path / "backups"))

    assert broken.read_bytes() == b"---\n" + FENCELESS_NOTE.encode("utf-8")
    assert healthy.read_bytes() == healthy_before


# --------------------------------------------------------------------------
# Preconditions — one test per rule, each failing ONLY that rule
# --------------------------------------------------------------------------


def test_precondition_1_note_with_opening_fence_not_flagged(tmp_path):
    """Rule 1: first line IS '---'.

    Note the guard is subsumed by rule 2 (a ``---`` line is not ``key:``
    shaped), so deleting rule 1 alone does not make this test fail. It is
    kept as an explicit statement of intent, and this test is the regression
    pin that a healthy note is never flagged, whichever guard rejects it.
    """
    vault = make_vault(tmp_path)
    write_note(vault, INSIGHTS, "2026-06-18-retro-8f72.md", HEALTHY_NOTE)
    assert scan(vault) == []


def test_precondition_2_first_line_not_key_shaped_not_flagged(tmp_path):
    """Rule 2: first line is a heading, not a ``key:`` line.

    Rules 1, 3 and 4 still hold — a closing fence exists and every line above
    it is frontmatter-shaped except the very first — so only rule 2 rejects.
    """
    vault = make_vault(tmp_path)
    content = "# Retrospective\n" + FENCELESS_FM + RETRO_BODY
    write_note(vault, INSIGHTS, "2026-06-25-retro-2a64.md", content)
    assert scan(vault) == []


def test_precondition_3_no_closing_fence_not_flagged(tmp_path):
    """Rule 3: no closing '---' anywhere.

    Rules 1, 2 and 4 hold: no opening fence, a ``key:`` first line, and EVERY
    line in the file is frontmatter-shaped — so rule 3 is the only thing that
    can reject it.
    """
    vault = make_vault(tmp_path)
    content = (
        FENCELESS_FM.replace("---\n", "")
        + "summary: this file never closes its frontmatter\n"
        + "  continued on an indented line\n"
        + "\n"
    )
    assert "---" not in content
    write_note(vault, INSIGHTS, "2026-07-05-retro-b304.md", content)
    assert scan(vault) == []


def test_precondition_4_prose_above_closing_fence_not_flagged(tmp_path):
    """Rule 4: a non-frontmatter-shaped line sits above the closing fence.

    Rules 1, 2 and 3 hold: no opening fence, a ``key:`` first line, and a
    closing ``---`` further down. Only the interloping prose line rejects.
    """
    vault = make_vault(tmp_path)
    content = (
        "type: claude-retro\n"
        "date: 2026-07-22\n"
        "This paragraph is prose, not frontmatter.\n"
        "project: obsidian-brain\n"
        "---\n"
        "\n"
        "# Retro\n"
    )
    write_note(vault, INSIGHTS, "2026-07-22-retro-prose.md", content)
    assert scan(vault) == []


def test_ordinary_markdown_with_colon_first_line_not_flagged(tmp_path):
    """A hand-written doc opening with a ``key: value``-looking line but with
    no closing fence must never be flagged — this is the shape the closing
    fence requirement exists to protect."""
    vault = make_vault(tmp_path)
    content = (
        "Note: this file is an ordinary markdown document.\n"
        "\n"
        "It has prose, a list, and no frontmatter at all.\n"
        "\n"
        "- one\n"
        "- two\n"
    )
    write_note(vault, INSIGHTS, "2026-07-01-ordinary.md", content)
    assert scan(vault) == []


def test_markdown_with_horizontal_rule_after_prose_not_flagged(tmp_path):
    """Ordinary markdown whose ``---`` is a horizontal rule below prose:
    rule 4 rejects it even though a '---' line exists."""
    vault = make_vault(tmp_path)
    content = (
        "Summary: quarterly notes\n"
        "\n"
        "Some prose paragraph that is clearly not frontmatter.\n"
        "\n"
        "---\n"
        "\n"
        "More prose.\n"
    )
    write_note(vault, INSIGHTS, "2026-07-02-hrule.md", content)
    assert scan(vault) == []


def test_empty_and_single_line_files_not_flagged(tmp_path):
    vault = make_vault(tmp_path)
    write_note(vault, INSIGHTS, "empty.md", "")
    write_note(vault, INSIGHTS, "one-line.md", "type: claude-retro\n")
    write_note(vault, INSIGHTS, "no-newline.md", "type: claude-retro")
    assert scan(vault) == []


def test_bom_prefixed_note_not_flagged(tmp_path):
    """A BOM before the first key breaks rule 2. Inserting a fence below the
    BOM would leave the note just as unparseable, so leave it alone."""
    vault = make_vault(tmp_path)
    write_note(vault, INSIGHTS, "bom.md", "\ufeff" + FENCELESS_NOTE)
    assert scan(vault) == []


def test_invalid_utf8_note_skipped(tmp_path):
    """Non-UTF8 bytes belong to the encoding-corruption check; scan must skip
    the file rather than crash or lossily re-encode it."""
    vault = make_vault(tmp_path)
    bad = vault / INSIGHTS / "bad-bytes.md"
    bad.write_bytes(b"type: claude-retro\ndesc: \xff\xfe bad\nproject: x\n---\n\n# R\n")
    good = write_note(vault, SESSIONS, "2026-06-12-retro-3a1c.md", FENCELESS_NOTE)

    issues = scan(vault)
    assert [i.note_path for i in issues] == [str(good)]
    assert bad.read_bytes().startswith(b"type: claude-retro")


# --------------------------------------------------------------------------
# Closing-fence search bound (exact-threshold pair; literal line counts)
# --------------------------------------------------------------------------


def _deep_fence_note(filler_lines: int) -> str:
    """``type:`` line, then N filler key lines, then the closing fence."""
    return (
        "type: claude-retro\n"
        + "".join(f"k{n}: v{n}\n" for n in range(filler_lines))
        + "---\n"
        + "\n# Retro\n"
    )


def test_closing_fence_at_last_allowed_line_is_flagged(tmp_path):
    """Closing fence on the 1000th line (index 999) is still repairable —
    after insertion it sits at index 1000, note_writer's own limit."""
    vault = make_vault(tmp_path)
    write_note(vault, INSIGHTS, "deep-ok.md", _deep_fence_note(998))
    assert len(scan(vault)) == 1


def test_closing_fence_one_line_too_deep_is_not_flagged(tmp_path):
    """One line deeper (index 1000) and the repaired note would still be
    rejected by note_writer's size limit, so do not touch it."""
    vault = make_vault(tmp_path)
    write_note(vault, INSIGHTS, "deep-too-far.md", _deep_fence_note(999))
    assert scan(vault) == []


def test_bound_matches_note_writer_after_repair(tmp_path):
    """Cross-check the bound against the real consumer: the deepest note this
    check repairs must parse through note_writer afterwards."""
    import note_writer

    vault = make_vault(tmp_path)
    note = write_note(vault, INSIGHTS, "deep-ok.md", _deep_fence_note(998))
    mff.apply(scan(vault), str(tmp_path / "backups"))
    lines = note_writer._split_lines_lf_crlf(note.read_text(encoding="utf-8"))
    assert note_writer._split_frontmatter(lines)[4] is None


# --------------------------------------------------------------------------
# Line endings, permissions, atomicity, backups, idempotency
# --------------------------------------------------------------------------


def test_crlf_note_repaired_with_crlf_preserved(tmp_path):
    vault = make_vault(tmp_path)
    crlf = FENCELESS_NOTE.replace("\n", "\r\n")
    note = write_note(vault, INSIGHTS, "2026-06-12-retro-crlf.md", crlf)

    results = mff.apply(scan(vault), str(tmp_path / "backups"))
    assert [r.status for r in results] == ["applied"]

    repaired = note.read_bytes()
    assert repaired.startswith(b"---\r\n"), (
        "inserted fence must use the note's own CRLF ending"
    )
    assert repaired == b"---\r\n" + crlf.encode("utf-8")
    # No LF that is not part of a CRLF pair: the file stayed uniformly CRLF.
    assert b"\n" not in repaired.replace(b"\r\n", b"")


def test_lf_note_gets_lf_fence_even_with_a_stray_cr(tmp_path):
    """A bare ``\\r`` inside the body (pasted progress bar) is not a line
    ending — the file is still LF-dominant and gets an LF fence."""
    vault = make_vault(tmp_path)
    content = FENCELESS_FM + "\nProgress: 50%\r75%\r100%\n\n# Retro\n"
    note = write_note(vault, INSIGHTS, "stray-cr.md", content)

    mff.apply(scan(vault), str(tmp_path / "backups"))
    assert note.read_bytes() == b"---\n" + content.encode("utf-8")


def test_file_mode_preserved(tmp_path):
    vault = make_vault(tmp_path)
    note = write_note(vault, INSIGHTS, "2026-06-18-retro-8f72.md", FENCELESS_NOTE)
    os.chmod(note, 0o640)

    results = mff.apply(scan(vault), str(tmp_path / "backups"))
    assert [r.status for r in results] == ["applied"]
    assert stat.S_IMODE(os.stat(note).st_mode) == 0o640


def test_backup_written_with_original_content(tmp_path):
    vault = make_vault(tmp_path)
    note = write_note(vault, INSIGHTS, "2026-06-25-retro-2a64.md", FENCELESS_NOTE)
    backup_root = tmp_path / "backups"

    results = mff.apply(scan(vault), str(backup_root))
    backup = Path(results[0].backup_path)
    assert backup.parent == backup_root / "missing-frontmatter-fence"
    assert backup.read_bytes() == FENCELESS_NOTE.encode("utf-8")
    assert not backup.read_text(encoding="utf-8").startswith("---")


def test_write_failure_leaves_file_byte_identical_and_no_temp_files(
    tmp_path, monkeypatch
):
    """os.replace failing mid-apply must leave the note untouched, report an
    error, and clean up the temp file."""
    vault = make_vault(tmp_path)
    note = write_note(vault, INSIGHTS, "2026-07-05-retro-b304.md", FENCELESS_NOTE)
    before = note.read_bytes()
    issues = scan(vault)

    def boom(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", boom)
    results = mff.apply(issues, str(tmp_path / "backups"))

    assert [r.status for r in results] == ["error"]
    assert "simulated rename failure" in results[0].error
    assert note.read_bytes() == before
    leftovers = [p.name for p in (vault / INSIGHTS).iterdir()
                 if p.name.startswith(".vd-fence-")]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_apply_is_idempotent_on_stale_issue_replay(tmp_path):
    """Replaying an Issue against an already-repaired note must skip, not
    prepend a second fence."""
    vault = make_vault(tmp_path)
    note = write_note(vault, INSIGHTS, "2026-07-22-retro-8b9b.md", FENCELESS_NOTE)
    issues = scan(vault)

    first = mff.apply(issues, str(tmp_path / "backups"))
    assert [r.status for r in first] == ["applied"]
    after_first = note.read_bytes()

    second = mff.apply(issues, str(tmp_path / "backups"))
    assert [r.status for r in second] == ["skipped"]
    assert "already repaired" in second[0].error
    assert note.read_bytes() == after_first
    assert not note.read_bytes().startswith(b"---\n---\n")


def test_rescan_after_repair_is_clean(tmp_path):
    vault = make_vault(tmp_path)
    write_note(vault, INSIGHTS, "2026-05-08-retro-5f48.md", FENCELESS_NOTE)
    mff.apply(scan(vault), str(tmp_path / "backups"))
    assert scan(vault) == []


def test_apply_reports_error_for_missing_file(tmp_path):
    vault = make_vault(tmp_path)
    note = write_note(vault, INSIGHTS, "gone.md", FENCELESS_NOTE)
    issues = scan(vault)
    note.unlink()

    results = mff.apply(issues, str(tmp_path / "backups"))
    assert [r.status for r in results] == ["error"]


def test_scan_does_not_modify_any_file(tmp_path):
    """Dry-run/report works without apply and writes nothing."""
    vault = make_vault(tmp_path)
    note = write_note(vault, INSIGHTS, "2026-06-13-retro-dc13.md", FENCELESS_NOTE)
    before = note.read_bytes()
    mtime_before = note.stat().st_mtime_ns

    assert len(scan(vault)) == 1
    assert note.read_bytes() == before
    assert note.stat().st_mtime_ns == mtime_before


# --------------------------------------------------------------------------
# Project filter
# --------------------------------------------------------------------------


def test_project_filter_matches_frontmatter_value(tmp_path):
    vault = make_vault(tmp_path)
    mine = write_note(vault, INSIGHTS, "mine.md", FENCELESS_NOTE)
    other = write_note(
        vault, INSIGHTS, "other.md",
        FENCELESS_NOTE.replace("project: obsidian-brain", "project: tiny-agent"),
    )

    assert [i.note_path for i in scan(vault, project="obsidian-brain")] == [str(mine)]
    assert [i.note_path for i in scan(vault, project="OBSIDIAN-BRAIN")] == [str(mine)]
    assert [i.note_path for i in scan(vault, project="tiny-agent")] == [str(other)]
    assert len(scan(vault)) == 2


def test_missing_vault_folders_return_no_issues(tmp_path):
    assert mff.scan(str(tmp_path / "nope"), SESSIONS, INSIGHTS, 9999) == []


# ---------------------------------------------------------------------------
# Rule (5): a `---` too close to the top is a setext H2, not a lost fence.
#
# `Text` + `---` is a first-class CommonMark construct, so these are ordinary
# markdown documents, not damaged notes. Repairing one would silently convert
# a valid <h2> into a frontmatter block and change how the document renders --
# and this check MUTATES user notes, so a cheap exclusion is worth having.
# ---------------------------------------------------------------------------

def test_setext_h2_heading_is_not_flagged(tmp_path):
    """The exact shape from the review: a `key: value`-looking line underlined
    with `---`. Before rule (5) this was flagged and would have been
    'repaired' into frontmatter."""
    note = tmp_path / "setext.md"
    note.write_text(
        "Summary: Q3 results\n"
        "---\n"
        "\n"
        "Body paragraph here.\n",
        encoding="utf-8",
    )
    assert mff._find_fenceless_frontmatter(note.read_text(encoding="utf-8")) is None


def test_lead_in_line_above_a_markdown_list_is_not_flagged(tmp_path):
    """Neighbour of the setext case: a lead-in line, a short list, then a
    horizontal rule. Every line is 'frontmatter-shaped' by the shape rules,
    so only the minimum-length floor separates it from a real note."""
    note = tmp_path / "list.md"
    note.write_text(
        "Shopping: this week\n"
        "- a\n"
        "---\n"
        "\n"
        "Rest of the document.\n",
        encoding="utf-8",
    )
    assert mff._find_fenceless_frontmatter(note.read_text(encoding="utf-8")) is None


def test_two_prose_lines_that_look_like_keys_are_not_flagged(tmp_path):
    note = tmp_path / "prose.md"
    note.write_text(
        "Note: this is prose\n"
        "Warning: so is this\n"
        "---\n"
        "\n"
        "Body.\n",
        encoding="utf-8",
    )
    assert mff._find_fenceless_frontmatter(note.read_text(encoding="utf-8")) is None


def test_real_damaged_note_at_the_floor_is_still_flagged(tmp_path):
    """The floor must not cost detection. Exactly _MIN_FRONTMATTER_LINES of
    frontmatter is still repaired -- this is the boundary case, and it is a
    LITERAL fixture built to the documented floor rather than derived from
    the constant, so raising the constant fails this test deliberately."""
    assert mff._MIN_FRONTMATTER_LINES == 3, "update this fixture deliberately"
    note = tmp_path / "damaged.md"
    note.write_text(
        "type: claude-insight\n"
        "date: 2026-01-01\n"
        "project: obsidian-brain\n"
        "---\n"
        "\n"
        "# Title\n",
        encoding="utf-8",
    )
    assert mff._find_fenceless_frontmatter(note.read_text(encoding="utf-8")) == 3


def test_one_line_below_the_floor_is_not_flagged(tmp_path):
    """The other side of the same boundary."""
    note = tmp_path / "shallow.md"
    note.write_text(
        "type: claude-insight\n"
        "date: 2026-01-01\n"
        "---\n"
        "\n"
        "# Title\n",
        encoding="utf-8",
    )
    assert mff._find_fenceless_frontmatter(note.read_text(encoding="utf-8")) is None
