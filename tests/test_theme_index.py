import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))
import vault_index


def test_idx_themes_updated_created(tmp_vault):
    """ensure_index must create an index on themes.updated_date for the
    /emerge window query (Slice D)."""
    p = str(tmp_vault / "c.db")
    vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=p)
    conn = vault_index._connect(p)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_themes_updated'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
