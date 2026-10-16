"""Private pipeline state cannot cross scopes or silently reuse changed inputs."""
from dataclasses import replace
from types import MappingProxyType

import pytest
from runtime_context import RuntimeContext
from operation_state import operation_directory, store_artifact, read_artifact

@pytest.fixture
def context(selected_host_context):
    return selected_host_context

def test_identity_hash_and_semantic_scope(context):
    identity, directory = operation_directory(context)
    path = store_artifact(context, identity, 'analysis.md', 'Synthetic body', semantic={'input': 'one'})
    assert path.parent == directory
    assert read_artifact(context, identity, 'analysis.md', semantic={'input': 'one'}) == b'Synthetic body'
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match='inputs changed'):
        read_artifact(context, identity, 'analysis.md', semantic={'input': 'two'})
    path.write_text('different')
    with pytest.raises(ValueError, match='content changed'):
        read_artifact(context, identity, 'analysis.md')

def test_explicit_identity_and_nofollow(context, tmp_path):
    with pytest.raises(ValueError):
        operation_directory(context, '../escape')
    identity, directory = operation_directory(context)
    outside = tmp_path / 'outside'; outside.write_text('keep')
    (directory / 'analysis.md').symlink_to(outside)
    with pytest.raises(ValueError, match='symlinks'):
        store_artifact(context, identity, 'analysis.md', 'overwrite')
    assert outside.read_text() == 'keep'

def test_other_host_and_session_cannot_reuse(context):
    identity, _ = operation_directory(context)
    store_artifact(context, identity, 'pipeline.json', '{}')
    for changed in (replace(context, host='codex' if context.host == 'claude' else 'claude', client='codex-cli' if context.host == 'claude' else 'claude-code'), replace(context, native_session_id='another')):
        with pytest.raises(FileNotFoundError):
            read_artifact(changed, identity, 'pipeline.json')

def test_changed_project_and_backend_model_reject_manifest(context):
    identity, _ = operation_directory(context)
    store_artifact(context, identity, 'pipeline.json', '{}')
    changed = replace(context, config=MappingProxyType({'codex_ai_model': 'different'} if context.host == 'codex' else {'summary_model': 'different'}))
    with pytest.raises(ValueError, match='scope changed'):
        read_artifact(changed, identity, 'pipeline.json')

def test_live_lock_timeout_never_publishes(context, monkeypatch):
    import operation_state
    identity, directory = operation_directory(context)
    def blocked(*args):
        raise BlockingIOError()
    times = iter([1.0, 4.0])
    monkeypatch.setattr(operation_state.fcntl, 'flock', blocked)
    monkeypatch.setattr(operation_state.time, 'monotonic', lambda: next(times))
    with pytest.raises(TimeoutError, match='ownership unavailable'):
        store_artifact(context, identity, 'analysis.md', 'must not publish')
    assert not (directory / 'analysis.md').exists()


def test_unregistered_external_output_is_rejected(context):
    identity, directory = operation_directory(context)
    store_artifact(context, identity, 'pipeline.json', '{}')
    output = directory / 'classification.json'
    output.write_text('{}'); output.chmod(0o600)
    with pytest.raises(ValueError, match='content changed'):
        read_artifact(context, identity, 'classification.json')

def test_private_artifacts_cannot_be_configured_into_vault(context):
    from dataclasses import replace
    with pytest.raises(ValueError, match='outside the vault'):
        operation_directory(replace(context, state_path=context.vault_path / 'state'))

def test_low_level_private_writer_rejects_arbitrary_vault_path(context):
    from operation_state import _write_private
    context.vault_path.mkdir(exist_ok=True)
    note = context.vault_path / 'analysis.md'
    with pytest.raises(ValueError, match='outside the vault'):
        _write_private(note, b'unsafe', context)
    assert not note.exists()

@pytest.mark.parametrize('cached', [False, True])
def test_bound_deep_pipeline_registers_output(context, monkeypatch, cached):
    import json
    import open_item_dedup as pipeline
    from runtime_context import using_runtime_context
    identity, directory = operation_directory(context)
    monkeypatch.setattr(pipeline, '_cache_key', lambda *args: 'synthetic')
    monkeypatch.setattr(pipeline, '_evidence_cache_get', lambda *args: ('OK:0:0:0:0', '{}') if cached else None)
    import vault_index
    index_calls = []
    def fresh_index(*args, **kwargs):
        index_calls.append(kwargs['db_path'])
        return context.index_path
    monkeypatch.setattr(vault_index, 'ensure_index', fresh_index)
    monkeypatch.setattr(pipeline, '_evidence_cache_put', lambda *args: pytest.fail('Native pipeline saved age-only evidence'))
    with using_runtime_context(context):
        status = pipeline.deep_analysis_pipeline([], '[]', str(directory / 'deep-pipeline.json'),
            str(context.vault_path), 'claude-sessions', 'claude-insights', db_path=str(context.index_path), operation_id=identity)
    assert status.startswith('OK:')
    assert index_calls == [str(context.index_path)]
    assert isinstance(json.loads(read_artifact(context, identity, 'deep-pipeline.json')), dict)


def test_bound_deep_pipeline_rejects_external_output(context, monkeypatch, tmp_path):
    import open_item_dedup as pipeline
    from runtime_context import using_runtime_context
    identity, _ = operation_directory(context)
    outside = tmp_path / 'external.json'
    with using_runtime_context(context):
        with pytest.raises(ValueError, match='operation'):
            pipeline.deep_analysis_pipeline([], '[]', str(outside), str(context.vault_path),
                'claude-sessions', 'claude-insights', db_path=str(context.index_path), operation_id=identity)
    assert not outside.exists()


def test_unbound_pipeline_cannot_publish_any_output(tmp_path):
    import open_item_dedup as pipeline
    from runtime_context import using_runtime_context
    output = tmp_path / 'pipeline.json'
    output.write_text('Existing private result')
    with using_runtime_context(None):
        with pytest.raises(ValueError, match='invoking host context'):
            pipeline.deep_analysis_pipeline([], '[]', str(output), str(tmp_path / 'vault'),
                'sessions', 'insights', db_path=str(tmp_path / 'index.db'))
    assert output.read_text() == 'Existing private result'

from parity_test_helpers import host, selected_host_context
pytestmark = pytest.mark.usefixtures("selected_host_context")
