"""Tests for vault_index._parse_note / _parse_note_detailed adopting the
shared frontmatter splitter (#277 task 2).

Before this change, ``_parse_note`` scanned only ``lines[1:40]`` for the
closing frontmatter fence, silently dropping any note whose fence sat deeper
(28 real notes in the live vault, /emerge and /standup output with long
``projects:`` lists, closing fences as deep as line 460). This file proves
the adopted ``frontmatter.split_frontmatter`` splitter fixes that while
preserving every existing behaviour of ``_parse_note``.
"""
from __future__ import annotations

import vault_index


# ---------------------------------------------------------------------------
# Load-bearing regression: frontmatter deeper than the old 40-line bound
# ---------------------------------------------------------------------------


def _deep_frontmatter_note(num_projects: int = 250) -> str:
    """Build a note whose frontmatter closing fence sits well past line 40 --
    a realistic /standup-style note with a long `projects:` list."""
    lines = [
        "---",
        "type: standup",
        "project: obsidian-brain",
        "date: 2026-07-20",
        "tags:",
        "  - claude/standup",
        "  - claude/auto",
        "projects:",
    ]
    for i in range(num_projects):
        lines.append(f"  - project-{i:04d}")
    lines.append("---")
    lines.append("")
    lines.append("# Standup 2026-07-20")
    lines.append("")
    lines.append("First paragraph of the body.")
    lines.append("")
    lines.append("Second paragraph, after a blank line.")
    lines.append("")
    return "\n".join(lines)


def test_deep_frontmatter_note_parses_with_correct_fields(tmp_path):
    note_path = tmp_path / "deep-standup.md"
    note_path.write_text(_deep_frontmatter_note(), encoding="utf-8")

    parsed = vault_index._parse_note(str(note_path))

    assert parsed is not None
    assert parsed["type"] == "standup"
    assert parsed["project"] == "obsidian-brain"
    assert parsed["date"] == "2026-07-20"
    assert parsed["tags"] == "claude/standup,claude/auto"
    assert parsed["body"] == (
        "# Standup 2026-07-20\n\n"
        "First paragraph of the body.\n\n"
        "Second paragraph, after a blank line."
    )
    # No title: in frontmatter -> falls back to the first H1 in the body.
    assert parsed["title"] == "Standup 2026-07-20"


def test_deep_frontmatter_note_fails_under_old_40_line_bound(tmp_path):
    """Prove the fixture above is actually load-bearing: replaying the OLD
    bounded scan (lines[1:40]) against the same fixture must fail to find
    the closing fence, because it sits past line 40 (8 header lines + 250
    project lines + 1 closing fence = line 259)."""
    text = _deep_frontmatter_note()
    lines = text.split("\n")
    assert lines[0].strip() == "---"

    end_idx = None
    for idx, line in enumerate(lines[1:40], start=1):
        if line.strip() == "---":
            end_idx = idx
            break

    assert end_idx is None, (
        "fixture is not load-bearing: the old 40-line-bounded scan found "
        "the closing fence, so it would not have caught the #277 bug"
    )


# ---------------------------------------------------------------------------
# Byte-exact round-trip: line-ending doubling trap
# ---------------------------------------------------------------------------


def test_body_with_blank_lines_round_trips_byte_exactly(tmp_path):
    """split_frontmatter/split_lines_lf_crlf preserve line terminators, so
    the body must be reassembled with "".join(...), not "\\n".join(...) --
    the latter would double every blank line in the body."""
    note_path = tmp_path / "multi-paragraph.md"
    note_path.write_text(
        "---\n"
        "type: insight\n"
        "project: obsidian-brain\n"
        "---\n"
        "Paragraph one, line one.\n"
        "Paragraph one, line two.\n"
        "\n"
        "Paragraph two.\n"
        "\n"
        "\n"
        "Paragraph three, after two blank lines.\n",
        encoding="utf-8",
    )

    parsed = vault_index._parse_note(str(note_path))

    assert parsed is not None
    assert parsed["body"] == (
        "Paragraph one, line one.\n"
        "Paragraph one, line two.\n"
        "\n"
        "Paragraph two.\n"
        "\n"
        "\n"
        "Paragraph three, after two blank lines."
    )


