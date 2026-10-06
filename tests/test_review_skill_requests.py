"""Check the actual authored JSON contracts, not only operation names."""
import ast
import inspect
import io
import json
from pathlib import Path
import re

import pytest

from parity_test_helpers import host, selected_host_context, context
import skill_procedures as procedures

pytestmark = pytest.mark.usefixtures('selected_host_context')
ROOT = Path(__file__).resolve().parents[1]


# Reviewed wire contracts, including delegated path/manifest and stdin reads.
# Keep this independent of the authored example values and runtime key extraction.
REVIEWED_REQUEST_KEYS = {
    '_apply_reviewed': frozenset('operation_id reviewed_group_ids'.split()),
    '_artifact_store': frozenset('operation_id name content'.split()),
    '_brief': frozenset('project'.split()),
    '_cascade': frozenset('operation_id project checked_texts'.split()),
    '_cascade_collect': frozenset('operation_id project'.split()),
    '_check_collect': frozenset('operation_id scope_path'.split()),
    '_check_items_stage_01': frozenset('operation_id inputs'.split()),
    '_check_items_stage_02': frozenset('operation_id inputs'.split()),
    '_check_items_stage_03': frozenset('operation_id inputs'.split()),
    '_check_items_stage_04': frozenset('operation_id inputs'.split()),
    '_check_items_stage_05': frozenset('operation_id inputs'.split()),
    '_check_items_stage_06': frozenset('operation_id inputs'.split()),
    '_check_items_stage_07': frozenset('operation_id inputs'.split()),
    '_check_items_stage_08': frozenset('operation_id inputs'.split()),
    '_check_scope': frozenset('operation_id argv'.split()),
    '_clock': frozenset(''.split()),
    '_config': frozenset(''.split()),
    '_config_read': frozenset(''.split()),
    '_configure': frozenset('settings expected_revision'.split()),
    '_consolidate': frozenset('full'.split()),
    '_deep_checkoffs': frozenset('stdin'.split()),
    '_deep_edit': frozenset('operation_id stdin'.split()),
    '_deep_pipeline': frozenset('operation_id stdin'.split()),
    '_deep_present': frozenset('operation_id pipeline_path classifications_path'.split()),
    '_dependencies': frozenset(''.split()),
    '_dev_install': frozenset('mode cache_path'.split()),
    '_doctor': frozenset('argv'.split()),
    '_emerge_build': frozenset('operation_id themes_path analysis_path'.split()),
    '_emerge_themes': frozenset('operation_id days'.split()),
    '_evidence': frozenset('also_session_ids'.split()),
    '_grep': frozenset('pattern ignore_case frontmatter_only'.split()),
    '_import_create': frozenset('operation_id source_host source_session_id filename content'.split()),
    '_import_list': frozenset('source_host source_root days project'.split()),
    '_import_read': frozenset('operation_id source_host source_session_id source_path source_root'.split()),
    '_match_candidates': frozenset('query'.split()),
    '_metadata': frozenset('paths'.split()),
    '_note_append': frozenset('operation_id path expected_revision update_text add_tags_csv last_updated'.split()),
    '_note_apply': frozenset('operation_id path expected_revision content'.split()),
    '_note_create': frozenset('operation_id folder filename content'.split()),
    '_note_read': frozenset('operation_id path'.split()),
    '_note_status': frozenset('operation_id path expected_revision status'.split()),
    '_operation_prepare': frozenset(''.split()),
    '_reindex': frozenset('full'.split()),
    '_retro_clear': frozenset(''.split()),
    '_retro_gate': frozenset('path'.split()),
    '_search': frozenset('query project limit caller type'.split()),
    '_session': frozenset(''.split()),
    '_snapshots': frozenset('source_session_id date project'.split()),
    '_stats': frozenset('project'.split()),
    '_summary_apply': frozenset('operation_id path expected_revision summary project'.split()),
    '_sync': frozenset(''.split()),
    '_theme_merge': frozenset('a b'.split()),
    '_theme_split': frozenset('theme_id'.split()),
    '_theme_stats': frozenset(''.split()),
    '_themes': frozenset('project'.split()),
    '_unsummarized': frozenset('project include_aged aged_threshold_days'.split()),
    '_upgrade_batch': frozenset('paths project'.split()),
    '_wiki_count': frozenset('data'.split()),
    '_wiki_file': frozenset('data'.split()),
    '_wiki_lookup': frozenset('data'.split()),
    '_wiki_memgrep': frozenset('data'.split()),
    '_wiki_rule': frozenset('data'.split()),
    '_wiki_stale': frozenset('data'.split()),
}


