"""Tests for hooks/deep_cli.py — text-anchored / checkbox-anchored checkoffs (#201).

Covers Guard A (run_batch_edit line-anchoring) and Guard B
(run_build_checkoffs text-resolution), with fixtures mirroring the issue's
real failure modes:

  mode 1 — a drifted classifier line number checks off the WRONG still-active item.
  mode 2 — a substring `old_text` corrupts a quoted-prose line.
"""

from __future__ import annotations

import io
import json

import pytest

import deep_cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_config(monkeypatch, tmp_vault):
    """Point deep_cli's load_config (imported inside the functions from
    obsidian_utils) at tmp_vault."""
    import obsidian_utils

    config = {
        "vault_path": str(tmp_vault),
        "sessions_folder": "claude-sessions",
        "insights_folder": "claude-insights",
    }
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: config)
    return config


def _run_build_checkoffs(monkeypatch, tmp_vault, items, capsys=None):
    """Invoke run_build_checkoffs with `items` on stdin; return parsed result."""
    _patch_config(monkeypatch, tmp_vault)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(items)))
    deep_cli.run_build_checkoffs()
    out = capsys.readouterr().out if capsys else None
    return out


def _run_batch_edit(monkeypatch, tmp_vault, edits, capsys):
    """Invoke run_batch_edit with `edits` on stdin; return (stdout, stderr)."""
    _patch_config(monkeypatch, tmp_vault)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(edits)))
    deep_cli.run_batch_edit()
    cap = capsys.readouterr()
    return cap.out, cap.err


# ---------------------------------------------------------------------------
# _is_checkbox_flip predicate
# ---------------------------------------------------------------------------


def test_is_checkbox_flip_true():
    assert deep_cli._is_checkbox_flip("- [ ] Fix the bug", "- [x] Fix the bug") is True
    assert deep_cli._is_checkbox_flip("  - [ ] Indented item", "  - [x] Indented item") is True


def test_is_checkbox_flip_false_non_checkbox():
    # Link addition (not a checkbox) stays on the legacy path.
    assert deep_cli._is_checkbox_flip("See [[note-a]]", "See [[note-a]] [[note-b]]") is False
    # Changing more than the marker is not a pure flip.
    assert deep_cli._is_checkbox_flip("- [ ] Fix the bug", "- [x] Fix the BUG!!") is False
    # Already-checked source is not an unchecked checkbox.
    assert deep_cli._is_checkbox_flip("- [x] done", "- [x] done") is False


# ---------------------------------------------------------------------------
# Guard B — run_build_checkoffs (text resolution)
# ---------------------------------------------------------------------------


def test_guard_b_wrong_line_resolves_by_text(tmp_vault, monkeypatch, capsys):
    """mode 1: the hint points at a DIFFERENT active item; text wins.

    Representative item at line L; a different active `- [ ]` item at L+1.
    Classification gives line=L+1 but text=<representative>. The resolver must
    target line L (the text match) and NOT the different active item.
    """
    note = tmp_vault / "claude-sessions" / "2026-04-10-proj-aaaa.md"
    body = (
        "---\ntype: claude-session\nproject: proj\n---\n\n"
        "## Open Questions / Next Steps\n"
        "- [ ] Refactor the authentication handler in src/auth.py\n"   # line 7 (rep)
        "- [ ] Add a dashboard widget for vault statistics\n"          # line 8 (different active)
    )
    note.write_text(body, encoding="utf-8")
    # Line 8 is the WRONG line; text is the representative (line 7's item).
    items = [{
        "file": note.name,
        "line": 8,
        "text": "Refactor the authentication handler in src/auth.py",
    }]
    out = _run_build_checkoffs(monkeypatch, tmp_vault, items, capsys)
    result = json.loads(out)

    assert len(result["edits"]) == 1
    fullpath, old_text, new_text = result["edits"][0]
    assert old_text == "- [ ] Refactor the authentication handler in src/auth.py"
    assert new_text == "- [x] Refactor the authentication handler in src/auth.py"
    # The different active item must never be targeted.
    assert "dashboard widget" not in old_text
    assert result["skipped"] == []


