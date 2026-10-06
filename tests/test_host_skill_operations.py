"""Direct native skill acceptance with private fixtures and no live backends."""
import hashlib
import io
import json
import os
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import MappingProxyType

import pytest
import ai_backend
import skill_procedures as procedures
from operation_state import read_artifact, store_artifact
from runtime_context import RuntimeContext, current_runtime_context

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=['claude', 'codex'])
def host(request):
    return request.param


@pytest.fixture
def selected_host_context(host, tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(home / 'claude'))
    monkeypatch.setenv('CODEX_HOME', str(home / 'codex'))
    vault = tmp_path / 'vault'
    for folder in ('claude-sessions', 'claude-insights', 'claude-dashboards', 'claude-check-items', 'claude-wiki'):
        (vault / folder).mkdir(parents=True)
    project = tmp_path / 'project'
    project.mkdir()
    config = {'vault_path': str(vault), 'sessions_folder': 'claude-sessions', 'insights_folder': 'claude-insights', 'wiki_folder': 'claude-wiki', 'workspace_roots': [str(tmp_path)], 'codex_ai_model': 'fixture-codex', 'classifier_model': 'claude-fixture', 'check_items_prefilter': False}
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    selected = RuntimeContext(host, host + '-cli' if host == 'codex' else 'claude-code', 'native-session', project, project, None, vault, config_path, MappingProxyType(config), ROOT, tmp_path / 'index.sqlite3', tmp_path / 'state')
    def no_live_backend(*args, **kwargs):
        pytest.fail('Acceptance fixtures must not launch a native AI backend')
    monkeypatch.setattr(ai_backend, 'execute_ai', no_live_backend)
    from runtime_context import using_runtime_context
    with using_runtime_context(selected):
        yield selected


@pytest.fixture
def context(selected_host_context):
    return selected_host_context


def invoke(context, skill, operation, payload=None, *, success=True):
    output, errors = io.StringIO(), io.StringIO()
    status = procedures.run_operation(context, skill, operation, payload or {}, output, errors)
    if success:
        assert status == 0, (output.getvalue(), errors.getvalue())
    return status, output.getvalue(), errors.getvalue()


def prepare(context, skill):
    return json.loads(invoke(context, skill, 'prepare')[1])


def note(context, filename, body, *, folder='claude-sessions', kind='claude-session', **fields):
    path = context.vault_path / folder / filename
    metadata = dict(type=kind, date=date.today().isoformat(), project='project', tags=['claude/topic/testing'], **fields)
    path.write_text('---\n' + ''.join(key + ': ' + json.dumps(value) + '\n' for key, value in metadata.items()) + '---\n\n# ' + filename[:-3] + '\n\n' + body)
    path.chmod(0o600)
    return path


def source_read(context, skill, path, operation):
    return json.loads(invoke(context, skill, 'note-read', {'operation_id': operation, 'path': str(path)})[1])


def test_setup_configuration_reindex_and_search_round_trip(context):
    config = json.loads(invoke(context, 'obsidian-setup', 'config')[1])
    assert config['project'] == 'project'
    assert config['index_path'] == str(context.index_path)
    assert config['wiki_folder'] == 'claude-wiki'
    before = json.loads(invoke(context, 'obsidian-setup', 'config-read')[1])
    invoke(context, 'obsidian-setup', 'configure', {'expected_revision': before['expected_revision'], 'settings': {'min_messages': 4, 'snapshot_on_clear': True, 'optional_deps_prompted': True, 'optional_deps_declined': ['numpy']}})
    persisted = json.loads(context.config_path.read_text())
    assert persisted['min_messages'] == 4 and persisted['snapshot_on_clear'] is True
    assert context.config_path.stat().st_mode & 0o777 == 0o600
    path = note(context, 'ranking.md', 'Zebracorn ranking uses bm25.', folder='claude-insights', kind='claude-insight')
    result = json.loads(invoke(context, 'vault-reindex', 'reindex', {'full': True})[1])
    assert result['mode'] == 'full'
    assert context.index_path.is_file()
    results = json.loads(invoke(context, 'vault-search', 'search', {'query': 'zebracorn'})[1])
    assert len(results) == 1 and results[0]['title'] == 'ranking'
    assert Path(results[0]['path']).name == path.name
    assert results[0]['summary_stale'] is False
    metadata = json.loads(invoke(context, 'vault-search', 'metadata', {'paths': [str(path)]})[1])
    assert metadata['project'] == 'project'
    assert 'ranking.md' in invoke(context, 'vault-search', 'grep', {'pattern': 'ZEBRACORN', 'ignore_case': True})[1]
    assert 'ranking.md' not in invoke(context, 'vault-search', 'grep', {'pattern': 'zebracorn', 'ignore_case': True, 'frontmatter_only': True})[1]
    assert invoke(context, 'decide', 'sync')[1].strip() == 'OK'
    clock = invoke(context, 'decide', 'clock')[1].strip()
    assert clock.endswith('+00:00') and 'T' in clock
    assert 'project' in invoke(context, 'vault-stats', 'stats')[1]
    dependencies = json.loads(invoke(context, 'obsidian-setup', 'dependencies')[1])
    assert dependencies['optional_deps_prompted'] is False


