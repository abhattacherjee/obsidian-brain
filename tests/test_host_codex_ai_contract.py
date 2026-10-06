"""Observed Codex RPC and exec envelopes are checked without native processes."""
import copy
import json
import os
import time
from pathlib import Path
from types import MappingProxyType

import pytest

import ai_backend
from ai_adapters import codex
from runtime_context import RuntimeContext


@pytest.fixture
def context(tmp_path):
    vault = tmp_path / 'vault'; vault.mkdir()
    return RuntimeContext('codex', 'cli', 'synthetic-contract', tmp_path, tmp_path,
                          None, vault, tmp_path / 'config.json', MappingProxyType({}),
                          tmp_path, tmp_path / 'index.sqlite3', tmp_path / 'state')


def native_config(restricted=False):
    servers = {name: {'enabled': not restricted} for name in ('alpha', 'beta', 'gamma')}
    plugin = {'enabled': True, 'mcp_servers': {
        name: {'enabled': not restricted} for name in ('one', 'two', 'three', 'four')}}
    return {'model': 'native-model', 'model_provider': 'native-provider',
            'mcp_servers': servers, 'plugins': {'tools@local': plugin},
            'features': {name: False for name in codex.DISABLED_FEATURES},
            'web_search': 'disabled'}


def server(name, plugin=None):
    return {'name': name, 'pluginId': plugin, 'runtimeStatus': None,
            'tools': {}, 'resources': [], 'resourceTemplates': []}


def fake_inventory(monkeypatch, *, config=None, restricted=None, listing=None,
                   detail=None, pages=None):
    initial = native_config() if config is None else config
    final = native_config(True) if restricted is None else restricted
    listing = listing if listing is not None else {
        'marketplaces': [{'path': '/synthetic-marketplace', 'plugins': [
            {'name': 'tools', 'id': 'tools@local', 'installed': True, 'enabled': True},
            {'name': 'catalog-only', 'id': 'catalog-only@local', 'installed': False, 'enabled': False}
        ]}], 'marketplaceLoadErrors': []}
    detail = detail if detail is not None else {'plugin': {'mcpServers': ['one', 'two', 'three', 'four']}}
    pages = pages if pages is not None else [
        {'data': [server('alpha'), server('beta')], 'nextCursor': 'page-two'},
        {'data': [server('gamma')] + [server(name, 'tools@local') for name in ('one', 'two', 'three', 'four')],
         'nextCursor': None}]
    instances = []
    class Rpc:
        def __init__(self, binary, arguments, ctx, env, deadline):
            self.index = len(instances)
            self.arguments = list(arguments)
            self.requests = []
            self.closed = False
            self.page = 0
            instances.append(self)
        def initialize(self):
            self.initialized = True
        def close(self):
            self.closed = True
        def request(self, method, params):
            self.requests.append((method, copy.deepcopy(params)))
            if method == 'config/read':
                return {'config': copy.deepcopy(initial if self.index == 0 else final), 'origins': {}}
            if method == 'plugin/list':
                return copy.deepcopy(listing)
            if method == 'plugin/read':
                return copy.deepcopy(detail)
            if method == 'mcpServerStatus/list':
                value = pages[min(self.page, len(pages) - 1)]
                self.page += 1
                return copy.deepcopy(value)
            pytest.fail('Unexpected native RPC method: ' + method)
    monkeypatch.setattr(codex, 'NativeRpc', Rpc)
    return instances


def discover(context):
    return codex.discover_restrictions('codex', context, {}, time.monotonic() + 10)