def test_guard_b_hint_past_eof_skips(tmp_vault, monkeypatch, capsys):
    """mode 1 (absent): hint past EOF -> resolver falls back to text; if no
    candidate text-matches, the item is SKIPPED, never corrupting."""
    note = tmp_vault / "claude-sessions" / "2026-04-10-proj-bbbb.md"
    note.write_text(
        "---\ntype: claude-session\nproject: proj\n---\n\n"
        "## Open Questions / Next Steps\n"
        "- [ ] Some entirely unrelated active task here\n",
        encoding="utf-8",
    )
    items = [{
        "file": note.name,
        "line": 999,  # past EOF
        "text": "Refactor the authentication handler in src/auth.py for PR 99",
    }]
    out = _run_build_checkoffs(monkeypatch, tmp_vault, items, capsys)
    result = json.loads(out)
    assert result["edits"] == []
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["reason"] == "no matching checkbox line"


def test_guard_b_hint_at_non_checkbox_skips(tmp_vault, monkeypatch, capsys):
    """Hint line is a non-checkbox (a heading); no text-matching candidate -> skip."""
    note = tmp_vault / "claude-sessions" / "2026-04-10-proj-cccc.md"
    note.write_text(
        "---\ntype: claude-session\nproject: proj\n---\n\n"
        "## Open Questions / Next Steps\n"           # line 6 (non-checkbox)
        "- [ ] Wholly different active work item\n",  # line 7
        encoding="utf-8",
    )
    items = [{
        "file": note.name,
        "line": 6,
        "text": "Implement the long-awaited consolidate clustering feature",
    }]
    out = _run_build_checkoffs(monkeypatch, tmp_vault, items, capsys)
    result = json.loads(out)
    assert result["edits"] == []
    assert result["skipped"][0]["reason"] == "no matching checkbox line"


def test_guard_b_quoted_prose_only_real_checkbox(tmp_vault, monkeypatch, capsys):
    """mode 2: item text appears both as a real `- [ ] X` checkbox AND inside a
    prose/quote line. Only the real checkbox line is emitted as the triple."""
    note = tmp_vault / "claude-sessions" / "2026-04-10-proj-dddd.md"
    item = "Wire run_build_checkoffs into the standup Step 18 pipeline"
    note.write_text(
        "---\ntype: claude-session\nproject: proj\n---\n\n"
        "## Conversation (raw)\n"
        f"**Assistant:** Next I'll {item} and then test it.\n"   # prose mention (NOT a checkbox)
        "\n"
        "## Open Questions / Next Steps\n"
        f"- [ ] {item}\n",                                       # the real checkbox
        encoding="utf-8",
    )
    # Hint deliberately points at the prose line (line 7), not the checkbox.
    items = [{"file": note.name, "line": 7, "text": item}]
    out = _run_build_checkoffs(monkeypatch, tmp_vault, items, capsys)
    result = json.loads(out)

    assert len(result["edits"]) == 1
    _, old_text, new_text = result["edits"][0]
    assert old_text == f"- [ ] {item}"
    assert new_text == f"- [x] {item}"
    # Prose line must not be picked.
    assert not old_text.startswith("**Assistant:**")


def test_guard_b_file_not_found_skipped(tmp_vault, monkeypatch, capsys):
    items = [{"file": "does-not-exist.md", "line": 1, "text": "Anything at all here"}]
    out = _run_build_checkoffs(monkeypatch, tmp_vault, items, capsys)
    result = json.loads(out)
    assert result["edits"] == []
    assert result["skipped"][0]["reason"] == "file not found"


def test_guard_b_containment_violation_skipped(tmp_vault, monkeypatch, capsys):
    """An absolute path outside the vault is rejected on containment."""
    outside = tmp_vault.parent / "outside.md"
    outside.write_text("- [ ] Outside the vault entirely here\n", encoding="utf-8")
    items = [{"file": str(outside), "line": 1, "text": "Outside the vault entirely here"}]
    out = _run_build_checkoffs(monkeypatch, tmp_vault, items, capsys)
    result = json.loads(out)
    assert result["edits"] == []
    assert result["skipped"][0]["reason"] == "containment"


# ---------------------------------------------------------------------------
# Guard A — run_batch_edit (line-anchoring)
# ---------------------------------------------------------------------------


