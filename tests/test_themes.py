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


def _indexed_note(tmp_vault, slug, title, body, project="proj"):
    """Write a note and return its path (caller re-indexes)."""
    path = tmp_vault / "claude-sessions" / f"2026-04-16-{project}-{slug}.md"
    path.write_text(
        "---\n"
        "type: claude-session\n"
        "date: 2026-04-16\n"
        f"project: {project}\n"
        f"title: {title}\n"
        "tags:\n  - claude/session\n"
        "status: summarized\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return str(path)


class TestAssignToTheme:
    def test_first_note_leaves_no_theme(self, tmp_vault):
        """With no existing themes, assignment is a no-op returning None."""
        path = _indexed_note(tmp_vault, "a", "retrieval scoring",
                             "retrieval scoring activation")
        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)
        result = vault_index.assign_to_theme(db_path, path, project="proj")
        assert result is None

        conn = sqlite3.connect(db_path)
        themes = conn.execute("SELECT COUNT(*) FROM themes").fetchone()[0]
        members = conn.execute("SELECT COUNT(*) FROM theme_members").fetchone()[0]
        conn.close()
        assert themes == 0
        assert members == 0

    def test_similar_note_joins_existing_theme(self, tmp_vault):
        path_a = _indexed_note(tmp_vault, "a", "retrieval scoring",
                               "retrieval scoring activation importance")
        path_b = _indexed_note(tmp_vault, "b", "activation importance",
                               "retrieval scoring activation importance")
        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)

        conn = sqlite3.connect(db_path)
        vec_a = json.loads(conn.execute(
            "SELECT tfidf_vector FROM notes WHERE path = ?", (path_a,)
        ).fetchone()[0])
        conn.execute(
            "INSERT INTO themes (name, summary, centroid, note_count, "
            "created_date, updated_date, project) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Retrieval", "", json.dumps(vec_a), 1,
             "2026-04-16", "2026-04-16", "proj"),
        )
        theme_id = conn.execute("SELECT id FROM themes").fetchone()[0]
        conn.execute(
            "INSERT INTO theme_members (theme_id, note_path, similarity, added_date) "
            "VALUES (?, ?, 1.0, ?)",
            (theme_id, path_a, "2026-04-16"),
        )
        conn.commit()
        conn.close()

        result = vault_index.assign_to_theme(db_path, path_b, project="proj")
        assert result is not None
        assert result["theme_id"] == theme_id
        assert result["similarity"] > 0.3

        conn = sqlite3.connect(db_path)
        members = conn.execute(
            "SELECT note_path FROM theme_members WHERE theme_id = ?", (theme_id,)
        ).fetchall()
        note_count = conn.execute(
            "SELECT note_count FROM themes WHERE id = ?", (theme_id,)
        ).fetchone()[0]
        conn.close()
        assert {m[0] for m in members} == {path_a, path_b}
        assert note_count == 2

    def test_low_similarity_stays_unassigned(self, tmp_vault):
        path_theme = _indexed_note(tmp_vault, "theme", "retrieval scoring",
                                   "retrieval scoring activation")
        path_stranger = _indexed_note(tmp_vault, "stranger", "garlic pasta",
                                      "garlic pasta recipe italian dinner")
        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)

        conn = sqlite3.connect(db_path)
        vec = json.loads(conn.execute(
            "SELECT tfidf_vector FROM notes WHERE path = ?", (path_theme,)
        ).fetchone()[0])
        conn.execute(
            "INSERT INTO themes (name, summary, centroid, note_count, "
            "created_date, updated_date, project) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Retrieval", "", json.dumps(vec), 1,
             "2026-04-16", "2026-04-16", "proj"),
        )
        conn.commit()
        conn.close()

        result = vault_index.assign_to_theme(db_path, path_stranger, project="proj")
        assert result is None

    def test_centroid_update_is_running_average(self, tmp_vault):
        """After a second note joins, centroid term weight is the mean of the two vectors."""
        path_a = _indexed_note(tmp_vault, "a", "retrieval", "retrieval retrieval")
        path_b = _indexed_note(tmp_vault, "b", "retrieval scoring",
                               "retrieval scoring")
        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)

        conn = sqlite3.connect(db_path)
        vec_a = json.loads(conn.execute(
            "SELECT tfidf_vector FROM notes WHERE path = ?", (path_a,)
        ).fetchone()[0])
        vec_b = json.loads(conn.execute(
            "SELECT tfidf_vector FROM notes WHERE path = ?", (path_b,)
        ).fetchone()[0])
        conn.execute(
            "INSERT INTO themes (name, summary, centroid, note_count, "
            "created_date, updated_date, project) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Retrieval", "", json.dumps(vec_a), 1,
             "2026-04-16", "2026-04-16", "proj"),
        )
        theme_id = conn.execute("SELECT id FROM themes").fetchone()[0]
        conn.execute(
            "INSERT INTO theme_members (theme_id, note_path, similarity, added_date) "
            "VALUES (?, ?, 1.0, ?)",
            (theme_id, path_a, "2026-04-16"),
        )
        conn.commit()
        conn.close()

        vault_index.assign_to_theme(db_path, path_b, project="proj")

        conn = sqlite3.connect(db_path)
        centroid = json.loads(conn.execute(
            "SELECT centroid FROM themes WHERE id = ?", (theme_id,)
        ).fetchone()[0])
        conn.close()

        for term, a_val in vec_a.items():
            b_val = vec_b.get(term, 0.0)
            expected = (a_val + b_val) / 2
            assert centroid.get(term) == pytest.approx(expected, rel=1e-6)
