"""Private artifacts fail closed across interrupted writes and ownership changes."""
import fcntl
import os
import threading
from dataclasses import replace
from types import MappingProxyType

import pytest

import operation_state as state
from runtime_context import RuntimeContext


@pytest.fixture
def context(tmp_path):
    return RuntimeContext('codex', 'codex-cli', 'artifact-validation', tmp_path,
                          tmp_path, None, tmp_path / 'vault', tmp_path / 'config',
                          MappingProxyType({'codex_summary_model': 'selected-model'}),
                          tmp_path, tmp_path / 'index', tmp_path / 'state')


@pytest.mark.parametrize('name,content,error', [
    ('manifest.json', b'{}', 'managed internally'),
    ('../escape.json', b'{}', 'artifact name'),
    ('answer.txt', b'{}', 'artifact name'),
    ('answer.json', {'unencoded': True}, 'artifact content'),
    ('answer.json', b'x' * 4_000_001, 'artifact content'),
], ids=['managed-manifest', 'path-traversal', 'wrong-extension',
        'unencoded-content', 'oversized-content'])
def test_invalid_publication_never_registers_artifacts(context, name, content, error):
    operation_id, directory = state.operation_directory(context)
    with pytest.raises(ValueError, match=error):
        state.store_artifact(context, operation_id, name, content)
    assert not (directory / 'manifest.json').exists()
    assert not (directory / 'answer.json').exists()


def test_manifest_failure_leaves_unregistered_bytes_and_retry_recovers(context, monkeypatch):
    operation_id, directory = state.operation_directory(context)
    original_replace = state.os.replace
    def interrupt(source, destination):
        if destination.name == 'manifest.json':
            raise OSError('synthetic interruption before registration')
        return original_replace(source, destination)
    with monkeypatch.context() as patch:
        patch.setattr(state.os, 'replace', interrupt)
        with pytest.raises(OSError, match='synthetic interruption'):
            state.store_artifact(context, operation_id, 'answer.json', b'{"answer":1}', semantic={'revision': 'first'})
    assert (directory / 'answer.json').read_bytes() == b'{"answer":1}'
    assert not list(directory.glob('.artifact-*'))
    with pytest.raises(ValueError, match='content changed'):
        state.read_artifact(context, operation_id, 'answer.json')
    state.store_artifact(context, operation_id, 'answer.json', b'{"answer":1}', semantic={'revision': 'first'})
    assert state.read_artifact(context, operation_id, 'answer.json', semantic={'revision': 'first'}) == b'{"answer":1}'


def test_failed_replacement_retains_registered_previous_artifact(context, monkeypatch):
    operation_id, directory = state.operation_directory(context)
    state.store_artifact(context, operation_id, 'answer.md', 'Old answer.', semantic={'input': 'same'})
    monkeypatch.setattr(state.os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('replace denied')))
    with pytest.raises(OSError, match='replace denied'):
        state.store_artifact(context, operation_id, 'answer.md', 'New answer.', semantic={'input': 'same'})
    assert state.read_artifact(context, operation_id, 'answer.md') == b'Old answer.'
    assert not list(directory.glob('.artifact-*'))


def test_semantic_input_mismatch_cannot_replace_existing_bytes(context):
    operation_id, directory = state.operation_directory(context)
    state.store_artifact(context, operation_id, 'answer.json', '{}', semantic={'evidence': 'a'})
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    with pytest.raises(ValueError, match='inputs changed'):
        state.store_artifact(context, operation_id, 'answer.json', '{"other":1}', semantic={'evidence': 'b'})
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    assert state.operation_semantic(context, operation_id) == {'evidence': 'a'}


@pytest.mark.parametrize('change', ['project', 'vault', 'model'])
def test_scope_changes_refuse_same_session_artifact(context, change):
    operation_id, _ = state.operation_directory(context)
    state.store_artifact(context, operation_id, 'answer.json', '{}')
    updates = {'project': {'canonical_project_root': context.canonical_project_root / 'other'},
               'vault': {'vault_path': context.vault_path.parent / 'other-vault'},
               'model': {'config': MappingProxyType({'codex_summary_model': 'other-model'})}}
    changed = replace(context, **updates[change])
    with pytest.raises((ValueError, FileNotFoundError)):
        state.read_artifact(changed, operation_id, 'answer.json')
    assert state.read_artifact(context, operation_id, 'answer.json') == b'{}'


def test_shared_permissions_and_oversize_artifact_are_rejected(context):
    operation_id, directory = state.operation_directory(context)
    artifact = state.store_artifact(context, operation_id, 'answer.json', '{}')
    artifact.chmod(0o644)
    with pytest.raises(ValueError, match='owner-only'):
        state.read_artifact(context, operation_id, 'answer.json')
    artifact.chmod(0o600)
    artifact.write_bytes(b'x' * 4_000_001)
    with pytest.raises(ValueError, match='size limit'):
        state.read_artifact(context, operation_id, 'answer.json')
    assert (directory / 'manifest.json').exists()


def test_caller_cannot_substitute_a_different_artifact_path(context, tmp_path):
    operation_id, _ = state.operation_directory(context)
    artifact = state.store_artifact(context, operation_id, 'answer.json', '{}')
    with pytest.raises(ValueError, match='path does not match'):
        state.read_artifact(context, operation_id, 'answer.json', supplied_path=tmp_path / 'answer.json')
    assert state.read_artifact(context, operation_id, 'answer.json', supplied_path=artifact) == b'{}'


def test_replaced_lock_inode_cannot_publish(context, monkeypatch):
    operation_id, directory = state.operation_directory(context)
    original_flock = state.fcntl.flock
    def replace_locked_inode(descriptor, flags):
        result = original_flock(descriptor, flags)
        if flags & fcntl.LOCK_EX:
            lock = directory / '.operation.lock'
            lock.unlink()
            lock.touch(mode=0o600)
        return result
    monkeypatch.setattr(state.fcntl, 'flock', replace_locked_inode)
    with pytest.raises(ValueError, match='ownership changed'):
        state.store_artifact(context, operation_id, 'answer.json', '{}')
    assert not (directory / 'answer.json').exists()
    assert not (directory / 'manifest.json').exists()


def test_world_readable_lock_cannot_publish(context):
    operation_id, directory = state.operation_directory(context)
    lock = directory / '.operation.lock'
    lock.touch(mode=0o644)
    with pytest.raises(ValueError, match='lock is not owner-only'):
        state.store_artifact(context, operation_id, 'answer.json', '{}')
    assert not (directory / 'answer.json').exists()


def test_contender_waits_for_real_owner_before_publication(context, monkeypatch):
    operation_id, directory = state.operation_directory(context)
    descriptor = os.open(directory / '.operation.lock', os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    contended = threading.Event()
    original_flock = state.fcntl.flock
    def observe_contention(fd, flags):
        try:
            return original_flock(fd, flags)
        except BlockingIOError:
            contended.set()
            raise
    monkeypatch.setattr(state.fcntl, 'flock', observe_contention)
    errors = []
    def publish():
        try:
            state.store_artifact(context, operation_id, 'answer.json', '{}')
        except Exception as error:
            errors.append(error)
    worker = threading.Thread(target=publish)
    worker.start()
    try:
        assert contended.wait(1)
        assert not (directory / 'answer.json').exists()
    finally:
        original_flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        worker.join(3)
    assert not worker.is_alive()
    assert errors == []
    assert state.read_artifact(context, operation_id, 'answer.json') == b'{}'