def test_real_metadata_shapes_disable_all_servers_and_page_fully(context, monkeypatch):
    instances = fake_inventory(monkeypatch)
    arguments, model = discover(context)
    assert model == 'native-model'
    assert len(instances) == 2 and all(item.closed and item.initialized for item in instances)
    flags = arguments[1::2]
    assert all(arguments[index] == '-c' for index in range(0, len(arguments), 2))
    for name in ('alpha', 'beta', 'gamma'):
        assert 'mcp_servers.' + name + '.enabled=false' in flags
    for name in ('one', 'two', 'three', 'four'):
        assert 'plugins.tools@local.mcp_servers.' + name + '.enabled=false' in flags
    assert not any('"tools@local"' in flag for flag in flags)
    reads = [params for method, params in instances[0].requests if method == 'plugin/read']
    assert reads == [{'pluginName': 'tools', 'marketplacePath': '/synthetic-marketplace'}]
    status = [params for method, params in instances[1].requests if method == 'mcpServerStatus/list']
    assert status == [{'limit': 100, 'detail': 'toolsAndAuthOnly'},
                      {'limit': 100, 'detail': 'toolsAndAuthOnly', 'cursor': 'page-two'}]
    assert all(method != 'turn/start' for item in instances for method, _ in item.requests)


@pytest.mark.parametrize('name', ['with.dot', 'with space', 'quote"', 'slash/name', 'newline\n', ''])
def test_unsafe_override_keys_are_rejected(context, monkeypatch, name):
    config = native_config(); config['mcp_servers'] = {name: {'enabled': True}}
    instances = fake_inventory(monkeypatch, config=config)
    with pytest.raises(ai_backend._BackendFailure) as exc:
        discover(context)
    assert exc.value.code == 'native_override_key_unsupported'
    assert len(instances) == 1 and instances[0].closed


@pytest.mark.parametrize('change', ['marketplace_error', 'entry_error', 'unreadable_plugin', 'missing_enabled_plugin'])
def test_partial_plugin_inventory_cannot_reach_execution(context, monkeypatch, change):
    listing = {'marketplaces': [{'path': '/synthetic', 'plugins': []}], 'marketplaceLoadErrors': []}
    detail = None
    if change == 'marketplace_error':
        listing['marketplaceLoadErrors'] = [{'message': 'synthetic failure'}]
    elif change == 'entry_error':
        listing['marketplaces'][0]['loadErrors'] = ['synthetic failure']
    elif change == 'unreadable_plugin':
        listing['marketplaces'][0]['plugins'] = [{'name': 'tools', 'id': 'tools@local', 'installed': True, 'enabled': True}]
        detail = {'error': 'synthetic'}
    fake_inventory(monkeypatch, listing=listing, detail=detail)
    with pytest.raises(ai_backend._BackendFailure):
        discover(context)


@pytest.mark.parametrize('changed', [
    {'name': 'unknown'}, {'pluginId': 'unknown@local'}, {'tools': {'tool': {}}},
    {'resources': [{'uri': 'synthetic'}]}, {'resourceTemplates': [{'uriTemplate': 'synthetic'}]},
])
def test_unknown_or_nonempty_runtime_catalog_fails_closed(context, monkeypatch, changed):
    value = server('alpha'); value.update(changed)
    fake_inventory(monkeypatch, pages=[{'data': [value], 'nextCursor': None}])
    with pytest.raises(ai_backend._BackendFailure) as exc:
        discover(context)
    assert exc.value.code == 'native_mcp_still_advertised'


@pytest.mark.parametrize('cursor', ['', 3, 'repeat'])
def test_invalid_or_repeating_pagination_fails_closed(context, monkeypatch, cursor):
    fake_inventory(monkeypatch, pages=[{'data': [], 'nextCursor': cursor}])
    with pytest.raises(ai_backend._BackendFailure):
        discover(context)


def test_effective_enabled_server_override_failure_blocks_model(context, monkeypatch):
    final = native_config(True); final['mcp_servers']['alpha']['enabled'] = True
    fake_inventory(monkeypatch, restricted=final)
    with pytest.raises(ai_backend._BackendFailure) as exc:
        discover(context)
    assert exc.value.code == 'native_mcp_restriction_failed'


def fake_exec(monkeypatch, events, *, code=0, errors=b'', output=None):
    calls = []
    monkeypatch.setattr(codex, 'discover_restrictions', lambda *args: (codex.restrictions(), 'native-model'))
    def run(command, prompt, **kwargs):
        calls.append((command, prompt, {'cwd': kwargs.get('cwd'), 'deadline': kwargs.get('deadline')}))
        path = Path(command[command.index('--output-last-message') + 1])
        if output is not None:
            path.write_text(json.dumps(output))
            path.chmod(0o600)
        raw = b'\n'.join(json.dumps(event).encode() for event in events)
        return code, raw, errors
    monkeypatch.setattr(codex, '_run_bounded', run)
    return calls