def _assert_documented_keys(procedure, payload):
    assert procedure.__name__ in REVIEWED_REQUEST_KEYS, procedure.__name__
    assert set(payload) <= REVIEWED_REQUEST_KEYS[procedure.__name__], sorted(set(payload) - REVIEWED_REQUEST_KEYS[procedure.__name__])


def _documented_stdin_fields(skill, operation, payload):
    if (skill, operation) == ('standup', 'deep-pipeline'):
        assert isinstance(payload.get('stdin'), dict)
        assert {'basenames', 'projects'} <= set(payload['stdin'])
    if (skill, operation) == ('standup', 'deep-checkoffs'):
        assert isinstance(payload.get('stdin'), list)


def test_authored_requests_supply_procedure_required_keys():
    found = 0
    for skill, operations in procedures.OPERATIONS.items():
        text = (ROOT / 'skills' / skill / 'SKILL.md').read_text()
        matches = list(re.finditer(r'^[ \t]*Request for `([^`]+)`[^\n]*\n[ \t]*\n[ \t]*```json\n(.*?)\n[ \t]*```', text, re.S | re.M))
        assert len(matches) == len(re.findall(r'^[ \t]*Request for `', text, re.M)), skill
        for match in matches:
            operation, raw = match.groups()
            assert operation in operations, (skill, operation)
            payload = json.loads(raw)
            _documented_stdin_fields(skill, operation, payload)
            source = ast.parse(inspect.getsource(operations[operation]))
            required = {node.slice.value for node in ast.walk(source)
                if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == 'payload' and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)}
            assert required <= set(payload), (skill, operation, sorted(required - set(payload)))
            _assert_documented_keys(operations[operation], payload)
            found += 1
    assert found > 40


def test_publication_guidance_only_names_registered_operations():
    for skill, operations in procedures.OPERATIONS.items():
        text = (ROOT / 'skills' / skill / 'SKILL.md').read_text()
        preamble = text.split('Use only the operations documented for this skill.', 1)[0]
        for operation in ('note-read', 'note-apply', 'note-create'):
            if '`' + operation + '`' in preamble:
                assert operation in operations, (skill, operation)


@pytest.mark.parametrize('ids', [['../../etc'], ['../secret'], ['a/b'], [''], [None], 'not-a-list'])
def test_retro_rejects_unsafe_prior_ids_before_reading(context, ids):
    stderr = io.StringIO()
    assert procedures.run_operation(context, 'retro', 'evidence', {'also_session_ids': ids}, io.StringIO(), stderr) == 1
    assert json.loads(stderr.getvalue())['code'] == 'operation_failed'


@pytest.mark.parametrize('operation', ['partition', 'update-cache', 'semantic-merge', 'classify'])
def test_unprotected_check_items_surface_is_not_callable(context, operation):
    stderr = io.StringIO()
    assert procedures.run_operation(context, 'check-items', operation, {}, io.StringIO(), stderr) == 2
    assert json.loads(stderr.getvalue())['code'] == 'operation_invalid'


@pytest.mark.parametrize('payload', [{'include_aged': 'yes'}, {'aged_threshold_days': True}, {'aged_threshold_days': 0}])
def test_recall_rejects_invalid_aged_flags(context, payload):
    stderr = io.StringIO()
    assert procedures.run_operation(context, 'recall', 'unsummarized', payload, io.StringIO(), stderr) == 1
    assert json.loads(stderr.getvalue())['code'] == 'operation_failed'


