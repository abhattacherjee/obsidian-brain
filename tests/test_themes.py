"""Schema + incremental assignment tests for the Phase 2 theme engine."""

import json
import sqlite3

import pytest

import vault_index


@pytest.fixture
def db_path(tmp_vault):
    p = str(tmp_vault / "test.db")
    vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=p)
    return p


class TestPhase2Schema:
    def test_themes_table_exists(self, db_path):
        conn = sqlite3.connect(db_path)
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "themes" in tables
        assert "theme_members" in tables
        assert "term_df" in tables

    def test_themes_columns(self, db_path):
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(themes)").fetchall()}
        conn.close()
        assert cols == {
            "id", "name", "summary", "centroid", "note_count",
            "activation", "created_date", "updated_date", "project",
        }

    def test_theme_members_columns(self, db_path):
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(theme_members)"
        ).fetchall()}
        conn.close()
        assert cols == {
            "theme_id", "note_path", "similarity", "surprise", "added_date",
        }

    def test_notes_has_tfidf_vector_column(self, db_path):
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(notes)").fetchall()}
        conn.close()
        assert "tfidf_vector" in cols

    def test_tfidf_vector_migration_on_existing_db(self, tmp_vault):
        """A DB created without the new column must gain it via ensure_index()."""
        db_path = str(tmp_vault / "legacy.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE notes ("
            "path TEXT PRIMARY KEY, type TEXT NOT NULL, date TEXT, "
            "project TEXT, title TEXT, source_session TEXT, "
            "source_note TEXT, tags TEXT, status TEXT, mtime REAL NOT NULL, "
            "size INTEGER, body TEXT DEFAULT '', importance INTEGER DEFAULT 5)"
        )
        conn.commit()
        conn.close()

        vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)

        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(notes)").fetchall()}
        conn.close()
        assert "tfidf_vector" in cols

    def test_term_df_columns(self, db_path):
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(term_df)").fetchall()}
        conn.close()
        assert cols == {"term", "df"}
