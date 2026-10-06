"""Legacy session adoption verifies full identity with bounded read-only lookup."""
import hashlib
from contextlib import closing
import sqlite3
import time
from pathlib import Path
from dataclasses import replace
import pytest
from session_lookup import find_existing_session, SessionLookupPending


@pytest.fixture
def lookup(selected_host_context):
    vault = selected_host_context.vault_path
    (vault / 'claude-sessions').mkdir(parents=True)
    database = selected_host_context.index_path
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute('CREATE TABLE notes(path TEXT PRIMARY KEY,type TEXT)')
    context = replace(selected_host_context, native_session_id='full-native-id')
    suffix = (hashlib.sha256(context.native_session_id.encode()).hexdigest()[:4]
              if context.host == 'claude' else context.host + '-' +
              hashlib.sha256((context.host + '\0' + context.native_session_id).encode()).hexdigest()[:16])

    def add(name, identity='full-native-id', provider=None, root=None):
        path = (root or vault / 'claude-sessions') / (name + '-' + suffix + '.md')
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = '---\ntype: claude-session\nstatus: auto-logged\nsession_id: ' + identity + '\n'
        if provider or context.host == 'codex':
            fields += 'agent_provider: ' + (provider or context.host) + '\n'
        path.write_text(fields + '---\nOriginal incomplete legacy note\n')
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute('INSERT INTO notes VALUES (?,?)', (str(path), 'claude-session'))
        return path
    return context, add


def test_adopts_full_identity_regardless_date_and_project_root(lookup):
    context, add = lookup
    path = add('2024-01-01-prior-worktree')
    assert find_existing_session(context, time.monotonic() + 1) == path


def test_short_hash_collision_never_adopts_wrong_identity(lookup):
    context, add = lookup
    add('old', identity='different-full-id')
    assert find_existing_session(context, time.monotonic() + 1) is None


def test_cross_host_same_native_id_excluded(lookup):
    context, add = lookup
    foreign = 'codex' if context.host == 'claude' else 'claude'
    add('foreign', provider=foreign)
    assert find_existing_session(context, time.monotonic() + 1) is None


def test_wrong_vault_or_outside_folder_candidates_not_adopted(lookup, tmp_path):
    context, add = lookup
    add('outside-vault', root=tmp_path / 'other-vault')
    add('outside-folder', root=context.vault_path / 'claude-insights')
    assert find_existing_session(context, time.monotonic() + 1) is None


def test_multiple_full_identity_matches_remain_pending(lookup):
    context, add = lookup
    add('one')
    add('two')
    with pytest.raises(SessionLookupPending, match='multiple'):
        find_existing_session(context, time.monotonic() + 1)


def test_candidate_cap_remains_pending_even_if_one_matches(lookup):
    context, add = lookup
    for number in range(9):
        add(str(number), identity='wrong' if number else context.native_session_id)
    with pytest.raises(SessionLookupPending, match='candidate'):
        find_existing_session(context, time.monotonic() + 1)


def test_expired_deadline_remains_pending(lookup):
    context, add = lookup
    with pytest.raises(SessionLookupPending, match='deadline'):
        find_existing_session(context, time.monotonic() - 1)


def test_missing_index_creates_nothing(lookup):
    context, add = lookup
    context.index_path.unlink()
    before = sorted(context.vault_path.rglob('*'))
    assert find_existing_session(context, time.monotonic() + 1) is None
    assert not context.index_path.exists()
    assert sorted(context.vault_path.rglob('*')) == before


def test_symlink_escape_candidate_is_excluded(lookup, tmp_path):
    context, add = lookup
    path = add('inside')
    outside = tmp_path / 'outside.md'
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    assert find_existing_session(context, time.monotonic() + 1) is None


def test_frontmatter_cap_retains_pending_without_reading_body(lookup):
    context, add = lookup
    path = add('large')
    path.write_text('---\n' + 'x' * 65536 + '\n---\n')
    with pytest.raises(SessionLookupPending, match='byte limit'):
        find_existing_session(context, time.monotonic() + 1)


def test_large_body_does_not_prevent_bounded_identity_lookup(lookup):
    context, add = lookup
    path = add('large-body')
    path.write_bytes(path.read_bytes() + b'x' * 100000)
    assert find_existing_session(context, time.monotonic() + 1) == path


def test_duplicate_identity_fields_remain_pending(lookup):
    context, add = lookup
    path = add('duplicate')
    path.write_text('---\ntype: claude-session\nsession_id: full-native-id\nsession_id: other\n---\n')
    with pytest.raises(SessionLookupPending, match='duplicate identity'):
        find_existing_session(context, time.monotonic() + 1)


def test_explicit_agent_identity_is_verified(lookup):
    context, add = lookup
    path = add('agent-fields', identity='legacy-id')
    path.write_text('---\ntype: claude-session\nsession_id: legacy-id\nagent_provider: "' + context.host + '"\nagent_session_id: "full-native-id"\n---\n')
    assert find_existing_session(context, time.monotonic() + 1) == path


def test_nonregular_candidate_cannot_block_lookup(lookup):
    import os
    context, add = lookup
    path = add('fifo')
    path.unlink()
    os.mkfifo(path)
    assert find_existing_session(context, time.monotonic() + 1) is None


def test_unreadable_index_remains_pending(lookup):
    context, add = lookup
    context.index_path.write_bytes(b'not a sqlite database')
    with pytest.raises(SessionLookupPending, match='index lookup'):
        find_existing_session(context, time.monotonic() + 1)


