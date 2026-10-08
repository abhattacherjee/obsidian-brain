"""All installs use synthetic home/cache/config; no installed client runs."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts/dev-test' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load('codex_install')
native_inventory = installer.native_hooks_inventory
version = load('version_sync')


@pytest.fixture(autouse=True)
def metadata_only_transport(monkeypatch):
    def inventory(source, home):
        caches = [path for path in (home / 'plugins/cache').glob('*/obsidian-brain/*')
                  if path.is_dir() and ((path / '.claude-plugin/plugin.json').is_file()
                       or (path / '.codex-plugin/plugin.json').is_file())]
        return {'data': [{'errors': [], 'hooks': [
            {'pluginId': 'obsidian-brain@' + cache.parent.parent.name,
             'sourcePath': str(installer._selected_hooks(cache)[0])} for cache in caches]}]}
    monkeypatch.setattr(installer, 'native_hooks_inventory', inventory)


@pytest.fixture
def package(tmp_path):
    source = tmp_path / 'source with spaces'
    files = {'scripts/vault_doctor.py': '# synthetic doctor',
             'scripts/doctor_repair_state.py': '# synthetic repair state',
             'scripts/test-dev-skill.sh': '# synthetic dev command',
             'scripts/vault_doctor_checks/__init__.py': '# synthetic check package',
             'hooks/obsidian_utils.py': '# synthetic source', 'hooks/brain_cli.py': '# launcher',
             'hooks/transcripts/codex.py': '# recursive parser', 'hooks/ai_adapters/codex.py': '# recursive backend',
             'skills/recall/SKILL.md': '# synthetic skill', 'skills/recall/references/host-codex.md': '# native reference',
             '.codex-plugin/plugin.json': json.dumps({'name': 'obsidian-brain', 'version': '3.8.0', 'hooks': './.codex/hooks.json'}),
             '.claude-plugin/plugin.json': json.dumps({'name': 'obsidian-brain', 'version': '3.8.0'}),
             '.claude-plugin/marketplace.json': json.dumps({'plugins': [{'name': 'obsidian-brain', 'version': '3.8.0'}]}),
             '.codex/hooks.json': '{"hooks":{}}', 'hooks/__pycache__/skip.pyc': 'not runtime',
             'hooks/state/private.json': 'not runtime', 'tests/skip.py': 'not runtime', '.git/private': 'not runtime'}
    for name, content in files.items():
        path = source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content)
    home = tmp_path / 'native home'
    cache = home / 'plugins/cache/synthetic-market/obsidian-brain/3.8.0'
    cache.mkdir(parents=True)
    (cache / 'released.txt').write_text('original exact bytes')
    (cache / '.claude-plugin').mkdir()
    (cache / '.claude-plugin/plugin.json').write_text('{"name":"obsidian-brain"}')
    (cache / 'hooks').mkdir()
    (cache / 'hooks/hooks.json').write_text('{"hooks":{}}')
    (cache / 'hooks/obsidian_utils.py').write_text('# released runtime sentinel')
    (cache / 'skills/recall').mkdir(parents=True)
    (cache / 'skills/recall/SKILL.md').write_text('# released recall skill')
    config = home / 'config.toml'
    config.write_text('model = "synthetic-model"\n[plugins."obsidian-brain@synthetic-market"]\nenabled = false\ncustom_setting = "keep"\n[unrelated]\nvalue = 7\n')
    return source, home, cache, config


def test_codex_install_recursive_runtime_and_restore_plugin_entry(package):
    source, home, cache, config = package
    (source / 'hooks/.coverage.synthetic').write_text('private coverage data')
    original = config.read_bytes()
    assert installer.run('install', source, home, cache_path=cache) == 0
    assert (cache / 'hooks/transcripts/codex.py').is_file()
    assert (cache / 'hooks/ai_adapters/codex.py').is_file()
    assert (cache / 'skills/recall/references/host-codex.md').is_file()
    assert not (cache / 'hooks/__pycache__').exists()
    assert not (cache / 'hooks/state').exists()
    assert not (cache / 'hooks/.coverage.synthetic').exists()
    assert not (cache / 'tests').exists()
    assert not (cache / '.git').exists()
    parsed = installer._toml(config.read_bytes())
    assert parsed['plugins']['obsidian-brain@synthetic-market'] == {'enabled': True, 'custom_setting': 'keep'}
    assert installer.run('restore', source, home, cache_path=cache) == 0
    assert config.read_bytes() == original
    assert (cache / 'released.txt').read_text() == 'original exact bytes'


@pytest.mark.parametrize('point', ['after_snapshot', 'after_cache_swap', 'after_config_swap'])
def test_codex_install_failure_rolls_back_both_resources(package, point):
    source, home, cache, config = package
    cache.chmod(0o555)
    original = config.read_bytes()
    original_modes = {str(p.relative_to(cache)): p.stat().st_mode & 0o7777 for p in [cache, *cache.rglob('*')]}
    def fail(at):
        if at == point:
            raise OSError('synthetic injected failure')
    with pytest.raises(OSError):
        installer.run('install', source, home, fault=fail, cache_path=cache)
    assert config.read_bytes() == original
    assert (cache / 'released.txt').read_text() == 'original exact bytes'
    assert not installer._recovery_paths(home, cache)[0].exists()
    assert {str(p.relative_to(cache)): p.stat().st_mode & 0o7777 for p in [cache, *cache.rglob('*')]} == original_modes


def test_restore_preserves_unrelated_config_edits(package):
    source, home, cache, config = package
    installer.run('install', source, home, cache_path=cache)
    config.write_bytes(config.read_bytes().replace(b'value = 7', b'value = 9'))
    installer.run('restore', source, home, cache_path=cache)
    parsed = installer._toml(config.read_bytes())
    assert parsed['unrelated']['value'] == 9
    assert parsed['plugins']['obsidian-brain@synthetic-market']['enabled'] is False


def test_malformed_toml_refuses_without_cache_change(package):
    source, home, cache, config = package
    config.write_text('model = [ invalid TOML')
    with pytest.raises(ValueError):
        installer.run('install', source, home, cache_path=cache)
    assert (cache / 'released.txt').read_text() == 'original exact bytes'
    assert not cache.with_name(cache.name + '.bak').exists()


@pytest.mark.parametrize('after_publication', [False, True])
def test_native_metadata_unavailable_refuses_or_rolls_back(package, monkeypatch, after_publication):
    source, home, cache, config = package
    original = config.read_bytes()
    original_inventory = installer.native_hooks_inventory
    calls = []
    def unavailable(source, home):
        calls.append(1)
        if after_publication and len(calls) == 1:
            return original_inventory(source, home)
        raise ValueError('Native Codex metadata request failed')
    monkeypatch.setattr(installer, 'native_hooks_inventory', unavailable)
    with pytest.raises(ValueError, match='metadata request failed'):
        installer.run('install', source, home, cache_path=cache)
    assert config.read_bytes() == original
    assert (cache / 'released.txt').read_text() == 'original exact bytes'
    assert not cache.with_name(cache.name + '.bak').exists()


def test_restore_does_not_require_native_metadata(package, monkeypatch):
    source, home, cache, config = package
    original = config.read_bytes()
    installer.run('install', source, home, cache_path=cache)
    monkeypatch.setattr(installer, 'native_hooks_inventory', lambda *args: pytest.fail('restore queried native client'))
    assert installer.run('restore', source, home, cache_path=cache) == 0
    assert config.read_bytes() == original


def test_post_publication_rollback_restores_plugin_entry_and_keeps_unrelated_change(package):
    source, home, cache, config = package
    def fail(point):
        if point == 'after_config_swap':
            config.write_bytes(config.read_bytes().replace(b'value = 7', b'value = 9'))
            raise OSError('synthetic failure after unrelated concurrent edit')
    with pytest.raises(OSError):
        installer.run('install', source, home, fault=fail, cache_path=cache)
    parsed = installer._toml(config.read_bytes())
    assert parsed['plugins']['obsidian-brain@synthetic-market']['enabled'] is False
    assert parsed['unrelated']['value'] == 9
    assert (cache / 'released.txt').read_text() == 'original exact bytes'


def test_native_inventory_only_initializes_and_reads_hooks(package, monkeypatch):
    from ai_adapters import codex
    source, home, _, _ = package
    calls = []
    class Rpc:
        def __init__(self, binary, arguments, context, environment, deadline):
            assert binary == '/synthetic/codex' and arguments == []
            assert context.worktree == source and environment['CODEX_HOME'] == str(home)
            calls.append('start')
        def initialize(self):
            calls.append('initialize')
        def request(self, method, params):
            assert method == 'hooks/list' and params == {'cwds': [str(source.resolve())]}
            calls.append(method)
            return {'data': [{'errors': [], 'hooks': []}]}
        def close(self):
            calls.append('close')
    monkeypatch.setattr(codex, 'NativeRpc', Rpc)
    monkeypatch.setattr(installer.shutil, 'which', lambda name: '/synthetic/codex')
    assert native_inventory(source, home) == {'data': [{'errors': [], 'hooks': []}]}
    assert calls == ['start', 'initialize', 'hooks/list', 'close']


def test_native_inventory_closes_transport_on_discovery_error(package, monkeypatch):
    from ai_adapters import codex
    source, home, _, _ = package
    closed = []
    class Rpc:
        def __init__(self, *args):
            pass
        def initialize(self):
            pass
        def request(self, *args):
            return {'data': [{'errors': ['synthetic discovery error']}]}
        def close(self):
            closed.append(True)
    monkeypatch.setattr(codex, 'NativeRpc', Rpc)
    monkeypatch.setattr(installer.shutil, 'which', lambda name: '/synthetic/codex')
    with pytest.raises(ValueError, match='discovery failed'):
        native_inventory(source, home)
    assert closed == [True]


def test_stdlib_toml_fallback_installs_and_restores(package, monkeypatch):
    source, home, cache, config = package
    original=config.read_bytes()
    monkeypatch.setattr(installer, 'tomllib', None)
    assert installer.run('status',source,home,cache_path=cache)==0
    assert installer.run('install',source,home,cache_path=cache)==0
    assert (cache/'hooks/obsidian_utils.py').read_text()=='# synthetic source'
    assert installer.run('restore',source,home,cache_path=cache)==0
    assert config.read_bytes()==original and (cache/'released.txt').read_text()=='original exact bytes'


def test_backup_only_interrupted_swap_can_restore(package):
    source, home, cache, config = package
    original = config.read_bytes()
    installer.run('install', source, home, cache_path=cache)
    import shutil
    shutil.rmtree(cache)
    assert installer.run('restore', source, home, cache_path=cache) == 0
    assert (cache / 'released.txt').is_file()
    assert config.read_bytes() == original


def test_version_bump_synchronizes_both_hosts_and_catalog(package):
    source, _, _, _ = package
    assert version.run(source, 'minor') == '3.9.0'
    for path in ('.claude-plugin/plugin.json', '.codex-plugin/plugin.json'):
        assert json.loads((source / path).read_text())['version'] == '3.9.0'
    assert json.loads((source / '.claude-plugin/marketplace.json').read_text())['plugins'][0]['version'] == '3.9.0'


def test_version_mid_publication_failure_restores_exact_bytes(package):
    source, _, _, _ = package
    paths = [source / name for name in ('.claude-plugin/plugin.json', '.codex-plugin/plugin.json', '.claude-plugin/marketplace.json')]
    originals = {path: path.read_bytes() for path in paths}
    count = 0
    def replace(temporary, path):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError('synthetic publication failure')
        os.replace(temporary, path)
    with pytest.raises(OSError):
        version.run(source, 'patch', replace=replace)
    assert {path: path.read_bytes() for path in paths} == originals


@pytest.mark.parametrize('current', ['x[$(synthetic)].0.0', '1.2.3-zz', '1.2.3-4', '01.08.09'])
def test_version_arithmetic_never_evaluates_strings(current):
    if current.startswith('x'):
        with pytest.raises(ValueError):
            version.bump(current, 'patch')
    else:
        assert version.bump(current, 'patch') in ('1.2.4', '1.8.10')


def test_interrupted_version_publication_recovers_before_next_bump(package):
    source, _, _, _ = package
    paths = [source / name for name in ('.claude-plugin/plugin.json', '.codex-plugin/plugin.json', '.claude-plugin/marketplace.json')]
    old = {path: path.read_bytes() for path in paths}
    records = []
    for path in paths:
        updated = old[path].replace(b'3.8.0', b'3.8.1')
        records.append({'path': str(path.relative_to(source)), 'old': old[path].hex(), 'new': updated.hex(), 'mode': 0o644})
    paths[0].write_bytes(bytes.fromhex(records[0]['new']))
    journal = source / '.version-sync.pending.json'
    journal.write_text(json.dumps(records)); journal.chmod(0o600)
    with pytest.raises(ValueError, match='Recovered interrupted'):
        version.run(source, 'patch')
    assert {path: path.read_bytes() for path in paths} == old
    assert not journal.exists()


def test_codex_multiple_versions_refuse_guessing_then_accept_explicit_installed_path(package):
    source, home, cache, config = package
    poison = cache.with_name('999.999.999'); poison.mkdir()
    (poison / 'poison.txt').write_text('not selected')
    with pytest.raises(ValueError, match='explicit native installed'):
        installer.run('install', source, home)
    assert (cache / 'released.txt').is_file()
    assert installer.run('install', source, home, cache_path=cache) == 0
    assert (poison / 'poison.txt').read_text() == 'not selected'


def test_explicit_codex_cache_path_cannot_escape_native_home(package):
    source, home, cache, config = package
    with pytest.raises(ValueError):
        installer.run('install', source, home, cache_path=source)
    assert (cache / 'released.txt').is_file()


def test_native_hook_inventory_requires_one_exact_selected_cache(package):
    _, home, cache, _ = package
    descriptor = cache / '.codex-plugin/plugin.json'; descriptor.parent.mkdir()
    descriptor.write_text(json.dumps({'name': 'obsidian-brain', 'version': '3.8.0', 'hooks': './.codex/hooks.json'}))
    hooks = cache / '.codex/hooks.json'; hooks.parent.mkdir(); hooks.write_text('{"hooks":{}}')
    metadata = {'data': [{'handlers': [{'pluginId': 'obsidian-brain@synthetic-market', 'sourcePath': str(hooks)}]}]}
    assert installer.validate_native_cache(home, 'obsidian-brain@synthetic-market', metadata, cache) == cache
    other = cache.with_name('3.9.0') / 'hooks/hooks.json'; other.parent.mkdir(parents=True); other.write_text('{}')
    metadata['data'][0]['handlers'].append({'pluginId': 'obsidian-brain@synthetic-market', 'sourcePath': str(other)})
    with pytest.raises(ValueError, match='disagree'):
        installer.validate_native_cache(home, 'obsidian-brain@synthetic-market', metadata, cache)


def test_native_hook_inventory_without_matching_plugin_refuses(package):
    _, home, cache, _ = package
    with pytest.raises(ValueError, match='No native hooks'):
        installer.validate_native_cache(home, 'obsidian-brain@synthetic-market', {'data': []}, cache)


@pytest.mark.parametrize('hooks', ['/absolute/hooks.json', '../outside.json', '', {}, './missing.json'])
def test_invalid_descriptor_hook_selection_cannot_replace_release(package, hooks):
    source, home, cache, config = package
    descriptor = source / '.codex-plugin/plugin.json'
    descriptor.write_text(json.dumps({'name': 'obsidian-brain', 'version': '3.8.0', 'hooks': hooks}))
    original = config.read_bytes()
    with pytest.raises((ValueError, FileNotFoundError)):
        installer.run('install', source, home, cache_path=cache)
    assert config.read_bytes() == original
    assert (cache / 'released.txt').read_text() == 'original exact bytes'
    assert not cache.with_name(cache.name + '.bak').exists()


def test_descriptor_version_mismatch_refuses_before_swapping_cache(package):
    source, home, cache, config = package
    descriptor = source / '.codex-plugin/plugin.json'
    descriptor.write_bytes(descriptor.read_bytes().replace(b'3.8.0', b'3.9.0'))
    original = config.read_bytes()
    with pytest.raises(ValueError, match='descriptors disagree'):
        installer.run('install', source, home, cache_path=cache)
    assert config.read_bytes() == original
    assert (cache / 'released.txt').is_file()


def test_native_selected_path_must_match_descriptor_not_only_cache_root(package):
    source, home, cache, _ = package
    installer.run('install', source, home, cache_path=cache)
    wrong = cache / 'hooks/hooks.json'; wrong.write_text('{"hooks":{}}')
    metadata = {'data': [{'handlers': [{'pluginId': 'obsidian-brain@synthetic-market', 'sourcePath': str(wrong)}]}]}
    with pytest.raises(ValueError, match='descriptor selection'):
        installer.validate_native_cache(home, 'obsidian-brain@synthetic-market', metadata, cache)
    correct = cache / '.codex/hooks.json'
    metadata['data'][0]['handlers'][0]['sourcePath'] = str(correct)
    assert installer.validate_native_cache(home, 'obsidian-brain@synthetic-market', metadata, cache) == cache


def test_observed_legacy_compatibility_hook_path_requires_native_evidence(package):
    _, home, cache, _ = package
    descriptor = cache / '.claude-plugin/plugin.json'; descriptor.parent.mkdir(exist_ok=True)
    descriptor.write_text(json.dumps({'name': 'obsidian-brain', 'version': '3.8.0'}))
    hooks = cache / 'hooks/hooks.json'; hooks.parent.mkdir(exist_ok=True); hooks.write_text('{"hooks":{}}')
    metadata = {'data': [{'handlers': [{'pluginId': 'obsidian-brain@synthetic-market', 'sourcePath': str(hooks)}]}]}
    assert installer.validate_native_cache(home, 'obsidian-brain@synthetic-market', metadata, cache) == cache
    with pytest.raises(ValueError, match='No native hooks'):
        installer.validate_native_cache(home, 'obsidian-brain@synthetic-market', {'data': []}, cache)


def test_recursive_snapshot_retains_current_runtime_packages(package):
    source, home, cache, _ = package
    additions = ['hooks/runtime_adapters/codex.py', 'hooks/runtime_adapters/claude.py',
                 'hooks/transcripts/claude.py', 'hooks/ai_adapters/__init__.py',
                 'hooks/operation_state.py', 'hooks/capture.py',
                 'scripts/dev-test/package_tree.py', 'scripts/vault_doctor_checks/wiki_pages.py']
    for name in additions:
        path = source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('# synthetic runtime')
    assert installer.run('install', source, home, cache_path=cache) == 0
    assert all((cache / name).read_text() == '# synthetic runtime' for name in additions)


@pytest.mark.parametrize('missing', ['scripts/doctor_repair_state.py', 'scripts/vault_doctor.py',
                                     'scripts/vault_doctor_checks/__init__.py'])
def test_native_install_refuses_missing_doctor_dependency_before_cache_swap(package, missing):
    source, home, cache, config = package
    (source / missing).unlink()
    before_config = config.read_bytes()
    before_cache = {str(p.relative_to(cache)): p.read_bytes() for p in cache.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='Runtime package is incomplete'):
        installer.run('install', source, home, cache_path=cache)
    assert config.read_bytes() == before_config
    assert {str(p.relative_to(cache)): p.read_bytes() for p in cache.rglob('*') if p.is_file()} == before_cache


def test_version_bump_keeps_embedded_architecture_and_framework_in_sync(package):
    import re
    source, _, _, _ = package
    architecture = source / 'docs/architecture/architecture.json'
    architecture.parent.mkdir(parents=True)
    value = {'version': '3.8.0', 'lastUpdated': '2000-01-01', 'name': 'UTF8 → α',
             'techStack': {'framework': {'version': '3.8.0'}}}
    architecture.write_text(json.dumps(value, ensure_ascii=False))
    html = architecture.with_suffix('.html')
    html.write_text('before<script id="arch-data" type="application/json">' + json.dumps(value, ensure_ascii=False) + '</script>after')
    assert version.run(source, 'patch') == '3.8.1'
    updated = json.loads(architecture.read_text())
    embedded = json.loads(re.search(r'<script id="arch-data" type="application/json">(.*?)</script>', html.read_text()).group(1))
    assert updated['techStack']['framework']['version'] == updated['version'] == '3.8.1'
    assert updated['lastUpdated'] == version.datetime.date.today().isoformat()
    assert embedded == updated
    assert html.read_text().startswith('before<script') and html.read_text().endswith('</script>after')


def test_architecture_html_mismatch_refuses_before_any_version_publication(package):
    source, _, _, _ = package
    architecture = source / 'docs/architecture/architecture.json'
    architecture.parent.mkdir(parents=True)
    architecture.write_text('{"version":"3.8.0"}')
    architecture.with_suffix('.html').write_text('<script id="arch-data" type="application/json">{"version":"wrong"}</script>')
    originals = {path: path.read_bytes() for path in source.rglob('*') if path.is_file()}
    with pytest.raises(ValueError, match='differs from its source'):
        version.run(source, 'patch')
    assert all(path.read_bytes() == value for path, value in originals.items())
    assert not (source / '.version-sync.pending.json').exists()


def test_architecture_html_publication_failure_rolls_back_all_metadata(package):
    source, _, _, _ = package
    architecture = source / 'docs/architecture/architecture.json'
    architecture.parent.mkdir(parents=True)
    value = {'version': '3.8.0', 'techStack': {'framework': {'version': '3.8.0'}}}
    architecture.write_text(json.dumps(value))
    html = architecture.with_suffix('.html')
    html.write_text('<script id="arch-data" type="application/json">' + json.dumps(value) + '</script>')
    originals = {path: path.read_bytes() for path in source.rglob('*') if path.is_file()}
    def fail_html(source_path, destination):
        if destination == html:
            raise OSError('synthetic HTML publication failure')
        version.os.replace(source_path, destination)
    with pytest.raises(OSError, match='synthetic HTML'):
        version.run(source, 'patch', replace=fail_html)
    assert all(path.read_bytes() == value for path, value in originals.items())
    assert not (source / '.version-sync.pending.json').exists()


def test_architecture_test_file_count_matches_repository_modules():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    architecture = json.loads((root / 'docs/architecture/architecture.json').read_text())
    assert architecture['testing']['unitSuite']['fileCount'] == len(list((root / 'tests').glob('test_*.py')))


def test_repository_architecture_html_embeds_current_json():
    import re
    root = Path(__file__).resolve().parents[1]
    architecture = root / 'docs/architecture/architecture.json'
    html = architecture.with_suffix('.html').read_text(encoding='utf-8')
    embedded = re.search(r'<script[^>]*id=["\']arch-data["\'][^>]*>(.*?)</script>', html, re.S)
    assert embedded is not None
    assert json.loads(embedded.group(1)) == json.loads(architecture.read_text(encoding='utf-8'))


@pytest.mark.parametrize('content',[
    b'a=1\na=2\n',b'a=1\n[a]\nx=2\n',b'[a]\n[a]\n',
    b'a={x=1}\na.y=2\n',b'a.b=1\n[a]\nx=2\n',
    b'a=1979-05-27T07:32:00Z\n',b'[[plugins]]\nx=1\n',
    b'a="""multiline"""\n',b'a=[1,\n2]\n',
    b'a="invalid\\/escape"\n', b'a={x={y=1},x.z=2}\n',
    b'a="\x7f"\n',
    b'a="\\ud800"\n', b'a=' + b'[' * 65 + b'0' + b']' * 65 + b'\n',
])
def test_stdlib_toml_fallback_rejects_ambiguous_or_unsupported_before_write(package,monkeypatch,content):
    source,home,cache,config=package;config.write_bytes(content)
    monkeypatch.setattr(installer,'tomllib',None)
    with pytest.raises(ValueError):installer.run('install',source,home,cache_path=cache)
    assert config.read_bytes()==content and (cache/'released.txt').read_text()=='original exact bytes'
    assert not cache.with_name(cache.name+'.bak').exists()


def test_stdlib_toml_fallback_parses_ordinary_native_config(monkeypatch):
    monkeypatch.setattr(installer,'tomllib',None)
    content=b'model="synthetic#model" # comment\n[projects."/tmp/a.b"]\ntrust_level="trusted"\n[mcp_servers.demo]\ncommand="node"\nargs=["a#b", "c", 3, true]\nenv={TOKEN="private#synthetic", COUNT=2}\n[features]\nflags.enabled=true\n'
    assert installer._toml(content)=={'model':'synthetic#model','projects':{'/tmp/a.b':{'trust_level':'trusted'}},
        'mcp_servers':{'demo':{'command':'node','args':['a#b','c',3,True],
            'env':{'TOKEN':'private#synthetic','COUNT':2}}},'features':{'flags':{'enabled':True}}}


def test_public_installer_without_site_packages_preserves_config_and_backup(package):
    import subprocess
    import sys

    source, home, cache, config = package
    original = config.read_bytes()
    installer_path = source / 'scripts/dev-test/codex_install.py'
    installer_path.parent.mkdir(parents=True)
    installer_path.write_bytes((ROOT / 'scripts/dev-test/codex_install.py').read_bytes())
    installer_path.with_name('package_tree.py').write_bytes((ROOT / 'scripts/dev-test/package_tree.py').read_bytes())
    driver = '''
import importlib.util, json, pathlib, sys
spec = importlib.util.spec_from_file_location('installer', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
if sys.version_info[:2] == (3, 9):
    assert importlib.util.find_spec('tomli') is None
module.tomllib = None
selected_cache = pathlib.Path(sys.argv[4])
def inventory(source, home):
    return {'data': [{'errors': [], 'hooks': [{
        'pluginId': 'obsidian-brain@synthetic-market',
        'sourcePath': str(module._selected_hooks(selected_cache)[0])}]}]}
module.native_hooks_inventory = inventory
sys.argv = [sys.argv[1], sys.argv[2], '--source', sys.argv[3], '--cache-path', sys.argv[4]]
raise SystemExit(module.main())
'''
    environment = dict(os.environ, CODEX_HOME=str(home), HOME=str(home))
    installed = False
    for mode in ('status', 'install', 'status', 'restore', 'status'):
        public_script = cache / 'scripts/dev-test/codex_install.py' if installed else installer_path
        result = subprocess.run(
            [sys.executable, '-S', '-c', driver,
             str(public_script), mode, str(source), str(cache)],
            env=environment, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        if mode == 'install':
            installed = True
            assert installer._recovery_paths(home, cache)[0].is_dir()
            assert installer._toml(config.read_bytes())['plugins']['obsidian-brain@synthetic-market']['enabled'] is True
        elif mode == 'restore':
            installed = False
    assert config.read_bytes() == original
    assert (cache / 'released.txt').read_text() == 'original exact bytes'
    assert not cache.with_name(cache.name + '.bak').exists()


@pytest.mark.parametrize('failure', [None, 'after_snapshot', 'after_cache_swap', 'after_config_swap'])
def test_native_discovery_never_sees_backup_or_staged_runtime(package, monkeypatch, failure):
    source, home, cache, config = package
    original = config.read_bytes()
    original_bytes = {str(p.relative_to(cache)): p.read_bytes() for p in cache.rglob('*') if p.is_file()}
    original_modes = {str(p.relative_to(cache)): p.stat().st_mode & 0o7777 for p in [cache, *cache.rglob('*')]}
    def inventory(source, home):
        # Native discovery scans every descriptor, including .bak/dot directories.
        roots = [p.parent.parent for p in (home / 'plugins/cache').rglob('plugin.json')
                 if p.parent.name in {'.claude-plugin', '.codex-plugin'}]
        roots = set(roots)
        assert roots == {cache}, 'Backup/staging must not enter native discovery'
        return {'data': [{'errors': [], 'hooks': [{'pluginId': 'obsidian-brain@synthetic-market',
            'sourcePath': str(installer._selected_hooks(p)[0])} for p in roots]}]}
    monkeypatch.setattr(installer, 'native_hooks_inventory', inventory)
    def inspect(point):
        inventory(source, home)
        if point == failure:
            raise OSError('synthetic rollback control')
    if failure is None:
        assert installer.run('install', source, home, fault=inspect, cache_path=cache) == 0
        inventory(source, home)
        assert installer.run('restore', source, home, cache_path=cache) == 0
    else:
        with pytest.raises(OSError, match='synthetic rollback'):
            installer.run('install', source, home, fault=inspect, cache_path=cache)
    inventory(source, home)
    assert config.read_bytes() == original
    assert {str(p.relative_to(cache)): p.read_bytes() for p in cache.rglob('*') if p.is_file()} == original_bytes
    assert {str(p.relative_to(cache)): p.stat().st_mode & 0o7777 for p in [cache, *cache.rglob('*')]} == original_modes


def test_restore_accepts_existing_legacy_backup(package):
    source, home, cache, config = package
    original = config.read_bytes()
    installer.run('install', source, home, cache_path=cache)
    backup, record = installer._recovery_paths(home, cache)
    legacy = cache.with_name(cache.name + '.bak')
    backup.rename(legacy)
    record.rename(legacy.with_name(legacy.name + '.config.json'))
    assert installer.run('restore', source, home, cache_path=cache) == 0
    assert config.read_bytes() == original
    assert (cache / 'released.txt').read_text() == 'original exact bytes'
    assert not legacy.exists()


@pytest.mark.parametrize('shape', ['public', 'symlink', 'stale-failed'])
def test_private_recovery_refuses_unsafe_paths_before_swap(package, shape):
    source, home, cache, config = package
    original = config.read_bytes()
    recovery = installer._recovery_paths(home, cache)[0].parent
    recovery.parent.mkdir(mode=0o700)
    if shape == 'public':
        recovery.parent.chmod(0o755)
    elif shape == 'symlink':
        recovery.symlink_to(cache, target_is_directory=True)
    else:
        recovery.mkdir(mode=0o700)
        (recovery / 'failed.partial').mkdir(mode=0o700)
        (recovery / 'failed.partial/keep').write_text('owned recovery evidence')
    with pytest.raises(ValueError):
        installer.run('install', source, home, cache_path=cache)
    assert config.read_bytes() == original
    assert (cache / 'released.txt').read_text() == 'original exact bytes'
    if shape == 'stale-failed':
        assert (recovery / 'failed.partial/keep').read_text() == 'owned recovery evidence'


@pytest.mark.parametrize('boundary', ['foreign-owner', 'different-volume'])
def test_recovery_requires_same_volume_and_owner(package, monkeypatch, boundary):
    source, home, cache, config = package
    original = config.read_bytes()
    recovery = installer._recovery_paths(home, cache)[0].parent
    if boundary == 'foreign-owner':
        actual_uid = os.geteuid()
        monkeypatch.setattr(installer.os, 'geteuid', lambda: actual_uid + 1)
    else:
        actual_stat = Path.stat
        def stat_result(path, *args, **kwargs):
            result = actual_stat(path, *args, **kwargs)
            if path == recovery:
                fields = list(result)
                fields[2] += 1
                return os.stat_result(fields)
            return result
        monkeypatch.setattr(Path, 'stat', stat_result)
    with pytest.raises(ValueError, match='owned|filesystem'):
        installer.run('install', source, home, cache_path=cache)
    assert config.read_bytes() == original
    assert (cache / 'released.txt').read_text() == 'original exact bytes'


def test_readonly_move_mode_failure_restores_active_cache(package, monkeypatch):
    source, home, cache, config = package
    cache.chmod(0o555)
    original = config.read_bytes()
    original_modes = {str(p.relative_to(cache)): p.stat().st_mode & 0o7777 for p in [cache, *cache.rglob('*')]}
    backup, record = installer._recovery_paths(home, cache)
    actual_chmod = Path.chmod
    def fail_target_mode(path, mode, *args, **kwargs):
        if path == backup:
            raise OSError('synthetic target chmod failure')
        return actual_chmod(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'chmod', fail_target_mode)
    with pytest.raises(OSError, match='synthetic target chmod failure'):
        installer.run('install', source, home, cache_path=cache)
    assert config.read_bytes() == original
    assert (cache / 'released.txt').read_text() == 'original exact bytes'
    assert {str(p.relative_to(cache)): p.stat().st_mode & 0o7777 for p in [cache, *cache.rglob('*')]} == original_modes
    assert not backup.exists()
    assert not record.exists()
