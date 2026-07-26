"""Tests for hooks/frontmatter.py — the shared frontmatter splitter (#277).

Extracted from note_writer.py (#269), which had already worked out the
bounded, shape-checked closing-fence scan; tests/test_note_writer.py is the
faithfulness guard for that move (it must keep passing completely unchanged)
and is deliberately NOT duplicated here. This file tests the new module
directly, in isolation, so it can be imported by other callers (e.g.
vault_index.py, #277 task 2) without hand-copying the logic again.
"""
from __future__ import annotations

import frontmatter


# ---------------------------------------------------------------------------
# split_lines_lf_crlf
# ---------------------------------------------------------------------------

def test_split_lines_lf_only():
    text = "a\nb\nc"
    assert frontmatter.split_lines_lf_crlf(text) == ["a\n", "b\n", "c"]


def test_split_lines_crlf():
    text = "a\r\nb\r\nc\r\n"
    assert frontmatter.split_lines_lf_crlf(text) == ["a\r\n", "b\r\n", "c\r\n"]


def test_split_lines_bare_cr_not_treated_as_terminator():
    """A bare '\\r' not part of a '\\r\\n' pair (e.g. a pasted terminal
    progress-bar redraw) must NOT be treated as a line break -- str.splitlines
    would split it, which is exactly the corruption this function exists to
    avoid."""
    text = "progress: 10%\r20%\r30%\ndone\n"
    lines = frontmatter.split_lines_lf_crlf(text)
    assert lines == ["progress: 10%\r20%\r30%\n", "done\n"]


def test_split_lines_mixed_endings():
    text = "a\r\nb\nc"
    assert frontmatter.split_lines_lf_crlf(text) == ["a\r\n", "b\n", "c"]


def test_split_lines_reassembly_is_lossless():
    text = "a\r\nb\nc\rd\n"
    lines = frontmatter.split_lines_lf_crlf(text)
    assert "".join(lines) == text


# ---------------------------------------------------------------------------
# split_frontmatter -- the five distinct error strings
# ---------------------------------------------------------------------------

def test_split_frontmatter_empty_lines_rejected():
    _open, fm, _close, _body, err = frontmatter.split_frontmatter([])
    assert fm is None
    assert "does not open with a '---' fence" in err


def test_split_frontmatter_no_opening_fence_rejected():
    lines = frontmatter.split_lines_lf_crlf("# Title\nbody\n")
    _open, fm, _close, _body, err = frontmatter.split_frontmatter(lines)
    assert fm is None
    assert "does not open with a '---' fence" in err


def test_split_frontmatter_non_frontmatter_shaped_line_stops_scan():
    """A line inside the fence that is neither blank, key-shaped, a list
    item, nor an indented continuation must stop the scan and fail loudly,
    rather than being swallowed as if it were frontmatter."""
    text = "---\ntitle: x\n# Heading\nkey: y\n---\nbody\n"
    lines = frontmatter.split_lines_lf_crlf(text)
    _open, fm, _close, _body, err = frontmatter.split_frontmatter(lines)
    assert fm is None
    assert "no closing '---'; stopped at a line that is not frontmatter" in err
    assert "'# Heading'" in err


def test_split_frontmatter_missing_closing_fence_with_body_horizontal_rule():
    """The exact regression this module's docstring names: a note whose
    frontmatter has no closing fence, but whose BODY contains a bare '---'
    horizontal rule, must be rejected rather than mis-split at the body's
    rule -- treating body prose as frontmatter mutates the wrong content."""
    text = "---\ntitle: x\n# My Note\n\nSome intro prose.\n\n---\n\nMore prose.\n"
    lines = frontmatter.split_lines_lf_crlf(text)
    _open, fm, _close, _body, err = frontmatter.split_frontmatter(lines)
    assert fm is None
    # Must be rejected via the shape-check path (stopped at the heading),
    # NOT silently accepted using the body's '---' as the closing fence.
    assert "no closing '---'; stopped at a line that is not frontmatter" in err
    assert "'# My Note'" in err


def test_split_frontmatter_all_frontmatter_shaped_no_fence_within_bound():
    """Every line up to (and including) the bound is frontmatter-shaped and
    no closing fence ever appears -- must fall through to the generic
    'no closing' error, not the size-limit error, since len(lines) does not
    exceed the bound."""
    n = frontmatter.MAX_FRONTMATTER_LINES
    # Open fence + (n - 1) key-shaped lines == n lines total, no closing
    # fence anywhere -- len(lines) == MAX_FRONTMATTER_LINES exactly, so the
    # size-limit branch (`len(lines) > MAX_FRONTMATTER_LINES`) is NOT taken.
    lines = ["---\n"] + [f"key{i}: v\n" for i in range(n - 1)]
    assert len(lines) == n
    _open, fm, _close, _body, err = frontmatter.split_frontmatter(lines)
    assert fm is None
    assert err == "malformed or missing frontmatter (no closing '---')"


