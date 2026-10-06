"""Scratch-only negative controls run pytest in disposable tiny projects."""
import json
import ast
import hashlib
from pathlib import Path
import subprocess
import sys
import pytest

PLUGIN = Path(__file__).with_name('parity_collection_plugin.py')


def _collect(tmp_path, test_source, fixture_source='', helper_files=None,
             launcher_contract=None, changed_launcher_bytes=False, execute=False):
    matrix = {'capabilities':[
        {'id':'writer.cas','required':True,'calls':['apply_mutations'],
         'hosts':{host:{'status':'supported'} for host in ('claude','codex')}},
        {'id':'capture','required':True,'calls':['capture_checkpoint'],
         'hosts':{host:{'status':'supported'} for host in ('claude','codex')}},
        {'id':'cli','required':True,'calls':['brain_cli.main'],
         'hosts':{host:{'status':'supported'} for host in ('claude','codex')}},
        {'id':'claude_native_format','required':False,'calls':['parse_claude_record'],
         'hosts':{'claude':{'status':'supported'},'codex':{'status':'unsupported:claude-record-format'}}},
    ]}
    (tmp_path/'matrix.json').write_text(json.dumps(matrix))
    (tmp_path/'test_case.py').write_text('import pytest\n'+test_source)
    (tmp_path/'conftest.py').write_text('import pytest\n'
        '@pytest.fixture\ndef selected_host_context(host):\n    return host\n'+fixture_source)
    for filename, source in (helper_files or {}).items():
        (tmp_path / filename).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / filename).write_text(source)
    if launcher_contract:
        filename, capability, scope, reason = launcher_contract
        source_path = tmp_path / filename
        invocation = next(node for node in ast.walk(ast.parse(source_path.read_text()))
                          if isinstance(node, ast.Call)
                          and ast.unparse(node.func) == 'subprocess.run')
        matrix['launchers'] = [{'source': filename,
                               'source_sha256': hashlib.sha256(source_path.read_bytes()).hexdigest(),
                               'expression': ast.unparse(invocation), 'capability': capability,
                               'scope': scope, 'reason': reason}]
        (tmp_path / 'matrix.json').write_text(json.dumps(matrix))
        if changed_launcher_bytes:
            source_path.write_text(source_path.read_text() + '\n# Changed after review\n')
    command = [sys.executable,'-m','pytest','-q','-p','parity_collection_plugin',
               '--parity-matrix',str(tmp_path/'matrix.json'),str(tmp_path)]
    if not execute:
        command.insert(3, '--collect-only')
    # This launches only pytest collection; fixtures and test bodies never run.
    env = dict(__import__('os').environ, PYTHONPATH=__import__('os').pathsep.join([
        str(PLUGIN.parent), str(Path.cwd() / 'hooks')]))
    env.update(HOME=str(tmp_path / 'home'), CODEX_HOME=str(tmp_path / 'home/.codex'),
               CLAUDE_CONFIG_DIR=str(tmp_path / 'home/.claude'))
    for name in ('OBSIDIAN_BRAIN_CONFIG', 'OBSIDIAN_BRAIN_STATE_DIR', 'OBSIDIAN_BRAIN_DB',
                 'CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID'):
        env.pop(name, None)
    return subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,text=True,
                          timeout=10,cwd=tmp_path,env=env)


def test_unannotated_new_writer_fails_collection(tmp_path):
    result = _collect(tmp_path,'def test_new():\n    apply_mutations(ctx, edits)\n')
    assert result.returncode != 0 and 'needs host fixture' in result.stderr


def test_container_attribute_cannot_crash_or_hide_writer_collection(tmp_path):
    source = ('def test_new():\n'
              '    for key, value in {' + repr('x' * 300) + ': 1}.items():\n'
              '        apply_mutations(ctx, edits)\n')
    result = _collect(tmp_path, source)
    assert result.returncode != 0 and 'needs host fixture' in result.stderr
    assert 'INTERNALERROR' not in result.stdout + result.stderr


