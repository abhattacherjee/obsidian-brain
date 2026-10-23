"""Tests for importance column on notes table and importance parsing."""

import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import capture_save
import obsidian_utils
import vault_index


def _write_note(path, frontmatter: dict, body: str = ""):
    lines = ["---"]
    for k, v in frontmatter.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    if body:
        lines.append(body)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class TestImportanceColumn:
    def test_notes_table_has_importance_column(self, tmp_vault):
        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)
        conn = sqlite3.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(notes)").fetchall()}
        conn.close()
        assert "importance" in cols

    def test_importance_defaults_to_5(self, tmp_vault):
        note = tmp_vault / "claude-sessions" / "2026-04-15-test-a1b2.md"
        _write_note(note, {
            "type": "claude-session",
            "date": "2026-04-15",
            "project": "test",
            "status": "summarized",
        }, body="Some content")
        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT importance FROM notes").fetchone()
        conn.close()
        assert row[0] == 5

    def test_importance_migration_on_existing_db(self, tmp_vault):
        db_path = str(tmp_vault / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE notes ("
            "  path TEXT PRIMARY KEY, type TEXT NOT NULL, date TEXT, project TEXT,"
            "  title TEXT, source_session TEXT, source_note TEXT, tags TEXT,"
            "  status TEXT, mtime REAL NOT NULL, size INTEGER, body TEXT DEFAULT ''"
            ")"
        )
        conn.execute(
            "INSERT INTO notes (path, type, mtime) VALUES (?, ?, ?)",
            ("/old/note.md", "claude-session", 1000.0),
        )
        conn.commit()
        conn.close()
        vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)
        conn = sqlite3.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(notes)").fetchall()}
        importance = conn.execute(
            "SELECT importance FROM notes WHERE path = ?", ("/old/note.md",)
        ).fetchone()[0]
        conn.close()
        assert "importance" in cols
        assert importance == 5

    def test_update_importance(self, tmp_vault):
        note = tmp_vault / "claude-sessions" / "2026-04-15-test-c3d4.md"
        _write_note(note, {
            "type": "claude-session",
            "date": "2026-04-15",
            "project": "test",
            "status": "summarized",
        }, body="Important work")
        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE notes SET importance = ? WHERE path = ?", (9, str(note)))
        conn.commit()
        row = conn.execute("SELECT importance FROM notes WHERE path = ?", (str(note),)).fetchone()
        conn.close()
        assert row[0] == 9


class TestImportancePreservedOnReindex:
    def test_ensure_index_preserves_importance(self, tmp_vault):
        """Re-indexing does not reset importance to default."""
        note = tmp_vault / "claude-sessions" / "2026-04-15-test-preserve-a1b2.md"
        _write_note(note, {
            "type": "claude-session",
            "date": "2026-04-15",
            "project": "test",
            "status": "summarized",
        }, body="Important security audit work")

        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)

        # Set importance to 9
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE notes SET importance = 9 WHERE path = ?", (str(note),))
        conn.commit()
        conn.close()

        # Re-index (simulates next ensure_index call)
        vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)

        # Verify importance is still 9, not reset to 5
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT importance FROM notes WHERE path = ?", (str(note),)).fetchone()
        conn.close()
        assert row[0] == 9


class TestImportanceParsing:
    def test_parse_importance_from_summary(self):
        summary = "## Summary\nDid work.\n\n## Importance\n7\n"
        assert obsidian_utils.parse_importance(summary) == 7

    def test_parse_importance_with_explanation(self):
        summary = "## Importance\n8 - Major architectural decision\n"
        assert obsidian_utils.parse_importance(summary) == 8

    def test_parse_importance_missing_returns_default(self):
        summary = "## Summary\nDid work.\n"
        assert obsidian_utils.parse_importance(summary) == 5

    def test_parse_importance_invalid_returns_default(self):
        summary = "## Importance\nhigh\n"
        assert obsidian_utils.parse_importance(summary) == 5

    def test_parse_importance_clamps_to_range(self):
        assert obsidian_utils.parse_importance("## Importance\n0\n") == 1
        assert obsidian_utils.parse_importance("## Importance\n15\n") == 10

    def test_parse_importance_from_subagent_line(self):
        summary = "## Summary\nDid work.\n\nIMPORTANCE: 6\n"
        assert obsidian_utils.parse_importance(summary) == 6