def test_split_frontmatter_success():
    text = "---\ntitle: x\ntags:\n  - a\n---\nbody line\n"
    lines = frontmatter.split_lines_lf_crlf(text)
    open_fence, fm_lines, close_fence, body_lines, err = frontmatter.split_frontmatter(lines)
    assert err is None
    assert open_fence == "---\n"
    assert fm_lines == ["title: x\n", "tags:\n", "  - a\n"]
    assert close_fence == "---\n"
    assert body_lines == ["body line\n"]


# ---------------------------------------------------------------------------
# MAX_FRONTMATTER_LINES exact boundary -- fence AT the limit accepted,
# ONE PAST the limit rejected. Fixture size is derived from the constant
# itself (so it tracks a future change to the constant), but the assertion
# is on the real observable behaviour -- the split result / error text --
# not on the number, so a guard that stopped enforcing the bound (e.g. an
# off-by-one in the range()) would still fail this test even though the
# fixture size still matches the constant.
# ---------------------------------------------------------------------------

def _frontmatter_text_with_fence_at_line(n: int) -> str:
    """Build ``---\\nkey0: v\\n...\\nkey<n-2>: v\\n---\\nbody\\n`` whose closing
    fence sits at line index ``n`` (0-indexed), i.e. there are ``n - 1``
    key-shaped frontmatter lines between the two fences."""
    keys = [f"key{i}: v\n" for i in range(n - 1)]
    return "".join(["---\n"] + keys + ["---\n", "body\n"])


def test_split_frontmatter_fence_exactly_at_max_lines_accepted():
    n = frontmatter.MAX_FRONTMATTER_LINES
    text = _frontmatter_text_with_fence_at_line(n)
    lines = frontmatter.split_lines_lf_crlf(text)
    # Sanity on the fixture itself: closing fence really is at index n.
    assert lines[n].rstrip("\r\n") == "---"
    _open, fm_lines, close_fence, body_lines, err = frontmatter.split_frontmatter(lines)
    assert err is None
    assert close_fence.rstrip("\r\n") == "---"
    assert body_lines == ["body\n"]


def test_split_frontmatter_fence_one_past_max_lines_rejected():
    n = frontmatter.MAX_FRONTMATTER_LINES + 1
    text = _frontmatter_text_with_fence_at_line(n)
    lines = frontmatter.split_lines_lf_crlf(text)
    assert lines[n].rstrip("\r\n") == "---"
    _open, fm_lines, _close, _body, err = frontmatter.split_frontmatter(lines)
    assert fm_lines is None
    assert err == (
        f"frontmatter exceeds {frontmatter.MAX_FRONTMATTER_LINES} lines "
        "(limit reached before the frontmatter block ended -- the note may "
        "be fine; this is a size limit, not a missing fence)"
    )


# ---------------------------------------------------------------------------
# CRLF / bare-\r inputs through split_frontmatter end-to-end
# ---------------------------------------------------------------------------

def test_split_frontmatter_crlf_note():
    text = "---\r\ntitle: x\r\ntags:\r\n  - a\r\n---\r\nbody line\r\n"
    lines = frontmatter.split_lines_lf_crlf(text)
    open_fence, fm_lines, close_fence, body_lines, err = frontmatter.split_frontmatter(lines)
    assert err is None
    assert open_fence == "---\r\n"
    assert fm_lines == ["title: x\r\n", "tags:\r\n", "  - a\r\n"]
    assert close_fence == "---\r\n"
    assert body_lines == ["body line\r\n"]


def test_split_frontmatter_bare_cr_inside_body_survives():
    """A bare '\\r' inside the body (post-closing-fence) must not be
    mistaken for a line terminator by split_lines_lf_crlf, and must not
    confuse split_frontmatter into misreading the body."""
    text = "---\ntitle: x\n---\nprogress: 1%\r2%\r3%\ndone\n"
    lines = frontmatter.split_lines_lf_crlf(text)
    _open, fm_lines, _close, body_lines, err = frontmatter.split_frontmatter(lines)
    assert err is None
    assert fm_lines == ["title: x\n"]
    assert body_lines == ["progress: 1%\r2%\r3%\n", "done\n"]
