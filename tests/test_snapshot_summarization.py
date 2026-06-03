import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import sqlite3

from hooks.obsidian_utils import find_unsummarized_notes, upgrade_unsummarized_note, _augment_session_input_with_snapshots, _note_has_inbound_links, _parse_note_tags


def _fixture(sess_dir: Path, name: str, type_: str, status: str, session_id: str, body: str = ""):
    p = sess_dir / name
    p.write_text(
        f"---\ntype: {type_}\ndate: 2026-04-18\nsession_id: {session_id}\n"
        f"project: demo\nstatus: {status}\n---\n\n# {name}\n\n{body}",
        encoding="utf-8",
    )


def _fixture_no_type(sess_dir: Path, name: str, status: str, session_id: str):
    p = sess_dir / name
    p.write_text(
        f"---\ndate: 2026-04-18\nsession_id: {session_id}\n"
        f"project: demo\nstatus: {status}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_find_unsummarized_picks_up_snapshots(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    _fixture(sess, "2026-04-18-demo-aaaa-snapshot-140000.md", "claude-snapshot", "auto-logged", "s1")
    _fixture(sess, "2026-04-18-demo-aaaa.md", "claude-session", "auto-logged", "s1")

    result = json.loads(find_unsummarized_notes(str(vault), "claude-sessions", "demo"))
    paths = result["unsummarized"]
    names = [Path(p).name for p in paths]
    assert any("snapshot-140000" in n for n in names)
    assert any(n == "2026-04-18-demo-aaaa.md" for n in names)


def test_find_unsummarized_skips_summarized_snapshots(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    _fixture(sess, "2026-04-18-demo-bbbb-snapshot-100000.md", "claude-snapshot", "summarized", "s2")

    result = json.loads(find_unsummarized_notes(str(vault), "claude-sessions", "demo"))
    assert result["unsummarized"] == []


def test_find_unsummarized_orders_snapshots_before_parent(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    _fixture(sess, "2026-04-18-demo-cccc.md", "claude-session", "auto-logged", "s3")
    _fixture(sess, "2026-04-18-demo-cccc-snapshot-120000.md", "claude-snapshot", "auto-logged", "s3")
    _fixture(sess, "2026-04-18-demo-cccc-snapshot-090000.md", "claude-snapshot", "auto-logged", "s3")

    result = json.loads(find_unsummarized_notes(str(vault), "claude-sessions", "demo"))
    names = [Path(p).name for p in result["unsummarized"]]
    # Snapshots before parent within the same session_id group
    assert names.index("2026-04-18-demo-cccc-snapshot-090000.md") < names.index("2026-04-18-demo-cccc.md")
    assert names.index("2026-04-18-demo-cccc-snapshot-120000.md") < names.index("2026-04-18-demo-cccc.md")


def test_find_unsummarized_rejects_non_session_non_snapshot_types(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    _fixture(sess, "2026-04-18-demo-dddd.md", "claude-session", "auto-logged", "s4")
    _fixture(sess, "2026-04-18-demo-dddd-insight.md", "claude-insight", "auto-logged", "s4")

    result = json.loads(find_unsummarized_notes(str(vault), "claude-sessions", "demo"))
    names = [Path(p).name for p in result["unsummarized"]]
    assert "2026-04-18-demo-dddd.md" in names
    assert "2026-04-18-demo-dddd-insight.md" not in names


def test_find_unsummarized_keeps_legacy_notes_without_type_field(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    _fixture_no_type(sess, "2026-04-18-demo-eeee-legacy.md", "auto-logged", "s5")

    result = json.loads(find_unsummarized_notes(str(vault), "claude-sessions", "demo"))
    names = [Path(p).name for p in result["unsummarized"]]
    assert "2026-04-18-demo-eeee-legacy.md" in names


def test_augment_defaults_missing_trigger_to_auto(tmp_path):
    """Regression for Copilot PR #43 round 2 finding: a snapshot without a
    `trigger:` frontmatter field emits `trigger=auto` in the augmented
    input, not `trigger=compact` — so the session summarizer isn't fed a
    misleading label.
    """
    sess = tmp_path / "claude-sessions"
    sess.mkdir()
    # No trigger field; summary is real so the block lands
    (sess / "2026-04-18-demo-zz-snapshot-140000.md").write_text(
        "---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: sz\n"
        "project: demo\nstatus: summarized\n"
        'source_session_note: "[[2026-04-18-demo-zz]]"\n'
        "---\n\n# T\n\n## Summary\nreal summary body\n",
        encoding="utf-8",
    )
    out = _augment_session_input_with_snapshots(
        "transcript tail",
        sessions_folder_path=sess,
        session_id="sz",
        date="2026-04-18",
        project="demo",
    )
    assert "[snapshot 14:00:00, trigger=auto]" in out or \
           "[snapshot 140000, trigger=auto]" in out
    assert "trigger=compact" not in out


def test_find_unsummarized_orders_snapshot_before_parent_with_quoted_type(tmp_path):
    """Regression for Copilot PR #43 round 2 finding: `_bias_key` reads the
    `type:` frontmatter value without stripping quotes. A snapshot that uses
    valid YAML quoting (`type: "claude-snapshot"`) previously sorted AFTER
    its parent because the raw quoted string != 'claude-snapshot'.
    """
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    parent = sess / "2026-04-18-demo-qqqq.md"
    parent.write_text(
        '---\ntype: "claude-session"\ndate: 2026-04-18\nsession_id: sq\n'
        'project: demo\nstatus: auto-logged\n---\n\n# Parent\n',
        encoding="utf-8",
    )
    # Double-quoted and single-quoted snapshot type declarations
    snap_double = sess / "2026-04-18-demo-qqqq-snapshot-100000.md"
    snap_double.write_text(
        '---\ntype: "claude-snapshot"\ndate: 2026-04-18\nsession_id: sq\n'
        'project: demo\nstatus: auto-logged\n---\n\n# Snap DQ\n',
        encoding="utf-8",
    )
    snap_single = sess / "2026-04-18-demo-qqqq-snapshot-110000.md"
    snap_single.write_text(
        "---\ntype: 'claude-snapshot'\ndate: 2026-04-18\nsession_id: sq\n"
        "project: demo\nstatus: auto-logged\n---\n\n# Snap SQ\n",
        encoding="utf-8",
    )

    result = json.loads(find_unsummarized_notes(str(vault), "claude-sessions", "demo"))
    names = [Path(p).name for p in result["unsummarized"]]
    # Both snapshots must sort before the parent (quote-stripping lets the
    # type check see 'claude-snapshot' rather than '"claude-snapshot"').
    assert names.index(snap_double.name) < names.index(parent.name)
    assert names.index(snap_single.name) < names.index(parent.name)


def test_snapshot_routes_through_snapshot_prompt(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    snap_path = sess / "2026-04-18-demo-dddd-snapshot-140000.md"
    snap_path.write_text(
        "---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: s4\n"
        "project: demo\ntrigger: compact\nstatus: auto-logged\n---\n\n"
        "# Context Snapshot: demo\n\n## What was happening\nWorking on X.\n\n"
        "## Last messages (raw)\n**User:** hi\n**Assistant:** hello\n",
        encoding="utf-8",
    )

    calls = []

    def fake_snapshot_summary(*args, **kwargs):
        calls.append(("snapshot", args, kwargs))
        return (
            "## Summary\nIn-flight work on X interrupted by /compact.\n\n"
            "## Key context that may be lost (summary)\n- Open decision: approach A vs B\n"
        ), None

    def fake_session_summary(*args, **kwargs):
        calls.append(("session", args, kwargs))
        return "## Summary\nshould-not-be-used\n", None

    with patch("hooks.obsidian_utils.generate_snapshot_summary", fake_snapshot_summary), \
         patch("hooks.obsidian_utils.generate_summary", fake_session_summary):
        result = upgrade_unsummarized_note(str(snap_path), str(vault), "claude-sessions", "demo")

    assert not result[0].startswith("Failed"), result[0]
    types_called = [c[0] for c in calls]
    assert types_called == ["snapshot"]
    # File now contains the snapshot-shaped summary
    assert "## Key context that may be lost (summary)" in snap_path.read_text(encoding="utf-8")


def test_session_routes_through_session_prompt(tmp_path):
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)
    sess_path = sess / "2026-04-18-demo-eeee.md"
    sess_path.write_text(
        "---\ntype: claude-session\ndate: 2026-04-18\nsession_id: s5\n"
        "project: demo\nstatus: auto-logged\n---\n\n"
        "# Session\n\n## Conversation (raw)\n"
        "**User:** hi\n**Assistant:** hello\n",
        encoding="utf-8",
    )

    calls = []

    def fake_snapshot_summary(*args, **kwargs):
        calls.append(("snapshot", args, kwargs))
        return "## Summary\nshould-not-be-used\n", None

    def fake_session_summary(*args, **kwargs):
        calls.append(("session", args, kwargs))
        return (
            "## Summary\nSession work.\n\n## Key Decisions\n- None noted.\n\n"
            "## Changes Made\n- None noted.\n\n## Errors Encountered\n- None.\n\n"
            "## Open Questions / Next Steps\n- [ ] None.\n\n## Importance\n5\n"
        ), None

    with patch("hooks.obsidian_utils.generate_snapshot_summary", fake_snapshot_summary), \
         patch("hooks.obsidian_utils.generate_summary", fake_session_summary):
        result = upgrade_unsummarized_note(str(sess_path), str(vault), "claude-sessions", "demo")

    assert not result[0].startswith("Failed"), result[0]
    types_called = [c[0] for c in calls]
    assert types_called == ["session"]


def test_augment_prepends_upgraded_snapshot_summaries(tmp_path):
    sess = tmp_path / "claude-sessions"
    sess.mkdir()
    snap = sess / "2026-04-18-demo-eeee-snapshot-143000.md"
    snap.write_text(
        "---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: s5\n"
        "project: demo\ntrigger: compact\nstatus: summarized\n---\n\n"
        "# Context Snapshot: demo\n\n"
        "## Summary\nMid-session decision on approach B.\n\n"
        "## Key context that may be lost (summary)\n- Open question about scalability.\n\n"
        "## Last messages (raw)\n**User:** earlier stuff\n",
        encoding="utf-8",
    )
    result = _augment_session_input_with_snapshots(
        transcript="current tail messages",
        sessions_folder_path=sess,
        session_id="s5",
        date="2026-04-18",
        project="demo",
    )
    assert "===== EARLIER IN THIS SESSION" in result
    assert "Mid-session decision on approach B." in result
    assert "current tail messages" in result
    # Summary section preferred over raw body
    assert "earlier stuff" not in result
    # Trigger annotation present
    assert "trigger=compact" in result


def test_augment_returns_transcript_unchanged_when_no_snapshots(tmp_path):
    sess = tmp_path / "claude-sessions"
    sess.mkdir()
    result = _augment_session_input_with_snapshots(
        "original", sess, "no-matches", "2026-04-18", "demo",
    )
    assert result == "original"


def test_augment_falls_back_to_raw_when_snapshot_not_yet_upgraded(tmp_path):
    sess = tmp_path / "claude-sessions"
    sess.mkdir()
    snap = sess / "2026-04-18-demo-ffff-snapshot-090000.md"
    snap.write_text(
        "---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: s6\n"
        "project: demo\ntrigger: clear\nstatus: auto-logged\n---\n\n"
        "# Context Snapshot: demo\n\n## What was happening\nRaw work in progress.\n",
        encoding="utf-8",
    )
    result = _augment_session_input_with_snapshots(
        "tail", sess, "s6", "2026-04-18", "demo",
    )
    assert "Raw work in progress." in result


def test_augment_omits_current_tail_banner_when_transcript_empty(tmp_path):
    sess = tmp_path / "claude-sessions"
    sess.mkdir()
    snap = sess / "2026-04-18-demo-gggg-snapshot-110000.md"
    snap.write_text(
        "---\ntype: claude-snapshot\ndate: 2026-04-18\nsession_id: s7\n"
        "project: demo\ntrigger: compact\nstatus: summarized\n---\n\n"
        "## Summary\nSome mid-session work.\n",
        encoding="utf-8",
    )
    result = _augment_session_input_with_snapshots(
        "", sess, "s7", "2026-04-18", "demo",
    )
    assert "===== EARLIER IN THIS SESSION" in result
    assert "Some mid-session work." in result
    # No dangling tail banner when transcript is empty (preamble mode)
    assert "CURRENT TAIL" not in result


def test_find_unsummarized_keeps_notes_with_empty_type_field(tmp_path):
    """Issue #94 site D — empty `type:` was previously cross-newline-captured
    into a non-allowlisted value and silently skipped. After the fix, empty
    `type:` returns None from the helper and is treated as legacy (kept).
    """
    vault = tmp_path / "v"
    sess = vault / "claude-sessions"
    sess.mkdir(parents=True)

    note = sess / "2026-04-24-demo-hhhh-legacy-empty-type.md"
    note.write_text(
        "---\n"
        "project: demo\n"
        "type: \n"
        "status: auto-logged\n"
        "date: 2026-04-24\n"
        "---\n"
        "# Session\n\n"
        "## Conversation (raw)\n"
        "**User:** hello\n**Assistant:** world\n",
        encoding="utf-8",
    )

    result = json.loads(find_unsummarized_notes(str(vault), "claude-sessions", "demo"))
    names = [Path(p).name for p in result["unsummarized"]]
    assert note.name in names, (
        f"Note with empty `type:` should be kept as legacy but was skipped; "
        f"returned: {names}"
    )


# ---------------------------------------------------------------------------
# TestAgedNoteDeferral — #168: defer aged-out, unreferenced session notes
# ---------------------------------------------------------------------------

def _aged_fixture(sess_dir: Path, name: str, mtime_offset_days: float,
                  tags: str = "", extra_frontmatter: str = "") -> Path:
    """Write a minimal auto-logged session note and set its mtime."""
    tags_line = f"tags: {tags}\n" if tags else ""
    p = sess_dir / name
    p.write_text(
        f"---\ntype: claude-session\ndate: 2026-01-01\nsession_id: aged-sid\n"
        f"project: demo\nstatus: auto-logged\n{tags_line}{extra_frontmatter}"
        f"---\n\n# {name}\n\n## Conversation (raw)\n**User:** hi\n**Assistant:** hey\n",
        encoding="utf-8",
    )
    mtime = time.time() - mtime_offset_days * 86400
    os.utime(p, (mtime, mtime))
    return p


class TestAgedNoteDeferral:
    """Tests for the aged-note deferral heuristic added in #168."""

    def test_fresh_and_linked_aged_summarized_unlinked_aged_skipped(self, tmp_path):
        """A (fresh) and C (aged, has inbound link) appear in unsummarized;
        B (aged, no link) is deferred to skipped_aged."""
        vault = tmp_path / "v"
        sess = vault / "claude-sessions"
        sess.mkdir(parents=True)

        note_a = _aged_fixture(sess, "2026-01-01-demo-note-a.md", mtime_offset_days=1)
        note_b = _aged_fixture(sess, "2026-01-01-demo-note-b.md", mtime_offset_days=120)
        note_c = _aged_fixture(sess, "2026-01-01-demo-note-c.md", mtime_offset_days=120)

        def fake_inbound(stem, db_path=None):
            # B has no links; C does
            return stem == note_c.stem

        with patch("hooks.obsidian_utils._note_has_inbound_links", side_effect=fake_inbound):
            result = json.loads(
                find_unsummarized_notes(str(vault), "claude-sessions", "demo",
                                        aged_threshold_days=90)
            )

        unsummarized_names = {Path(p).name for p in result["unsummarized"]}
        skipped_names = {Path(p).name for p in result["skipped_aged"]}

        assert note_a.name in unsummarized_names, "fresh note A must be in unsummarized"
        assert note_c.name in unsummarized_names, "aged-but-linked note C must be in unsummarized"
        assert note_b.name in skipped_names, "aged-unlinked note B must be in skipped_aged"
        assert note_b.name not in unsummarized_names, "note B must NOT appear in unsummarized"

    def test_include_aged_bypasses_deferral(self, tmp_path):
        """With include_aged=True, all notes appear in unsummarized regardless of age."""
        vault = tmp_path / "v"
        sess = vault / "claude-sessions"
        sess.mkdir(parents=True)

        note_b = _aged_fixture(sess, "2026-01-01-demo-note-b2.md", mtime_offset_days=120)

        def fake_inbound(stem, db_path=None):
            return False  # no links

        with patch("hooks.obsidian_utils._note_has_inbound_links", side_effect=fake_inbound):
            result = json.loads(
                find_unsummarized_notes(str(vault), "claude-sessions", "demo",
                                        aged_threshold_days=90, include_aged=True)
            )

        unsummarized_names = {Path(p).name for p in result["unsummarized"]}
        assert note_b.name in unsummarized_names, "B must be in unsummarized when include_aged=True"
        assert result["skipped_aged"] == [], "skipped_aged must be empty when include_aged=True"

    def test_pinned_aged_note_not_deferred(self, tmp_path):
        """An aged note with a pin tag is never deferred, even without inbound links."""
        vault = tmp_path / "v"
        sess = vault / "claude-sessions"
        sess.mkdir(parents=True)

        note_pinned = _aged_fixture(sess, "2026-01-01-demo-note-pinned.md",
                                    mtime_offset_days=120, tags="[claude/keep]")

        def fake_inbound(stem, db_path=None):
            return False  # no links

        with patch("hooks.obsidian_utils._note_has_inbound_links", side_effect=fake_inbound):
            result = json.loads(
                find_unsummarized_notes(str(vault), "claude-sessions", "demo",
                                        aged_threshold_days=90,
                                        pin_tags=["claude/keep", "claude/permanent"])
            )

        unsummarized_names = {Path(p).name for p in result["unsummarized"]}
        skipped_names = {Path(p).name for p in result["skipped_aged"]}
        assert note_pinned.name in unsummarized_names, "pinned note must be in unsummarized"
        assert note_pinned.name not in skipped_names, "pinned note must NOT be in skipped_aged"

    def test_max_age_days_override(self, tmp_path):
        """aged_threshold_days param overrides the default threshold."""
        vault = tmp_path / "v"
        sess = vault / "claude-sessions"
        sess.mkdir(parents=True)

        # 30 days old — within default 90d threshold, but beyond a 14d override
        note = _aged_fixture(sess, "2026-01-01-demo-note-30d.md", mtime_offset_days=30)

        def fake_inbound(stem, db_path=None):
            return False

        with patch("hooks.obsidian_utils._note_has_inbound_links", side_effect=fake_inbound):
            # With default threshold (90d): 30d note is NOT aged → in unsummarized
            result_default = json.loads(
                find_unsummarized_notes(str(vault), "claude-sessions", "demo",
                                        aged_threshold_days=90)
            )
            # With 14d threshold: 30d note IS aged → in skipped_aged
            result_tight = json.loads(
                find_unsummarized_notes(str(vault), "claude-sessions", "demo",
                                        aged_threshold_days=14)
            )

        assert note.name in {Path(p).name for p in result_default["unsummarized"]}, \
            "30d note should be in unsummarized with default 90d threshold"
        assert note.name in {Path(p).name for p in result_tight["skipped_aged"]}, \
            "30d note should be in skipped_aged with 14d threshold"
        assert note.name not in {Path(p).name for p in result_tight["unsummarized"]}, \
            "30d note must NOT be in unsummarized with 14d threshold"

    def test_inbound_link_helper_conservative_on_missing_index(self):
        """_note_has_inbound_links returns True (conservative) when DB doesn't exist."""
        import uuid
        missing_db = f"/tmp/does-not-exist-{uuid.uuid4().hex}.db"
        result = _note_has_inbound_links("nonexistent-stem", db_path=missing_db)
        assert result is True, "must return True (conservative) when index DB is missing"

    def test_pinned_aged_note_block_list_tags_not_deferred(self, tmp_path):
        """Aged note with pin tag in YAML block-list form is NOT deferred (#168 Fix 1).

        obsidian_session_log.py writes tags as a YAML block-list. Without Fix 1,
        parse_frontmatter_field returns None for block-list tags, making the pin
        check see no tags and wrongly defer the note.
        """
        vault = tmp_path / "v"
        sess = vault / "claude-sessions"
        sess.mkdir(parents=True)

        # Write note with YAML block-list tags (real on-disk format from session log)
        block_list_fm = (
            "type: claude-session\ndate: 2026-01-01\nsession_id: aged-sid\n"
            "project: demo\nstatus: auto-logged\n"
            "tags:\n  - claude/session\n  - claude/keep\n"
        )
        note_pinned = tmp_path / "v" / "claude-sessions" / "2026-01-01-demo-note-pinned-bl.md"
        note_pinned.write_text(
            f"---\n{block_list_fm}---\n\n# Block-list pin\n\n## Conversation (raw)\n**User:** hi\n",
            encoding="utf-8",
        )
        # Age the note past the threshold
        mtime = time.time() - 120 * 86400
        os.utime(note_pinned, (mtime, mtime))

        def fake_inbound(stem, db_path=None):
            return False  # no inbound links — deferral depends only on pin tag

        with patch("hooks.obsidian_utils._note_has_inbound_links", side_effect=fake_inbound):
            result = json.loads(
                find_unsummarized_notes(str(vault), "claude-sessions", "demo",
                                        aged_threshold_days=90,
                                        pin_tags=["claude/keep", "claude/permanent"])
            )

        unsummarized_names = {Path(p).name for p in result["unsummarized"]}
        skipped_names = {Path(p).name for p in result["skipped_aged"]}
        assert note_pinned.name in unsummarized_names, \
            "block-list-pinned note must be in unsummarized (not deferred)"
        assert note_pinned.name not in skipped_names, \
            "block-list-pinned note must NOT be in skipped_aged"

    def test_threshold_boundary_not_deferred(self, tmp_path):
        """A note aged ONE SECOND LESS than the threshold is NOT deferred.

        The deferral check is strict ``>``; a note that is (threshold - 1s) old
        must appear in unsummarized, not skipped_aged. This verifies the strict
        boundary semantics without relying on sub-millisecond mtime precision.
        (#168 Fix 1 / boundary semantics)
        """
        vault = tmp_path / "v"
        sess = vault / "claude-sessions"
        sess.mkdir(parents=True)

        note = _aged_fixture(sess, "2026-01-01-demo-boundary.md", mtime_offset_days=0)
        # Set mtime to 1 second less than the threshold — must NOT trigger deferral
        under_threshold_mtime = time.time() - (90 * 86400 - 1)
        os.utime(note, (under_threshold_mtime, under_threshold_mtime))

        def fake_inbound(stem, db_path=None):
            return False

        with patch("hooks.obsidian_utils._note_has_inbound_links", side_effect=fake_inbound):
            result = json.loads(
                find_unsummarized_notes(str(vault), "claude-sessions", "demo",
                                        aged_threshold_days=90)
            )

        unsummarized_names = {Path(p).name for p in result["unsummarized"]}
        assert note.name in unsummarized_names, \
            "note aged (threshold - 1s) must NOT be deferred (strict > check)"
        assert result["skipped_aged"] == [], "skipped_aged must be empty for under-threshold note"

    def test_skipped_aged_empty_for_all_fresh_default(self, tmp_path):
        """All-fresh notes always appear in unsummarized; skipped_aged is empty.

        Anchors the happy-path contract: with freshly-written notes the age gate
        never fires, so no monkeypatching of _note_has_inbound_links is needed.
        """
        vault = tmp_path / "v"
        sess = vault / "claude-sessions"
        sess.mkdir(parents=True)

        note_a = _aged_fixture(sess, "2026-01-01-demo-fresh-a.md", mtime_offset_days=0)
        note_b = _aged_fixture(sess, "2026-01-01-demo-fresh-b.md", mtime_offset_days=1)

        result = json.loads(
            find_unsummarized_notes(str(vault), "claude-sessions", "demo",
                                    aged_threshold_days=90)
        )

        unsummarized_names = {Path(p).name for p in result["unsummarized"]}
        assert note_a.name in unsummarized_names, "fresh note A must be in unsummarized"
        assert note_b.name in unsummarized_names, "fresh note B must be in unsummarized"
        assert result["skipped_aged"] == [], "skipped_aged must be empty for all-fresh notes"


# ---------------------------------------------------------------------------
# Unit tests for _parse_note_tags
# ---------------------------------------------------------------------------

class TestParseNoteTags:
    """Unit tests for the _parse_note_tags helper added in #168 Fix 1."""

    def test_inline_list_form(self):
        """Parses YAML inline list: tags: [a, b]"""
        fm = "---\ntags: [claude/session, claude/keep]\n---"
        assert _parse_note_tags(fm) == ["claude/session", "claude/keep"]

    def test_block_list_form(self):
        """Parses YAML block-list form written by obsidian_session_log.py."""
        fm = "---\ntags:\n  - claude/session\n  - claude/keep\n---"
        assert _parse_note_tags(fm) == ["claude/session", "claude/keep"]

    def test_absent_tags_returns_empty(self):
        """Returns [] when no tags key is present."""
        fm = "---\nstatus: auto-logged\nproject: demo\n---"
        assert _parse_note_tags(fm) == []

    def test_both_forms_parsed_equivalently(self):
        """Inline and block-list forms for the same tags produce equal results."""
        inline_fm = "---\ntags: [a, b]\n---"
        block_fm = "---\ntags:\n  - a\n  - b\n---"
        assert _parse_note_tags(inline_fm) == _parse_note_tags(block_fm) == ["a", "b"]

    def test_inline_with_quotes(self):
        """Strips quotes from inline tag values."""
        fm = '---\ntags: ["claude/keep", "claude/session"]\n---'
        tags = _parse_note_tags(fm)
        assert "claude/keep" in tags
        assert "claude/session" in tags

    def test_block_list_with_quotes(self):
        """Strips quotes from block-list tag values."""
        fm = '---\ntags:\n  - "claude/keep"\n  - "claude/session"\n---'
        tags = _parse_note_tags(fm)
        assert "claude/keep" in tags
        assert "claude/session" in tags


# ---------------------------------------------------------------------------
# Integration tests for _note_has_inbound_links with a real SQLite DB
# ---------------------------------------------------------------------------

def _make_notes_db(tmp_path: Path) -> Path:
    """Create a minimal vault-index-style SQLite DB with a notes table."""
    db = tmp_path / "test-vault.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE notes (path TEXT PRIMARY KEY, body TEXT)")
    conn.commit()
    conn.close()
    return db


class TestNoteHasInboundLinksRealDB:
    """Tests for _note_has_inbound_links using a real SQLite DB (Fix 2)."""

    def test_real_db_match(self, tmp_path):
        """Returns True when a note body contains [[stem]], False otherwise."""
        db = _make_notes_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO notes VALUES (?, ?)",
                     ("other-note.md", "See also [[my-stem]] for details."))
        conn.commit()
        conn.close()

        assert _note_has_inbound_links("my-stem", db_path=str(db)) is True, \
            "must return True when body contains [[my-stem]]"
        assert _note_has_inbound_links("unrelated-stem", db_path=str(db)) is False, \
            "must return False for a stem not referenced by any note"

    def test_escapes_wildcards_underscore(self, tmp_path):
        """Underscore in stem is treated as a literal, not a LIKE single-char wildcard.

        Inserts a body containing [[a_b]] and checks that:
        - 'a_b' → True  (exact match)
        - 'axb' → False (would match if _ were not escaped)
        """
        db = _make_notes_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO notes VALUES (?, ?)",
                     ("linking-note.md", "Check [[a_b]] for context."))
        conn.commit()
        conn.close()

        assert _note_has_inbound_links("a_b", db_path=str(db)) is True, \
            "a_b must match [[a_b]] (literal underscore)"
        assert _note_has_inbound_links("axb", db_path=str(db)) is False, \
            "axb must NOT match [[a_b]] — underscore must be escaped, not a wildcard"

    def test_escapes_wildcards_percent(self, tmp_path):
        """Percent in stem is treated as a literal, not a LIKE multi-char wildcard."""
        db = _make_notes_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO notes VALUES (?, ?)",
                     ("linking-note.md", "See [[a%b]] here."))
        conn.commit()
        conn.close()

        assert _note_has_inbound_links("a%b", db_path=str(db)) is True, \
            "a%b must match [[a%b]] (literal percent)"
        # A stem without percent should NOT match due to escaping
        assert _note_has_inbound_links("ab", db_path=str(db)) is False, \
            "ab must NOT match [[a%b]] — percent must be escaped, not a wildcard"
