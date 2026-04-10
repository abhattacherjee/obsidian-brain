"""Tests for hooks/open_item_dedup.py."""

from __future__ import annotations

import sys
import os

import pytest

# Ensure hooks/ is on sys.path (conftest.py does this too, but be explicit)
_HOOKS_DIR = os.path.join(os.path.dirname(__file__), "..", "hooks")
if os.path.abspath(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, os.path.abspath(_HOOKS_DIR))

from open_item_dedup import (
    _strip_markdown,
    _extract_distinctive_tokens,
    _tokenize,
    collect_open_items,
    find_duplicates,
    cascade_checkoff,
    dedup_note_open_items,
    batch_cascade_checkoff,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _create_session_note(sessions_dir, filename, project, open_items):
    items_text = "\n".join(f"- [ ] {item}" for item in open_items)
    note = sessions_dir / filename
    note.write_text(
        f"---\ntype: claude-session\nproject: {project}\nstatus: summarized\n---\n\n"
        f"# Session\n\n## Summary\nDid stuff.\n\n"
        f"## Open Questions / Next Steps\n{items_text}\n",
        encoding="utf-8",
    )
    return note


# ---------------------------------------------------------------------------
# 1. _strip_markdown
# ---------------------------------------------------------------------------

def test_strip_markdown():
    assert _strip_markdown("`code`") == "code"
    assert _strip_markdown("**bold**") == "bold"
    assert _strip_markdown("_italic_") == "italic"
    assert _strip_markdown("[link text](https://example.com)") == "link text"
    assert _strip_markdown("plain text") == "plain text"
    # Multiple patterns in one string
    result = _strip_markdown("Fix `obsidian_utils.py` and **refactor** the _loop_")
    assert "obsidian_utils.py" in result
    assert "refactor" in result
    assert "loop" in result
    assert "`" not in result
    assert "**" not in result
    # Italic delimiters removed: "_loop_" → "loop" (no standalone _ around words)
    assert "_loop_" not in result
    assert "loop" in result


# ---------------------------------------------------------------------------
# 2. _extract_distinctive_tokens
# ---------------------------------------------------------------------------

def test_extract_distinctive_tokens():
    # File paths
    tokens = _extract_distinctive_tokens("Update hooks/obsidian_utils.py and README.md")
    assert any("obsidian_utils.py" in t for t in tokens)
    assert any("README.md" in t for t in tokens)

    # PR refs
    tokens = _extract_distinctive_tokens("Closes #42 and PR 99")
    assert any("#42" in t for t in tokens)

    # Branch names
    tokens = _extract_distinctive_tokens("Merge feature/new-hook into develop")
    assert any("feature/new-hook" in t for t in tokens)

    # Version numbers
    tokens = _extract_distinctive_tokens("Bump to v1.2.3 for release")
    assert any("v1.2.3" in t for t in tokens)

    # Nothing distinctive
    tokens = _extract_distinctive_tokens("Add more unit tests")
    assert tokens == []


# ---------------------------------------------------------------------------
# 3. _tokenize
# ---------------------------------------------------------------------------

def test_tokenize():
    tokens = _tokenize("Fix the login bug in auth module")
    # Should be lowercase
    assert all(t == t.lower() for t in tokens)
    # Stopwords dropped
    assert "the" not in tokens
    assert "in" not in tokens
    # Short tokens dropped
    assert all(len(t) >= 3 for t in tokens)
    # Meaningful words kept
    assert "fix" in tokens
    assert "login" in tokens
    assert "bug" in tokens
    assert "auth" in tokens
    assert "module" in tokens


# ---------------------------------------------------------------------------
# 4. collect_open_items
# ---------------------------------------------------------------------------

def test_collect_open_items(tmp_vault):
    sessions_dir = tmp_vault / "claude-sessions"
    _create_session_note(sessions_dir, "2026-04-10-proj-aaa.md", "myproject",
                         ["Write unit tests", "Update README.md"])
    _create_session_note(sessions_dir, "2026-04-09-proj-bbb.md", "myproject",
                         ["Deploy to production"])

    items = collect_open_items(str(tmp_vault), "claude-sessions", "myproject")
    texts = [t for _, _, t in items]

    assert len(texts) == 3
    assert "Write unit tests" in texts
    assert "Update README.md" in texts
    assert "Deploy to production" in texts


# ---------------------------------------------------------------------------
# 5. collect_open_items_empty_vault
# ---------------------------------------------------------------------------

def test_collect_open_items_empty_vault(tmp_vault):
    # No notes created
    items = collect_open_items(str(tmp_vault), "claude-sessions", "myproject")
    assert items == []


# ---------------------------------------------------------------------------
# 6. collect_open_items_wrong_project
# ---------------------------------------------------------------------------

def test_collect_open_items_wrong_project(tmp_vault):
    sessions_dir = tmp_vault / "claude-sessions"
    _create_session_note(sessions_dir, "2026-04-10-proj-ccc.md", "otherproject",
                         ["Fix the widget bug"])

    items = collect_open_items(str(tmp_vault), "claude-sessions", "myproject")
    assert items == []


# ---------------------------------------------------------------------------
# 7. find_duplicates_exact (distinctive token match → "high")
# ---------------------------------------------------------------------------

def test_find_duplicates_exact():
    existing = [
        ("/vault/sessions/note-a.md", 10, "Merge feature/new-hook into develop"),
        ("/vault/sessions/note-b.md", 20, "Write unit tests"),
    ]
    # Candidate shares distinctive token "feature/new-hook"
    matches = find_duplicates("Finish feature/new-hook branch", existing)
    assert len(matches) >= 1
    high_matches = [m for m in matches if m[3] == "high"]
    assert len(high_matches) >= 1
    assert any("feature/new-hook" in m[2] for m in high_matches)


# ---------------------------------------------------------------------------
# 8. find_duplicates_fuzzy (token overlap above threshold → "fuzzy")
# ---------------------------------------------------------------------------

def test_find_duplicates_fuzzy():
    # Craft items with lots of overlapping non-distinctive tokens
    existing_text = (
        "Review the summarization pipeline configuration for batch recall upgrade"
    )
    candidate_text = (
        "Check the summarization pipeline configuration for batch recall upgrade process"
    )
    existing = [("/vault/sessions/note.md", 5, existing_text)]
    matches = find_duplicates(candidate_text, existing, threshold=5)
    assert len(matches) >= 1
    # At least one fuzzy match (no distinctive tokens)
    assert any(m[3] in ("fuzzy", "high") for m in matches)


# ---------------------------------------------------------------------------
# 9. find_duplicates_no_match (dissimilar items → [])
# ---------------------------------------------------------------------------

def test_find_duplicates_no_match():
    existing = [
        ("/vault/sessions/note.md", 1, "Deploy the authentication service"),
    ]
    matches = find_duplicates("Update the CSS color scheme", existing)
    assert matches == []


# ---------------------------------------------------------------------------
# 10. cascade_checkoff — excludes source item from results
# ---------------------------------------------------------------------------

def test_cascade_checkoff():
    source_file = "/vault/sessions/note-a.md"
    source_line = 7
    existing = [
        (source_file, source_line, "Merge feature/auth-fix into develop"),
        ("/vault/sessions/note-b.md", 12, "Merge feature/auth-fix into develop"),
    ]
    # The source item itself should be excluded
    matches = cascade_checkoff(
        "Merge feature/auth-fix into develop",
        existing,
        source_file=source_file,
        source_line=source_line,
    )
    assert all(
        not (m[0] == source_file and m[1] == source_line)
        for m in matches
    )
    # The duplicate in note-b should still be returned
    assert any(m[0] == "/vault/sessions/note-b.md" for m in matches)


# ---------------------------------------------------------------------------
# 11. dedup_note_open_items — removes high-confidence duplicate from newer note
# ---------------------------------------------------------------------------

def test_dedup_note_open_items(tmp_vault):
    sessions_dir = tmp_vault / "claude-sessions"

    # Older note (already has the item)
    _create_session_note(
        sessions_dir,
        "2026-04-09-proj-old.md",
        "myproject",
        ["Fix hooks/obsidian_utils.py import error"],
    )

    # Newer note (duplicate item — same distinctive file path token)
    newer_note = _create_session_note(
        sessions_dir,
        "2026-04-10-proj-new.md",
        "myproject",
        ["Fix hooks/obsidian_utils.py import error", "Write new tests"],
    )

    removed = dedup_note_open_items(
        str(tmp_vault), "claude-sessions", "myproject", str(newer_note)
    )

    assert len(removed) >= 1
    assert any("obsidian_utils.py" in r for r in removed)

    # Verify file was rewritten without the duplicate
    content = newer_note.read_text(encoding="utf-8")
    assert "Write new tests" in content  # non-duplicate preserved


# ---------------------------------------------------------------------------
# 12. batch_cascade_checkoff — returns a string result
# ---------------------------------------------------------------------------

def test_batch_cascade_checkoff(tmp_vault):
    sessions_dir = tmp_vault / "claude-sessions"

    _create_session_note(
        sessions_dir,
        "2026-04-09-proj-aaa.md",
        "myproject",
        ["Merge feature/new-hook into develop"],
    )

    result = batch_cascade_checkoff(
        str(tmp_vault),
        "claude-sessions",
        "myproject",
        ["Merge feature/new-hook into develop"],
    )
    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# 13. batch_cascade_checkoff_empty — no items → appropriate message
# ---------------------------------------------------------------------------

def test_batch_cascade_checkoff_empty(tmp_vault):
    # No notes in vault at all
    result = batch_cascade_checkoff(
        str(tmp_vault),
        "claude-sessions",
        "myproject",
        ["Fix the authentication bug"],
    )
    assert isinstance(result, str)
    assert "no open items" in result.lower() or "no duplicate" in result.lower()