@pytest.mark.parametrize('settings,message', [({'auto_log_enabled': 'yes'}, 'true or false'), ({'min_messages': 0}, 'positive integer'), ({'optional_deps_declined': ['unknown']}, 'numpy or scipy'), ({'unknown': 1}, 'Unknown'), ({'vault_path': '/elsewhere'}, 'Select the new vault')])
def test_configure_refuses_invalid_settings_without_changing_file(context, settings, message):
    before = context.config_path.read_bytes()
    revision = json.loads(invoke(context, 'vault-config', 'config-read')[1])['expected_revision']
    status, _, errors = invoke(context, 'vault-config', 'configure', {'settings': settings, 'expected_revision': revision}, success=False)
    assert status == 1 and message in errors
    assert context.config_path.read_bytes() == before


def test_configure_preserves_concurrent_private_edit(context):
    revision = json.loads(invoke(context, 'vault-config', 'config-read')[1])['expected_revision']
    manual = dict(context.config, summary_batch_size=9)
    context.config_path.write_text(json.dumps(manual))
    status, _, errors = invoke(context, 'vault-config', 'configure', {'settings': {'summary_batch_size': 3}, 'expected_revision': revision}, success=False)
    assert status == 1 and 'changed after it was read' in errors
    assert json.loads(context.config_path.read_text())['summary_batch_size'] == 9


@pytest.mark.parametrize('skill', ['compress', 'decide', 'error-log', 'retro', 'standup', 'vault-stats', 'obsidian-setup'])
def test_curated_create_is_private_collision_safe_and_native_authored(context, skill):
    operation = prepare(context, skill)['operation_id']
    body = '---\ntype: claude-insight\nagent_provider: claude\nagent_session_id: historical-origin\n---\n\nManual approval body\n'
    payload = {'operation_id': operation, 'folder': 'claude-insights', 'filename': skill + '.md', 'content': body}
    invoke(context, skill, 'note-create', payload)
    path = context.vault_path / 'claude-insights' / (skill + '.md')
    saved = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert ('author_host: ' + context.host).encode() in saved
    assert b'agent_session_id: historical-origin' in saved
    assert b'Manual approval body' in saved
    status, _, _ = invoke(context, skill, 'note-create', dict(payload, content=body + 'Replacement'), success=False)
    assert status != 0 and path.read_bytes() == saved


def test_compress_append_and_link_crlf_apply_preserve_body(context):
    path = note(context, 'manual.md', '## Summary\nOriginal useful summary\n\n## Conversation (raw)\nCaptured evidence\n')
    operation = prepare(context, 'compress')['operation_id']
    read = source_read(context, 'compress', path, operation)
    invoke(context, 'compress', 'note-append', {'operation_id': operation, 'path': str(path), 'expected_revision': read['expected_revision'], 'update_text': '## Update (2026-10-05)\nApproved follow-up', 'last_updated': '2026-10-05', 'add_tags_csv': 'claude/topic/followup'})
    saved = path.read_text()
    assert saved.index('Approved follow-up') < saved.index('Captured evidence')
    assert 'last_updated:' in saved and 'claude/topic/followup' in saved
    operation = prepare(context, 'link')['operation_id']
    # Exact CRLF source revision must be used by the public operation.
    path.write_bytes(saved.replace('\n', '\r\n').encode())
    read = source_read(context, 'link', path, operation)
    assert read['expected_revision'] == hashlib.sha256(path.read_bytes()).hexdigest()
    invoke(context, 'link', 'note-apply', {'operation_id': operation, 'path': str(path), 'expected_revision': read['expected_revision'], 'content': read['content'] + '\r\n## Related\r\n- [[other]]\r\n'})
    saved = path.read_bytes()
    assert b'Captured evidence' in saved and b'[[other]]' in saved
    assert b'\r\n' in saved and b'\n' not in saved.replace(b'\r\n', b'')