def test_recall_passes_config_default_and_explicit_aged_flags(context, monkeypatch):
    import obsidian_utils
    observed = []

    def lookup(*args, **kwargs):
        observed.append(kwargs)
        return '[]'

    monkeypatch.setattr(obsidian_utils, 'find_unsummarized_notes', lookup)
    for payload in ({}, {'include_aged': True, 'aged_threshold_days': 12}):
        assert procedures.run_operation(context, 'recall', 'unsummarized', payload, io.StringIO(), io.StringIO()) == 0
    assert observed == [{'include_aged': False, 'aged_threshold_days': None},
                        {'include_aged': True, 'aged_threshold_days': 12}]


def test_import_refuses_regular_file_outside_selected_history_root(context):
    path = context.user_home / 'outside.jsonl'
    path.write_text('{"type":"user","message":{"role":"user","content":"private text"}}\n')
    stdout, stderr = io.StringIO(), io.StringIO()
    code = procedures.run_operation(context, 'vault-import', 'import-read',
        {'source_host': context.host, 'source_session_id': context.native_session_id,
         'source_path': str(path), 'operation_id': 'a' * 32}, stdout, stderr)
    assert code == 1 and not stdout.getvalue()
    assert 'selected transcript root' in json.loads(stderr.getvalue())['message']
    assert not list(context.state_path.rglob('import-source-*.json'))


def test_import_refuses_symlink_inside_explicit_history_root(context):
    root = procedures._approved_import_roots(context,context.host)[0] / 'history'
    root.mkdir(parents=True)
    real = context.user_home / 'private-source.jsonl'
    real.write_text('{"type":"user","message":{"role":"user","content":"private text"}}\n')
    path = root / 'linked.jsonl'
    path.symlink_to(real)
    stdout, stderr = io.StringIO(), io.StringIO()
    assert procedures.run_operation(context, 'vault-import', 'import-read',
        {'source_host': context.host, 'source_session_id': context.native_session_id,
         'source_root': str(root), 'source_path': str(path), 'operation_id': 'b' * 32}, stdout, stderr) == 1
    assert not stdout.getvalue()
    assert not list(context.state_path.rglob('import-source-*.json'))


@pytest.mark.parametrize('root_kind',['filesystem','unrelated'])
def test_import_root_cannot_expand_native_history(context,root_kind):
    root=Path('/') if root_kind=='filesystem' else context.user_home/'unrelated-history'
    if root_kind=='unrelated':root.mkdir()
    with pytest.raises(ValueError,match='only narrow'):
        procedures._approved_import_roots(context,context.host,str(root))
    stdout,stderr=io.StringIO(),io.StringIO()
    assert procedures.run_operation(context,'vault-import','import-list',
        {'source_host':context.host,'source_root':str(root)},stdout,stderr)==1
    assert not stdout.getvalue()


def test_import_root_can_narrow_frozen_native_history(context,monkeypatch):
    native=procedures._approved_import_roots(context,context.host)[0]
    narrowed=native/'selected-project';narrowed.mkdir(parents=True)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR' if context.host=='claude' else 'CODEX_HOME',str(context.user_home/'foreign'))
    assert procedures._approved_import_roots(context,context.host,str(narrowed))==(narrowed.resolve(),)
    assert procedures._approved_import_roots(context,context.host)==tuple(
        context.native_home/name for name in (('projects',) if context.host=='claude' else ('sessions','archived_sessions')))


