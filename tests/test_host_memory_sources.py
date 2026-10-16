"""Native memory discovery preserves the selected host boundary."""
from pathlib import Path
import pytest


def test_native_memory_discovery_respects_bound_host(selected_host_context, tmp_path, monkeypatch):
    from runtime_context import using_runtime_context
    import memory_sources
    import os
    context = selected_host_context
    host = context.host
    native = Path(os.environ['CLAUDE_CONFIG_DIR'])
    memory = native / 'projects' / 'project' / 'memory'
    memory.mkdir(parents=True)
    (memory / 'decision.md').write_text('A native decision')
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(native))
    errors = []
    with using_runtime_context(context):
        paths = memory_sources.memory_sources('claude-code', errors=errors)
    if host == 'claude':
        assert paths == [(memory / 'decision.md').resolve()]
        assert not errors
    else:
        assert not paths
        assert errors and errors[0]['unsupported'] is True
        assert errors[0]['host'] == 'codex'


def test_unsupported_memory_scope_never_opens_another_host_root(selected_host_context, monkeypatch):
    import memory_sources
    def forbidden_root():
        pytest.fail('Unsupported memory must not inspect another host root')
    monkeypatch.setattr(memory_sources, '_projects_root', forbidden_root)
    errors = []
    from runtime_context import using_runtime_context
    with using_runtime_context(None):
        assert memory_sources.memory_sources('codex', errors=errors) == []
    assert memory_sources.failed_scopes(errors) == (True, set(), set())
    assert errors == [{'path': '', 'error': 'Native memory discovery is unsupported for codex', 'host': 'codex', 'unsupported': True}]


def test_native_memory_uses_recorded_home_after_environment_changes(selected_host_context, tmp_path, monkeypatch):
    import memory_sources
    context = selected_host_context
    original = context.native_home / 'projects' / 'original' / 'memory'
    original.mkdir(parents=True)
    (original / 'decision.md').write_text('Recorded native home')
    foreign = tmp_path / 'later-claude-home'
    replacement = foreign / 'projects' / 'replacement' / 'memory'
    replacement.mkdir(parents=True)
    (replacement / 'decision.md').write_text('Later environment must not select this file')
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(foreign))
    errors = []
    paths = memory_sources.memory_sources(context.host, errors=errors)
    if context.host == 'claude':
        assert paths == [(original / 'decision.md').resolve()]
        assert not errors
    else:
        assert paths == []
        assert errors[0]['unsupported'] is True