# ---------------------------------------------------------------------------
# Fenceless / unclosed frontmatter must not silently harvest body metadata
# ---------------------------------------------------------------------------


def test_note_with_no_closing_fence_and_body_rule_returns_none(tmp_path):
    """A note whose frontmatter never closes (the shape check stops at the
    first non-frontmatter-shaped line) but whose BODY happens to contain a
    '---' horizontal rule must return None -- not silently treat the rule as
    the closing fence and harvest a title/tags from what is actually body
    prose. This is the exact bug class split_frontmatter's shape check
    exists to prevent."""
    note_path = tmp_path / "unclosed.md"
    note_path.write_text(
        "---\n"
        "type: session\n"
        "# This heading breaks the frontmatter shape check\n"
        "\n"
        "Some prose that looks like it could be a body.\n"
        "\n"
        "---\n"
        "\n"
        "More text after a stray horizontal rule.\n",
        encoding="utf-8",
    )

    parsed = vault_index._parse_note(str(note_path))

    assert parsed is None

    parsed_detailed, err = vault_index._parse_note_detailed(str(note_path))
    assert parsed_detailed is None
    assert err is not None
    assert "not frontmatter" in err or "no closing" in err


def test_note_missing_opening_fence_returns_none(tmp_path):
    note_path = tmp_path / "no-fence.md"
    note_path.write_text("Just a plain markdown file.\n\nNo frontmatter here.\n", encoding="utf-8")

    assert vault_index._parse_note(str(note_path)) is None
    parsed, err = vault_index._parse_note_detailed(str(note_path))
    assert parsed is None
    assert "does not open with a '---' fence" in err


def test_unreadable_file_returns_none(tmp_path):
    missing_path = tmp_path / "does-not-exist.md"
    assert vault_index._parse_note(str(missing_path)) is None
    parsed, err = vault_index._parse_note_detailed(str(missing_path))
    assert parsed is None
    assert err is not None
    assert "unreadable file" in err


# ---------------------------------------------------------------------------
# Existing behaviours that must survive unchanged
# ---------------------------------------------------------------------------


def test_normal_short_note_round_trips_unchanged(tmp_path):
    note_path = tmp_path / "short.md"
    note_path.write_text(
        "---\n"
        "type: session\n"
        'project: "obsidian-brain"\n'
        "date: 2026-07-20\n"
        "tags:\n"
        "  - claude/session\n"
        "  - claude/auto\n"
        "source_session_note: '[[2026-07-19-session]]'\n"
        "---\n"
        "Just a short body.\n",
        encoding="utf-8",
    )

    parsed = vault_index._parse_note(str(note_path))

    assert parsed == {
        "type": "session",
        "project": "obsidian-brain",
        "date": "2026-07-20",
        "tags": "claude/session,claude/auto",
        "body": "Just a short body.",
        "title": "",
        "source_note": "2026-07-19-session",
    }


def test_title_falls_back_to_first_h1_in_body(tmp_path):
    note_path = tmp_path / "no-title-field.md"
    note_path.write_text(
        "---\n"
        "type: insight\n"
        "---\n"
        "\n"
        "# The Real Title\n"
        "\n"
        "Body text.\n",
        encoding="utf-8",
    )

    parsed = vault_index._parse_note(str(note_path))

    assert parsed["title"] == "The Real Title"


def test_explicit_title_field_takes_precedence_over_body_h1(tmp_path):
    note_path = tmp_path / "explicit-title.md"
    note_path.write_text(
        "---\n"
        "type: insight\n"
        "title: Explicit Title\n"
        "---\n"
        "# A Different Heading\n",
        encoding="utf-8",
    )

    parsed = vault_index._parse_note(str(note_path))

    assert parsed["title"] == "Explicit Title"