def test_import_create_rejects_same_byte_parent_symlink_swap(context,monkeypatch):
    import hashlib
    from operation_state import operation_directory,store_artifact
    root=procedures._approved_import_roots(context,context.host)[0]
    parent=root/'selected';parent.mkdir(parents=True)
    source=parent/'source.jsonl';source.write_bytes(b'{"type":"metadata"}\n')
    operation,_=operation_directory(context)
    artifact={'source_host':context.host,'source_session_id':context.native_session_id,
              'source_path':str(source),'source_root':str(root),
              'source_revision':hashlib.sha256(source.read_bytes()).hexdigest()}
    store_artifact(context,operation,procedures._import_artifact_name(context.host,context.native_session_id),json.dumps(artifact))
    payload={'operation_id':operation,'source_host':context.host,'source_session_id':context.native_session_id,'filename':'import.md','content':'---\ntype: claude-session\n---\nSummary\n'}
    assert procedures._import_create(context,payload)==0
    imported = context.vault_path / context.config['sessions_folder'] / 'import.md'
    assert imported.is_file()
    original = imported.read_bytes()
    # A second full origin must prove its source before another publication.
    artifact['source_session_id'] = context.native_session_id + '-second'
    store_artifact(context, operation, procedures._import_artifact_name(context.host, artifact['source_session_id']), json.dumps(artifact))
    payload = dict(payload, source_session_id=artifact['source_session_id'], filename='second-import.md')
    relocated=root/'relocated';parent.rename(relocated);parent.symlink_to(relocated,target_is_directory=True)
    assert source.read_bytes()==b'{"type":"metadata"}\n'
    with pytest.raises(ValueError,match='containment'):
        procedures._import_create(context,payload)
    assert imported.read_bytes() == original
    assert not list(context.vault_path.rglob('second-import.md'))


def test_dev_install_requires_an_explicit_mode_before_child(context, monkeypatch):
    import subprocess
    def forbidden(*args, **kwargs):
        raise AssertionError('missing mode launched a child')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    stderr = io.StringIO()
    assert procedures.run_operation(context, 'dev-test', 'dev-install', {}, io.StringIO(), stderr) == 1
    assert json.loads(stderr.getvalue())['code'] == 'operation_failed'


def test_optional_absent_wiki_does_not_hide_required_folders(context, monkeypatch):
    import vault_scan
    import obsidian_utils
    monkeypatch.setattr(obsidian_utils, 'indexed_folders', lambda config: ['sessions', 'insights', 'claude-wiki'])
    captured = []
    monkeypatch.setattr(vault_scan, 'main', lambda argv: captured.append(argv) or 0)
    assert procedures._grep(context, {'pattern': 'test'}) == 0
    assert captured[0][2:4] == ['sessions', 'insights']
    assert 'claude-wiki' not in captured[0]


def test_cascade_sources_keep_two_projects_and_their_original_revisions(context, monkeypatch):
    from operation_state import operation_directory, read_artifact
    import open_item_dedup
    identity, _ = operation_directory(context)
    def collect(vault, folder, project):
        return [{'path': str(context.vault_path / (project + '.md')), 'line': 1,
                 'text': project + ' task', 'source_revision': project + '-original'}]
    monkeypatch.setattr(open_item_dedup, 'collect_open_item_records', collect)
    for project in ('first', 'second'):
        assert procedures.run_operation(context, 'standup', 'cascade-collect',
            {'operation_id': identity, 'project': project}, io.StringIO(), io.StringIO()) == 0
    rows = json.loads(read_artifact(context, identity, 'cascade-source.json'))
    assert {row['text'] for row in rows} == {'first task', 'second task'}
    assert {row['source_revision'] for row in rows} == {'first-original', 'second-original'}


def test_vault_relative_note_reader_registers_exact_source(context):
    from operation_state import operation_directory
    note = context.vault_path / 'relative.md'
    note.write_text('manual source')
    identity, _ = operation_directory(context)
    stdout = io.StringIO()
    assert procedures.run_operation(context, 'standup', 'note-read',
        {'operation_id': identity, 'path': 'relative.md'}, stdout, io.StringIO()) == 0
    assert json.loads(stdout.getvalue())['content'] == 'manual source'
    with pytest.raises(ValueError):
        procedures._note_path(context, {'path': '../outside.md'})