@pytest.mark.parametrize('body', [b'not frontmatter\n', b'---\nsession_id: \xff\n---\n', b'---\nsession_id: 7\n---\n', b'---\nsession_id: full-native-id\n'])
def test_unverified_frontmatter_cannot_adopt(lookup, body):
    context, add = lookup
    path = add('unverified')
    path.write_bytes(body)
    assert find_existing_session(context, time.monotonic() + 1) is None


def test_invalid_sessions_folder_remains_pending(lookup):
    context, add = lookup
    context = replace(context, config={'sessions_folder': '../outside'})
    with pytest.raises(SessionLookupPending, match='Invalid selected'):
        find_existing_session(context, time.monotonic() + 1)


def test_sessions_folder_symlink_escape_remains_pending(lookup, tmp_path):
    context, add = lookup
    target = tmp_path / 'outside'
    target.mkdir()
    (context.vault_path / 'elsewhere').symlink_to(target)
    context = replace(context, config={'sessions_folder': 'elsewhere'})
    with pytest.raises(SessionLookupPending, match='escapes vault'):
        find_existing_session(context, time.monotonic() + 1)


def _cantopen_once(monkeypatch, database, *, error_code=14):
    original = sqlite3.connect
    calls = []
    def connect(path, *args, **kwargs):
        if str(path).startswith(database.resolve().as_uri()):
            calls.append(str(path))
            if len(calls) == 1:
                error = sqlite3.OperationalError('unable to open database file')
                error.sqlite_errorcode = error_code
                raise error
        return original(path, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    return calls


def test_closed_wal_cantopen_uses_immutable_lookup_without_file_changes(lookup, monkeypatch):
    context, add = lookup
    path = add('legacy')
    before = context.index_path.read_bytes()
    calls = _cantopen_once(monkeypatch, context.index_path)
    assert find_existing_session(context, time.monotonic() + 1) == path
    assert calls == [context.index_path.resolve().as_uri() + '?mode=ro',
                     context.index_path.resolve().as_uri() + '?mode=ro&immutable=1']
    assert context.index_path.read_bytes() == before
    assert not Path(str(context.index_path) + '-wal').exists()
    assert not Path(str(context.index_path) + '-shm').exists()


@pytest.mark.parametrize('sidecar', ['-wal', '-shm'])
def test_cantopen_with_existing_sidecar_never_uses_immutable(lookup, monkeypatch, sidecar):
    context, add = lookup
    add('legacy')
    Path(str(context.index_path) + sidecar).write_bytes(b'private-sidecar')
    calls = _cantopen_once(monkeypatch, context.index_path)
    with pytest.raises(SessionLookupPending, match='index lookup is pending'):
        find_existing_session(context, time.monotonic() + 1)
    assert len(calls) == 1


def test_non_cantopen_code_never_uses_immutable_even_if_message_matches(lookup, monkeypatch):
    context, add = lookup
    add('legacy')
    calls = _cantopen_once(monkeypatch, context.index_path, error_code=11)
    with pytest.raises(SessionLookupPending, match='index lookup is pending'):
        find_existing_session(context, time.monotonic() + 1)
    assert len(calls) == 1


def test_immutable_lookup_rejects_index_changed_during_read(lookup, monkeypatch):
    context, add = lookup
    add('legacy')
    _cantopen_once(monkeypatch, context.index_path)
    from session_lookup import _readonly_index
    with pytest.raises(SessionLookupPending, match='changed during read-only lookup'):
        with _readonly_index(context.index_path, time.monotonic() + 1) as connection:
            assert connection.execute('SELECT COUNT(*) FROM notes').fetchone() == (1,)
            context.index_path.touch()


def test_immutable_fallback_does_not_choose_between_duplicate_origins(lookup, monkeypatch):
    context, add = lookup
    add('one'); add('two')
    _cantopen_once(monkeypatch, context.index_path)
    with pytest.raises(SessionLookupPending, match='multiple full-identity'):
        find_existing_session(context, time.monotonic() + 1)


def _indexed_hint_fixture(context):
    from note_transactions import ownership_lock, connect_coordination
    with ownership_lock(context), closing(connect_coordination(context)):
        pass
    with closing(sqlite3.connect(context.index_path)) as connection, connection:
        for name in ('project', 'date', 'body'):
            connection.execute('ALTER TABLE notes ADD COLUMN '+name+' TEXT')
        connection.execute('UPDATE notes SET project=?,date=?,body=?',
            (context.canonical_project_root.name, '2026-10-06', '## Summary\nPrevious fixture summary\n'))


def test_legacy_index_hint_uses_guarded_immutable_fallback(lookup, monkeypatch):
    from native_lifecycle import _context_hint
    context, add = lookup
    add('legacy')
    _indexed_hint_fixture(context)
    before = context.index_path.read_bytes()
    calls = _cantopen_once(monkeypatch, context.index_path)
    output = _context_hint(context, time.monotonic() + 1)
    assert 'Previous fixture summary' in output['hookSpecificOutput']['additionalContext']
    assert len(calls) == 2 and calls[-1].endswith('?mode=ro&immutable=1')
    assert context.index_path.read_bytes() == before
    assert not Path(str(context.index_path)+'-wal').exists()
    assert not Path(str(context.index_path)+'-shm').exists()


def test_legacy_index_hint_refuses_wrong_coordination_identity(lookup, monkeypatch):
    from native_lifecycle import _context_hint
    from note_transactions import coordination_location
    context, add = lookup
    add('legacy')
    _indexed_hint_fixture(context)
    with closing(sqlite3.connect(coordination_location(context)/'state.sqlite3')) as connection, connection:
        connection.execute('UPDATE identity SET vault=?', ('/unrelated-vault',))
    calls = _cantopen_once(monkeypatch, context.index_path)
    assert _context_hint(context, time.monotonic() + 1) is None
    assert not calls
