"""Migration parity tests for issue #94 — verifies each migrated site
returns the same value as the old regex on a non-empty fixture, and
returns None / empty / skip-equivalent on an empty fixture.

Pins the safe-helper migration to behavioral equivalence so future
refactors do not silently regress.
"""
from __future__ import annotations

import pytest

from obsidian_utils import parse_frontmatter_field


# (key, non_empty_value, expected_after_strip)
SCALAR_KEYS = [
    ("project", "obsidian-brain", "obsidian-brain"),
    ("project", '"obsidian-brain"', "obsidian-brain"),
    ("project", "'obsidian-brain'", "obsidian-brain"),
    ("date", "2026-05-07", "2026-05-07"),
    ("status", "auto-logged", "auto-logged"),
    ("type", "claude-session", "claude-session"),
    ("session_id", "ce4d-1234-5678", "ce4d-1234-5678"),
]


@pytest.mark.parametrize("key,value,expected", SCALAR_KEYS)
def test_helper_reads_non_empty_scalar(key, value, expected):
    fm = f"---\n{key}: {value}\nother: x\n---\nbody\n"
    assert parse_frontmatter_field(fm, key) == expected


@pytest.mark.parametrize("key", ["project", "date", "status", "type", "session_id"])
def test_helper_returns_none_for_empty_value(key):
    """Issue #94: empty value must NOT cross newline to next key."""
    fm = f"---\n{key}: \nnext_key: trap-value\nproject_path: \"/\"\n---\n"
    assert parse_frontmatter_field(fm, key) is None


@pytest.mark.parametrize("key", ["project", "date", "status", "type", "session_id"])
def test_helper_returns_none_for_absent_key(key):
    fm = "---\nunrelated: foo\n---\n"
    assert parse_frontmatter_field(fm, key) is None


def test_head_buffer_with_no_closing_fence():
    """Simulates vault_stats's 2 KB head read where the closing --- may
    not appear within the buffer."""
    head = "---\nsession_id: abc-123\nproject: obsidian-brain\n# truncated before closing fence\n"
    assert parse_frontmatter_field(head, "session_id") == "abc-123"


def test_full_file_content_passes_through():
    """Simulates project_name_normalization passing the whole file."""
    content = (
        "---\n"
        "project: my_project\n"
        "type: claude-session\n"
        "---\n"
        "## Body\n"
        "Some text mentioning project: not-a-real-key\n"
    )
    # Helper restricts to frontmatter slice, so body line does NOT match.
    assert parse_frontmatter_field(content, "project") == "my_project"


def test_empty_type_treated_as_legacy_keep():
    """Code review observation on Task 4 site D: empty `type:` was previously
    skipped (cross-newline bug captured next line, then `not in (allowed)` ->
    continue). After fix, empty `type:` returns None, which makes the
    surrounding `if type_val and type_val not in (...)` check skip the
    filter -- i.e., the note is KEPT as legacy. Pins this behavioral change."""
    fm = "---\ntype: \nother: x\n---\n"
    assert parse_frontmatter_field(fm, "type") is None