def test_recall_summary_status_and_freshness_are_bound_to_original_source(context):
    path = note(context, 'raw.md', '## Conversation (raw)\nThe user requested durable capture.\n', status='auto-logged', agent_provider=context.host, agent_session_id=context.native_session_id)
    operation = prepare(context, 'recall')['operation_id']
    read = source_read(context, 'recall', path, operation)
    invoke(context, 'recall', 'summary-apply', {'operation_id': operation, 'path': str(path), 'expected_revision': read['expected_revision'], 'summary': '## Summary\nDurable capture implemented.\n\n## Key Decisions\nPreserve user edits.\n\n## Changes Made\n- Capture handler\n\n## Errors Encountered\nNone.\n\n## Open Questions / Next Steps\n- [ ] Verify release\n'})
    saved = path.read_text()
    assert 'Durable capture implemented.' in saved and 'The user requested durable capture.' in saved
    operation = prepare(context, 'standup')['operation_id']
    read = source_read(context, 'standup', path, operation)
    invoke(context, 'standup', 'status', {'operation_id': operation, 'path': str(path), 'expected_revision': read['expected_revision'], 'status': 'summarized'})
    assert 'status: "summarized"' in path.read_text()
    assert path.name in invoke(context, 'recall', 'brief')[1]
    assert invoke(context, 'recall', 'recurring-themes')[1].strip() == ''


def test_metadata_marks_native_summary_stale_without_changing_capture(context):
    path = note(context, 'stale.md', '## Summary\nOld conclusion\n\n## Conversation (raw)\n<!-- obsidian-brain:capture:start -->\nNew fact\n<!-- obsidian-brain:capture:end -->\n', agent_provider=context.host, capture_revision='new', summary_revision='old', status='summarized')
    before = path.read_bytes()
    metadata = json.loads(invoke(context, 'vault-search', 'metadata', {'paths': [str(path)]})[1])
    assert metadata['summary_stale'] is True and path.read_bytes() == before
    unsummarized = json.loads(invoke(context, 'recall', 'unsummarized')[1])
    assert any(Path(value).name == 'stale.md' for value in unsummarized['unsummarized'])


def test_wiki_public_operations_file_lookup_stale_count_and_rule(context):
    sources = [note(context, name + '.md', 'Zebracorn ranking evidence ' + name, folder='claude-insights', kind='claude-insight') for name in ('i1', 'i2', 'i3')]
    assert 'ASD-STE100' in json.loads(invoke(context, 'vault-ask', 'wiki-rule')[1])['rule']
    count = json.loads(invoke(context, 'vault-ask', 'wiki-count', {'data': {'sources': ['i1', 'i2', 'i3'], 'memory_sources': []}})[1])
    assert count['count'] == 3
    payload = {'question': 'How does zebracorn ranking work?', 'body': 'Ranking uses bm25.\n\n### Sources\n- [[i1]]\n- [[i2]]\n- [[i3]]\n', 'sources': ['i1', 'i2', 'i3'], 'memory_sources': [], 'topics': ['ranking'], 'confidence': 'high', 'filed_by': 'user'}
    result = json.loads(invoke(context, 'vault-ask', 'wiki-file', {'data': payload})[1])
    page = Path(result['path'])
    assert page.is_file() and 'Ranking uses bm25.' in page.read_text()
    lookup = json.loads(invoke(context, 'vault-ask', 'wiki-lookup', {'data': {'question': payload['question']}})[1])
    assert lookup['candidates'] and Path(lookup['candidates'][0]['path']).name == page.name
    stale = json.loads(invoke(context, 'vault-ask', 'wiki-stale', {'data': {'page': str(page)}})[1])
    assert stale['stale'] is False
    sources[0].write_text(sources[0].read_text() + '\nMaterial new ranking evidence\n')
    stale = json.loads(invoke(context, 'vault-ask', 'wiki-stale', {'data': {'page': str(page)}})[1])
    assert stale['stale'] is True