def test_unknown_operation_input_rejects_before_environment_changes(context):
    import os
    before = dict(os.environ)
    errors = io.StringIO()
    assert procedures.run_operation(context,'recall','config',{'inputs':{'UNREVIEWED_ENV':'synthetic'}},io.StringIO(),errors)==2
    assert json.loads(errors.getvalue())['code']=='input_invalid'
    assert dict(os.environ)==before


@pytest.mark.parametrize('value', [None,0,1,'true',[],{}])
def test_consolidate_full_requires_boolean_before_backend(context,monkeypatch,value):
    import consolidate_cli
    monkeypatch.setattr(consolidate_cli,'run_consolidate',lambda **kw:pytest.fail('Invalid full reached backend'))
    errors=io.StringIO()
    assert procedures.run_operation(context,'consolidate','consolidate',{'full':value},io.StringIO(),errors)==1
    assert 'full must be a boolean' in json.loads(errors.getvalue())['message']


@pytest.mark.parametrize('value', [None,0,-1,True,1.5,'1'])
def test_split_requires_positive_integer_theme_before_backend(context,monkeypatch,value):
    import consolidate_cli
    monkeypatch.setattr(consolidate_cli,'run_split',lambda *a:pytest.fail('Invalid theme_id reached backend'))
    errors=io.StringIO()
    assert procedures.run_operation(context,'consolidate','theme-split',{'theme_id':value},io.StringIO(),errors)==1
    assert 'theme_id must be a positive integer' in json.loads(errors.getvalue())['message']


@pytest.mark.parametrize('field', ['a','b'])
@pytest.mark.parametrize('value', [None,0,-1,True,1.5,'1'])
def test_merge_requires_both_positive_integer_themes_before_backend(context,monkeypatch,field,value):
    import consolidate_cli
    monkeypatch.setattr(consolidate_cli,'run_merge',lambda *a:pytest.fail('Invalid theme ID reached backend'))
    payload={'a':1,'b':2};payload[field]=value
    errors=io.StringIO()
    assert procedures.run_operation(context,'consolidate','theme-merge',payload,io.StringIO(),errors)==1
    assert 'a and b must be positive integer' in json.loads(errors.getvalue())['message']


def test_indented_requests_and_nested_pipeline_inputs_are_not_skipped(context):
    text = '  Request for `deep-pipeline`:\n\n  ```json\n  {"operation_id":"id","stdin":{"basenames":[]}}\n  ```\n'
    matches = list(re.finditer(r'^[ \t]*Request for `([^`]+)`[^\n]*\n[ \t]*\n[ \t]*```json\n(.*?)\n[ \t]*```', text, re.S | re.M))
    assert len(matches) == 1
    payload = json.loads(matches[0].group(2))
    with pytest.raises(AssertionError):
        _documented_stdin_fields('standup', 'deep-pipeline', payload)
    _documented_stdin_fields('standup', 'deep-pipeline', {'stdin': {'basenames': [], 'projects': []}})
    out, err = io.StringIO(), io.StringIO()
    assert procedures.run_operation(context, 'standup', 'deep-pipeline', payload, out, err) != 0
    assert 'projects' in err.getvalue()
    assert not list(context.vault_path.rglob('*.md'))


def test_setup_initial_lookup_cannot_override_configured_vault(context):
    text = (ROOT / 'skills/obsidian-setup/SKILL.md').read_text()
    initial = text.split('### Step 1 —', 1)[0]
    for line in initial.splitlines():
        if 'brain_cli.py' in line and (' context ' in line or "--operation 'prepare'" in line):
            assert '--vault' not in line
    step1 = text.split('### Step 1 —', 1)[1].split('### Step 1.5', 1)[0]
    for line in step1.splitlines():
        if 'brain_cli.py' in line:
            assert '--vault' not in line


def test_every_registered_handler_has_reviewed_request_keys(context):
    for operations in procedures.OPERATIONS.values():
        for procedure in operations.values():
            assert procedure.__name__ in REVIEWED_REQUEST_KEYS
            with pytest.raises(AssertionError):
                _assert_documented_keys(procedure, {'zz': 1})


