import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))
import obsidian_utils
import vault_index


@pytest.fixture
def db(tmp_vault):
    p = str(tmp_vault / "c.db")
    vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=p)
    return p


def _mk_theme(conn, tid, name, summary, project, note_count, activation):
    conn.execute(
        "INSERT INTO themes "
        "(id,name,summary,centroid,note_count,activation,created_date,updated_date,project) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (tid, name, summary, "{}", note_count, activation,
         "2026-01-01", "2026-06-14", project),
    )


def test_recurring_themes_section_formats_and_ranks(db):
    conn = vault_index._connect(db)
    # Project-scoped theme (lower activation) and a cross-project NULL theme
    # (higher activation) so the higher one must sort first.
    _mk_theme(conn, 1, "Auth refactor", "Auth work. More.", "p", 5, 1.0)
    _mk_theme(conn, 2, "Cross cutting", "Spans repos. More.", None, 8, 3.0)
    conn.commit()
    conn.close()

    block = obsidian_utils.recurring_themes_section(db, "p")
    assert block.startswith("## Recurring Themes")
    lines = block.splitlines()
    # Higher-activation theme listed first.
    assert "Cross cutting" in lines[1]
    assert "Auth refactor" in lines[2]
    assert lines[1].index("Cross cutting") >= 0
    # Note counts surfaced as "(N notes)".
    assert "(8 notes)" in lines[1]
    assert "(5 notes)" in lines[2]


def test_recurring_themes_section_empty_when_no_themes(db):
    # No themes seeded.
    assert obsidian_utils.recurring_themes_section(db, "p") == ""
