"""Inject edits at the final publication boundary in disposable vaults."""
from dataclasses import replace
from pathlib import Path
import time
import sqlite3
import json
import threading

import pytest

from parity_test_helpers import host, selected_host_context, context, host_identity_scenario
import note_transactions as tx


def test_coordination_ignores_index_and_native_home(context, host_identity_scenario):
    other = replace(context, index_path=context.index_path.parent / 'other.sqlite',
                    native_home=context.native_home.parent / 'other-native',
                    native_session_id='second-session')
    host_identity_scenario.register(context, other)
    assert tx.coordination_path(context) == tx.coordination_path(other)
    path = context.vault_path / 'note.md'
    path.write_text('original')
    revision = tx.read_revision(context, path)
    assert tx.read_revision(other, path) == revision


def test_new_vault_has_new_coordination_namespace(context, host_identity_scenario):
    vault = context.vault_path.parent / 'other-vault'
    vault.mkdir()
    other = replace(context, vault_path=vault)
    host_identity_scenario.register(context, other)
    assert tx.coordination_path(context) != tx.coordination_path(other)
    with tx.ownership_lock(context):
        tx.connect_coordination(context).close()
    with tx.ownership_lock(other):
        tx.connect_coordination(other).close()


def test_coordination_keeps_frozen_home_after_environment_change(context, monkeypatch):
    before = tx.coordination_location(context)
    monkeypatch.setenv('HOME', str(context.vault_path.parent / 'unrelated-home'))
    assert tx.coordination_location(context) == before
    assert not before.exists()


def test_other_host_and_index_cannot_take_writer_lock(context, host_identity_scenario):
    other = replace(context, host='codex' if context.host == 'claude' else 'claude',
                    client='codex-cli' if context.host == 'claude' else 'claude-code',
                    index_path=context.index_path.parent / 'other.sqlite')
    host_identity_scenario.register(context, other)
    observed = []

    def compete():
        try:
            with tx.ownership_lock(other):
                observed.append('entered')
        except tx.LockBusy:
            observed.append('busy')

    with tx.ownership_lock(context):
        thread = threading.Thread(target=compete)
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert observed == ['busy']


@pytest.mark.parametrize('same_vault', [True, False])
def test_prior_coordination_migrates_only_matching_vault(context, same_vault):
    old = context.index_path.parent / ('.' + context.index_path.name + '.coordination')
    old.mkdir(parents=True)
    database = old / 'state.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.executescript('CREATE TABLE identity(vault TEXT PRIMARY KEY); CREATE TABLE marker(value TEXT);')
        connection.execute('INSERT INTO identity VALUES (?)', (str(context.vault_path) if same_vault else '/different/vault',))
        connection.execute('INSERT INTO marker VALUES (?)', ('preserved',))
    database.chmod(0o600)
    original = database.read_bytes()
    with tx.ownership_lock(context):
        connection = tx.connect_coordination(context)
        try:
            if same_vault:
                assert connection.execute('SELECT value FROM marker').fetchone() == ('preserved',)
            else:
                assert connection.execute("SELECT name FROM sqlite_master WHERE name='marker'").fetchone() is None
        finally:
            connection.close()
    assert database.read_bytes() == original


@pytest.mark.parametrize('action', ['apply', 'move', 'delete'])
@pytest.mark.parametrize('attack', ['edit', 'parent-symlink'])
def test_final_publication_guard_preserves_manual_edit(context, monkeypatch, action, attack):
    folder = context.vault_path / 'folder'
    folder.mkdir()
    path = folder / 'source.md'
    path.write_text('original')
    destination = folder / 'destination.md'
    revision = tx.read_revision(context, path)
    seam = {'apply': 'before_replace', 'move': 'before_move_link', 'delete': 'before_unlink'}[action]
    preserved = context.vault_path / 'preserved'
    outside = context.vault_path.parent / 'outside'
    outside.mkdir()
    # Same-byte replacement makes containment an independent safety check.
    outside_content = 'original' if attack == 'parent-symlink' else 'outside bytes'
    (outside / 'source.md').write_text(outside_content)
    triggered = []

    def inject(point):
        if point != seam:
            return
        triggered.append(point)
        if attack == 'edit':
            path.write_text('manual edit')
        else:
            folder.rename(preserved)
            folder.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(tx, '_fault', inject)
    if action == 'apply':
        result = tx.apply_mutations(context, [tx.NoteMutation(path, revision, {'document': 'generated'}, 'apply-review')])
    elif action == 'move':
        result = tx.move_note(context, path, destination, revision, 'move-review')
    else:
        result = tx.delete_note(context, path, revision, 'delete-review')
    assert triggered == [seam]
    assert result.status == 'conflict'
    assert (outside / 'source.md').read_text() == outside_content
    assert not (outside / 'destination.md').exists()
    assert (path if attack == 'edit' else preserved / 'source.md').read_text() == ('manual edit' if attack == 'edit' else 'original')


def test_move_rechecks_after_destination_link(context, monkeypatch):
    path = context.vault_path / 'source.md'
    destination = context.vault_path / 'destination.md'
    path.write_text('original')
    revision = tx.read_revision(context, path)
    seen = []

    def inject(point):
        if point == 'after_move_link':
            seen.append(point)
            path.write_text('manual edit')

    monkeypatch.setattr(tx, '_fault', inject)
    result = tx.move_note(context, path, destination, revision, 'move-post-link')
    assert seen and result.status == 'conflict'
    assert path.read_text() == destination.read_text() == 'manual edit'


def test_pending_batch_replays_original_cas_and_cleans_file(context):
    path = context.vault_path / 'pending.md'
    path.write_text('original')
    revision = tx.read_revision(context, path)
    payload = json.dumps([{'path': str(path), 'expected': revision,
        'changes': {'document': 'recovered'}, 'operation': 'recover-review', 'mode': None}])
    pending = tx._pending(context, 'pending-batch', payload)
    result = tx.recover_pending_mutations(context, deadline=time.monotonic() + 1)
    assert result.status == 'unchanged'
    assert path.read_text() == 'recovered'
    assert not pending.exists()


def test_pending_replay_keeps_new_manual_bytes(context):
    path = context.vault_path / 'pending.md'
    path.write_text('original')
    revision = tx.read_revision(context, path)
    payload = json.dumps({'path': str(path), 'expected': revision,
        'changes': {'document': 'generated'}, 'operation': 'conflicting-review', 'mode': None})
    pending = tx._pending(context, 'pending-conflict', payload)
    path.write_text('manual edit')
    result = tx.recover_pending_mutations(context, max_operations=1)
    assert result.status == 'pending' and pending.exists()
    assert path.read_text() == 'manual edit'