def test_guard_a_flips_present_checkbox(tmp_vault, monkeypatch, capsys):
    note = tmp_vault / "claude-sessions" / "2026-04-10-flip.md"
    note.write_text(
        "## Open Questions / Next Steps\n"
        "- [ ] Refactor the authentication handler in src/auth.py\n",
        encoding="utf-8",
    )
    edits = [[
        str(note),
        "- [ ] Refactor the authentication handler in src/auth.py",
        "- [x] Refactor the authentication handler in src/auth.py",
    ]]
    out, _ = _run_batch_edit(monkeypatch, tmp_vault, edits, capsys)
    assert "Applied 1/1 edits" in out
    assert "- [x] Refactor the authentication handler in src/auth.py" in note.read_text()


def test_guard_a_absent_checkbox_skips_no_write(tmp_vault, monkeypatch, capsys):
    """Triple whose old_text checkbox is ABSENT -> Applied 0/1, file unchanged."""
    note = tmp_vault / "claude-sessions" / "2026-04-10-absent.md"
    original = (
        "## Open Questions / Next Steps\n"
        "- [ ] A completely different still-active item line\n"
    )
    note.write_text(original, encoding="utf-8")
    edits = [[
        str(note),
        "- [ ] Refactor the authentication handler in src/auth.py",
        "- [x] Refactor the authentication handler in src/auth.py",
    ]]
    out, err = _run_batch_edit(monkeypatch, tmp_vault, edits, capsys)
    assert "Applied 0/1 edits" in out
    assert "checkoff skipped (no matching checkbox line)" in err
    # The different active item is untouched.
    assert note.read_text() == original


def test_guard_a_prose_substring_not_corrupted(tmp_vault, monkeypatch, capsys):
    """mode 2 fail-first guard: old_text appears as substring in prose AND as a
    real checkbox. Line-anchoring must flip ONLY the checkbox, not the prose."""
    note = tmp_vault / "claude-sessions" / "2026-04-10-prose.md"
    item = "- [ ] Migrate the legacy importer to the new pipeline format"
    # NOTE: the prose line CONTAINS the exact old_text as a substring.
    prose = f"**Assistant:** Plan: {item} before the release.\n"
    original = (
        "## Conversation (raw)\n"
        f"{prose}"
        "\n## Open Questions / Next Steps\n"
        f"{item}\n"
    )
    note.write_text(original, encoding="utf-8")
    edits = [[str(note), item, item.replace("[ ]", "[x]", 1)]]
    out, _ = _run_batch_edit(monkeypatch, tmp_vault, edits, capsys)
    assert "Applied 1/1 edits" in out

    text = note.read_text()
    # The prose line is preserved verbatim (still contains the unchecked text).
    assert prose.rstrip("\n") in text
    # Exactly one checked checkbox exists.
    assert text.count("- [x] Migrate the legacy importer to the new pipeline format") == 1
    # The prose mention was NOT flipped — the "- [ ] " inside the prose remains.
    assert "Plan: - [ ] Migrate the legacy importer" in text


def test_guard_a_link_addition_still_applies(tmp_vault, monkeypatch, capsys):
    """Non-checkbox edit (link addition) keeps the substring-replace path — no regression."""
    note = tmp_vault / "claude-sessions" / "2026-04-10-link.md"
    note.write_text("## Summary\nSee [[note-a]] for context.\n", encoding="utf-8")
    edits = [[
        str(note),
        "See [[note-a]] for context.",
        "See [[note-a]] [[note-b]] for context.",
    ]]
    out, _ = _run_batch_edit(monkeypatch, tmp_vault, edits, capsys)
    assert "Applied 1/1 edits" in out
    assert "[[note-b]]" in note.read_text()


def test_guard_a_preserves_line_ending(tmp_vault, monkeypatch, capsys):
    """Flipping the first of several lines must not disturb following lines."""
    note = tmp_vault / "claude-sessions" / "2026-04-10-multi.md"
    note.write_text(
        "## Open Questions / Next Steps\n"
        "- [ ] First item that is long enough to anchor cleanly here\n"
        "- [ ] Second item that should remain untouched entirely\n",
        encoding="utf-8",
    )
    edits = [[
        str(note),
        "- [ ] First item that is long enough to anchor cleanly here",
        "- [x] First item that is long enough to anchor cleanly here",
    ]]
    _run_batch_edit(monkeypatch, tmp_vault, edits, capsys)
    text = note.read_text()
    assert "- [x] First item that is long enough to anchor cleanly here\n" in text
    assert "- [ ] Second item that should remain untouched entirely\n" in text
