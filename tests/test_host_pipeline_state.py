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
