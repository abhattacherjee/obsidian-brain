"""Authored skills use the loaded installation and conditional shared writes."""
import io
from pathlib import Path

import pytest
import note_transactions
import skill_procedures

ROOT = Path(__file__).resolve().parents[1]
PAIRED = {'standup', 'emerge', 'recall', 'obsidian-setup', 'retro', 'vault-doctor'}


def _claude_history_source(context,monkeypatch,filename):
    if context.host!='claude':
        monkeypatch.setenv('CLAUDE_CONFIG_DIR',str(context.user_home/'historical-claude-home'))
    root=skill_procedures._approved_import_roots(context,'claude')[0]
    root.mkdir(parents=True,exist_ok=True)
    return root/filename


@pytest.fixture
def tmp_vault(selected_host_context, tmp_path, monkeypatch):
    import obsidian_utils
    import vault_index
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-home'))
    vault = selected_host_context.vault_path
    (vault / 'claude-insights').mkdir()
    (vault / 'claude-sessions').mkdir()
    monkeypatch.setattr(obsidian_utils, '_SECURE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(vault_index, '_default_db_path', lambda: str(tmp_path / 'index.sqlite3'))
    return vault


def test_registry_matches_all_authored_skills():
    discovered = {path.parent.name for path in (ROOT / 'skills').glob('*/SKILL.md')}
    assert set(skill_procedures.OPERATIONS) == discovered == set(skill_procedures.SKILLS)
    assert len(discovered) == 19
    for operations in skill_procedures.OPERATIONS.values():
        assert operations and all(callable(value) for value in operations.values())


def test_required_host_references_are_paired():
    actual = {path.parent.parent.name for path in (ROOT / 'skills').glob('*/references/host-*.md')}
    assert actual == PAIRED
    for name in PAIRED:
        for host in ('claude', 'codex'):
            assert (ROOT / 'skills' / name / 'references' / ('host-' + host + '.md')).is_file()


def test_shared_procedures_never_guess_an_installation():
    for path in (ROOT / 'skills').glob('*/SKILL.md'):
        text = path.read_text()
        assert 'known_marketplaces.json' not in text, path
        assert 'plugins/cache/' not in text, path
        assert 'default="hooks"' not in text and "default='hooks'" not in text, path
        assert '--resource-root "$OB_RESOURCE_ROOT"' in text, path
        assert '--skill-path "$OB_SKILL_PATH"' in text, path


def test_unknown_operation_does_not_execute_payload(tmp_vault):
    context = note_transactions.context_for_vault(tmp_vault)
    destination = tmp_vault / 'should-not-exist'
    stderr = io.StringIO()
    status = skill_procedures.run_operation(context, 'link', 'python',
        {'code': 'Path(' + repr(str(destination)) + ').write_text("bad")'},
        io.StringIO(), stderr)
    assert status == 2
    assert not destination.exists()
    assert 'operation_invalid' in stderr.getvalue()


def test_curated_apply_requires_preparation_revision(tmp_vault):
    context = note_transactions.context_for_vault(tmp_vault)
    destination = tmp_vault / 'claude-insights' / 'existing.md'
    original = '---\ntype: claude-insight\n---\n\nManual prose\n'
    destination.write_text(original)
    stderr = io.StringIO()
    status = skill_procedures.run_operation(context, 'link', 'note-apply',
        {'path': str(destination), 'content': '---\ntype: claude-insight\n---\n\nReplacement\n'},
        io.StringIO(), stderr)
    assert status == 1
    assert destination.read_text() == original
    assert 'before preparing edits' in stderr.getvalue()


def test_curated_apply_keeps_edit_made_after_source_read(tmp_vault):
    import json
    context = note_transactions.context_for_vault(tmp_vault)
    destination = tmp_vault / 'claude-insights' / 'existing.md'
    destination.write_text('---\ntype: claude-insight\n---\n\nOriginal\n')
    prepared = io.StringIO()
    assert skill_procedures.run_operation(context, 'link', 'prepare', {}, prepared, io.StringIO()) == 0
    operation_id = json.loads(prepared.getvalue())['operation_id']
    output = io.StringIO()
    assert skill_procedures.run_operation(context, 'link', 'note-read',
        {'path': str(destination), 'operation_id': operation_id}, output, io.StringIO()) == 0
    preparation = json.loads(output.getvalue())
    manual = '---\ntype: claude-insight\n---\n\nManual edit during analysis\n'
    destination.write_text(manual)
    output = io.StringIO()
    status = skill_procedures.run_operation(context, 'link', 'note-apply',
        {'path': str(destination), 'content': preparation['content'] + '\n## Related\n- [[target]]\n',
         'expected_revision': preparation['expected_revision'], 'operation_id': operation_id}, output, io.StringIO())
    assert status == 1
    assert destination.read_text() == manual
    assert json.loads(output.getvalue())['status'] in {'pending', 'conflict'}



def test_curated_metadata_keeps_origin_provider_separate_from_author(tmp_vault):
    from dataclasses import replace
    import json
    context = replace(note_transactions.context_for_vault(tmp_vault), native_session_id='invoking-session')
    destination = tmp_vault / 'claude-insights' / 'existing.md'
    original = '---\ntype: claude-insight\nagent_provider: claude\nagent_session_id: original-claude-session\n---\n\nOriginal\n'
    destination.write_text(original)
    prepared = io.StringIO()
    assert skill_procedures.run_operation(context, 'link', 'prepare', {}, prepared, io.StringIO()) == 0
    operation_id = json.loads(prepared.getvalue())['operation_id']
    output = io.StringIO()
    assert skill_procedures.run_operation(context, 'link', 'note-read',
        {'path': str(destination), 'operation_id': operation_id}, output, io.StringIO()) == 0
    source = json.loads(output.getvalue())
    assert skill_procedures.run_operation(context, 'link', 'note-apply',
        {'path': str(destination), 'content': source['content'] + '\n## Related\n- [[target]]\n',
         'expected_revision': source['expected_revision'], 'operation_id': operation_id},
        io.StringIO(), io.StringIO()) == 0
    content = destination.read_text()
    assert 'agent_provider: claude\n' in content
    assert 'agent_session_id: original-claude-session\n' in content
    assert 'author_host: ' + context.host + '\n' in content
    assert 'operation_id: ' + operation_id + '\n' in content


def test_payload_cannot_switch_native_context(tmp_vault):
    context = note_transactions.context_for_vault(tmp_vault)
    assert skill_procedures.run_operation(context, 'link', 'config',
        {'host': 'codex'}, io.StringIO(), io.StringIO()) == 2


def test_config_publisher_rejects_vault_destination(tmp_vault):
    from dataclasses import replace
    import hashlib
    destination = tmp_vault / 'config.json'
    original = b'{"manual":true}'
    destination.write_bytes(original)
    context = replace(note_transactions.context_for_vault(tmp_vault), config_path=destination)
    with pytest.raises(ValueError, match='outside the selected vault'):
        skill_procedures._publish_config(context, {}, hashlib.sha256(original).hexdigest())
    assert destination.read_bytes() == original


def test_native_classifier_failure_does_not_publish_heuristic(tmp_vault, monkeypatch):
    import open_item_dedup
    import check_items_cache
    context = note_transactions.context_for_vault(tmp_vault)
    group = {'group_id': 'one', 'project': 'p', 'representative': 'finish task', '_reason': 'new'}
    monkeypatch.setenv('SCOPE_PATH', '/scope.json')
    monkeypatch.setenv('MERGED_PATH', '/merged.json')
    monkeypatch.setenv('EVIDENCE_PATH', '/evidence.json')
    monkeypatch.setattr(skill_procedures, '_load_json', lambda c, p, path: {'merged_by_proj': {'p': [group]}} if path == '/merged.json' else {})
    monkeypatch.setattr(check_items_cache, 'build_classifier_provenance', lambda *args, **kwargs: {})
    monkeypatch.setattr(check_items_cache, 'load_cache', lambda: {})
    monkeypatch.setattr(open_item_dedup, 'classify_groups_with_agent', lambda *args: [])
    monkeypatch.setattr(open_item_dedup, 'get_last_classifier_mode', lambda: 'heuristic-fallback')
    monkeypatch.setattr(open_item_dedup, 'classify_groups_heuristic', lambda *args: pytest.fail('Native failure cannot become heuristic success'))
    monkeypatch.setattr(skill_procedures, '_store_json', lambda *args: pytest.fail('Failed classifier cannot publish output'))
    with pytest.raises(ValueError, match='pending'):
        skill_procedures._check_items_stage_04(context, {})


def test_historical_import_keeps_source_host_and_invoking_author(tmp_vault, tmp_path, monkeypatch):
    from dataclasses import replace
    import json
    import session_lookup
    monkeypatch.setattr(session_lookup, 'find_existing_session', lambda *args: None)
    context = replace(note_transactions.context_for_vault(tmp_vault), native_session_id='invoking-session')
    transcript = _claude_history_source(context,monkeypatch,'historic.jsonl')
    transcript.write_text(json.dumps({'type': 'user', 'sessionId': 'source-claude', 'uuid': 'human', 'message': {'content': 'Remember source facts'}}) + '\n' + json.dumps({'type': 'assistant', 'sessionId': 'source-claude', 'uuid': 'answer', 'message': {'content': 'Visible answer'}}) + '\n')
    prepared = io.StringIO()
    assert skill_procedures.run_operation(context, 'vault-import', 'prepare', {}, prepared, io.StringIO()) == 0
    identifier = json.loads(prepared.getvalue())['operation_id']
    normalized = io.StringIO()
    assert skill_procedures.run_operation(context, 'vault-import', 'import-read', {'operation_id': identifier, 'source_host': 'claude', 'source_session_id': 'source-claude', 'source_path': str(transcript)}, normalized, io.StringIO()) == 0
    source = json.loads(normalized.getvalue())
    assert [row['text'] for row in source['records']] == ['Remember source facts', 'Visible answer']
    assert skill_procedures.run_operation(context, 'vault-import', 'note-create', {'operation_id': identifier, 'source_host':'claude', 'source_session_id':'source-claude', 'filename': 'old-date-original-project.md', 'content': '---\ntype: claude-session\nagent_provider: codex\nagent_session_id: fabricated\n---\n\n## Summary\nImported facts\n'}, io.StringIO(), io.StringIO()) == 0
    content = (tmp_vault / 'claude-sessions' / 'old-date-original-project.md').read_text()
    assert 'agent_provider: claude\n' in content
    assert 'agent_session_id: source-claude\n' in content
    assert 'author_host: ' + context.host + '\n' in content
    assert 'fabricated' not in content


def test_historical_unknown_record_preserves_pending_and_no_import_artifact(tmp_vault, tmp_path, monkeypatch):
    import json
    import session_lookup
    monkeypatch.setattr(session_lookup, 'find_existing_session', lambda *args: None)
    context = note_transactions.context_for_vault(tmp_vault)
    transcript = _claude_history_source(context,monkeypatch,'unknown.jsonl')
    transcript.write_text(json.dumps({'type': 'unknown-substantive', 'sessionId': 'source'}) + '\n')
    prepared = io.StringIO()
    skill_procedures.run_operation(context, 'vault-import', 'prepare', {}, prepared, io.StringIO())
    operation = json.loads(prepared.getvalue())
    stderr = io.StringIO()
    assert skill_procedures.run_operation(context, 'vault-import', 'import-read', {'operation_id': operation['operation_id'], 'source_host': 'claude', 'source_session_id': 'source', 'source_path': str(transcript)}, io.StringIO(), stderr) == 1
    assert 'pending' in stderr.getvalue()
    assert not list(Path(operation['operation_dir']).glob('import-source-*.json'))


def test_every_authored_fixed_operation_is_registered():
    import re
    for path in (ROOT / 'skills').glob('*/SKILL.md'):
        operations = set(re.findall(r"--operation '([^']+)'", path.read_text()))
        assert operations
        assert {name for name in operations if not name.startswith('<')} <= set(skill_procedures.OPERATIONS[path.parent.name]), path


def test_compress_update_keeps_manual_edit_after_preparation(tmp_vault):
    import json
    context = note_transactions.context_for_vault(tmp_vault)
    note = tmp_vault / 'claude-insights' / 'update.md'
    note.write_text('---\ntype: claude-insight\ntags:\n  - claude/insight\n---\n\nOriginal\n')
    prepared = io.StringIO()
    skill_procedures.run_operation(context, 'compress', 'prepare', {}, prepared, io.StringIO())
    operation_id = json.loads(prepared.getvalue())['operation_id']
    output = io.StringIO()
    skill_procedures.run_operation(context, 'compress', 'note-read', {'path': str(note), 'operation_id': operation_id}, output, io.StringIO())
    source = json.loads(output.getvalue())
    manual = note.read_text() + 'Manual edit during update analysis\n'
    note.write_text(manual)
    errors = io.StringIO()
    assert skill_procedures.run_operation(context, 'compress', 'note-append', {'path': str(note), 'operation_id': operation_id, 'expected_revision': source['expected_revision'], 'update_text': '## Update (2026-10-05)\nNew analysis\n'}, io.StringIO(), errors) == 1
    assert note.read_text() == manual
    assert 'SOURCE REVISION CONFLICT' in errors.getvalue()


def test_model_artifact_cannot_replace_prepared_source_manifest(tmp_vault):
    import json
    context = note_transactions.context_for_vault(tmp_vault)
    prepared = io.StringIO()
    skill_procedures.run_operation(context, 'link', 'prepare', {}, prepared, io.StringIO())
    operation_id = json.loads(prepared.getvalue())['operation_id']
    stderr = io.StringIO()
    assert skill_procedures.run_operation(context, 'link', 'artifact-store', {'operation_id': operation_id, 'name': 'source-manifest.json', 'content': {'sources': {'note.md': 'post-AI-sha'}}}, io.StringIO(), stderr) == 1
    assert 'reserved' in stderr.getvalue()


def _prepared_checkoffs(context, vault):
    import hashlib
    import json
    from operation_state import store_artifact
    output = io.StringIO()
    assert skill_procedures.run_operation(context, 'check-items', 'prepare', {}, output, io.StringIO()) == 0
    operation = json.loads(output.getvalue())['operation_id']
    members = []
    for filename in ('primary.md', 'sibling.md', 'deselected.md'):
        path = vault / 'claude-sessions' / filename
        raw = b'---\ntype: claude-session\n---\n\n- [ ] Ship feature\n'
        path.write_bytes(raw)
        members.append({'file': filename, 'line': 5, 'text': 'Ship feature', 'source_revision': hashlib.sha256(raw).hexdigest()})
    merged = {'merged_by_proj': {'project': [{'group_id': 'selected', 'members': members[:2]}, {'group_id': 'other', 'members': members[2:]}]}}
    buckets = {'review': [{'group_id': 'selected'}, {'group_id': 'other'}]}
    store_artifact(context, operation, 'merged.json', json.dumps(merged))
    store_artifact(context, operation, 'buckets.json', json.dumps(buckets))
    return operation


def test_reviewed_checkoffs_apply_primary_and_sibling_only(tmp_vault):
    import json
    from operation_state import read_artifact
    context = note_transactions.context_for_vault(tmp_vault)
    operation = _prepared_checkoffs(context, tmp_vault)
    output = io.StringIO()
    assert skill_procedures.run_operation(context, 'check-items', 'apply-reviewed', {'operation_id': operation, 'reviewed_group_ids': ['selected']}, output, io.StringIO()) == 0
    for filename in ('primary.md', 'sibling.md'):
        content = (tmp_vault / 'claude-sessions' / filename).read_text()
        assert '- [x] Ship feature' in content
        assert 'author_host: ' + context.host in content
        assert 'operation_id: ' + operation in content
    assert '- [ ] Ship feature' in (tmp_vault / 'claude-sessions' / 'deselected.md').read_text()
    assert json.loads(output.getvalue()) == {'cascaded': 1, 'primary': 1, 'skipped': 0, 'status': 'applied',
                                          'counts':{},'unclassified_group_ids':[],'warnings':[]}
    buckets = json.loads(read_artifact(context, operation, 'buckets.json'))
    assert buckets['review'][0]['applied'] is True
    assert 'applied' not in buckets['review'][1]


def test_reviewed_checkoffs_preserve_manual_edit_during_ai(tmp_vault):
    from operation_state import read_artifact
    context = note_transactions.context_for_vault(tmp_vault)
    operation = _prepared_checkoffs(context, tmp_vault)
    destination = tmp_vault / 'claude-sessions' / 'sibling.md'
    manual = destination.read_bytes() + b'\nManual edit during AI\n'
    destination.write_bytes(manual)
    stderr = io.StringIO()
    assert skill_procedures.run_operation(context, 'check-items', 'apply-reviewed', {'operation_id': operation, 'reviewed_group_ids': ['selected']}, io.StringIO(), stderr) == 1
    assert 'SOURCE REVISION CONFLICT' in stderr.getvalue()
    assert destination.read_bytes() == manual
    assert '- [ ] Ship feature' in (tmp_vault / 'claude-sessions' / 'primary.md').read_text()
    assert b'applied' not in read_artifact(context, operation, 'buckets.json')


def test_historical_import_rejects_source_changed_after_analysis(tmp_vault, tmp_path, monkeypatch):
    import json
    import session_lookup
    monkeypatch.setattr(session_lookup, 'find_existing_session', lambda *args: None)
    context = note_transactions.context_for_vault(tmp_vault)
    source = _claude_history_source(context,monkeypatch,'historical.jsonl')
    source.write_text(json.dumps({'type': 'user', 'sessionId': 'source', 'uuid': 'human', 'message': {'content': 'Before'}}) + '\n')
    output = io.StringIO()
    skill_procedures.run_operation(context, 'vault-import', 'prepare', {}, output, io.StringIO())
    operation = json.loads(output.getvalue())['operation_id']
    assert skill_procedures.run_operation(context, 'vault-import', 'import-read', {'operation_id': operation, 'source_host': 'claude', 'source_session_id': 'source', 'source_path': str(source)}, io.StringIO(), io.StringIO()) == 0
    source.write_text(source.read_text() + json.dumps({'type': 'user', 'sessionId': 'source', 'uuid': 'later', 'message': {'content': 'After'}}) + '\n')
    stderr = io.StringIO()
    assert skill_procedures.run_operation(context, 'vault-import', 'note-create', {'operation_id': operation, 'source_host':'claude', 'source_session_id':'source', 'filename': 'imported.md', 'content': '---\ntype: claude-session\n---\n\nSummary\n'}, io.StringIO(), stderr) == 1
    assert 'changed during summary generation' in stderr.getvalue()
    assert not (tmp_vault / 'claude-sessions' / 'imported.md').exists()


@pytest.mark.parametrize('skill', sorted(skill_procedures.SKILLS))
@pytest.mark.parametrize('declaration', [None, '', 'cli', 'claude-code', 'codex-cli', 'codex-desktop'])
def test_skill_client_guard_uses_claude_constant_and_declared_codex_client(
        selected_host_context, skill, declaration):
    import os
    import re
    import subprocess
    source = (ROOT / 'skills' / skill / 'SKILL.md').read_text()
    block = re.search(r'```bash\n(.*?)\n```', source, re.S).group(1)
    # Execute the real guard alone; no native operation or AI can run.
    block = block.split('OB_SKILL_PATH=', 1)[0] + "printf '%s' \"$OB_CLIENT\""
    environment = dict(os.environ, OB_HOST=selected_host_context.host)
    environment.pop('OB_CLIENT', None)
    if declaration is not None:
        environment['OB_CLIENT'] = declaration
    result = subprocess.run(['bash', '-c', block], env=environment,
                            cwd=selected_host_context.worktree, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=5)
    valid = (declaration in (None, 'claude-code') if selected_host_context.host == 'claude'
             else declaration in ('codex-cli', 'codex-desktop'))
    if valid:
        assert result.returncode == 0, result.stderr
        assert result.stdout == ('claude-code' if selected_host_context.host == 'claude' else declaration)
        assert not result.stderr
    else:
        assert result.returncode != 0
        assert 'Current native client binding is unavailable' in result.stderr
        assert not result.stdout
    assert not list(selected_host_context.vault_path.iterdir())
    assert 'infer the frontend from transcript creation' in source
    assert 'OB_CLIENT=codex-cli' not in block


@pytest.mark.host_only('claude', reason='claude-record-format', capability='claude_native_format')
@pytest.mark.parametrize('custom_home', [False, True, 'empty'])
def test_claude_setup_reference_commands_use_selected_home_and_project(tmp_path, custom_home):
    import os
    import re
    import subprocess
    text = (ROOT / 'skills/obsidian-setup/references/host-claude.md').read_text()
    commands = re.findall(r'```bash\n(.*?)```', text, re.S)
    home = tmp_path / 'private user home'
    global_home = home / '.claude'
    global_home.mkdir(parents=True)
    sentinel = global_home / '.obsidian-brain-canary'
    sentinel.write_text('preserve original canary')
    global_rule = global_home / 'hookify.claudeception-compress-nudge.local.md'
    global_rule.write_text('preserve global rule')
    project = tmp_path / 'native project with spaces'
    project.mkdir()
    unrelated = tmp_path / 'unrelated shell directory'
    unrelated.mkdir()
    selected = tmp_path / 'custom Claude home with spaces' if custom_home else global_home
    environment = dict(os.environ, HOME=str(home))
    environment.pop("OB_CWD", None)
    import shlex
    commands[1] = "OB_CWD=" + shlex.quote(str(project)) + "\n" + commands[1]
    environment.pop('CLAUDE_CONFIG_DIR', None)
    if custom_home:
        environment['CLAUDE_CONFIG_DIR'] = '' if custom_home == 'empty' else str(selected)
    canary = subprocess.run(['bash', '-c', commands[0]], env=environment, cwd=unrelated,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    if custom_home == 'empty':
        assert canary.stdout.strip() == 'FAIL'
        assert sentinel.read_text() == 'preserve original canary'
        assert not selected.exists()
        return
    assert canary.returncode == 0 and canary.stdout.strip() == 'OK'
    assert selected.is_dir()
    assert not list(selected.glob('.obsidian-brain-canary-*'))
    assert sentinel.read_text() == 'preserve original canary'
    nudge = subprocess.run(['bash', '-c', commands[1]], env=environment, cwd=unrelated,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    assert nudge.returncode == 0 and nudge.stdout.strip() == 'CREATED'
    rules = list(project.glob('.claude/hookify.*.local.md'))
    assert len(rules) == 1
    content = rules[0].read_text()
    assert 'enabled: true\nevent: stop\n' in content and 'action: warn' in content
    assert 'Run `/compress`' in content
    assert not (unrelated / '.claude').exists()
    assert global_rule.read_text() == 'preserve global rule'
    rules[0].write_text('preserve existing project rule')
    repeated = subprocess.run(['bash', '-c', commands[1]], env=environment, cwd=unrelated,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    assert repeated.returncode == 0 and repeated.stdout.strip() == 'EXISTS'
    assert rules[0].read_text() == 'preserve existing project rule'
    assert global_rule.read_text() == 'preserve global rule'
