"""Real native child capture feeds the invoking host's recall summary pipeline."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import ai_backend
import obsidian_utils
from runtime_context import resolve_runtime_context, using_runtime_context

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def selected_host_context(selected_host_context, host):
    original = selected_host_context
    directory = original.native_home / ('projects' if host == 'claude' else 'sessions') / 'synthetic'
    directory.mkdir(parents=True)
    source = directory / 'synthetic.jsonl'
    messages = [('user', 'Capture the first synthetic fact.'),
                ('assistant', 'Synthetic response.'),
                ('user', 'Capture the second synthetic fact.'),
                ('user', 'Keep all synthetic facts.')]
    rows = []
    if host == 'codex':
        rows.append({'type': 'session_meta', 'payload': {'id': original.native_session_id}})
    for index, (role, text) in enumerate(messages):
        if host == 'claude':
            rows.append({'type': role, 'sessionId': original.native_session_id,
                         'uuid': 'synthetic-message-' + str(index),
                         'message': {'role': role, 'content': text}})
        else:
            rows.append({'type': 'response_item', 'payload': {'type': 'message',
                'id': 'synthetic-message-' + str(index), 'role': role,
                'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}],
                'internal_chat_message_metadata_passthrough': {'turn_id': 'synthetic-turn'}}})
    source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    selected = resolve_runtime_context(host, original.client,
        {'session_id': original.native_session_id, 'cwd': str(original.worktree),
         'transcript_path': str(source)},
        {'config_path': original.config_path, 'resource_root': ROOT,
         'index_path': original.index_path, 'state_path': original.state_path})
    with using_runtime_context(selected):
        yield selected


@pytest.mark.parametrize("skill", ["recall", "standup"])
def test_snapshot_e2e_pipeline(selected_host_context, monkeypatch, skill):
    context = selected_host_context
    before_source = context.transcript_path.read_bytes()
    def child(event):
        command = [sys.executable, str(ROOT / 'hooks' / 'brain_cli.py'),
            '--host', context.host, '--client', context.client, '--event', event,
            '--config', str(context.config_path), '--resource-root', str(context.resource_root),
            '--index', str(context.index_path), '--state', str(context.state_path), 'hook']
        result = subprocess.run(command, input=json.dumps({
            'session_id': context.native_session_id, 'cwd': str(context.worktree),
            'transcript_path': str(context.transcript_path), 'trigger': 'manual'}),
            env=dict(os.environ, CLAUDE_CODE_SESSION_ID='foreign-inherited-id',
                     CODEX_THREAD_ID='foreign-inherited-thread'),
            cwd=context.worktree, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ''
    child('pre_compact')
    child('session_end')
    snapshots = list(context.vault_path.rglob('*snapshot*.md'))
    sessions = [path for path in context.vault_path.rglob('*.md') if path not in snapshots]
    assert len(snapshots) == len(sessions) == 1
    snapshot, session = snapshots[0], sessions[0]
    for path in (snapshot, session):
        content = path.read_text()
        assert obsidian_utils.parse_frontmatter_field(content, 'agent_provider') == context.host
        assert obsidian_utils.parse_frontmatter_field(content, 'agent_session_id') == context.native_session_id
        assert obsidian_utils.parse_frontmatter_field(content, 'project') == context.canonical_project_root.name
        assert 'Keep all synthetic facts.' in content
        assert 'foreign-inherited' not in content
    assert obsidian_utils.parse_frontmatter_field(snapshot.read_text(), 'parent_session') == '[[' + session.stem + ']]'
    queue = json.loads(obsidian_utils.find_unsummarized_notes(str(context.vault_path), 'claude-sessions',
                                                 context.canonical_project_root.name))
    assert set(queue['unsummarized']) == {str(snapshot), str(session)}
    seen = []
    summary = ('## Summary\nNative pipeline summary.\n\n## Key Decisions\n- Kept facts.\n\n'
               '## Changes Made\n- Synthetic test.\n\n## Errors Encountered\nNone.\n\n'
               '## Open Questions / Next Steps\nNone.\n\nIMPORTANCE: 5\n')
    def execute(ctx, operation, request):
        assert ctx is context
        seen.append(operation)
        data = {1: summary} if operation == 'session_summaries' else summary
        return ai_backend.AIResult('ok', data, request.input_revision,
                                   backend=context.host, model='actual-synthetic-model')
    monkeypatch.setattr(ai_backend, 'execute_ai', execute)
    from skill_procedures import run_operation
    stdout, stderr = io.StringIO(), io.StringIO()
    result = run_operation(context, skill, 'upgrade-batch', {
        'paths': queue['unsummarized'],
        'project': context.canonical_project_root.name,
    }, stdout, stderr)
    assert result == 0, stderr.getvalue()
    upgrades = json.loads(stdout.getvalue())
    assert {row['path'] for row in upgrades} == {str(snapshot), str(session)}
    assert all(row['status'].startswith('Upgraded ') for row in upgrades), upgrades
    for path in (snapshot, session):
        fields = path.read_text()
        assert obsidian_utils.parse_frontmatter_field(fields, 'status') == 'summarized'
        assert obsidian_utils.parse_frontmatter_field(fields, 'summary_revision') == obsidian_utils.parse_frontmatter_field(fields, 'capture_revision')
        assert 'Keep all synthetic facts.' in fields
    assert seen == ['snapshot_summary', 'session_summaries']
    brief = obsidian_utils.build_context_brief(str(context.vault_path), 'claude-sessions',
                        'claude-insights', context.canonical_project_root.name)
    assert 'Native pipeline summary.' in brief
    assert context.transcript_path.read_bytes() == before_source