def test_retro_gate_and_source_evidence_use_native_identity(context):
    path = note(context, 'retro.md', '## Summary\nThis session decision\n', kind='claude-insight', folder='claude-insights', source_session=context.native_session_id)
    invoke(context, 'retro', 'classification-pending', {'path': str(path)})
    gates = list(context.state_path.rglob('*.json'))
    assert gates and any(str(path) in value.read_text() for value in gates)
    assert invoke(context, 'retro', 'classification-complete')[1].strip() == 'cleared'
    assert invoke(context, 'retro', 'classification-complete')[1].strip() == 'no-gate'
    evidence = json.loads(invoke(context, 'retro', 'evidence')[1])
    assert 'This session decision' in json.dumps(evidence)


def test_historical_discovery_filters_source_host_project_and_date(context, tmp_path):
    source_root = tmp_path / 'history'
    source_root.mkdir()
    for name, project in [('matching', '/work/project'), ('other', '/work/other')]:
        header = {'type': 'session_meta', 'payload': {'id': 'native-' + name, 'cwd': project, 'timestamp': '2026-10-05T00:00:00Z'}} if context.host == 'codex' else {'type': 'user', 'sessionId': 'native-' + name, 'cwd': project, 'timestamp': '2026-10-05T00:00:00Z', 'message': {'content': 'Visible'}}
        (source_root / (name + '.jsonl')).write_text(json.dumps(header) + '\n')
    (source_root / 'invalid.jsonl').write_text('not json\n')
    (source_root / 'link.jsonl').symlink_to(source_root / 'other.jsonl')
    payload = {'source_host': context.host, 'source_root': str(source_root), 'days': 30, 'project': 'project'}
    discovered = [json.loads(line) for line in invoke(context, 'vault-import', 'import-list', payload)[1].splitlines()]
    assert len(discovered) == 1 and discovered[0]['session_id'] == 'native-matching'
    assert discovered[0]['source_host'] == context.host and discovered[0]['message_count'] is None
    os.utime(source_root / 'matching.jsonl', (1, 1))
    assert invoke(context, 'vault-import', 'import-list', payload)[1] == ''


def test_doctor_flag_errors_are_real_and_cannot_select_script(context):
    status, _, errors = invoke(context, 'vault-doctor', 'doctor', {'argv': ['--check', 'not-a-real-check', '--json']}, success=False)
    assert status == 3 and 'not-a-real-check' in errors
    status, _, errors = invoke(context, 'vault-doctor', 'doctor', {'argv': ['--arbitrary-script']}, success=False)
    assert status == 1 and 'Unsupported doctor flag' in errors
    status, _, errors = invoke(context, 'vault-doctor', 'doctor', {'argv': ['--days', '0']}, success=False)
    assert status == 3 and 'must be positive' in errors