def events(notice=None):
    values = [{'type': 'thread.started', 'thread_id': 'synthetic-thread'}]
    if notice is not None:
        values.append({'type': 'item.completed', 'item': {'type': 'error', 'message': notice}})
    values += [{'type': 'turn.started'},
               {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"text":"Synthetic summary."}'}},
               {'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}]
    return values


def private_directory(context):
    path = context.worktree / 'private-analysis'; path.mkdir(mode=0o700)
    return path


def test_exact_observed_startup_notice_before_turn_is_allowed(context, monkeypatch):
    calls = fake_exec(monkeypatch, events(codex.CODE_MODE_DISABLED_NOTICE), output={'text': 'Synthetic summary.'})
    directory = private_directory(context)
    output, model = codex.execute(context, 'Synthetic inline input.', {}, None,
                                 time.monotonic() + 10, {}, directory)
    assert output == {'text': 'Synthetic summary.'} and model == 'native-model'
    command, prompt, options = calls[0]
    assert prompt == 'Synthetic inline input.' and str(context.vault_path) not in prompt
    assert command[command.index('--sandbox') + 1] == 'read-only'
    assert '--ephemeral' in command and '--dangerously-bypass-approvals-and-sandbox' not in command
    assert command[-1] == '-'
    assert all(path.parent == directory for path in directory.iterdir())
    assert list(context.vault_path.iterdir()) == []
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())


@pytest.mark.parametrize('variant', ['unknown_notice', 'late_notice', 'tool', 'unknown_event', 'incomplete', 'policy'])
def test_native_errors_tools_and_incomplete_turns_fail_closed(context, monkeypatch, variant):
    stream = events()
    code, errors = 0, b''
    if variant == 'unknown_notice':
        stream = events('A different native error')
    elif variant == 'late_notice':
        stream.insert(2, {'type': 'item.completed', 'item': {'type': 'error', 'message': codex.CODE_MODE_DISABLED_NOTICE}})
    elif variant == 'tool':
        stream.insert(2, {'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'synthetic-only'}})
    elif variant == 'unknown_event':
        stream.insert(2, {'type': 'new.native.event'})
    elif variant == 'incomplete':
        stream = stream[:-1]
    else:
        code, errors = 1, b'permission denied: synthetic private diagnostic'
    fake_exec(monkeypatch, stream, code=code, errors=errors, output={'text': 'Should not return'})
    with pytest.raises(ai_backend._BackendFailure) as exc:
        codex.execute(context, 'Synthetic input.', {}, None, time.monotonic() + 10, {}, private_directory(context))
    assert 'private diagnostic' not in str(exc.value)
    assert list(context.vault_path.iterdir()) == []


def test_hidden_reasoning_is_not_model_output(context, monkeypatch):
    stream = events()
    stream.insert(2, {'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'synthetic hidden sentinel'}})
    fake_exec(monkeypatch, stream, output={'text': 'Visible summary'})
    output, _ = codex.execute(context, 'Synthetic input.', {}, None, time.monotonic() + 10, {}, private_directory(context))
    assert output == {'text': 'Visible summary'}
    assert 'hidden sentinel' not in repr(output)


@pytest.mark.parametrize('variant', ['no_thread', 'no_start', 'no_message', 'duplicate_end', 'after_end'])
def test_native_output_requires_one_complete_ordered_turn(context, monkeypatch, variant):
    stream = events()
    if variant == 'no_thread':
        stream = stream[1:]
    elif variant == 'no_start':
        stream.pop(1)
    elif variant == 'no_message':
        stream.pop(2)
    elif variant == 'duplicate_end':
        stream.append(stream[-1])
    else:
        stream.append({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'late'}})
    fake_exec(monkeypatch, stream, output={'text': 'Must not return'})
    with pytest.raises(ai_backend._BackendFailure):
        codex.execute(context, 'Synthetic', {}, None, time.monotonic() + 10, {}, private_directory(context))


def test_private_analysis_directory_cannot_be_inside_vault(context, monkeypatch):
    fake_exec(monkeypatch, events(), output={'text': 'Should not run'})
    directory = context.vault_path / 'private'; directory.mkdir(mode=0o700)
    with pytest.raises(ai_backend._BackendFailure) as exc:
        codex.execute(context, 'Synthetic', {}, None, time.monotonic() + 10, {}, directory)
    assert exc.value.code == 'native_private_directory_invalid'
    assert list(directory.iterdir()) == []

@pytest.mark.parametrize('feature', ['shell_tool', 'apps', 'browser_use', 'multi_agent'])
def test_managed_effective_feature_cannot_override_tool_restriction(context, monkeypatch, feature):
    final = native_config(True); final['features'][feature] = True
    fake_inventory(monkeypatch, restricted=final)
    with pytest.raises(ai_backend._BackendFailure):
        discover(context)


def test_non_null_runtime_status_is_not_a_disabled_definition(context, monkeypatch):
    value = server('alpha'); value['runtimeStatus'] = {'status': 'ready'}
    fake_inventory(monkeypatch, pages=[{'data': [value], 'nextCursor': None}])
    with pytest.raises(ai_backend._BackendFailure):
        discover(context)


@pytest.mark.parametrize('field', ['tools', 'resources', 'resourceTemplates', 'runtimeStatus'])
def test_missing_runtime_catalog_fields_do_not_prove_restriction(context, monkeypatch, field):
    value = server('alpha'); del value[field]
    fake_inventory(monkeypatch, pages=[{'data': [value], 'nextCursor': None}])
    with pytest.raises(ai_backend._BackendFailure):
        discover(context)


def test_backend_scrubs_inline_secrets_before_codex_model_input(context, monkeypatch):
    calls = fake_exec(monkeypatch, events(), output={'text': 'Synthetic summary'})
    request = ai_backend.AIRequest('Synthetic input token=synthetic-private-value', input_revision='source-revision')
    result = ai_backend.execute_ai(context, 'session_summary', request)
    assert result.status == 'ok'
    assert result.input_revision == 'source-revision'
    assert len(calls) == 1
    assert 'synthetic-private-value' not in calls[0][1]
    assert '[REDACTED]' in calls[0][1]
    assert str(context.vault_path) not in calls[0][1]
    assert list(context.vault_path.iterdir()) == []

def test_effective_web_search_override_cannot_reenable_outbound_tool(context, monkeypatch):
    final = native_config(True); final['web_search'] = 'live'
    fake_inventory(monkeypatch, restricted=final)
    with pytest.raises(ai_backend._BackendFailure):
        discover(context)


@pytest.mark.parametrize('mode', ['symlink', 'hardlink', 'public'])
def test_native_output_file_cannot_alias_or_expose_vault_note(context, monkeypatch, mode):
    note = context.vault_path / 'user-note.md'; note.write_text('Synthetic user text.'); note.chmod(0o600)
    monkeypatch.setattr(codex, 'discover_restrictions', lambda *args: (codex.restrictions(), 'native-model'))
    def run(command, prompt, **kwargs):
        path = Path(command[command.index('--output-last-message') + 1])
        if mode == 'public':
            path.write_text('{"text":"Synthetic output"}'); path.chmod(0o644)
        else:
            path.unlink()
            if mode == 'symlink':
                path.symlink_to(note)
            else:
                os.link(note, path)
        return 0, b'\n'.join(json.dumps(event).encode() for event in events()), b''
    monkeypatch.setattr(codex, '_run_bounded', run)
    result = ai_backend.execute_ai(context, 'session_summary', ai_backend.AIRequest('Synthetic input'))
    assert result.status == 'invalid_output'
    assert result.data is None
    assert note.read_text() == 'Synthetic user text.'
    assert list(context.vault_path.iterdir()) == [note]
