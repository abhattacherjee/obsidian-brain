"""Bind existing temporary-vault tests to a real selected host resolver."""
import json
import pytest
from runtime_context import resolve_runtime_context, using_runtime_context


@pytest.fixture
def selected_host_context(selected_host_context, host, tmp_path, tmp_path_factory):
    original = selected_host_context
    configuration = dict(original.config, vault_path=str(tmp_path))
    original.config_path.write_text(json.dumps(configuration))
    private_root = tmp_path_factory.mktemp('selected-private-state')
    selected = resolve_runtime_context(original.host, original.client,
        {'session_id': original.native_session_id, 'cwd': str(original.worktree)},
        {'config_path': original.config_path, 'vault_path': tmp_path,
         'resource_root': original.resource_root, 'index_path': private_root / 'index.db',
         'state_path': private_root / 'state'})
    with using_runtime_context(selected):
        yield selected


@pytest.fixture
def native_ai_frontend(selected_host_context, monkeypatch):
    import ai_backend
    import native_ai_test_adapter
    monkeypatch.setattr(ai_backend, 'execute_ai', native_ai_test_adapter.execute_ai)
    return selected_host_context