def test_imported_shared_api_rejects_hardcoded_foreign_context(tmp_path):
    source = ('from dataclasses import replace\n'
              'from helpers import invoke\n'
              '@pytest.mark.parametrize("host",["claude","codex"])\n'
              'def test_new(host, selected_host_context):\n'
              '    invoke(replace(selected_host_context, host="claude"))\n')
    helpers = {
        'note_transactions.py': 'def apply_mutations(context, edits):\n    return True\n',
        'helpers.py': 'from note_transactions import apply_mutations as write\n'
                      'def invoke(context):\n    return write(context, [])\n',
    }
    result = _collect(tmp_path, source,
                      'from parity_test_helpers import selected_host_context\n',
                      helper_files=helpers, execute=True)
    assert result.returncode != 0
    assert 'actual RuntimeContext does not match invoking host' in result.stdout, result.stdout + result.stderr
    assert '1 failed, 1 passed' in result.stdout


def test_fixture_saved_guard_does_not_leak_into_next_invoking_host(tmp_path):
    source = ('import note_transactions\n'
              '@pytest.mark.parametrize("host",["claude","codex"])\n'
              'def test_new(host, selected_host_context, saved_guard):\n'
              '    saved_guard[:] = [note_transactions.apply_mutations]\n'
              '    assert note_transactions.apply_mutations(selected_host_context, [])\n')
    fixture = ('from parity_test_helpers import selected_host_context\n'
               'import note_transactions\n'
               '@pytest.fixture\ndef saved_guard():\n'
               '    yield saved\n'
               '    note_transactions.apply_mutations = saved[0]\n'
               'saved = []\n')
    result = _collect(tmp_path, source, fixture, execute=True)
    assert result.returncode == 0 and '2 passed' in result.stdout, result.stdout + result.stderr


def test_same_named_method_in_other_class_does_not_change_scope(tmp_path):
    source = ('class TestPure:\n'
              '    def helper(self):\n        return 1\n'
              '    def test_value(self):\n        assert self.helper() == 1\n'
              'class WriterHelper:\n'
              '    def helper(self):\n        apply_mutations(ctx, edits)\n')
    result = _collect(tmp_path, source)
    assert result.returncode == 0 and '1 test collected' in result.stdout, result.stdout + result.stderr


def test_both_invoking_hosts_collect(tmp_path):
    result = _collect(tmp_path,'def test_new(host, selected_host_context):\n    apply_mutations(selected_host_context, edits)\n',
                      '@pytest.fixture(params=["claude","codex"])\ndef host(request):\n    return request.param\n')
    assert result.returncode == 0 and '2 tests collected' in result.stdout


def test_one_invoking_host_is_not_enough(tmp_path):
    result = _collect(tmp_path,'def test_new(host, selected_host_context):\n    apply_mutations(selected_host_context, edits)\n',
                      '@pytest.fixture(params=["claude"])\ndef host(request):\n    return request.param\n')
    assert result.returncode != 0 and 'missing invoking host' in result.stderr


def test_native_origin_format_declaration_matches_exact_reason(tmp_path):
    declaration='@pytest.mark.host_only("claude",reason="claude-record-format",capability="claude_native_format")\n'
    result = _collect(tmp_path,declaration+'def test_format():\n    parse_claude_record(raw)\n')
    assert result.returncode == 0


def test_mutated_native_format_reason_fails(tmp_path):
    declaration='@pytest.mark.host_only("claude",reason="wrong-reason",capability="claude_native_format")\n'
    result = _collect(tmp_path,declaration+'def test_format():\n    parse_claude_record(raw)\n')
    assert result.returncode != 0 and 'does not match' in result.stderr


def test_writer_cannot_mislabel_itself_as_native_format(tmp_path):
    declaration='@pytest.mark.host_only("claude",reason="claude-record-format",capability="claude_native_format")\n'
    result = _collect(tmp_path,declaration+'def test_format():\n    parse_claude_record(raw)\n    apply_mutations(ctx, edits)\n')
    assert result.returncode != 0 and 'cannot waive' in result.stderr


def test_indirect_local_helper_cannot_hide_writer(tmp_path):
    result = _collect(tmp_path,'def helper():\n    apply_mutations(ctx, edits)\ndef test_new():\n    helper()\n')
    assert result.returncode != 0 and 'needs host fixture' in result.stderr


def test_host_coverage_cannot_be_split_across_different_inputs(tmp_path):
    source='@pytest.mark.parametrize("host,case",[("claude","a"),("codex","b")])\ndef test_new(host,case,selected_host_context):\n    apply_mutations(selected_host_context, edits)\n'
    result=_collect(tmp_path,source)
    assert result.returncode != 0 and 'missing invoking host' in result.stderr


