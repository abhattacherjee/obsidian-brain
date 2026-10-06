"""The named Claude compatibility reader keeps its historical config path."""
import json

import pytest
from runtime_context import using_runtime_context
from vault_doctor_checks.wiki_pages import _read_config


@pytest.mark.host_only('claude', reason='claude-record-format', capability='claude_native_format')
@pytest.mark.parametrize('value', ['', '../outside', 'tmp-only-wiki'])
def test_legacy_reader_preserves_config_values_at_call_time(tmp_path, monkeypatch, value):
    monkeypatch.setenv('HOME', str(tmp_path))
    path = tmp_path / '.claude' / 'obsidian-brain-config.json'
    path.parent.mkdir()
    with using_runtime_context(None):
        path.write_text(json.dumps({'wiki_folder': 'before'}))
        assert _read_config()['wiki_folder'] == 'before'
        path.write_text(json.dumps({'wiki_folder': value}))
        assert _read_config()['wiki_folder'] == value


@pytest.mark.host_only('claude', reason='claude-record-format', capability='claude_native_format')
def test_legacy_reader_rejects_corrupt_config(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    path = tmp_path / '.claude' / 'obsidian-brain-config.json'
    path.parent.mkdir()
    path.write_text('{not json')
    with using_runtime_context(None), pytest.raises(ValueError, match='cannot read'):
        _read_config()
