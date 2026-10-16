"""Historical origin selection never changes the invoking runtime or AI host."""
from types import MappingProxyType

import pytest

import runtime_context as runtime


@pytest.mark.parametrize('source_host,environment,subdirs', [
    ('claude', 'CLAUDE_CONFIG_DIR', ('projects',)),
    ('codex', 'CODEX_HOME', ('sessions', 'archived_sessions')),
])
def test_selected_native_home_only(tmp_path, monkeypatch, source_host, environment, subdirs):
    selected = tmp_path / 'selected'; selected.mkdir()
    other = tmp_path / 'other'; other.mkdir()
    monkeypatch.setenv(environment, str(selected))
    monkeypatch.setenv('CODEX_HOME' if source_host == 'claude' else 'CLAUDE_CONFIG_DIR', str(other))
    assert runtime.historical_source_roots(source_host) == tuple(selected / name for name in subdirs)
    assert not list(selected.iterdir())


def test_explicit_source_overrides_selected_home_without_changing_context(tmp_path, monkeypatch):
    source = tmp_path / 'archive'; source.mkdir()
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'unavailable'))
    context = runtime.RuntimeContext('codex', 'codex-cli', 'invoking-id', tmp_path, tmp_path,
                                    None, tmp_path / 'vault', tmp_path / 'config', MappingProxyType({}),
                                    tmp_path, tmp_path / 'index', tmp_path / 'state')
    with runtime.using_runtime_context(context):
        assert runtime.historical_source_roots('claude', source) == (source,)
        assert runtime.current_runtime_context() is context
        assert context.host == 'codex' and context.native_session_id == 'invoking-id'


@pytest.mark.parametrize('source_host,subdirs', [('claude', ('projects',)), ('codex', ('sessions', 'archived_sessions'))])
def test_default_home_does_not_use_other_assistant(tmp_path, monkeypatch, source_host, subdirs):
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    monkeypatch.delenv('CODEX_HOME', raising=False)
    monkeypatch.setattr(runtime.Path, 'home', classmethod(lambda cls: tmp_path))
    host_home = tmp_path / ('.claude' if source_host == 'claude' else '.codex')
    assert runtime.historical_source_roots(source_host) == tuple(host_home / name for name in subdirs)
    assert not host_home.exists()


@pytest.mark.parametrize('host', [None, '', 'desktop', 'unknown'])
def test_source_host_is_always_explicit(tmp_path, host):
    with pytest.raises(runtime.RuntimeContextError, match='source host'):
        runtime.historical_source_roots(host, tmp_path)


@pytest.mark.parametrize('kind', ['relative', 'missing', 'file', 'symlink', 'parent-symlink', 'empty'])
def test_invalid_explicit_roots_refuse(tmp_path, kind):
    source = tmp_path / 'source'; source.mkdir()
    file = tmp_path / 'file'; file.write_text('not a directory')
    link = tmp_path / 'link'; link.symlink_to(source, target_is_directory=True)
    (source / 'child').mkdir()
    values = {'relative': 'relative', 'missing': tmp_path / 'missing', 'file': file,
              'symlink': link, 'parent-symlink': link / 'child', 'empty': ''}
    with pytest.raises(runtime.RuntimeContextError):
        runtime.historical_source_roots('claude', values[kind])


def test_symlinked_default_storage_cannot_cross_hosts(tmp_path, monkeypatch):
    native = tmp_path / 'native'; native.mkdir()
    other = tmp_path / 'other'; other.mkdir()
    (native / 'sessions').symlink_to(other, target_is_directory=True)
    monkeypatch.setenv('CODEX_HOME', str(native))
    with pytest.raises(runtime.RuntimeContextError, match='symbolic'):
        runtime.historical_source_roots('codex')


def _context(tmp_path, host, native_home=None):
    return runtime.RuntimeContext(host, 'claude-code' if host == 'claude' else 'codex-cli',
                                  'native-id', tmp_path, tmp_path, None, tmp_path / 'vault',
                                  tmp_path / 'custom-config' / 'config.json', MappingProxyType({}),
                                  tmp_path, tmp_path / 'index', tmp_path / 'state',
                                  native_home=native_home)


def test_memory_root_uses_recorded_home_not_custom_config_or_changed_environment(tmp_path, monkeypatch):
    selected = tmp_path / 'selected'; selected.mkdir()
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'later-environment'))
    context = _context(tmp_path, 'claude', selected)
    with runtime.using_runtime_context(context):
        assert runtime.native_memory_projects_root() == selected / 'projects'
    assert not (selected / 'projects').exists()


def test_memory_root_codex_is_unsupported_without_reading_claude_home(tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', 'invalid-relative-path')
    assert runtime.native_memory_projects_root(_context(tmp_path, 'codex')) is None
    assert runtime.native_memory_projects_root() is None


def test_manual_claude_context_uses_explicit_selected_home_not_config_parent(tmp_path, monkeypatch):
    selected = tmp_path / 'selected'; selected.mkdir()
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(selected))
    assert runtime.native_memory_projects_root(_context(tmp_path, 'claude')) == selected / 'projects'