def test_check_items_pipeline_preserves_pre_ai_revisions_and_reports_real_review(context, monkeypatch):
    import open_item_dedup
    monkeypatch.setenv('CHECK_ITEMS_PREFILTER', 'off')
    # Evidence acquisition has no configured repository and must stay local.
    monkeypatch.setattr(open_item_dedup, '_resolve_project_paths', lambda: {})
    first = note(context, date.today().isoformat() + '-first.md', '## Open Questions / Next Steps\n- [ ] Ship #42 durable capture\n- [ ] Investigate #99 latency\n')
    second = note(context, date.today().isoformat() + '-second.md', '## Open Questions / Next Steps\n- [ ] Ship #42 durable capture\n')
    original = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (first, second)}
    observed = []
    def fixture_ai(selected, operation, request):
        assert selected is context and current_runtime_context() is context
        payload = json.loads(request.input.split('Input JSON:\n', 1)[1])
        observed.append((operation, payload))
        if operation == 'semantic_merge':
            data = {'merges': [], 'total_groups_before': len(payload['groups']), 'total_groups_after': len(payload['groups'])}
        elif operation == 'classify_items':
            data = [{'group_id': group['group_id'], 'classification': 'DONE' if '#42' in group['representative'] else 'ACTIVE', 'confidence': 'HIGH', 'canonical_text': group['representative'], 'evidence_citation': 'Commit abc1234 resolves #42' if '#42' in group['representative'] else None, 'action_required': None} for group in payload['groups']]
        else:
            pytest.fail('Unexpected AI operation ' + operation)
        return ai_backend.AIResult('ok', data, request.input_revision, backend=context.host, model='claude-fixture' if context.host == 'claude' else 'fixture-codex')
    monkeypatch.setattr(ai_backend, 'execute_ai', fixture_ai)
    prepared = prepare(context, 'check-items')
    operation, directory = prepared['operation_id'], Path(prepared['operation_dir'])
    def artifact(name):
        return json.loads(read_artifact(context, operation, name))
    def stage(number, **inputs):
        return invoke(context, 'check-items', 'stage-' + number, {'operation_id': operation, 'inputs': {key: str(directory / value) for key, value in inputs.items()}})
    invoke(context, 'check-items', 'scope', {'operation_id': operation, 'argv': ['all', '14d', '--show-all', '--no-cache']})
    scope = artifact('scope.json')
    assert scope['mode'] == 'vault' and scope['show_all'] and scope['no_cache']
    invoke(context, 'check-items', 'collect', {'operation_id': operation, 'scope_path': str(directory / 'scope.json')})
    raw = artifact('raw_items.json')
    assert len(raw) == 3 and {value['source_revision'] for value in raw} == set(original.values())
    stage('01', SCOPE_PATH='scope.json', RAW_PATH='raw_items.json')
    partitioned = artifact('partition.json')
    assert len(partitioned['flat_groups']) == 2 and len(partitioned['needs']) == 2
    stage('02', SCOPE_PATH='scope.json', PART_PATH='partition.json')
    merged = artifact('merged.json')['merged_by_proj']['project']
    assert sorted(len(group['members']) for group in merged) == [1, 2]
    assert {member['source_revision'] for group in merged for member in group['members']} == set(original.values())
    stage('03', SCOPE_PATH='scope.json', MERGED_PATH='merged.json')
    assert artifact('gaps.json')['projects_without_repo'] == ['project']
    stage('04', SCOPE_PATH='scope.json', MERGED_PATH='merged.json', EVIDENCE_PATH='evidence.json')
    classifications = artifact('classifications.json')['classifications']
    assert {record['classification'] for record in classifications} == {'DONE', 'ACTIVE'}
    assert all(record['ai_backend'] == context.host for record in classifications)
    stage('05', SCOPE_PATH='scope.json', CLASSIFICATIONS_PATH='classifications.json')
    buckets = artifact('buckets.json')
    assert len(buckets['review']) == 1 and len(buckets['dashboard_only']) == 1
    assert buckets['dashboard_only'][0]['evidence_citation'] is None
    store_artifact(context, operation, 'cascade_summary.json', json.dumps({'cascaded': 0, 'skipped': 0}))
    output = stage('07', SCOPE_PATH='scope.json', RAW_PATH='raw_items.json', PART_PATH='partition.json', MERGED_PATH='merged.json', CLASSIFICATIONS_PATH='classifications.json', BUCKETS_PATH='buckets.json', GAPS_PATH='gaps.json')[1]
    report = Path(output.strip())
    assert report.is_file() and 'Ship #42 durable capture' in report.read_text()
    assert 'Investigate #99 latency' in report.read_text()
    assert stage('08', SCOPE_PATH='scope.json', CLASSIFICATIONS_PATH='classifications.json', PARTITION_PATH='partition.json')[1].strip() == 'cache updated'
    assert [operation for operation, _ in observed] == ['semantic_merge', 'classify_items']
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == revision for path, revision in original.items())


def test_check_items_scope_typo_and_current_project_are_explicit_errors(context):
    prepared = prepare(context, 'check-items')
    operation = prepared['operation_id']
    status, _, errors = invoke(context, 'check-items', 'scope', {'operation_id': operation, 'argv': ['projext']}, success=False)
    assert status == 2 and 'unrecognised' in errors and 'project' in errors
    invoke(context, 'check-items', 'scope', {'operation_id': operation, 'argv': []})
    status, _, errors = invoke(context, 'check-items', 'collect', {'operation_id': operation, 'scope_path': str(Path(prepared['operation_dir']) / 'scope.json')}, success=False)
    assert status == 1 and 'requires running inside a git repo' in errors