class TestImportanceWriteBack:
    def test_upgrade_note_writes_importance_to_db(self, tmp_vault, mock_config):
        """upgrade_note_with_summary() writes parsed importance to vault index DB."""
        note_path = str(tmp_vault / "claude-sessions" / "2026-04-15-test-wb-a1b2.md")
        # Write a raw unsummarized note
        (tmp_vault / "claude-sessions" / "2026-04-15-test-wb-a1b2.md").write_text(
            "---\n"
            "type: claude-session\n"
            "date: 2026-04-15\n"
            "session_id: wb-test-session\n"
            "project: test-project\n"
            "status: auto-logged\n"
            "tags:\n"
            "  - claude/session\n"
            "---\n"
            "\n"
            "# Session: test-project\n"
            "\n"
            "## Summary\n"
            "AI summary unavailable\n"
            "\n"
            "## Conversation (raw)\n"
            "**User:** test\n",
            encoding="utf-8",
        )

        # Index the vault so the note is in the DB
        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)

        # Patch _default_db_path to point to our test DB
        with patch.object(vault_index, "_default_db_path", return_value=db_path):
            summary = (
                "## Summary\nDid important security work.\n\n"
                "## Key Decisions\nNone noted.\n\n"
                "## Changes Made\nNone noted.\n\n"
                "## Errors Encountered\nNone.\n\n"
                "## Open Questions / Next Steps\nNone.\n\n"
                "IMPORTANCE: 9\n"
            )
            result = obsidian_utils.upgrade_note_with_summary(
                note_path, summary, str(tmp_vault), "claude-sessions", "test-project"
            )
            assert not result.startswith("Failed:")

        # Verify importance was written to DB
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT importance FROM notes WHERE path = ?", (note_path,)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 9


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "importance_backfill.py"


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "importance.db")
    conn = vault_index._connect(path)
    try:
        vault_index._init_schema(conn)
    finally:
        conn.close()
    return path


def _execute(db, sql, args=()):
    conn = vault_index._connect(db)
    try:
        rows = conn.execute(sql, args).fetchall()
        conn.commit()
        return [tuple(row) for row in rows]
    finally:
        conn.close()


def _note(tmp_path, name, kind="claude-insight", extra=""):
    path = tmp_path / name
    path.write_text(f"---\ntype: {kind}\ndate: 2026-10-20\n{extra}"
                    "---\n# Shared title\n\nShared searchable knowledge.\n")
    return str(path)


def _importance(db, path):
    return _execute(db, "SELECT importance FROM notes WHERE path=?", (path,))[0][0]


@pytest.mark.parametrize("kind", vault_index.CURATED_IMPORTANCE_TYPES)
@pytest.mark.parametrize("source, expected", [("auto-capture", 3), ("manual", 6), (None, 6)])
def test_curated_derivation(kind, source, expected):
    assert vault_index._derive_importance({"type": kind, "source": source}, 9) == expected


@pytest.mark.parametrize("kind", ["claude-session", "unknown"])
@pytest.mark.parametrize("existing, expected", [(8, 8), (None, 5)])
def test_other_types_keep_existing(kind, existing, expected):
    assert vault_index._derive_importance({"type": kind}, existing) == expected


@pytest.mark.parametrize("kind", [*vault_index.CURATED_IMPORTANCE_TYPES, "claude-session"])
@pytest.mark.parametrize("raw", ["1", "10", " 6 "])
def test_override(kind, raw):
    assert vault_index._derive_importance(
        {"type": kind, "source": "auto-capture", "importance": raw}, 8,
    ) == int(raw)


@pytest.mark.parametrize("raw", ["6.5", "high", "0", "11", "", "+6", "-1", None, 6, "٦"])
def test_invalid_importance_falls_back(raw):
    assert vault_index._frontmatter_importance(raw) is None
    assert vault_index._derive_importance(
        {"type": "claude-insight", "importance": raw}, 8,
    ) == 6
    assert vault_index._derive_importance(
        {"type": "claude-session", "importance": raw}, 8,
    ) == 8


def test_real_capture_writer(mock_config, tmp_vault, db, monkeypatch):
    monkeypatch.setattr(capture_save, "find_match", lambda *a, **k: None)
    row = capture_save.save_candidate(
        {"type": "insight", "title": "Shared knowledge", "body": "Useful knowledge.",
         "topic": "testing"}, session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        project="test", config=mock_config,
    )
    path = str(tmp_vault / row["note_path"])
    assert vault_index.index_note(db, path)
    assert _importance(db, path) == 3


def test_hand_saved_and_resync(tmp_path, db):
    hand = _note(tmp_path, "hand.md", extra="source_session: example\ntags:\n  - claude/insight\n")
    auto = _note(tmp_path, "auto.md", extra="source: auto-capture\n")
    assert vault_index.index_note(db, hand)
    assert vault_index.index_note(db, auto)
    assert _importance(db, hand) == 6
    assert _importance(db, auto) == 3
    Path(auto).write_text(Path(auto).read_text().replace("source: auto-capture\n", ""))
    conn = vault_index._connect(db)
    try:
        vault_index._sync(conn, str(tmp_path), ["."])
        conn.commit()
    finally:
        conn.close()
    assert _importance(db, auto) == 6


