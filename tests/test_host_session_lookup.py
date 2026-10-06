"""Legacy session adoption verifies full identity with bounded read-only lookup."""
import hashlib
from contextlib import closing
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from session_lookup import find_existing_session, SessionLookupPending


@pytest.fixture
def lookup(tmp_path):
    vault = tmp_path / 'vault'
    (vault / 'claude-sessions').mkdir(parents=True)
    database = tmp_path / 'index.sqlite3'
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute('CREATE TABLE notes(path TEXT PRIMARY KEY,type TEXT)')
    context = SimpleNamespace(host='claude', native_session_id='full-native-id',
                              vault_path=vault, index_path=database, config={})
    suffix = hashlib.sha256(context.native_session_id.encode()).hexdigest()[:4]

    def add(name, identity='full-native-id', provider=None, root=None):
        path = (root or vault / 'claude-sessions') / (name + '-' + suffix + '.md')
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = '---\ntype: claude-session\nstatus: auto-logged\nsession_id: ' + identity + '\n'
        if provider:
            fields += 'agent_provider: ' + provider + '\n'
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
    add('codex', provider='codex')
    assert find_existing_session(context, time.monotonic() + 1) is None
    context.host = 'codex'
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
    path.write_text('---\ntype: claude-session\nsession_id: legacy-id\nagent_provider: "claude"\nagent_session_id: "full-native-id"\n---\n')
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
    context.config = {'sessions_folder': '../outside'}
    with pytest.raises(SessionLookupPending, match='Invalid selected'):
        find_existing_session(context, time.monotonic() + 1)


def test_sessions_folder_symlink_escape_remains_pending(lookup, tmp_path):
    context, add = lookup
    target = tmp_path / 'outside'
    target.mkdir()
    (context.vault_path / 'elsewhere').symlink_to(target)
    context.config = {'sessions_folder': 'elsewhere'}
    with pytest.raises(SessionLookupPending, match='escapes vault'):
        find_existing_session(context, time.monotonic() + 1)