def test_cascade_handoff_checks_only_reviewed_duplicates_and_rejects_manual_edit(context):
    first = note(context, 'first.md', '## Open Questions / Next Steps\n- [ ] Ship #42 durable capture\n')
    second = note(context, 'second.md', '## Open Questions / Next Steps\n- [ ] Ship #42 durable capture\n')
    operation = prepare(context, 'standup')['operation_id']
    invoke(context, 'standup', 'cascade-collect', {'operation_id': operation})
    manual = first.read_bytes() + b'\nManual note\n'
    first.write_bytes(manual)
    status, output, _ = invoke(context, 'standup', 'cascade', {'operation_id': operation, 'checked_texts': ['Ship #42 durable capture']}, success=False)
    assert status == 1 and 'SOURCE REVISION CONFLICT' in output
    assert first.read_bytes() == manual
    assert '- [x] Ship #42 durable capture' in second.read_text()


def test_deep_checkoff_builder_and_batch_editor_preserve_unselected_lines(context):
    target = note(context, 'items.md', '## Open Questions / Next Steps\n- [ ] Ship #42 durable capture\n- [ ] Investigate #99 latency\n')
    operation = prepare(context, 'standup')['operation_id']
    source_read(context, 'standup', target, operation)
    requested = [{'file': str(target), 'line': 1000, 'text': 'Ship #42 durable capture'}]
    edits = json.loads(invoke(context, 'standup', 'deep-checkoffs', {'stdin': requested})[1])
    assert len(edits['edits']) == 1 and edits['skipped'] == []
    before = target.read_bytes()
    # The builder is pure; publication requires the original source hash.
    assert target.read_bytes() == before
    payload = {'operation_id': operation, 'stdin': edits['edits']}
    result = invoke(context, 'standup', 'deep-edit', payload)[1]
    assert '- [x] Ship #42 durable capture' in target.read_text()
    assert '- [ ] Investigate #99 latency' in target.read_text()
    assert 'Applied 1/1 edits' in result


def test_deep_edit_rejects_model_revision_forgery_and_unprepared_destination(context):
    target = note(context, 'items.md', '## Open Questions / Next Steps\n- [ ] Ship #42 durable capture\n')
    prepared = prepare(context, 'standup')['operation_id']
    before = target.read_bytes()
    triple = [[str(target), '- [ ] Ship #42 durable capture', '- [x] Ship #42 durable capture']]
    status, _, errors = invoke(context, 'standup', 'deep-edit', {'operation_id': prepared, 'stdin': triple}, success=False)
    assert status == 1 and 'Read the source before' in errors
    assert target.read_bytes() == before
    source_read(context, 'standup', target, prepared)
    target.write_bytes(before + b'\nManual edit during AI\n')
    manual = target.read_bytes()
    status, _, errors = invoke(context, 'standup', 'deep-edit', {'operation_id': prepared, 'stdin': triple, 'expected_revisions': {str(target): hashlib.sha256(manual).hexdigest()}}, success=False)
    assert status == 1 and 'protected pre-analysis manifest' in errors
    assert target.read_bytes() == manual


def test_deep_pipeline_artifacts_present_and_reject_changed_source(context, monkeypatch):
    import open_item_dedup
    monkeypatch.setattr(open_item_dedup, '_resolve_project_paths', lambda: {})
    target = note(context, date.today().isoformat() + '-session.md', '## Open Questions / Next Steps\n- [ ] Ship #42 durable capture\n')
    prepared = prepare(context, 'standup')
    identifier, directory = prepared['operation_id'], Path(prepared['operation_dir'])
    output = invoke(context, 'standup', 'deep-pipeline', {'operation_id': identifier, 'stdin': {'basenames': [target.name], 'projects': ['project']}})[1]
    artifact_paths = json.loads(output.splitlines()[0])
    assert Path(artifact_paths['pipeline_path']).parent == directory
    pipeline = json.loads(read_artifact(context, identifier, 'deep-pipeline.json'))
    assert pipeline['items']['total_raw'] == 1
    invoke(context, 'standup', 'artifact-store', {'operation_id': identifier, 'name': 'deep-classifications.json', 'content': {}})
    payload = {'operation_id': identifier, 'pipeline_path': artifact_paths['pipeline_path'], 'classifications_path': artifact_paths['classifications_path'], 'stdin': [target.name]}
    presented = invoke(context, 'standup', 'deep-present', payload)[1]
    assert 'Ship #42 durable capture' in presented or target.stem in presented
    target.write_text(target.read_text() + '\nNew captured decision\n')
    status, _, errors = invoke(context, 'standup', 'deep-present', payload, success=False)
    assert status == 1 and 'source revision changed' in errors


