"""Deep pipelines use explicit native operations and input revisions."""
import io
import json
from types import MappingProxyType

import pytest
import deep_cli
from runtime_context import RuntimeContext, using_runtime_context
from operation_state import store_artifact

@pytest.fixture
def context(tmp_path):
    vault = tmp_path / 'vault'; (vault / 'sessions').mkdir(parents=True)
    return RuntimeContext('codex', 'cli', 'pipeline-native', tmp_path, tmp_path, None,
                          vault, tmp_path / 'config', MappingProxyType({}),
                          tmp_path, tmp_path / 'index', tmp_path / 'state')

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
    with pytest.raises(ValueError, match='context unavailable'):
        deep_cli.run_pipeline(str(tmp_path), 'sessions', 'insights')
