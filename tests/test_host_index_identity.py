"""Derived origin identity is distinct from actor and source references."""
import hashlib
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
import vault_index
from session_lookup import find_existing_session, SessionLookupPending


@pytest.fixture
def indexed(tmp_path):
    vault = tmp_path / 'vault'; (vault / 'sessions').mkdir(parents=True)
    database = tmp_path / 'index.sqlite3'
    return vault, database


def note(vault, name, fields):
    path = vault / 'sessions' / name
    path.write_text('---\ntype: claude-session\nproject: project\n' + fields + '\n---\n# Synthetic session\n')
    return path


def ctx(vault, database, host='codex', identity='opaque-native-id'):
    return SimpleNamespace(host=host, native_session_id=identity, vault_path=vault,
                           index_path=database, config={'sessions_folder': 'sessions'})


def test_identity_is_indexed_and_found_across_dates_without_filename_hash(indexed):
    vault, database = indexed
    path = note(vault, '2020-01-01-arbitrary-name.md', 'agent_provider: codex\nagent_session_id: opaque-native-id\nsession_id: opaque-native-id')
    vault_index.ensure_index(str(vault), ['sessions'], db_path=str(database))
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('SELECT agent_provider,agent_session_id FROM notes').fetchone() == ('codex', 'opaque-native-id')
    assert find_existing_session(ctx(vault, database), time.monotonic() + 1) == path


def test_same_id_different_hosts_and_fork_ids_are_distinct(indexed):
    vault, database = indexed
    claude = note(vault, 'claude.md', 'session_id: same-id')
    codex = note(vault, 'codex.md', 'agent_provider: codex\nagent_session_id: same-id\nsession_id: same-id')
    fork = note(vault, 'fork.md', 'agent_provider: codex\nagent_session_id: fork-id\nsession_id: fork-id\nparent_session: same-id')
    vault_index.ensure_index(str(vault), ['sessions'], db_path=str(database))
    for host, identity, expected in [('claude', 'same-id', claude), ('codex', 'same-id', codex), ('codex', 'fork-id', fork)]:
        assert find_existing_session(ctx(vault, database, host, identity), time.monotonic() + 1) == expected


def test_actor_and_source_session_do_not_replace_origin(indexed):
    vault, database = indexed
    original = note(vault, 'original.md', 'session_id: claude-origin\nauthor_host: codex\nsource_session: unrelated-reference')
    reference = note(vault, 'reference.md', 'source_session: not-native-id\nauthor_host: codex')
    vault_index.ensure_index(str(vault), ['sessions'], db_path=str(database))
    with closing(sqlite3.connect(database)) as connection:
        rows = dict(connection.execute('SELECT path, agent_provider FROM notes'))
        assert rows[str(original)] == 'claude'
        assert rows[str(reference)] is None
        assert connection.execute('SELECT agent_session_id FROM notes WHERE path=?', (str(reference),)).fetchone() == (None,)
    assert find_existing_session(ctx(vault, database, 'claude', 'claude-origin'), time.monotonic() + 1) == original
    assert find_existing_session(ctx(vault, database, 'codex', 'claude-origin'), time.monotonic() + 1) is None


def test_writable_schema_migrates_without_rebuilding_notes_or_fts(indexed):
    vault, database = indexed
    path = note(vault, 'legacy.md', 'session_id: legacy-id')
    old_schema = vault_index._SCHEMA_SQL.replace('    agent_provider  TEXT,\n', '').replace('    agent_session_id TEXT,\n', '')
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(old_schema)
        connection.execute("INSERT INTO notes(path,type,mtime,title,body,importance) VALUES (?,?,?,?,?,?)",
                           (str(path), 'claude-session', 0, 'Old title', 'Old body', 9))
        connection.execute("INSERT INTO notes_fts(rowid,title,body,tags) VALUES (1,'Old title','Old body','')")
        connection.execute("INSERT INTO access_log(note_path,timestamp,context_type) VALUES (?,1,'test')", (str(path),))
        connection.commit()
    assert vault_index.index_note(str(database), str(path))
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('SELECT agent_provider,agent_session_id,importance FROM notes').fetchone() == ('claude', 'legacy-id', 9)
        assert connection.execute('SELECT COUNT(*) FROM access_log').fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM notes_fts WHERE notes_fts MATCH 'Synthetic'").fetchone()[0] == 1
        indexes = {row[1] for row in connection.execute('PRAGMA index_list(notes)')}
        assert 'idx_notes_origin_identity' in indexes


def test_reindex_updates_origin_without_using_current_actor(indexed):
    vault, database = indexed
    path = note(vault, 'reindexed.md', 'session_id: first-id')
    vault_index.ensure_index(str(vault), ['sessions'], db_path=str(database))
    path.write_text(path.read_text().replace('first-id', 'second-id') + '\nUser addition.\n')
    assert vault_index.index_note(str(database), str(path))
    assert find_existing_session(ctx(vault, database, 'claude', 'first-id'), time.monotonic() + 1) is None
    assert find_existing_session(ctx(vault, database, 'claude', 'second-id'), time.monotonic() + 1) == path


