"""Unit tests for parse_frontmatter_field() — issue #94.

Pins the helper's contract: horizontal whitespace only between key and
value, empty values map to None, no cross-newline regex bug.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Add hooks/ to sys.path so we can import obsidian_utils without installing.
HOOKS_DIR = Path(__file__).resolve().parents[1] / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from obsidian_utils import parse_frontmatter_field  # noqa: E402


# ---------- Happy path ----------

def test_present_plain_value():
    content = "---\nproject: my-app\n---\n"
    assert parse_frontmatter_field(content, "project") == "my-app"


def test_present_double_quoted():
    content = '---\nproject: "my-app"\n---\n'
    assert parse_frontmatter_field(content, "project") == "my-app"


def test_present_single_quoted():
    content = "---\nproject: 'my-app'\n---\n"
    assert parse_frontmatter_field(content, "project") == "my-app"


def test_present_with_leading_tabs():
    content = "---\nproject:\t\tmy-app\n---\n"
    assert parse_frontmatter_field(content, "project") == "my-app"


def test_value_contains_colon():
    content = "---\ndate: 2026-05-07T12:00:00Z\n---\n"
    assert parse_frontmatter_field(content, "date") == "2026-05-07T12:00:00Z"


# ---------- Empty / missing — must return None ----------

def test_empty_value_does_not_cross_newline():
    """Issue #94 regression — empty `project:` must NOT capture next YAML key."""
    content = '---\nproject: \nproject_path: "/"\ntype: claude-session\n---\n'
    assert parse_frontmatter_field(content, "project") is None


def test_empty_value_no_trailing_space():
    content = '---\nproject:\nproject_path: "/"\n---\n'
    assert parse_frontmatter_field(content, "project") is None


def test_empty_double_quoted():
    content = '---\nproject: ""\n---\n'
    assert parse_frontmatter_field(content, "project") is None


def test_empty_single_quoted():
    content = "---\nproject: ''\n---\n"
    assert parse_frontmatter_field(content, "project") is None


def test_absent_key():
    content = "---\ndate: 2026-05-07\n---\n"
    assert parse_frontmatter_field(content, "project") is None


def test_no_frontmatter_fence():
    content = "project: my-app\n"
    # Helper still tolerates content without --- fences (treats whole content
    # as search region), but only via the safe regex.
    assert parse_frontmatter_field(content, "project") == "my-app"


def test_unterminated_frontmatter():
    """No closing `---`. Helper falls back to scanning the whole buffer."""
    content = "---\nproject: my-app\n(no close fence ever appears)\n"
    assert parse_frontmatter_field(content, "project") == "my-app"


def test_key_prefix_collision():
    """Parsing `project:` must NOT match `project_path:`."""
    content = '---\nproject_path: "/foo"\n---\n'
    assert parse_frontmatter_field(content, "project") is None


def test_empty_content():
    assert parse_frontmatter_field("", "project") is None
