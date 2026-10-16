"""Selected native memory discovery never borrows the other host's files."""
import memory_sources


def test_native_discovery_uses_selected_home_or_reports_unsupported(selected_host_context, monkeypatch, tmp_path):
    context = selected_host_context
    memory = context.native_home / 'projects' / 'synthetic-project' / 'memory'
    memory.mkdir(parents=True)
    note = memory / 'synthetic.md'
    note.write_text('Synthetic memory fact.\n')
    (memory / 'MEMORY.md').write_text('Synthetic index.\n')
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'unselected-home'))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'unselected-home'))
    monkeypatch.setattr(memory_sources, '_legacy_projects_root',
                        lambda: (_ for _ in ()).throw(AssertionError('Foreign legacy discovery')))
    errors = []
    found = memory_sources.memory_sources(memory_sources.detect_host(), errors=errors)
    if context.host == 'claude':
        assert found == [note.resolve()]
        assert errors == []
        assert memory_sources.memory_name(found[0]) == 'synthetic-project/synthetic.md'
        assert memory_sources.failed_scopes(errors) == (False, set(), set())
    else:
        assert found == []
        assert len(errors) == 1
        assert errors[0]['unsupported'] is True
        assert errors[0]['host'] == context.host
        assert memory_sources.failed_scopes(errors) == (True, set(), set())


def test_unsupported_scope_does_not_query_any_memory_root(selected_host_context, monkeypatch):
    monkeypatch.setattr(memory_sources, '_projects_root',
                        lambda: (_ for _ in ()).throw(AssertionError('Unsupported discovery queried a root')))
    assert memory_sources.failed_scopes([{'unsupported': True, 'path': ''}]) == (True, set(), set())
