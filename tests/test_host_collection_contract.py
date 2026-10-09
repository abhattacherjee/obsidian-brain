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
             launcher_contract=None, changed_launcher_bytes=False, execute=False, parallel=False, coverage=False):
    matrix = {'capabilities':[
        {'id':'writer.cas','required':True,'calls':['apply_mutations'],
         'hosts':{host:{'status':'supported'} for host in ('claude','codex')}},
        {'id':'capture','required':True,'calls':['capture_checkpoint'],
         'hosts':{host:{'status':'supported'} for host in ('claude','codex')}},
        {'id':'cli','required':True,'calls':['brain_cli.main'],
         'hosts':{host:{'status':'supported'} for host in ('claude','codex')}},
        {'id':'skill.dispatch','required':True,'calls':['skill_procedures.run_operation'],
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
    if parallel:
        # Each worker must apply the guards, including the runtime checks.
        command.extend(["-n", "2", "--dist=each", "--max-worker-restart=0",
                        "--basetemp=" + str(tmp_path / "worker-temp")])
    if coverage:
        command.extend(["--cov=sample", "--cov-fail-under=90"])
    if not execute:
        command.insert(3, '--collect-only')
    # Execute only disposable scratch tests when a runtime control requests it.
    env = dict(__import__('os').environ, PYTHONPATH=__import__('os').pathsep.join([
        str(PLUGIN.parent), str(Path.cwd() / 'hooks')]))
    env.update(HOME=str(tmp_path / 'home'), CODEX_HOME=str(tmp_path / 'home/.codex'),
               CLAUDE_CONFIG_DIR=str(tmp_path / 'home/.claude'))
    if coverage:
        # This child measures synthetic sample.py for its own failing gate.
        # Keep both modern and legacy coverage startup out of the outer data.
        for name in tuple(env):
            if name.startswith('COV_CORE_') or name in {
                    'COVERAGE_PROCESS_CONFIG', 'COVERAGE_PROCESS_START',
                    'COVERAGE_RCFILE', 'COVERAGE_FILE'}:
                env.pop(name, None)
        env['COVERAGE_FILE'] = str(tmp_path / '.coverage-child')
    for name in ('OBSIDIAN_BRAIN_CONFIG', 'OBSIDIAN_BRAIN_STATE_DIR', 'OBSIDIAN_BRAIN_DB',
                 'CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID'):
        env.pop(name, None)
    # Full-suite coverage starts and flushes measured child processes. Keep
    # this orchestration bound separate from production hook deadlines.
    try:
        return subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,text=True,
                              timeout=60,cwd=tmp_path,env=env)
    except subprocess.TimeoutExpired as exc:
        def tail(value):
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            return (value or "")[-4000:]
        pytest.fail("Scratch pytest exceeded its 60s deadline.\nstdout:\n"
                    + tail(exc.stdout) + "\nstderr:\n" + tail(exc.stderr),
                    pytrace=False)


@pytest.mark.parametrize("parallel", [False, True])
def test_oversized_test_id_fails_before_verbose_output(tmp_path, parallel):
    source = ('@pytest.mark.parametrize("value", [b"x" * 20_000])\n'
              'def test_large(value):\n    assert len(value) == 20_000\n')
    result = _collect(tmp_path, source, execute=parallel, parallel=parallel)
    assert result.returncode != 0
    assert 'test ID exceeds 4096 bytes' in result.stdout + result.stderr
    assert len(result.stdout + result.stderr) < 4096


def test_explicit_short_id_keeps_large_payload_executable(tmp_path):
    source = ('@pytest.mark.parametrize("value", [b"x" * 20_000], ids=["large-payload"])\n'
              'def test_large(value):\n    assert len(value) == 20_000\n')
    result = _collect(tmp_path, source, execute=True)
    assert result.returncode == 0
    assert '1 passed' in result.stdout
    assert len(result.stdout + result.stderr) < 4096


@pytest.mark.parametrize('byte_length', [4096, 4097])
def test_test_id_limit_counts_utf8_bytes_at_boundary(tmp_path, byte_length):
    prefix = 'test_case.py::test_boundary['
    parameter_id = '\u00e9' + 'x' * (byte_length - len(prefix.encode('utf-8')) - 3)
    nodeid = prefix + parameter_id + ']'
    assert len(nodeid.encode('utf-8')) == byte_length
    source = ('@pytest.mark.parametrize("value", [1], ids=[' + repr(parameter_id) + '])\n'
              'def test_boundary(value):\n    assert value == 1\n')
    result = _collect(tmp_path, source, helper_files={
        'pytest.ini': '[pytest]\ndisable_test_id_escaping_and_forfeit_all_rights_to_community_support = True\n'})
    if byte_length == 4096:
        assert result.returncode == 0
        assert nodeid in result.stdout
    else:
        assert result.returncode != 0
        assert 'test ID exceeds 4096 bytes' in result.stdout + result.stderr
        assert len(result.stdout + result.stderr) < 4096


@pytest.mark.parametrize("parallel", [False, True])
def test_unannotated_new_writer_fails_collection(tmp_path, parallel):
    result = _collect(tmp_path,'def test_new():\n    apply_mutations(ctx, edits)\n',
                      execute=parallel, parallel=parallel)
    assert result.returncode != 0 and 'needs host fixture' in result.stdout + result.stderr


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


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize('wrong_host,inactive', [(False, False), (True, False),
                                               (False, True)])
def test_actual_active_context_must_match_invoking_host(tmp_path, wrong_host, inactive, parallel):
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
    result = _collect(tmp_path, source, fixture, execute=True, parallel=parallel)
    if wrong_host:
        assert result.returncode != 0 and 'does not match invoking host' in result.stdout, result.stdout + result.stderr
    elif inactive:
        assert result.returncode != 0 and 'context is not active' in result.stdout, result.stdout + result.stderr
    if parallel and (wrong_host or inactive):
        expected = '2 failed, 2 passed' if wrong_host else '4 failed'
        assert expected in result.stdout, result.stdout + result.stderr
    if not wrong_host and not inactive:
        assert result.returncode == 0 and ('4 passed' if parallel else '2 passed') in result.stdout, result.stdout + result.stderr


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


def test_scratch_timeout_reports_bounded_child_output(tmp_path, monkeypatch):
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"],
            output=b"x" * 5000 + b"child stdout tail",
            stderr=b"y" * 5000 + b"child stderr tail")
    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(pytest.fail.Exception) as failure:
        _collect(tmp_path, "def test_value():\n    assert True\n")
    diagnostic = str(failure.value)
    assert "exceeded its 60s deadline" in diagnostic
    assert "child stdout tail" in diagnostic and "child stderr tail" in diagnostic
    assert "x" * 4001 not in diagnostic and "y" * 4001 not in diagnostic


def test_dynamic_imported_skill_alias_requires_real_host_fixture(tmp_path):
    source = ('import skill_procedures as procedures\n'
              'run = getattr(procedures, "run_" + "operation")\n'
              'def test_new():\n    run(ctx, "vault-stats", {})\n')
    result = _collect(tmp_path, source, helper_files={
        'skill_procedures.py': 'def run_operation(context, operation, payload):\n    return 0\n'})
    assert result.returncode != 0 and 'needs host fixture' in result.stderr


def test_local_shared_service_import_cannot_hide_actor_scope(tmp_path):
    result = _collect(tmp_path, 'def test_new():\n'
                     '    from skill_procedures import run_operation as invoke\n'
                     '    invoke(None, "vault-stats", {})\n', helper_files={
        'skill_procedures.py': 'def run_operation(context, operation, payload):\n    return 0\n'})
    assert result.returncode != 0 and 'needs host fixture' in result.stderr


def test_dynamic_skill_alias_still_checks_actual_foreign_actor(tmp_path):
    source = ('from dataclasses import replace\n'
              'import skill_procedures as procedures\n'
              'run = getattr(procedures, "run_" + "operation")\n'
              '@pytest.mark.parametrize("host",["claude","codex"])\n'
              'def test_new(host, selected_host_context):\n'
              '    run(replace(selected_host_context, host="claude"), "vault-stats", {})\n')
    result = _collect(tmp_path, source,
                      'from parity_test_helpers import selected_host_context\n',
                      helper_files={'skill_procedures.py':
                          'def run_operation(context, operation, payload):\n    return 0\n'},
                      execute=True)
    assert result.returncode != 0
    assert 'actual RuntimeContext does not match invoking host' in result.stdout + result.stderr


def test_unresolved_shared_module_attribute_requires_actor_declaration(tmp_path):
    source = ('import skill_procedures as procedures\n'
              'method = "run_" + str("operation")\n'
              'run = getattr(procedures, method)\n'
              'def test_new():\n    run(None, "vault-stats", {})\n')
    result = _collect(tmp_path, source, helper_files={
        'skill_procedures.py': 'def run_operation(context, operation, payload):\n    return 0\n'})
    assert result.returncode != 0 and 'needs host fixture' in result.stderr


def test_aliased_getsource_reflection_is_structural_but_actual_call_is_not(tmp_path):
    source = ('from inspect import getsource as source_of\n'
              'import skill_procedures as procedures\n'
              'def test_new():\n'
              '    assert source_of(getattr(procedures, "run_" + "operation"))\n')
    helpers = {'skill_procedures.py':'def run_operation(context, operation, payload):\n    return 0\n'}
    reflected = _collect(tmp_path,source,helper_files=helpers)
    assert reflected.returncode == 0
    injected = _collect(tmp_path,source+'    procedures.run_operation(None, "vault-stats", {})\n',helper_files=helpers)
    assert injected.returncode != 0 and 'needs host fixture' in injected.stderr


@pytest.mark.parametrize("access", [
    'run = sp.run_operation',
    'run = importlib.import_module("skill_procedures").run_operation',
    'run = __import__("skill_procedures").run_operation',
    'run = vars(sp)["run_operation"]',
    'run = sp.OPERATIONS["recall"]["config"]',
])
def test_reflective_shared_alias_requires_invoking_actor(tmp_path, access):
    source = ('import importlib\nimport skill_procedures as sp\n'
              + access + '\ndef test_new():\n    run(None, "recall", {})\n')
    helper = ('def run_operation(context, skill, payload):\n    return 0\n'
              'OPERATIONS = {"recall": {"config": run_operation}}\n')
    result = _collect(tmp_path, source, helper_files={'skill_procedures.py': helper})
    assert result.returncode != 0
    assert 'needs host fixture' in result.stderr


@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('setup, invocation', [
    ('fn = capture.capture_checkpoint', 'fn(ctx, event, deadline)'),
    ('fn = functools.partial(capture.capture_checkpoint, ctx)', 'fn(event, deadline)'),
    ('', 'list(map(capture.capture_checkpoint, rows))'),
    ('fn = lambda ctx: capture.capture_checkpoint(ctx, event, deadline)', 'fn(ctx)'),
    ('module = capture', 'module.capture_checkpoint(ctx, event, deadline)'),
    ('dispatch = {"capture": capture.capture_checkpoint}', 'dispatch["capture"](ctx, event, deadline)'),
])
def test_callable_capture_alias_requires_paired_hosts(tmp_path, setup, invocation, paired):
    declaration = ('@pytest.mark.parametrize("host", ["claude", "codex"])\n'
                   'def test_new(host, selected_host_context):\n    ctx = selected_host_context\n'
                   if paired else 'def test_new():\n')
    source = 'import capture\nimport functools\nctx = None\n' + setup + '\n' + declaration + '    ' + invocation + '\n'
    result = _collect(tmp_path, source, helper_files={
        'capture.py': 'def capture_checkpoint(*args):\n    return True\n',
    })
    if paired:
        assert result.returncode == 0 and '2 tests collected' in result.stdout, result.stdout + result.stderr
    else:
        assert result.returncode != 0 and 'needs host fixture' in result.stderr, result.stdout + result.stderr


@pytest.mark.parametrize('access', ['globals()["sp"].run_operation',
                                  'sp.__dict__["run_operation"]'])
def test_runtime_guard_refuses_dynamic_call_without_actor_before_service(tmp_path, access):
    result = _collect(tmp_path,
        'import skill_procedures as sp\ndef test_new():\n    '
        + access + '(None, "recall", {})\n',
        helper_files={'skill_procedures.py':
            'from pathlib import Path\ndef run_operation(*args):\n'
            '    Path("service-ran").write_text("unsafe")\n    return 0\n'},
        execute=True)
    assert result.returncode != 0
    assert ('selected_host_context fixture is required' in result.stdout
            or 'needs host fixture' in result.stderr), result.stdout + result.stderr
    assert not (tmp_path / 'service-ran').exists()


def test_source_change_during_collection_invalidates_cached_syntax(tmp_path):
    fixture = ('import parity_collection_plugin as plugin\n'
               'from pathlib import Path\n'
               'original_scope = plugin._scope\n'
               'def changed_scope(*args, **kwargs):\n'
               '    result = original_scope(*args, **kwargs)\n'
               '    path = Path(__file__).with_name("test_case.py")\n'
               '    path.write_text(path.read_text() + "\\n# changed during collection\\n")\n'
               '    return result\n'
               'plugin._scope = changed_scope\n')
    result = _collect(tmp_path, 'def test_new():\n    assert True\n', fixture)
    assert result.returncode != 0
    assert 'source bytes changed during collection' in result.stderr


@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('setup, call, fixture', [
    ('NAME = "skill_procedures"\nrun = importlib.import_module(NAME).run_operation', 'run', ''),
    ('run = sp.__dict__["run_operation"]', 'run', ''),
    ('run = operator.attrgetter("run_operation")(sp)', 'run', ''),
    ('', 'run', '@pytest.fixture\ndef run():\n    import skill_procedures as sp\n    return sp.run_operation\n'),
    ('run, ignored = sp.run_operation, None', 'run', ''),
    ('def helper():\n    return sp.run_operation\n', 'helper()', ''),
    ('class Dispatch:\n    run = staticmethod(sp.run_operation)', 'Dispatch.run', ''),
    ('for run in [sp.run_operation]:\n    pass', 'run', ''),
    ('(run := sp.run_operation)', 'run', ''),
    ('', 'run', ''),
    ('', 'globals()["sp"].run_operation', ''),
    ('', 'sys.modules["skill_procedures"].run_operation', ''),
    ('run = sp.OPERATIONS.get("recall")["config"]', 'run', ''),
    ('OPS = [sp.run_operation]', 'OPS[0]', ''),
    ('run = lambda *args: (None, sp.run_operation(*args))', 'run', ''),
    ('run: object = sp.run_operation', 'run', ''),
    ('run = operator.methodcaller("run_operation", None, "recall", {})',
     'lambda *args: run(sp)', ''),
])
def test_reviewed_reflective_forms_require_paired_invoking_actors(tmp_path, setup, call, fixture, paired):
    default = 'run=sp.run_operation' if not setup and call == 'run' and not fixture else ''
    parameters = ['host', 'selected_host_context'] if paired else []
    if fixture:
        parameters.append('run')
    if default:
        parameters.append(default)
    declaration = '@pytest.mark.parametrize("host", ["claude", "codex"])\n' if paired else ''
    source = ('import skill_procedures as sp\nimport importlib\nimport operator\nimport sys\n'
              + setup + '\n' + declaration + 'def test_new(' + ', '.join(parameters)
              + '):\n    (' + call + ')(None, "recall", {})\n')
    helper = ('def run_operation(*args):\n    return 0\n'
              'OPERATIONS = {"recall": {"config": run_operation}}\n')
    result = _collect(tmp_path, source, fixture, {'skill_procedures.py': helper})
    if paired:
        assert result.returncode == 0 and '2 tests collected' in result.stdout, result.stdout + result.stderr
    else:
        assert result.returncode != 0 and 'needs host fixture' in result.stderr, result.stdout + result.stderr


@pytest.mark.parametrize('setup,invoke',[
    ('OPS={"c":capture.capture_checkpoint}', 'for f in OPS.values():\n        f(None)'),
    ('OPS=[capture.capture_checkpoint]', 'for f in OPS:\n        f(None)'),
    ('CL=(lambda f: (lambda *a: f(*a)))(capture.capture_checkpoint)', 'CL(None)'),
    ('import types\nNS=types.SimpleNamespace(f=capture.capture_checkpoint)', 'NS.f(None)'),
    ('import types\nOPS=types.MappingProxyType({"c":capture.capture_checkpoint})', 'for f in OPS.values():\n        f(None)'),
    ('OPS={capture.capture_checkpoint}', 'for f in OPS:\n        f(None)'),
    ('def holder():\n    pass\nholder.f=capture.capture_checkpoint', 'holder.f(None)'),
    ('class B:\n    f=staticmethod(capture.capture_checkpoint)\nclass K(B):\n    pass', 'K.f(None)'),
])
def test_stored_shared_original_cannot_bypass_runtime_actor(tmp_path,setup,invoke):
    source='import capture\n'+setup+'\ndef test_new():\n    '+invoke+'\n'
    result=_collect(tmp_path,source,helper_files={'capture.py':
        'from pathlib import Path\ndef capture_checkpoint(*args):\n    Path("service-ran").write_text("unsafe")\n'},execute=True)
    assert result.returncode!=0
    assert ('selected_host_context fixture is required' in result.stdout
            or 'needs host fixture' in result.stderr),result.stdout+result.stderr
    assert not (tmp_path/'service-ran').exists()


@pytest.mark.parametrize('setup,invoke',[
    ('OPS={"c":capture.capture_checkpoint}', 'for f in OPS.values():\n        assert f(selected_host_context)'),
    ('OPS=[capture.capture_checkpoint]', 'for f in OPS:\n        assert f(selected_host_context)'),
    ('CL=(lambda f: (lambda *a: f(*a)))(capture.capture_checkpoint)', 'assert CL(selected_host_context)'),
    ('import types\nNS=types.SimpleNamespace(f=capture.capture_checkpoint)', 'assert NS.f(selected_host_context)'),
    ('import types\nOPS=types.MappingProxyType({"c":capture.capture_checkpoint})', 'for f in OPS.values():\n        assert f(selected_host_context)'),
    ('OPS={capture.capture_checkpoint}', 'for f in OPS:\n        assert f(selected_host_context)'),
    ('def holder():\n    pass\nholder.f=capture.capture_checkpoint', 'assert holder.f(selected_host_context)'),
    ('class B:\n    f=staticmethod(capture.capture_checkpoint)\nclass K(B):\n    pass', 'assert K.f(selected_host_context)'),
])
def test_stored_shared_original_accepts_real_selected_actor(tmp_path,setup,invoke):
    source=('import capture\n'+setup+'\n@pytest.mark.parametrize("host",["claude","codex"])\n'
        'def test_new(host,selected_host_context):\n    '+invoke+'\n')
    result=_collect(tmp_path,source,'from parity_test_helpers import selected_host_context\n',
        helper_files={'capture.py':'def capture_checkpoint(context):\n    return True\n'},execute=True)
    assert result.returncode==0 and '2 passed' in result.stdout,result.stdout+result.stderr


def test_stored_shared_original_read_only_import_does_not_require_actor(tmp_path):
    source=('import capture\nOPS={"c":capture.capture_checkpoint}\n'
        'class B:\n    f=staticmethod(capture.capture_checkpoint)\nclass K(B):\n    pass\n'
        'def test_new():\n    assert list(OPS)==["c"]\n    assert callable(K.f)\n')
    result=_collect(tmp_path,source,helper_files={'capture.py':'def capture_checkpoint(*args):\n    return True\n'},execute=True)
    assert result.returncode==0 and '1 passed' in result.stdout,result.stdout+result.stderr


@pytest.mark.parametrize('setup,access,restore',[
    ('OPS={"c":capture.capture_checkpoint}', 'OPS["c"]', 'module.OPS["c"]=saved[0]'),
    ('OPS=[capture.capture_checkpoint]', 'OPS[0]', 'module.OPS[0]=saved[0]'),
    ('class B:\n    f=staticmethod(capture.capture_checkpoint)\nclass K(B):\n    pass', 'K.f', 'module.B.f=staticmethod(saved[0])'),
])
def test_saved_container_guard_cannot_leak_to_next_actor(tmp_path,setup,access,restore):
    source=('import capture\n'+setup+'\n@pytest.mark.parametrize("host",["claude","codex"])\n'
        'def test_new(host,selected_host_context,saved_guard):\n'
        '    saved_guard[:]=['+access+']\n    assert '+access+'(selected_host_context)\n')
    fixture=('from parity_test_helpers import selected_host_context\n'
        '@pytest.fixture\ndef saved_guard(request):\n    saved=[]\n    yield saved\n'
        '    module=request.module\n    '+restore+'\n')
    result=_collect(tmp_path,source,fixture,
        helper_files={'capture.py':'def capture_checkpoint(context):\n    return True\n'},execute=True)
    assert result.returncode==0 and '2 passed' in result.stdout,result.stdout+result.stderr


def test_identity_guard_preserves_closure_defaults_and_restores_code(tmp_path):
    source = ('import capture\n'
              'saved=capture.capture_checkpoint\n'
              'code=saved.__code__\n'
              'CL=(lambda f: (lambda *a, **k: f(*a, **k)))(saved)\n'
              '@pytest.mark.parametrize("host",["claude","codex"])\n'
              'def test_new(host,selected_host_context,check_restoration):\n'
              '    assert saved is capture.capture_checkpoint\n'
              '    assert CL(selected_host_context)==("closed",7,9)\n'
              '    with pytest.raises(ValueError,match="original failure"):\n'
              '        CL(selected_host_context,flag=-1)\n')
    fixture = ('from parity_test_helpers import selected_host_context\n'
               '@pytest.fixture\ndef check_restoration(request):\n'
               '    yield\n'
               '    module=request.module\n'
               '    assert module.saved.__code__ is module.code\n'
               '    assert not any(k.startswith("__parity_dispatch_") for k in module.saved.__globals__)\n')
    helper = ('def factory():\n    value="closed"\n'
              '    def capture_checkpoint(context, number=7, *, flag=9):\n'
              '        if flag==-1:\n            raise ValueError("original failure")\n'
              '        return value,number,flag\n'
              '    return capture_checkpoint\ncapture_checkpoint=factory()\n')
    result = _collect(tmp_path, source, fixture, {'capture.py': helper}, execute=True)
    assert result.returncode == 0 and '2 passed' in result.stdout, result.stdout + result.stderr


@pytest.mark.parametrize('module_name', ['capture', 'hooks.capture'])
@pytest.mark.parametrize('method', ['exec', 'importlib'])
def test_late_real_capture_import_cannot_bypass_actor_guard(tmp_path, module_name, method):
    statement = 'import ' + module_name + '; ' + module_name + '.capture_checkpoint(None,None,1)'
    invocation = ('exec(' + repr(statement) + ')' if method == 'exec' else
                  'exec(' + repr('loader(' + repr(module_name) + ').capture_checkpoint(None,None,1)') + ')')
    source = ('import sys,importlib\nloader=importlib.import_module\ndef test_new():\n'
              '    assert ' + repr(module_name) + ' not in sys.modules\n'
              '    try:\n        ' + invocation + '\n'
              '    except AssertionError:\n        raise\n'
              '    except Exception:\n        pass\n')
    fixture = 'import sys\nsys.path.insert(0,' + repr(str(PLUGIN.parent.parent)) + ')\n'
    result = _collect(tmp_path, source, fixture, execute=True)
    assert result.returncode != 0
    assert 'selected_host_context fixture is required' in result.stdout, result.stdout + result.stderr


@pytest.mark.parametrize('module_name', ['capture', 'hooks.capture'])
def test_late_api_import_accepts_selected_actor_and_restores(tmp_path, module_name):
    source = ('import sys,importlib\n'
              '@pytest.mark.parametrize("host",["claude","codex"])\n'
              'def test_new(host,selected_host_context):\n'
              '    module=importlib.import_module(' + repr(module_name) + ')\n'
              '    assert module.capture_checkpoint(selected_host_context)\n')
    helper_files = {module_name.replace('.', '/') + '.py':
                    'def capture_checkpoint(context):\n    return True\n'}
    if module_name.startswith('hooks.'):
        helper_files['hooks/__init__.py'] = ''
    result = _collect(tmp_path, source, 'from parity_test_helpers import selected_host_context\n',
                      helper_files, execute=True)
    assert result.returncode == 0 and '2 passed' in result.stdout, result.stdout + result.stderr


def test_late_api_import_failure_is_not_hidden_by_guard(tmp_path):
    source = ('import importlib\ndef test_new():\n'
              '    with pytest.raises(ImportError,match="synthetic import failure"):\n'
              '        importlib.import_module("capture")\n')
    result = _collect(tmp_path, source, helper_files={
        'capture.py': 'raise ImportError("synthetic import failure")\n'}, execute=True)
    assert result.returncode == 0 and '1 passed' in result.stdout, result.stdout + result.stderr


def test_parallel_coverage_still_rejects_below_ninety_percent(tmp_path, monkeypatch):
    import coverage
    hook = str(PLUGIN.parent.parent / 'hooks/native_entry.py')
    outer_path = tmp_path / '.coverage-outer'
    outer_data = coverage.CoverageData(basename=str(outer_path))
    outer_data.add_lines({hook: [1]})
    outer_data.write()
    outer_bytes = outer_path.read_bytes()
    monkeypatch.setenv('COVERAGE_FILE', str(outer_path))
    active = coverage.Coverage.current()
    def hook_lines():
        if active is None:
            return None
        data = active.get_data()
        return {filename: tuple(sorted(data.lines(filename) or []))
                for filename in data.measured_files()}
    before = hook_lines()
    result = _collect(tmp_path, 'from sample import covered\ndef test_value():\n    assert covered() == 1\n',
                      helper_files={'sample.py': 'def covered():\n    return 1\ndef uncovered():\n    a = 2\n    b = 3\n    return a + b\n'},
                      execute=True, parallel=True, coverage=True)
    assert result.returncode != 0
    assert '2 passed' in result.stdout, result.stdout + result.stderr
    assert 'Required test coverage of 90%' in result.stdout + result.stderr
    assert outer_path.read_bytes() == outer_bytes
    assert hook_lines() == before, 'Child coverage changed the active outer hook data'
    child_data = coverage.CoverageData(basename=str(tmp_path / '.coverage-child'))
    child_data.read()
    assert str(tmp_path / 'sample.py') in child_data.measured_files()
    assert hook not in child_data.measured_files()


def test_definition_cache_reuses_unread_parameters_but_keeps_skill_bindings(tmp_path, monkeypatch):
    import importlib.util
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('cached_collection_scope', PLUGIN)
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    path = tmp_path / 'test_case.py'
    path.write_text('from skill_procedures import run_operation\n'
                    'def helper(skill):\n    run_operation(ctx, skill, op, payload)\n'
                    'def test_new(skill):\n    helper(skill)\n')
    item = SimpleNamespace(path=path, originalname='test_new', name='test_new', nodeid='first',
                           config=SimpleNamespace(rootpath=tmp_path),
                           callspec=SimpleNamespace(params={'skill': 'one', 'unused': 'first'}),
                           _fixtureinfo=SimpleNamespace(name2fixturedefs={}))
    capabilities = [{'id': 'skill.dispatch', 'calls': ['skill_procedures.run_operation']},
                    {'id': 'skill.one', 'calls': []}, {'id': 'skill.two', 'calls': []}]
    cache = {}
    calls = []
    original = plugin._extend_aliases
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(plugin, '_extend_aliases', counted)
    assert plugin._scope(item, capabilities, analysis_cache=cache) == {'skill.dispatch', 'skill.one'}
    first_count = len(calls)
    item.callspec.params['unused'] = 'second'
    assert plugin._scope(item, capabilities, analysis_cache=cache) == {'skill.dispatch', 'skill.one'}
    assert len(calls) == first_count, 'Identical definition analysis was repeated'
    item.callspec.params['skill'] = 'two'
    assert plugin._scope(item, capabilities, analysis_cache=cache) == {'skill.dispatch', 'skill.two'}
    assert len(calls) > first_count


def test_definition_cache_replays_failed_launcher_for_each_item_and_changed_matrix(tmp_path):
    import importlib.util
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('cached_launcher_scope', PLUGIN)
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    path = tmp_path / 'test_case.py'
    path.write_text('import subprocess\ndef test_new():\n    subprocess.run(["private-control"])\n')
    item = SimpleNamespace(path=path, originalname='test_new', name='test_new', nodeid='first',
                           config=SimpleNamespace(rootpath=tmp_path),
                           callspec=SimpleNamespace(params={}),
                           _fixtureinfo=SimpleNamespace(name2fixturedefs={}))
    cache, errors = {}, []
    assert plugin._scope(item, [], errors=errors, analysis_cache=cache) == set()
    item.nodeid = 'second'
    assert plugin._scope(item, [], errors=errors, analysis_cache=cache) == set()
    assert len(errors) == 2
    assert errors[0].startswith('first: launcher contract missing')
    assert errors[1].startswith('second: launcher contract missing')
    launchers = [{'source': 'test_case.py', 'expression': "subprocess.run(['private-control'])",
                  'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                  'scope': 'structural', 'reason': 'private control'}]
    errors.clear()
    assert plugin._scope(item, [], launchers, errors, cache) == set()
    assert errors == []
    launchers[0]['source_sha256'] = 'changed'
    assert plugin._scope(item, [], launchers, errors, cache) == set()
    assert errors and errors[0].startswith('second: launcher contract missing')


def test_cached_definition_does_not_waive_missing_invoking_host(tmp_path):
    result = _collect(tmp_path,
        '@pytest.mark.parametrize("case", [1, 2])\n'
        'def test_new(host, selected_host_context, case):\n'
        '    apply_mutations(selected_host_context, [])\n',
        '@pytest.fixture(params=["claude"])\ndef host(request):\n    return request.param\n')
    assert result.returncode != 0
    assert 'missing invoking host' in result.stderr