def test_imported_helper_alias_cannot_hide_writer(tmp_path):
    source = 'from helpers import outer as execute\ndef test_new():\n    execute()\n'
    helpers = {'helpers.py': 'def inner():\n    apply_mutations(ctx, edits)\ndef outer():\n    inner()\n'}
    result = _collect(tmp_path, source, helper_files=helpers)
    assert result.returncode != 0 and 'needs host fixture' in result.stderr


def test_imported_fixture_local_helper_cannot_hide_writer(tmp_path):
    fixture = 'from helpers import prepared as source\n'
    helpers = {'helpers.py': 'import pytest\ndef repair():\n    apply_mutations(ctx, edits)\n@pytest.fixture\ndef prepared():\n    repair()\n'}
    result = _collect(tmp_path, 'def test_new(source):\n    assert source\n',
                      fixture, helper_files=helpers)
    assert result.returncode != 0 and 'needs host fixture' in result.stderr


def test_relative_import_in_fixture_cannot_hide_writer(tmp_path):
    helpers = {
        'helperpkg/__init__.py': '',
        'helperpkg/fixtures.py': ('import pytest\nfrom .inner import repair as invoke\n'
                                 '@pytest.fixture\ndef prepared():\n    invoke()\n'),
        'helperpkg/inner.py': 'def repair():\n    apply_mutations(ctx, edits)\n',
    }
    result = _collect(tmp_path, 'def test_new(source):\n    assert source\n',
                      'from helperpkg.fixtures import prepared as source\n', helpers)
    assert result.returncode != 0 and 'needs host fixture' in result.stderr


@pytest.mark.parametrize('operation', ['apply_mutations(ctx, edits)',
                                      'capture_checkpoint(ctx, event, deadline)',
                                      'brain_cli.main(argv)'])
def test_mutating_imported_fixture_into_data_operation_changes_collection(tmp_path, operation):
    fixture = 'from helpers import prepared as source\n'
    helpers = {'helpers.py': 'import pytest\n@pytest.fixture\ndef prepared():\n    return True\n'}
    test = 'def test_new(source):\n    assert source\n'
    assert _collect(tmp_path, test, fixture, helpers).returncode == 0
    helpers['helpers.py'] = ('import pytest\n@pytest.fixture\ndef prepared():\n    '
                             + operation + '\n    return True\n')
    result = _collect(tmp_path, test, fixture, helpers)
    assert result.returncode != 0 and 'needs host fixture' in result.stderr


def test_unknown_launcher_cannot_disappear_from_collection(tmp_path):
    source = 'import subprocess\ndef test_new():\n    subprocess.run(command)\n'
    result = _collect(tmp_path, source)
    assert result.returncode != 0 and 'launcher contract missing' in result.stderr


def test_reviewed_writer_launcher_still_requires_host_pair(tmp_path):
    source = 'import subprocess\ndef test_new():\n    subprocess.run(command)\n'
    result = _collect(tmp_path, source,
                      launcher_contract=('test_case.py', 'writer.cas', None, None))
    assert result.returncode != 0 and 'needs host fixture' in result.stderr


def test_exact_support_launcher_has_explicit_reason(tmp_path):
    source = 'import subprocess\ndef test_new():\n    subprocess.run(command)\n'
    result = _collect(tmp_path, source,
                      launcher_contract=('test_case.py', None, 'support', 'disposable Git setup'))
    assert result.returncode == 0


def test_changed_launcher_source_invalidates_reviewed_support_contract(tmp_path):
    source = 'import subprocess\ndef test_new():\n    subprocess.run(command)\n'
    result = _collect(tmp_path, source,
                      launcher_contract=('test_case.py', None, 'support', 'disposable Git setup'),
                      changed_launcher_bytes=True)
    assert result.returncode != 0 and 'launcher contract missing or changed' in result.stderr


def test_unused_host_label_cannot_replace_selected_context(tmp_path):
    source='@pytest.mark.parametrize("host",["claude","codex"])\ndef test_new(host):\n    apply_mutations(ctx, edits)\n'
    result = _collect(tmp_path, source)
    assert result.returncode != 0 and 'selected_host_context fixture is required' in result.stderr


@pytest.mark.parametrize('wrong_host,inactive', [(False, False), (True, False),
                                               (False, True)])