def test_recall_ignored_documented_key_is_rejected(context):
    procedure = procedures.OPERATIONS['recall']['config']
    _assert_documented_keys(procedure, {})
    with pytest.raises(AssertionError):
        _assert_documented_keys(procedure, {'zz': 1})


@pytest.mark.parametrize('operation', ['wiki-count', 'wiki-lookup', 'wiki-stale', 'wiki-memgrep', 'wiki-rule', 'wiki-file'])
def test_wiki_operations_reject_ignored_top_level_fields(context, operation, monkeypatch):
    import wiki
    called = []
    monkeypatch.setattr(wiki, 'main', lambda args: called.append(args) or 0)
    for payload in ({'zz': 1}, {'data': {}, 'zz': 1}, {'data': []}):
        out, err = io.StringIO(), io.StringIO()
        assert procedures.run_operation(context, 'vault-ask', operation, payload, out, err) != 0
        assert not called
    assert procedures.run_operation(context, 'vault-ask', operation, {'data': {}}, io.StringIO(), io.StringIO()) == 0
    assert len(called) == 1


def test_authored_deep_checkoffs_requires_list_stdin(context):
    with pytest.raises(AssertionError):
        _documented_stdin_fields('standup', 'deep-checkoffs', {'stdin': {}})
    _documented_stdin_fields('standup', 'deep-checkoffs', {'stdin': []})


def test_deep_checkoffs_rejects_object_stdin_and_accepts_empty_target_list(context):
    out, err = io.StringIO(), io.StringIO()
    assert procedures.run_operation(context, 'standup', 'deep-checkoffs', {'stdin': {}}, out, err) != 0
    assert 'list' in err.getvalue() and not out.getvalue()
    out, err = io.StringIO(), io.StringIO()
    assert procedures.run_operation(context, 'standup', 'deep-checkoffs', {'stdin': []}, out, err) == 0
    assert json.loads(out.getvalue()) == {'edits': [], 'skipped': []}
    assert not list(context.vault_path.rglob('*.md'))


def test_setup_fresh_vault_restarts_bootstrap_only_after_validation(context):
    text = (ROOT / 'skills/obsidian-setup/SKILL.md').read_text()
    step3 = text.split('### Step 3 —', 1)[1].split('### Step 4 —', 1)[0]
    assert step3.index('test -d "$VAULT_PATH"') < step3.index('OB_VAULT="$VAULT_PATH"')
    commands = [line for line in step3.splitlines() if 'brain_cli.py' in line]
    assert len(commands) == 2
    assert all('--vault "$OB_VAULT"' in line for line in commands)
    assert ' context < /dev/null' in commands[0]
    assert "--operation 'prepare'" in commands[1]


def test_config_uses_default_wiki_and_preserves_explicit_disable(context):
    from dataclasses import replace
    from types import MappingProxyType
    for configured, expected in [(None, 'claude-wiki'), ('', '')]:
        value = dict(context.config)
        if configured is None:
            value.pop('wiki_folder', None)
        else:
            value['wiki_folder'] = configured
        selected = replace(context, config=MappingProxyType(value))
        output, errors = io.StringIO(), io.StringIO()
        assert procedures.run_operation(selected, 'vault-ask', 'config', {}, output, errors) == 0
        assert json.loads(output.getvalue())['wiki_folder'] == expected


