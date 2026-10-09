"""Project checkoffs and imported origins stay distinct within one operation."""
import io
import json
import pytest

import skill_procedures as procedures
from parity_test_helpers import host, selected_host_context, context


def call(context, skill, operation, payload):
    out, err = io.StringIO(), io.StringIO()
    code = procedures.run_operation(context, skill, operation, payload, out, err)
    assert code == 0, err.getvalue()
    return out.getvalue()


def test_same_text_cascade_changes_only_confirmed_project(context):
    operation = json.loads(call(context, 'standup', 'prepare', {}))['operation_id']
    folder = context.vault_path / context.config['sessions_folder']
    folder.mkdir(parents=True, exist_ok=True)
    notes = {}
    for project in ('first', 'second'):
        notes[project] = []
        for number in range(2):
            path = folder / (project + str(number) + '.md')
            path.write_text('---\ntype: claude-session\nproject: ' + project + '\n---\n\n## Open Questions / Next Steps\n- [ ] Fix the frobnicator bug\n')
            notes[project].append(path)
        call(context, 'standup', 'cascade-collect', {'operation_id': operation, 'project': project})
    second_before = [p.read_bytes() for p in notes['second']]
    call(context, 'standup', 'cascade', {'operation_id': operation, 'project': 'first', 'checked_texts': ['Fix the frobnicator bug']})
    assert all('- [x] Fix the frobnicator bug' in p.read_text() for p in notes['first'])
    assert [p.read_bytes() for p in notes['second']] == second_before
    call(context, 'standup', 'cascade', {'operation_id': operation, 'project': 'second', 'checked_texts': ['Fix the frobnicator bug']})
    assert all('- [x] Fix the frobnicator bug' in p.read_text() for p in notes['second'])


def test_two_import_sources_keep_their_own_full_origin_in_one_operation(context):
    from runtime_context import historical_source_roots
    from obsidian_utils import parse_frontmatter_field
    operation = json.loads(call(context, 'vault-import', 'prepare', {}))['operation_id']
    root = historical_source_roots('claude')[0] / 'synthetic-history'
    root.mkdir(parents=True, exist_ok=True)
    for identity in ('original-A', 'original-B'):
        source = root / (identity + '.jsonl')
        source.write_text(json.dumps({'type': 'user', 'sessionId': identity, 'uuid': identity,
            'message': {'content': 'Facts for ' + identity}}) + '\n')
        call(context, 'vault-import', 'import-read', {'operation_id': operation, 'source_host': 'claude',
             'source_session_id': identity, 'source_path': str(source)})
    for identity in ('original-A', 'original-B'):
        call(context, 'vault-import', 'note-create', {'operation_id': operation, 'source_host': 'claude',
             'source_session_id': identity, 'filename': identity + '.md',
             'content': '---\ntype: claude-session\n---\n\n## Summary\nFacts for ' + identity + '\n'})
        path = context.vault_path / context.config['sessions_folder'] / (identity + '.md')
        text = path.read_text()
        assert parse_frontmatter_field(text, 'agent_provider') == 'claude'
        assert parse_frontmatter_field(text, 'agent_session_id') == identity
        assert parse_frontmatter_field(text, 'session_id') == identity
        assert parse_frontmatter_field(text, 'author_host') == context.host
        assert 'Facts for ' + identity in text


def test_trusted_native_home_alias_is_resolved_but_children_still_refused(context, monkeypatch):
    from dataclasses import replace
    from runtime_context import historical_source_roots
    alias = context.user_home / 'approved-native-home-alias'
    context.native_home.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(context.native_home, target_is_directory=True)
    selected = replace(context, native_home=alias)
    roots = procedures._approved_import_roots(selected, context.host)
    assert all(root.parent == context.native_home.resolve() for root in roots)
    root = roots[0]
    root.mkdir(parents=True, exist_ok=True)
    source = root / 'record.jsonl'
    source.write_text('{"type":"metadata"}\n')
    assert procedures._historical_source_bytes(selected, context.host, source) == source.read_bytes()
    nested = root / 'selected'; nested.mkdir()
    nested_source = nested / 'record.jsonl'; nested_source.write_bytes(source.read_bytes())
    real = root / 'relocated'; nested.rename(real); nested.symlink_to(real, target_is_directory=True)
    import pytest
    with pytest.raises(ValueError, match='containment|regular-file'):
        procedures._historical_source_bytes(selected, context.host, nested_source)


