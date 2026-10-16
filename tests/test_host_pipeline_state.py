"""Deep pipelines use explicit native operations and input revisions."""
import io
import json
from types import MappingProxyType

import pytest
import deep_cli
from runtime_context import RuntimeContext, using_runtime_context
from operation_state import store_artifact

@pytest.fixture
def context(selected_host_context):
    (selected_host_context.vault_path / "sessions").mkdir(parents=True, exist_ok=True)
    return selected_host_context

def test_producer_and_consumer_handoff_checks_revision(context, monkeypatch, capsys):
    note = context.vault_path / 'sessions' / 'note.md'; note.write_text('original')
    def pipeline(basenames, projects, output, *args, **kwargs):
        from pathlib import Path
        Path(output).write_text(json.dumps({'items': {'groups': [], 'total_raw': 1}}))
        return 'OK:1:0:0'
    monkeypatch.setattr(deep_cli, 'deep_analysis_pipeline', pipeline)
    monkeypatch.setattr(deep_cli, 'build_deep_presentation', lambda *args: 'Rendered')
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'basenames': ['note.md'], 'projects': ['p']})))
    with using_runtime_context(context):
        deep_cli.run_pipeline(str(context.vault_path), 'sessions', 'insights', operation_id='a' * 32)
        handoff = json.loads(capsys.readouterr().out.splitlines()[0])
        assert handoff['operation_id'] == 'a' * 32
        store_artifact(context, 'a' * 32, 'deep-classifications.json', '{}')
        monkeypatch.setattr('sys.stdin', io.StringIO('["note.md"]'))
        deep_cli.run_present(str(context.vault_path), 'sessions', 'insights',
                             operation_id='a' * 32, pipeline_path=handoff['pipeline_path'],
                             classifications_path=handoff['classifications_path'])
        assert capsys.readouterr().out.strip() == 'Rendered'
        note.write_text('new source revision')
        monkeypatch.setattr('sys.stdin', io.StringIO('["note.md"]'))
        with pytest.raises(ValueError, match='source revision changed'):
            deep_cli.run_present(str(context.vault_path), 'sessions', 'insights', operation_id='a' * 32)

def test_producer_requires_native_context(monkeypatch, tmp_path):
    monkeypatch.setattr('sys.stdin', io.StringIO('{"basenames": [], "projects": []}'))
    with using_runtime_context(None), pytest.raises(ValueError, match='context unavailable'):
        deep_cli.run_pipeline(str(tmp_path), 'sessions', 'insights')

from parity_test_helpers import host, selected_host_context
pytestmark = pytest.mark.usefixtures("selected_host_context")


def test_pipeline_error_returns_failure_without_handoff(context, monkeypatch, capsys):
    monkeypatch.setattr(deep_cli, 'deep_analysis_pipeline', lambda *args, **kwargs: 'ERROR:Operation inputs changed')
    monkeypatch.setattr(deep_cli, '_load_acted_items', lambda: pytest.fail('Failed pipelines must not process acted-item state'))
    monkeypatch.setattr('sys.stdin', io.StringIO('{"basenames": [], "projects": []}'))
    with using_runtime_context(context):
        assert deep_cli.run_pipeline(str(context.vault_path), 'sessions', 'insights', operation_id='b' * 32) == 1
    assert capsys.readouterr().out.strip() == 'ERROR:Operation inputs changed'


def test_registered_pipeline_preserves_failure_exit(context, monkeypatch):
    from skill_procedures import run_operation
    output, errors = io.StringIO(), io.StringIO()
    monkeypatch.setattr(deep_cli, 'deep_analysis_pipeline', lambda *args, **kwargs: 'ERROR:vault index failed: refused')
    status = run_operation(context, 'standup', 'deep-pipeline', {
        'operation_id': 'c' * 32, 'stdin': {'basenames': [], 'projects': []}
    }, output, errors)
    assert status == 1
    assert 'ERROR:vault index failed: refused' in output.getvalue()
    assert 'pipeline_path' not in output.getvalue()


def _read_source(context, operation, note):
    from skill_procedures import run_operation
    output, errors = io.StringIO(), io.StringIO()
    status = run_operation(context, 'standup', 'note-read', {
        'operation_id': operation, 'path': str(note)
    }, output, errors)
    assert status == 0, errors.getvalue()
    return json.loads(output.getvalue())


