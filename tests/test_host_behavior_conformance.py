"""Fixed visible facts and preservation contracts, once per invoking host."""
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import time

import pytest

from parity_test_helpers import host, context, selected_host_context, host_identity_scenario

import capture
from note_transactions import NoteMutation, apply_mutations, read_revision
from runtime_context import current_runtime_context, using_runtime_context
import transcripts

GOLDEN = json.loads((Path(__file__).parent / 'fixtures/hosts/golden/visible-facts.json').read_text())


def native_rows(host, sid):
    stamp = '2026-10-05T00:00:00Z'
    messages = GOLDEN['messages']
    if host == 'claude':
        return [
            {'type': 'system', 'sessionId': sid, 'message': {'role': 'system', 'content': 'private-policy'}},
            {'type': 'user', 'uuid': 'u', 'sessionId': sid, 'timestamp': stamp,
             'message': {'role': 'user', 'content': messages[0][1]}},
            {'type': 'assistant', 'uuid': 'a', 'sessionId': sid, 'timestamp': stamp,
             'message': {'role': 'assistant', 'content': [
                 {'type': 'thinking', 'thinking': 'private-reasoning'},
                 {'type': 'text', 'text': messages[1][1]}]},
             'usage': {'input_tokens': 987654321}, 'account': 'private-account'},
        ]
    return [
        {'type': 'session_meta', 'payload': {'id': sid, 'account': 'private-account'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'developer',
            'content': [{'type': 'input_text', 'text': 'private-policy'}]}},
        {'type': 'response_item', 'payload': {'type': 'reasoning', 'summary': [], 'encrypted_content': 'private-reasoning'}},
        *[{'type': 'response_item', 'timestamp': stamp, 'payload': {'type': 'message',
            'id': ident, 'role': role, 'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}],
            'internal_chat_message_metadata_passthrough': {'turn_id': 'turn'}}}
          for ident, (role, text) in zip(('u', 'a'), messages)],
        {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'input_tokens': 987654321}}},
    ]


def source(context):
    path = Path.home() / ('.claude/projects/synthetic/synthetic.jsonl' if context.host == 'claude' else '.codex/sessions/synthetic.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in native_rows(context.host, context.native_session_id)))
    return replace(context, transcript_path=path)


def test_normalized_native_facts_match_fixed_golden(host, context):
    bound = source(context)
    batch = transcripts.read_records(bound, transcripts.SourceCursor(), time.monotonic() + 2)
    assert batch.status == 'ok'
    assert [[row.role, row.text] for row in batch.records if row.kind == 'message'] == GOLDEN['messages']
    assert batch.metadata['native_session_id'] == context.native_session_id
    serialized = json.dumps({'records': [row.__dict__ for row in batch.records], 'metadata': dict(batch.metadata), 'parser': dict(batch.parser_state)})
    for hidden in GOLDEN['forbidden']:
        assert hidden not in serialized


def test_capture_replay_preserves_manual_prose(host, context):
    bound = source(context)
    note = bound.vault_path / 'session.md'
    legacy = '---\ntype: claude-session\nsession_id: synthetic-session\nagent_provider: ' + host + '\n---\n# Existing\n' + GOLDEN['manual_prose'] + '\n'
    note.write_text(legacy)
    for _ in range(2):
        result = capture.capture_checkpoint(bound, capture.CaptureEvent('stop', note_path=note, min_messages=1), time.monotonic() + 2)
        assert result.status == 'complete'
    text = note.read_text()
    assert GOLDEN['manual_prose'] in text
    for role, message in GOLDEN['messages']:
        assert text.count(message) == 1
    assert 'agent_provider: "' + host + '"' in text
    for hidden in GOLDEN['forbidden']:
        assert hidden not in text


def test_document_cas_rejects_later_manual_edit(host, context):
    note = context.vault_path / 'decision.md'
    note.write_text('Original decision.\n')
    baseline = read_revision(context, note)
    manual = GOLDEN['manual_prose'] + '\n'
    note.write_text(manual)
    result = apply_mutations(context, [NoteMutation(note, baseline, {'document': 'Stale answer.\n'}, 'fixed-stale-operation')])
    assert result.status == 'conflict'
    assert note.read_text() == manual


def test_context_fork_and_foreign_environment_do_not_change_host(host, context, monkeypatch):
    from runtime_context import resolve_runtime_context
    original = current_runtime_context()
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'foreign-claude')
    monkeypatch.setenv('CODEX_THREAD_ID', 'foreign-codex')
    fork = resolve_runtime_context(host, context.client, {'session_id': 'child', 'cwd': str(context.worktree)},
        {'config_path': context.config_path, 'resource_root': context.resource_root})
    assert fork.host == host and fork.native_session_id == 'child'
    assert fork.session_key != context.session_key
    assert fork.vault_path == context.vault_path
    with using_runtime_context(context):
        assert current_runtime_context() is context
        with using_runtime_context(fork):
            assert current_runtime_context() is fork
        assert current_runtime_context() is context
    assert current_runtime_context() is original