def test_readonly_old_index_fallback_does_not_migrate(indexed):
    vault, database = indexed
    identity = 'legacy-full-id'
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:4]
    path = note(vault, 'prior-date-' + suffix + '.md', 'session_id: ' + identity)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('CREATE TABLE notes(path TEXT,type TEXT)')
        connection.execute('INSERT INTO notes VALUES (?,?)', (str(path), 'claude-session'))
        connection.commit()
    before = database.read_bytes()
    assert find_existing_session(ctx(vault, database, 'claude', identity), time.monotonic() + 1) == path
    assert database.read_bytes() == before
    with closing(sqlite3.connect(database)) as connection:
        assert [row[1] for row in connection.execute('PRAGMA table_info(notes)')] == ['path', 'type']


def test_indexed_pair_still_verifies_header_and_ambiguity(indexed):
    vault, database = indexed
    first = note(vault, 'first.md', 'agent_provider: codex\nagent_session_id: opaque-native-id')
    second = note(vault, 'second.md', 'agent_provider: codex\nagent_session_id: opaque-native-id')
    vault_index.ensure_index(str(vault), ['sessions'], db_path=str(database))
    with pytest.raises(SessionLookupPending, match='multiple'):
        find_existing_session(ctx(vault, database), time.monotonic() + 1)
    second.write_text(second.read_text().replace('opaque-native-id', 'manual-different-id'))
    assert find_existing_session(ctx(vault, database), time.monotonic() + 1) == first

def test_old_native_filename_fallback_keeps_index_readonly(indexed):
    vault, database = indexed
    identity = 'old-native-id'
    digest = hashlib.sha256(('codex\0' + identity).encode()).hexdigest()[:16]
    path = note(vault, '2020-01-01-project-codex-' + digest + '.md',
                'agent_provider: codex\nagent_session_id: ' + identity)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('CREATE TABLE notes(path TEXT,type TEXT)')
        connection.execute('INSERT INTO notes VALUES (?,?)', (str(path), 'claude-session'))
        connection.commit()
    before = database.read_bytes()
    assert find_existing_session(ctx(vault, database, 'codex', identity), time.monotonic() + 1) == path
    assert database.read_bytes() == before


@pytest.mark.parametrize('fields', [
    'source_session: reference-only',
    'agent_session_id: ambiguous-provider',
    'session_id: null',
    'agent_provider: unknown-host\nagent_session_id: unknown-id',
    'session_id: legacy-id\nagent_session_id: null',
    'session_id: one\nsession_id: two',
])
def test_missing_or_ambiguous_provider_is_not_guessed(indexed, fields):
    vault, database = indexed
    path = note(vault, 'ambiguous.md', fields)
    vault_index.ensure_index(str(vault), ['sessions'], db_path=str(database))
    with closing(sqlite3.connect(database)) as connection:
        provider, identity = connection.execute('SELECT agent_provider,agent_session_id FROM notes').fetchone()
    assert provider is None or identity is None


def test_direct_upsert_migrates_only_derived_schema_for_existing_callers(indexed):
    vault, database = indexed
    path = note(vault, 'direct.md', 'session_id: direct-origin')
    old_schema = vault_index._SCHEMA_SQL.replace('    agent_provider  TEXT,\n', '').replace('    agent_session_id TEXT,\n', '')
    with closing(vault_index._connect(str(database))) as connection:
        connection.executescript(old_schema)
        connection.execute('BEGIN IMMEDIATE')
        vault_index._upsert_note(connection, str(path), vault_index._parse_note(str(path)), 1, path.stat().st_size)
        connection.commit()
        assert tuple(connection.execute('SELECT agent_provider,agent_session_id FROM notes').fetchone()) == ('claude', 'direct-origin')

def test_agent_id_without_provider_or_real_legacy_id_cannot_be_adopted(indexed):
    vault, database = indexed
    identity = 'ambiguous-full-id'
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:4]
    path = note(vault, 'ambiguous-' + suffix + '.md', 'agent_session_id: ' + identity)
    vault_index.ensure_index(str(vault), ['sessions'], db_path=str(database))
    assert find_existing_session(ctx(vault, database, 'claude', identity), time.monotonic() + 1) is None

def test_concurrent_completed_column_migration_is_idempotent(indexed):
    vault, database = indexed
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(vault_index._SCHEMA_SQL)
        class ConcurrentMigration:
            def __init__(self):
                self.first = True
            def execute(self, sql, *args):
                if sql == 'PRAGMA table_info(notes)' and self.first:
                    self.first = False
                    return [row for row in connection.execute(sql) if row[1] not in {'agent_provider', 'agent_session_id'}]
                return connection.execute(sql, *args)
        vault_index._ensure_identity_columns(ConcurrentMigration())
        assert {'agent_provider', 'agent_session_id'} <= {row[1] for row in connection.execute('PRAGMA table_info(notes)')}