def test_read_then_isolated_cascade_then_real_deep_pipeline(context, monkeypatch, capsys):
    from skill_procedures import run_operation
    from operation_state import operation_semantic, operation_directory, read_artifact
    note = context.vault_path / 'sessions' / 'note.md'
    note.write_text('---\ntype: claude-session\nproject: p\n---\n\nSynthetic source without open items.\n')
    primary, cascade = 'd' * 32, 'e' * 32
    _read_source(context, primary, note)
    output, errors = io.StringIO(), io.StringIO()
    assert run_operation(context, 'standup', 'cascade-collect', {
        'operation_id': cascade, 'project': 'p'
    }, output, errors) == 0
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'basenames': [note.name], 'projects': []})))
    with using_runtime_context(context):
        assert deep_cli.run_pipeline(str(context.vault_path), 'sessions', 'insights', operation_id=primary) == 0
    assert operation_semantic(context, primary)['algorithm'] == 'deep-v1'
    assert operation_semantic(context, cascade) is None
    assert json.loads(read_artifact(context, primary, 'deep-pipeline.json'))['items']['total_raw'] == 0
    _, directory = operation_directory(context, primary)
    before = (directory / 'deep-pipeline.json').read_bytes()
    note.write_text('Changed source after analysis')
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'basenames': [note.name], 'projects': []})))
    with using_runtime_context(context), pytest.raises(ValueError, match='Operation inputs changed'):
        deep_cli.run_pipeline(str(context.vault_path), 'sessions', 'insights', operation_id=primary)
    assert (directory / 'deep-pipeline.json').read_bytes() == before


@pytest.mark.parametrize('damage', ['source', 'artifact', 'scope', 'unexpected'])
def test_first_binding_rejects_invalid_prerequisites_before_pipeline(context, monkeypatch, damage):
    import operation_state
    note = context.vault_path / 'sessions' / 'note.md'; note.write_text('original')
    operation = 'f' * 32
    _read_source(context, operation, note)
    _, directory = operation_state.operation_directory(context, operation)
    if damage == 'source':
        note.write_text('changed')
    elif damage == 'artifact':
        (directory / 'source-manifest.json').write_text('{}')
    elif damage == 'scope':
        prepared = json.loads(operation_state.read_artifact(context, operation, 'source-manifest.json'))
        prepared['host'] = 'foreign'
        operation_state.store_artifact(context, operation, 'source-manifest.json', json.dumps(prepared))
    else:
        operation_state.store_artifact(context, operation, 'cascade-source.json', '[]')
    before = (directory / 'manifest.json').read_bytes()
    monkeypatch.setattr(deep_cli, 'deep_analysis_pipeline', lambda *a, **k: pytest.fail('Refusal must precede pipeline/provider work'))
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'basenames': [note.name], 'projects': []})))
    with using_runtime_context(context), pytest.raises(ValueError):
        deep_cli.run_pipeline(str(context.vault_path), 'sessions', 'insights', operation_id=operation)
    assert (directory / 'manifest.json').read_bytes() == before


def test_same_semantic_reuse_still_checks_protected_source(context):
    from operation_state import bind_semantic, operation_semantic
    note = context.vault_path / 'sessions' / 'note.md'; note.write_text('original')
    operation = '1' * 32
    _read_source(context, operation, note)
    semantic = deep_cli._pipeline_identity(str(context.vault_path), [note.name], [])
    bind_semantic(context, operation, semantic)
    bind_semantic(context, operation, semantic)
    note.write_text('concurrent manual edit')
    with pytest.raises(ValueError, match='Prepared source revision changed'):
        bind_semantic(context, operation, semantic)
    assert operation_semantic(context, operation) == semantic


@pytest.mark.parametrize('damage', ['symlink', 'directory', 'oversize'])
def test_binding_rejects_invalid_protected_note(context, damage):
    from operation_state import bind_semantic, operation_directory
    note = context.vault_path / 'sessions' / 'note.md'; note.write_text('original')
    operation = '2' * 32
    _read_source(context, operation, note)
    semantic = deep_cli._pipeline_identity(str(context.vault_path), [note.name], [])
    _, directory = operation_directory(context, operation)
    before = (directory / 'manifest.json').read_bytes()
    if damage == 'symlink':
        target = context.vault_path / 'sessions' / 'other.md'; target.write_text('original')
        note.unlink(); note.symlink_to(target)
    elif damage == 'directory':
        note.unlink(); note.mkdir()
    else:
        note.write_bytes(b'x' * 1_000_001)
    with pytest.raises((ValueError, OSError)):
        bind_semantic(context, operation, semantic)
    assert (directory / 'manifest.json').read_bytes() == before


def test_same_operation_cascade_collision_returns_failure_before_pipeline(context, monkeypatch):
    from skill_procedures import run_operation
    from operation_state import operation_directory
    operation = '3' * 32
    output, errors = io.StringIO(), io.StringIO()
    assert run_operation(context, 'standup', 'cascade-collect', {
        'operation_id': operation, 'project': 'p'
    }, output, errors) == 0
    _, directory = operation_directory(context, operation)
    before = (directory / 'manifest.json').read_bytes()
    monkeypatch.setattr(deep_cli, 'deep_analysis_pipeline', lambda *a, **k: pytest.fail('Semantic collision must refuse before pipeline/provider work'))
    output, errors = io.StringIO(), io.StringIO()
    assert run_operation(context, 'standup', 'deep-pipeline', {
        'operation_id': operation, 'stdin': {'basenames': [], 'projects': []}
    }, output, errors) == 1
    assert 'Unexpected artifacts before semantic binding' in errors.getvalue()
    assert (directory / 'manifest.json').read_bytes() == before