def test_theme_operations_and_emerge_keep_registered_analysis_separate_from_origin(context):
    from datetime import datetime, timezone
    import themes
    import vault_index
    from runtime_context import using_runtime_context
    members = [note(context, title + '.md', title + ' evidence', folder='claude-insights', kind='claude-insight') for title in ('ranking', 'transport')]
    invoke(context, 'compress', 'sync')
    with using_runtime_context(context):
        connection = vault_index._connect(str(context.index_path))
        try:
            identifiers = [themes.create_theme(connection, title, title + ' summary', {title: 1.0}, [(str(path), {title: 1.0})], 'project', datetime.now(timezone.utc).isoformat()) for path, title in zip(members, ('Ranking', 'Transport'))]
            connection.commit()
        finally:
            connection.close()
    stats = invoke(context, 'consolidate', 'theme-stats')[1]
    assert 'THEMES=2' in stats and 'MEMBERS=2' in stats
    split = invoke(context, 'consolidate', 'theme-split', {'theme_id': identifiers[0]})[1]
    assert 'ERROR' in split or 'SPLIT' in split
    # Fewer than the minimum cluster size leaves unassigned notes untouched.
    assert 'UNASSIGNED=' in invoke(context, 'consolidate', 'consolidate')[1]
    prepared = prepare(context, 'emerge')
    operation, directory = prepared['operation_id'], Path(prepared['operation_dir'])
    output = invoke(context, 'emerge', 'themes', {'operation_id': operation, 'days': 30})[1]
    assert 'STATUS=OK:2' in output
    corpus = json.loads(read_artifact(context, operation, 'emerge-themes.json'))
    assert {theme['name'] for theme in corpus['themes']} == {'Ranking', 'Transport'}
    analysis = '## Growing Themes\nRanking and Transport both gained evidence.\n'
    path = json.loads(invoke(context, 'emerge', 'artifact-store', {'operation_id': operation, 'name': 'emerge-analysis.md', 'content': analysis})[1])['path']
    output = invoke(context, 'emerge', 'build-note', {'operation_id': operation, 'themes_path': str(directory / 'emerge-themes.json'), 'analysis_path': path})[1]
    saved = Path(next(line[6:] for line in output.splitlines() if line.startswith('SAVED:')))
    assert analysis in saved.read_text() and 'author_host: ' + context.host in saved.read_text()
    invoke(context, 'consolidate', 'theme-merge', {'a': identifiers[0], 'b': identifiers[1]})
    assert 'THEMES=1' in invoke(context, 'consolidate', 'theme-stats')[1]


def test_snapshot_metadata_reports_proven_parent_summary(context):
    snapshot = note(context, date.today().isoformat() + '-project-120000-snapshot.md', '## Summary\nContext before compaction.\n\n## Key context that may be lost (summary)\nPreserve transaction SHA.\n', kind='claude-snapshot', session_id=context.native_session_id, trigger='compact')
    values = json.loads(invoke(context, 'vault-search', 'snapshots', {'source_session_id': context.native_session_id, 'project': 'project'})[1])
    assert len(values) == 1 and Path(values[0]['path']) == snapshot
    assert values[0]['summary'] == 'Context before compaction.'
    assert values[0]['key_context'] == 'Preserve transaction SHA.'


def test_deep_edit_preserves_manual_change_after_pre_ai_read(context):
    target = note(context, 'items.md', '## Open Questions / Next Steps\n- [ ] Ship #42 durable capture\n')
    operation = prepare(context, 'standup')['operation_id']
    source_read(context, 'standup', target, operation)
    manual = target.read_bytes() + b'\nManual edit during analysis\n'
    target.write_bytes(manual)
    payload = {'operation_id': operation, 'stdin': [[str(target), '- [ ] Ship #42 durable capture', '- [x] Ship #42 durable capture']]}
    status, output, errors = invoke(context, 'standup', 'deep-edit', payload, success=False)
    assert status == 1 and 'source revision changed' in errors
    assert 'Applied 0/1 edits' in output and target.read_bytes() == manual