def test_setup_literal_dashboard_templates_publish_and_preserve_user_bytes(context):
    text = (ROOT / 'skills/obsidian-setup/SKILL.md').read_text()
    templates = re.findall(r'\*\*File: `\$VAULT_PATH/claude-dashboards/([^`]+)`\*\*\s+```markdown\n(.*?)\n```', text, re.S)
    assert len(templates) == 6
    output, errors = io.StringIO(), io.StringIO()
    assert procedures.run_operation(context, 'obsidian-setup', 'prepare', {}, output, errors) == 0
    operation = json.loads(output.getvalue())['operation_id']
    for filename, content in templates:
        content = content.replace('\\```', '```')
        payload = {'operation_id': operation, 'folder': 'claude-dashboards',
                   'filename': filename, 'content': content}
        output, errors = io.StringIO(), io.StringIO()
        assert procedures.run_operation(context, 'obsidian-setup', 'note-create', payload, output, errors) == 0, errors.getvalue()
        path = context.vault_path / 'claude-dashboards' / filename
        assert 'type: claude-dashboard' in path.read_text() or 'type: "claude-dashboard"' in path.read_text()
        manual = path.read_bytes() + b'\nUser custom dashboard\n'
        path.write_bytes(manual)
        assert procedures.run_operation(context, 'obsidian-setup', 'note-create', payload, io.StringIO(), io.StringIO()) != 0
        assert path.read_bytes() == manual


def test_authored_wiki_rule_and_file_use_public_data_wrapper(context):
    text = (ROOT / 'skills/vault-ask/SKILL.md').read_text()
    assert '`wiki-rule` operation with `{"data": {}}`' in text
    output, errors = io.StringIO(), io.StringIO()
    assert procedures.run_operation(context, 'vault-ask', 'wiki-rule', {'data': {}}, output, errors) == 0
    assert 'rule' in json.loads(output.getvalue())
    filing = text[text.index('4. **File.**'):]
    literal = json.loads(re.search(r'```json\s*(.*?)\s*```', filing, re.S).group(1))
    assert set(literal) == {'data'} and isinstance(literal['data'], dict)
    assert {'question', 'body', 'sources'} <= set(literal['data'])
    for operation in ('wiki-rule', 'wiki-file'):
        output, errors = io.StringIO(), io.StringIO()
        assert procedures.run_operation(context, 'vault-ask', operation, {'question': 'ignored'}, output, errors) != 0
        assert not output.getvalue()


def test_codex_dev_install_requires_exact_contained_cache_without_guessing(context, monkeypatch):
    from dataclasses import replace
    selected = context
    observed = []
    def no_child(*args, **kwargs):
        observed.append((args, kwargs))
        raise AssertionError('Invalid cache selection launched a child')
    import subprocess
    monkeypatch.setattr(subprocess, 'run', no_child)
    if selected.host == 'codex':
        for payload in ({'mode': 'status'}, {'mode': 'status', 'cache_path': '/'},
                        {'mode': 'status', 'cache_path': str(selected.native_home / 'plugins/cache/m/other/3.8.1')}):
            assert procedures.run_operation(selected, 'dev-test', 'dev-install', payload, io.StringIO(), io.StringIO()) != 0
        assert observed == []
        cache = selected.native_home / 'plugins/cache/fixture/obsidian-brain/3.8.1'
        cache.mkdir(parents=True)
        from types import SimpleNamespace
        def record_child(command, **kwargs):
            observed.append((command, kwargs))
            return SimpleNamespace(returncode=0)
        monkeypatch.setattr(subprocess, 'run', record_child)
        monkeypatch.setenv('CODEX_HOME', str(selected.user_home / 'foreign-home'))
        assert procedures.run_operation(selected, 'dev-test', 'dev-install',
            {'mode': 'status', 'cache_path': str(cache)}, io.StringIO(), io.StringIO()) == 0
        command, options = observed[0]
        assert command[-2:] == ['--cache-path', str(cache)]
        assert options['env']['CODEX_HOME'] == str(selected.native_home)
        assert options['stdin'] == subprocess.DEVNULL and options['timeout'] == 120
    else:
        # A Claude invocation never needs a foreign Codex cache selection.
        from types import SimpleNamespace
        def record_child(command, **kwargs):
            observed.append((command, kwargs))
            return SimpleNamespace(returncode=0)
        monkeypatch.setattr(subprocess, 'run', record_child)
        assert procedures.run_operation(selected, 'dev-test', 'dev-install', {'mode': 'status'}, io.StringIO(), io.StringIO()) == 0
        assert '--cache-path' not in observed[0][0]