def test_doctor_repair_keeps_original_backup(host, context, tmp_path):
    from vault_doctor_checks import encoding_corruption
    folder = context.vault_path / 'claude-sessions'
    folder.mkdir()
    note = folder / 'bad.md'
    raw = b'Manual prose.\xff\r\n'
    note.write_bytes(raw)
    with using_runtime_context(context):
        issues = encoding_corruption.scan(str(context.vault_path), 'claude-sessions', 'claude-insights', 0)
        results = encoding_corruption.apply(issues, str(tmp_path / 'backups'))
    assert len(results) == 1 and results[0].status == 'applied'
    assert note.read_bytes() == 'Manual prose.\ufffd\r\n'.encode()
    assert Path(results[0].backup_path).read_bytes() == raw


def test_wiki_page_index_log_publication(host, context, monkeypatch):
    import wiki
    folders = ['claude-sessions', 'claude-insights', 'claude-wiki']
    insight = context.vault_path / 'claude-insights'
    insight.mkdir()
    for name in ('one', 'two', 'three'):
        (insight / (name + '.md')).write_text('---\ntype: claude-insight\ndate: 2026-10-05\nproject: demo\n---\nFixed source.\n')
    monkeypatch.setattr(wiki, '_memory_files', lambda: [])
    monkeypatch.setattr(wiki, '_memory_listing', lambda: ([], [], host))
    payload = {'question': 'Why preserve source bytes?', 'body': 'Use a revision check.\n',
        'sources': ['one', 'two', 'three'], 'memory_sources': [], 'topics': [], 'confidence': 'high', 'filed_by': 'user'}
    with using_runtime_context(context):
        result = wiki.file_page({'vault': str(context.vault_path), 'folders': folders,
            'wiki_folder': 'claude-wiki', 'db': str(context.index_path)}, payload, date(2026, 10, 5))
    assert result['count'] == 3 and result['action'] == 'file'
    page = Path(result['path'])
    assert 'Use a revision check.' in page.read_text()
    assert page.stat().st_mode & 0o777 == 0o600
    pages = list((context.vault_path / 'claude-wiki').rglob('*.md'))
    assert len(pages) == 3  # Page, index and log.


def test_native_memory_discovery_obeys_declared_host_capability(host, context, monkeypatch):
    import memory_sources
    root = Path.home() / '.claude' / 'projects' / 'synthetic' / 'memory'
    root.mkdir(parents=True)
    note = root / 'decision.md'
    note.write_text('Synthetic memory.\n')
    errors = []
    with using_runtime_context(context):
        found = memory_sources.memory_sources(memory_sources.detect_host(), dict(context.config), errors)
    if host == 'codex':
        assert found == []
        assert len(errors) == 1 and errors[0]['unsupported'] is True
        assert errors[0]['host'] == 'codex'
    else:
        assert errors == []
        assert found == [note.resolve()]
    assert note.read_text() == 'Synthetic memory.\n'