def test_actual_active_context_must_match_invoking_host(tmp_path, wrong_host, inactive):
    fixture = '''from pathlib import Path
from runtime_context import resolve_runtime_context, using_runtime_context
@pytest.fixture
def selected_host_context(host, tmp_path):
    resources = tmp_path / 'resources'
    (resources / 'hooks').mkdir(parents=True)
    for directory in ['.claude-plugin', '.codex-plugin']:
        path = resources / directory
        path.mkdir(parents=True)
        (path / 'plugin.json').write_text('{"name":"synthetic"}')
    config = tmp_path / 'config.json'
    config.write_text('{"vault_path":"''' + str(tmp_path) + '''"}')
    chosen = ''' + ('"claude"' if wrong_host else 'host') + '''
    selected = resolve_runtime_context(chosen, 'claude-code' if chosen == 'claude' else 'codex-cli',
        {'session_id':'fixture-session','cwd':str(tmp_path)},
        {'config_path':config,'resource_root':resources})
    with using_runtime_context(selected):
        yield selected
'''
    if inactive:
        fixture = fixture.replace('    with using_runtime_context(selected):\n        yield selected',
                                  '    yield selected')
    source = ('@pytest.mark.parametrize("host",["claude","codex"])\n'
              'def test_new(host,selected_host_context):\n'
              '    if False:\n        apply_mutations(selected_host_context, edits)\n')
    result = _collect(tmp_path, source, fixture, execute=True)
    if wrong_host:
        assert result.returncode != 0 and 'does not match invoking host' in result.stdout, result.stdout + result.stderr
    elif inactive:
        assert result.returncode != 0 and 'context is not active' in result.stdout, result.stdout + result.stderr
    else:
        assert result.returncode == 0 and '2 passed' in result.stdout, result.stdout + result.stderr


@pytest.mark.parametrize('source,parameters,helper', [
    ('def test_new():\n    run_operation(ctx, "one", op, payload)\n', {}, None),
    ('def test_new():\n    name = "one"\n    run_operation(ctx, name, op, payload)\n', {}, None),
    ('def test_new(skill):\n    run_operation(ctx, skill, op, payload)\n', {'skill': 'one'}, None),
    ('def invoke(name):\n    run_operation(ctx, name, op, payload)\ndef test_new():\n    invoke("one")\n', {}, None),
    ('from helpers import invoke\ndef test_new():\n    invoke("one")\n', {},
     'from skill_procedures import run_operation\ndef invoke(name):\n    run_operation(ctx, name, op, payload)\n'),
])
def test_skill_argument_selects_exact_capability(tmp_path, source, parameters, helper):
    import importlib.util
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('collection_scope', PLUGIN)
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    path = tmp_path / 'test_case.py'
    path.write_text('from skill_procedures import run_operation\n' + source)
    if helper is not None:
        (tmp_path / 'helpers.py').write_text(helper)
    item = SimpleNamespace(path=path, originalname='test_new', name='test_new',
                           config=SimpleNamespace(rootpath=tmp_path),
                           callspec=SimpleNamespace(params=parameters),
                           _fixtureinfo=SimpleNamespace(name2fixturedefs={}))
    capabilities = [{'id':'skill.dispatch','calls':['skill_procedures.run_operation']},
                    {'id':'skill.one','calls':[]}, {'id':'skill.two','calls':[]}]
    assert plugin._scope(item, capabilities) == {'skill.dispatch', 'skill.one'}


def test_only_actual_ai_service_can_return_context_required(selected_host_context):
    import ai_backend
    from parity_collection_plugin import _actual_ai_context_rejection
    result = ai_backend.execute_ai(None, 'classify_items', ai_backend.AIRequest('{}'))
    actual = getattr(ai_backend.execute_ai, '__parity_original__', ai_backend.execute_ai)
    assert _actual_ai_context_rejection(actual, 'ai_backend.execute_ai', None, result)
    assert not _actual_ai_context_rejection(lambda *args: result, 'ai_backend.execute_ai', None, result)
    assert not _actual_ai_context_rejection(actual, 'note_transactions.apply_mutations', None, result)
    assert not _actual_ai_context_rejection(actual, 'ai_backend.execute_ai', object(), result)
    assert not _actual_ai_context_rejection(actual, 'ai_backend.execute_ai', None,
                                           ai_backend.AIResult('complete', error_code='context_required'))
    assert not _actual_ai_context_rejection(actual, 'ai_backend.execute_ai', None,
                                           ai_backend.AIResult('unavailable', error_code='transport_error'))