def test_stage08_unpinned_model_reports_disabled_without_cache_publication(context, monkeypatch):
    from dataclasses import replace
    from types import MappingProxyType
    from operation_state import operation_directory, store_artifact
    import check_items_cache
    config = dict(context.config)
    for key in ('classifier_model', 'codex_ai_model', 'codex_summary_model'):
        config.pop(key, None)
    selected = replace(context, config=MappingProxyType(config))
    operation, directory = operation_directory(selected)
    artifacts = {'scope.json': {'json_output': True},
                 'classifications.json': {'classifications': []},
                 'partition.json': {'heads': {}},
                 'classifier-provenance.json': {'groups': [], 'evidence': {}},
                 'outcome.json': {'status': 'complete', 'warnings': [], 'applied': 0}}
    for name, value in artifacts.items():
        store_artifact(selected, operation, name, json.dumps(value))
    def refuse_cache(*args, **kwargs):
        pytest.fail('Unresolved model must not publish a replay cache')
    monkeypatch.setattr(check_items_cache, 'locked_cache', refuse_cache)
    output, errors = io.StringIO(), io.StringIO()
    payload = {'operation_id': operation, 'inputs': {
        'SCOPE_PATH': str(directory / 'scope.json'),
        'CLASSIFICATIONS_PATH': str(directory / 'classifications.json'),
        'PARTITION_PATH': str(directory / 'partition.json')}}
    assert procedures.run_operation(selected, 'check-items', 'stage-08', payload, output, errors) == 0, errors.getvalue()
    outcome = json.loads(output.getvalue())
    assert outcome['cache_status'] == 'disabled' and outcome['applied'] == 0
    assert any('not pinned' in warning for warning in outcome['warnings'])


def test_configure_accepts_explicit_native_models_and_rejects_alias_as_cache_identity(context):
    context.config_path.chmod(0o600)
    output, errors = io.StringIO(), io.StringIO()
    assert procedures.run_operation(context, 'vault-config', 'config-read', {}, output, errors) == 0
    revision = json.loads(output.getvalue())['expected_revision']
    valid = {'classifier_model': 'claude-haiku-4-5', 'codex_summary_model': 'gpt-5.4'}
    output, errors = io.StringIO(), io.StringIO()
    assert procedures.run_operation(context, 'vault-config', 'configure', {
        'expected_revision': revision, 'settings': valid}, output, errors) == 0, errors.getvalue()
    assert all(json.loads(context.config_path.read_text())[key] == value for key, value in valid.items())
    before = context.config_path.read_bytes()
    for key, value in [('classifier_model', 'haiku'), ('codex_summary_model', 'claude-haiku-4-5'), ('codex_ai_model', True)]:
        assert procedures.run_operation(context, 'vault-config', 'configure', {
            'expected_revision': revision, 'settings': {key: value}}, io.StringIO(), io.StringIO()) != 0
        assert context.config_path.read_bytes() == before


def test_native_install_environment_uses_only_frozen_invoking_home(context):
    from runtime_context import selected_native_environment
    from dataclasses import replace
    base = {'CLAUDE_CONFIG_DIR': '/foreign/claude', 'CODEX_HOME': '/foreign/codex',
            'UNRELATED': 'keep'}
    before = dict(base)
    selected = selected_native_environment(context, base)
    key = 'CLAUDE_CONFIG_DIR' if context.host == 'claude' else 'CODEX_HOME'
    foreign = 'CODEX_HOME' if context.host == 'claude' else 'CLAUDE_CONFIG_DIR'
    assert selected[key] == str(context.native_home)
    assert selected[foreign] == base[foreign] and selected['UNRELATED'] == 'keep'
    assert base == before
    with pytest.raises(ValueError, match='frozen native home'):
        selected_native_environment(replace(context, native_home=None), base)