def test_import_read_accepts_selected_home_alias_without_widening_roots(context):
    alias = context.user_home / 'history-home-alias'
    context.native_home.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(context.native_home, target_is_directory=True)
    root_name = 'projects' if context.host == 'claude' else 'sessions'
    root = context.native_home / root_name
    root.mkdir(parents=True, exist_ok=True)
    identity = context.native_session_id
    source = root / 'alias-source.jsonl'
    if context.host == 'claude':
        rows = [{'type': 'user', 'sessionId': identity, 'uuid': 'source-one', 'message': {'content': 'Visible synthetic fact'}}]
    else:
        rows = [{'type': 'session_meta', 'payload': {'id': identity}},
                {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Visible synthetic fact', 'local_images': [], 'local_audio': [], 'text_elements': []}}]
    source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    operation = json.loads(call(context, 'vault-import', 'prepare', {}))['operation_id']
    data = json.loads(call(context, 'vault-import', 'import-read', {'operation_id': operation,
        'source_host': context.host, 'source_session_id': identity,
        'source_path': str(alias / root_name / source.name)}))
    assert data['source_path'] == str(source.resolve())
    assert data['records'] and data['source_session_id'] == identity


def test_upgrade_batch_resolves_vault_relative_paths_before_ai(context, monkeypatch):
    import obsidian_utils
    folder = context.vault_path / context.config['sessions_folder']
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'selected.md'
    path.write_text('---\nstatus: auto-logged\n---\nFacts\n')
    observed = []
    monkeypatch.setattr(obsidian_utils, 'upgrade_batch', lambda paths, *args: observed.append(paths) or {'ok': True})
    for requested in (str(path.relative_to(context.vault_path)), str(path)):
        call(context, 'recall', 'upgrade-batch', {'paths': [requested], 'project': 'synthetic'})
        assert observed[-1] == [str(path)]
    for requested in ('../outside.md', str(context.vault_path.parent / 'outside.md')):
        out, err = io.StringIO(), io.StringIO()
        before = len(observed)
        code = procedures.run_operation(context, 'recall', 'upgrade-batch', {'paths': [requested]}, out, err)
        assert code != 0
        assert len(observed) == before
    assert path.read_text().endswith('Facts\n')


def test_public_import_read_rejects_same_target_project_alias(context):
    root_name = 'projects' if context.host == 'claude' else 'sessions'
    root = context.native_home / root_name
    real = root / 'real-project'; real.mkdir(parents=True, exist_ok=True)
    identity = context.native_session_id
    if context.host == 'claude':
        rows = [{'type': 'user', 'sessionId': identity, 'uuid': 'one', 'message': {'content': 'Synthetic fact'}}]
    else:
        rows = [{'type': 'session_meta', 'payload': {'id': identity}},
                {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Synthetic fact', 'local_images': [], 'local_audio': [], 'text_elements': []}}]
    source = real / 'source.jsonl'
    source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    original = source.read_bytes()
    alias = root / 'alias-project'; alias.symlink_to(real, target_is_directory=True)
    operation = json.loads(call(context, 'vault-import', 'prepare', {}))['operation_id']
    out, err = io.StringIO(), io.StringIO()
    assert procedures.run_operation(context, 'vault-import', 'import-read', {
        'operation_id': operation, 'source_host': context.host,
        'source_session_id': identity, 'source_path': str(alias / source.name)}, out, err) != 0
    assert 'symbolic links below its trusted root' in err.getvalue()
    assert not out.getvalue()
    assert not list(context.state_path.rglob('import-source-*.json'))
    assert source.read_bytes() == original
    # A real native path is still usable with the same actor and source bytes.
    ready = json.loads(call(context, 'vault-import', 'import-read', {
        'operation_id': operation, 'source_host': context.host,
        'source_session_id': identity, 'source_path': str(source)}))
    assert ready['source_session_id'] == identity and ready['records']


def test_public_import_read_rejects_same_bytes_moved_parent_alias(context):
    root_name = 'projects' if context.host == 'claude' else 'sessions'
    root = context.native_home / root_name
    selected = root / 'selected-project'; selected.mkdir(parents=True, exist_ok=True)
    source = selected / 'source.jsonl'; source.write_text('{"type":"metadata"}\n')
    original = source.read_bytes()
    moved = root / 'moved-project'; selected.rename(moved)
    selected.symlink_to(moved, target_is_directory=True)
    operation = json.loads(call(context, 'vault-import', 'prepare', {}))['operation_id']
    out, err = io.StringIO(), io.StringIO()
    assert procedures.run_operation(context, 'vault-import', 'import-read', {
        'operation_id': operation, 'source_host': context.host,
        'source_session_id': context.native_session_id, 'source_path': str(source)}, out, err) != 0
    assert 'symbolic links below its trusted root' in err.getvalue()
    assert source.read_bytes() == original and not out.getvalue()
    assert not list(context.state_path.rglob('import-source-*.json'))


@pytest.mark.parametrize('outside_home_alias', [False, True])
def test_public_import_read_rejects_project_alias_that_returns_to_native_home(context, outside_home_alias):
    root_name = 'projects' if context.host == 'claude' else 'sessions'
    root = context.native_home / root_name
    real = root / 'real-project'; real.mkdir(parents=True, exist_ok=True)
    identity = context.native_session_id
    if context.host == 'claude':
        rows = [{'type': 'user', 'sessionId': identity, 'uuid': 'one', 'message': {'content': 'Synthetic fact'}}]
    else:
        rows = [{'type': 'session_meta', 'payload': {'id': identity}},
                {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Synthetic fact', 'local_images': [], 'local_audio': [], 'text_elements': []}}]
    source = real / 'source.jsonl'
    source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    original = source.read_bytes()
    loop = root / 'loop-to-home'; loop.symlink_to(context.native_home, target_is_directory=True)
    requested = loop / root_name / real.name / source.name
    if outside_home_alias:
        alias = context.user_home / 'outside-trusted-home-alias'
        alias.symlink_to(context.native_home, target_is_directory=True)
        requested = alias / root_name / loop.name / root_name / real.name / source.name
    operation = json.loads(call(context, 'vault-import', 'prepare', {}))['operation_id']
    out, err = io.StringIO(), io.StringIO()
    assert procedures.run_operation(context, 'vault-import', 'import-read', {
        'operation_id': operation, 'source_host': context.host,
        'source_session_id': identity, 'source_path': str(requested)}, out, err) != 0
    assert 'symbolic links below its trusted root' in err.getvalue()
    assert not out.getvalue()
    assert not list(context.state_path.rglob('import-source-*.json'))
    assert source.read_bytes() == original


def test_cascade_requires_collected_project_but_accepts_collected_empty_project(context):
    operation = json.loads(call(context, 'standup', 'prepare', {}))['operation_id']
    call(context, 'standup', 'cascade-collect', {'operation_id': operation, 'project': 'collected-empty'})
    out, err = io.StringIO(), io.StringIO()
    assert procedures.run_operation(context, 'standup', 'cascade', {
        'operation_id': operation, 'project': 'never-collected', 'checked_texts': ['Confirmed task']}, out, err) != 0
    assert 'collected' in err.getvalue()
    assert not list(context.vault_path.rglob('*.md'))
    assert call(context, 'standup', 'cascade', {
        'operation_id': operation, 'project': 'collected-empty', 'checked_texts': []})


def test_import_dotdot_is_refused_before_private_source_read(context, monkeypatch):
    from runtime_context import historical_source_roots
    root = historical_source_roots('claude')[0]
    project = root / 'dotdot-probe'
    project.mkdir(parents=True, exist_ok=True)
    outside = root.parent / 'outside-dotdot.jsonl'
    outside.write_text(json.dumps({'type': 'user', 'sessionId': 'dotdot-source',
        'uuid': 'dotdot-message', 'message': {'content': 'Disposable private fact'}}) + '\n')
    operation = json.loads(call(context, 'vault-import', 'prepare', {}))['operation_id']
    attempted = []
    def observed_private_read(*args, **kwargs):
        attempted.append(args[2])
        return outside.read_bytes()
    monkeypatch.setattr(procedures, '_historical_source_bytes', observed_private_read)
    output, errors = io.StringIO(), io.StringIO()
    code = procedures.run_operation(context, 'vault-import', 'import-read', {
        'operation_id': operation, 'source_host': 'claude',
        'source_session_id': 'dotdot-source',
        'source_path': str(project / '..' / '..' / outside.name)}, output, errors)
    assert attempted == []
    assert code != 0 and 'remain inside' in errors.getvalue()
    assert 'Disposable private fact' not in output.getvalue() + errors.getvalue()


def _prepare_import_source(context, identity):
    from runtime_context import historical_source_roots
    root = historical_source_roots('claude')[0] / 'dogfood-import'
    root.mkdir(parents=True, exist_ok=True)
    source = root / (identity + '.jsonl')
    source.write_text(json.dumps({'type': 'user', 'sessionId': identity,
        'uuid': identity + '-message', 'message': {'content': 'Import facts ' + identity}}) + '\n')
    operation = json.loads(call(context, 'vault-import', 'prepare', {}))['operation_id']
    payload = {'operation_id': operation, 'source_host': 'claude',
               'source_session_id': identity, 'source_path': str(source)}
    call(context, 'vault-import', 'import-read', payload)
    return payload


def test_import_replay_finds_saved_note_after_index_publication_crash(context, monkeypatch):
    import vault_index
    source = _prepare_import_source(context, 'crash-import')
    real_index_note = vault_index.index_note
    def crash_after_note(*args):
        raise RuntimeError('Synthetic crash after visible note publication')
    monkeypatch.setattr(vault_index, 'index_note', crash_after_note)
    payload = dict(source, filename='original-import.md', content='---\ntype: claude-session\n---\n\n## Summary\nImported facts.\n')
    output, errors = io.StringIO(), io.StringIO()
    with pytest.raises(RuntimeError, match='Synthetic crash'):
        procedures.run_operation(context, 'vault-import', 'note-create', payload, output, errors)
    path = context.vault_path / context.config['sessions_folder'] / 'original-import.md'
    assert path.is_file()
    manual = path.read_bytes() + b'\nManual imported-note edit\n'
    path.write_bytes(manual)
    monkeypatch.setattr(vault_index, 'index_note', real_index_note)
    replay = json.loads(call(context, 'vault-import', 'import-read', source))
    assert replay['status'] == 'skipped' and replay['existing_path'] == str(path)
    alternate = dict(payload, filename='duplicate-import.md')
    assert json.loads(call(context, 'vault-import', 'note-create', alternate))['status'] == 'skipped'
    assert not path.with_name('duplicate-import.md').exists()
    assert path.read_bytes() == manual


def test_concurrent_import_creates_only_one_full_source_identity(context):
    from runtime_context import using_runtime_context
    from concurrent.futures import ThreadPoolExecutor
    import threading
    source = _prepare_import_source(context, 'concurrent-import')
    barrier = threading.Barrier(2)
    def publish(filename):
        with using_runtime_context(context):
            barrier.wait(timeout=5)
            return procedures._import_create(context, dict(source, filename=filename,
                content='---\ntype: claude-session\n---\n\n## Summary\nOne imported origin.\n'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, ['import-a.md', 'import-b.md']))
    assert results == [0, 0]
    paths = list((context.vault_path / context.config['sessions_folder']).glob('import-*.md'))
    assert len(paths) == 1
    assert 'agent_session_id: concurrent-import' in paths[0].read_text()