@pytest.mark.parametrize('project_name,expected', [
    pytest.param('Parity Lab', 'parity-lab', id='spaces'),
    pytest.param('Parity_Lab', 'parity-lab', id='underscore'),
    pytest.param('Café_Lab', 'café-lab', id='unicode'),
    pytest.param('Long_' + 'Project_' * 7 + 'Name',
                 'long-' + 'project-' * 7 + 'name', id='long'),
])
def test_native_project_metadata_and_skill_filters_agree(
        context, host_identity_scenario, project_name, expected):
    import io
    import skill_procedures
    from obsidian_utils import get_session_context

    root = context.canonical_project_root.parent / project_name
    root.mkdir()
    bound = source(replace(context, canonical_project_root=root, worktree=root))
    host_identity_scenario.register(bound)
    original_key = context.session_key
    with using_runtime_context(bound):
        result = capture.capture_checkpoint(
            bound, capture.CaptureEvent('pre_compact', min_messages=1),
            time.monotonic() + 3)
        assert result.status == 'complete'
        output, errors = io.StringIO(), io.StringIO()
        assert skill_procedures.run_operation(
            bound, 'vault-stats', 'config', {}, output, errors) == 0
        config = json.loads(output.getvalue())
        assert config['project'] == expected
        assert get_session_context()['project'] == expected
        output = io.StringIO()
        assert skill_procedures.run_operation(
            bound, 'vault-stats', 'stats', {'project': config['project']},
            output, errors) == 0
        stats = json.loads(output.getvalue())
        assert stats['project']['name'] == expected
        assert stats['project']['total_notes'] == 2
        assert stats['vault_wide']['total_notes'] == 2
        output = io.StringIO()
        assert skill_procedures.run_operation(
            bound, 'vault-stats', 'stats', {}, output, errors) == 0
        assert json.loads(output.getvalue())['project']['total_notes'] == 2
        output = io.StringIO()
        assert skill_procedures.run_operation(
            bound, 'vault-stats', 'stats', {'project': 'foreign-project'},
            output, errors) == 0
        assert json.loads(output.getvalue())['project']['total_notes'] == 0
    assert bound.canonical_project_root == root
    assert bound.session_key == original_key
    notes = list((bound.vault_path / 'claude-sessions').glob('*.md'))
    assert len(notes) == 2
    for path in notes:
        assert capture._note_identity(path)['project'] == expected


@pytest.mark.parametrize('project_name,expected', [
    pytest.param('Café_Lab', 'café-lab', id='unicode'),
    pytest.param('Long_' + 'Project_' * 7 + 'Name',
                 'long-' + 'project-' * 7 + 'name', id='long'),
])
def test_native_start_index_fallback_uses_full_project_label(
        context, host_identity_scenario, project_name, expected):
    from contextlib import closing
    import sqlite3
    import native_lifecycle
    from note_transactions import connect_coordination

    root = context.canonical_project_root.parent / project_name
    root.mkdir()
    bound = replace(context, canonical_project_root=root, worktree=root)
    host_identity_scenario.register(bound)
    note = bound.vault_path / 'claude-sessions' / 'prior.md'
    note.parent.mkdir(parents=True)
    note.write_text('---\ntype: claude-session\nproject: ' + expected
                    + '\n---\n## Summary\nCorrect project context.\n')
    with closing(connect_coordination(bound)):
        pass
    bound.index_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(bound.index_path) as connection:
        connection.execute('CREATE TABLE notes(path TEXT, project TEXT, type TEXT, date TEXT, body TEXT)')
        connection.execute('INSERT INTO notes VALUES (?, ?, ?, ?, ?)',
                           (str(note), expected, 'claude-session', '2026-10-05',
                            '## Summary\nCorrect project context.\n'))
    result = native_lifecycle._context_hint(bound, time.monotonic() + 2)
    assert result is not None
    assert result['hookSpecificOutput']['additionalContext'].endswith('Correct project context.')


def test_native_project_repo_map_uses_logical_label(context, monkeypatch):
    import open_item_dedup
    root = context.canonical_project_root.parent / 'Parity_Lab'
    root.mkdir()
    (root / '.git').mkdir()
    bound = replace(context, canonical_project_root=root, worktree=root)
    monkeypatch.setattr(open_item_dedup, 'get_workspace_roots', lambda: [])
    with using_runtime_context(bound):
        assert open_item_dedup._resolve_project_paths() == {'parity-lab': str(root)}