def test_quoted_override_and_session_resync(tmp_path, db):
    path = _note(tmp_path, "session.md", "claude-session")
    assert vault_index.index_note(db, path)
    _execute(db, "UPDATE notes SET importance=9")
    assert vault_index.index_note(db, path)
    assert _importance(db, path) == 9
    Path(path).write_text(Path(path).read_text().replace("date:", 'importance: "7"\ndate:'))
    assert vault_index.index_note(db, path)
    assert _importance(db, path) == 7


@pytest.fixture
def legacy(db, tmp_path):
    paths = [
        _note(tmp_path, "auto.md", extra="source: auto-capture\n"),
        _note(tmp_path, "hand.md"),
        _note(tmp_path, "session.md", "claude-session", "importance: 10\n"),
        _note(tmp_path, "missing.md"),
        _note(tmp_path, "malformed.md"),
    ]
    for path in paths:
        assert vault_index.index_note(db, path)
    _execute(db, "UPDATE notes SET importance=5")
    Path(paths[3]).unlink()
    Path(paths[4]).write_text("No frontmatter")
    return paths


def test_backfill_dry_run_and_apply(db, legacy):
    result = vault_index.backfill_importance(db)
    assert result == {"applied": False, "scanned": 4, "changed": 2,
                      "by_value": {"3": 1, "6": 1},
                      "from_to": {"5->3": 1, "5->6": 1}, "unreadable": 2}
    assert [_importance(db, p) for p in legacy] == [5] * 5
    result = vault_index.backfill_importance(db, apply=True)
    assert result["updated"] == 2
    assert [_importance(db, p) for p in legacy] == [3, 6, 5, 5, 5]


def test_backfill_compare_and_set(db, legacy, monkeypatch):
    real_parse = vault_index._parse_note

    def parse_then_change(path):
        parsed = real_parse(path)
        if path == legacy[0]:
            _execute(db, "UPDATE notes SET importance=9 WHERE path=?", (path,))
        return parsed

    monkeypatch.setattr(vault_index, "_parse_note", parse_then_change)
    result = vault_index.backfill_importance(db, apply=True)
    assert result["changed"] == 2
    assert result["updated"] == 1
    assert _importance(db, legacy[0]) == 9


def test_backfill_rolls_back_on_failure(db, legacy, monkeypatch):
    real_connect = vault_index._connect

    class Proxy:
        def __init__(self, conn):
            self.conn = conn
            self.updates = 0

        def execute(self, sql, *args):
            if sql.startswith("UPDATE notes SET importance"):
                self.updates += 1
                if self.updates == 2:
                    raise sqlite3.OperationalError("failed update")
            return self.conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    monkeypatch.setattr(vault_index, "_connect", lambda path: Proxy(real_connect(path)))
    with pytest.raises(sqlite3.OperationalError, match="failed update"):
        vault_index.backfill_importance(db, apply=True)
    assert [_importance(db, p) for p in legacy] == [5] * 5


def test_missing_db(tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(FileNotFoundError):
        vault_index.backfill_importance(str(path))
    assert not path.exists()


def test_hand_saved_ranks_first(tmp_path, db):
    auto = _note(tmp_path, "auto.md", extra="source: auto-capture\n")
    hand = _note(tmp_path, "hand.md")
    for path in (auto, hand):
        assert vault_index.index_note(db, path)
    results = vault_index.search_vault(db, "searchable knowledge", log_access=False)
    assert [r["path"] for r in results] == [hand, auto]
    assert _execute(db, "SELECT count(*) FROM access_log") == [(0,)]


def _run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                          text=True, timeout=60, stdin=subprocess.DEVNULL)


def test_script_dry_run_and_apply(db, legacy):
    proc = _run("--db", db)
    assert proc.returncode == 0, proc.stderr
    for text in ("dry run", "scanned: 4", "changed: 2", "unreadable: 2", "by_value:", "from_to:"):
        assert text in proc.stdout
    assert [_importance(db, p) for p in legacy] == [5] * 5
    proc = _run("--db", db, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert "updated: 2" in proc.stdout


def test_script_missing_db(tmp_path):
    proc = _run("--db", str(tmp_path / "absent.db"))
    assert proc.returncode == 1
    assert "no index DB" in proc.stderr


def test_script_bad_flag():
    proc = _run("--bad-flag")
    assert proc.returncode == 2
    assert "usage: importance_backfill.py" in proc.stderr
