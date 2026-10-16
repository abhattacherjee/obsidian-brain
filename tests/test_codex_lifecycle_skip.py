"""Explicit native lifecycle selection resists inherited markers and foreign roots."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from test_snapshot_e2e import selected_host_context

ROOT = Path(__file__).resolve().parents[1]


def owned_state(context):
    # Scope the no-write claim to plugin/native state. Apple Python can create
    # unrelated stdlib bytecode in HOME/Library/Caches before the hook runs.
    roots = (context.native_home, context.state_path, context.index_path.parent,
             context.user_home / '.local/state/obsidian-brain', context.vault_path, context.config_path)
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for root in roots for path in ((root,) if root.is_file() else root.rglob('*'))
            if path.is_file()}


def invoke_hook(context, command, source, env):
    return subprocess.run(command, input=json.dumps({
        'session_id': context.native_session_id, 'cwd': str(context.worktree),
        'transcript_path': str(source), 'trigger': 'manual'}),
        text=True, capture_output=True, env=env, cwd=context.worktree, timeout=5)


@pytest.mark.parametrize('script,event', [
    ('obsidian_session_log.py', 'session_end'),
    ('obsidian_session_hint.py', 'session_start'),
    ('obsidian_context_snapshot.py', 'pre_compact'),
    ('obsidian_retro_gate.py', 'stop')])
@pytest.mark.parametrize('foreign_source,foreign_markers', [(False, False), (False, True),
                                                           (True, False), (True, True)])
def test_lifecycle_host_guard(selected_host_context, tmp_path, script, event, foreign_source, foreign_markers):
    context = selected_host_context
    env = dict(os.environ)
    if foreign_markers:
        env.update(CLAUDE_CODE_SESSION_ID='foreign-session', CODEX_THREAD_ID='foreign-thread')
    else:
        env.pop('CLAUDE_CODE_SESSION_ID', None)
        env.pop('CODEX_THREAD_ID', None)
    source = context.transcript_path
    if foreign_source:
        source = tmp_path / 'unselected-native-home' / 'foreign.jsonl'
        source.parent.mkdir()
        source.write_bytes(context.transcript_path.read_bytes())
        command = [sys.executable, str(ROOT / 'hooks' / 'brain_cli.py'),
            '--host', context.host, '--client', context.client, '--event', event,
            '--config', str(context.config_path), '--resource-root', str(context.resource_root),
            '--index', str(context.index_path), '--state', str(context.state_path), 'hook']
    else:
        command = [sys.executable, str(ROOT / 'tests' / 'native_hook_test_driver.py'),
            '--host', context.host, '--client', context.client,
            '--session-id', context.native_session_id, '--cwd', str(context.worktree),
            '--vault', str(context.vault_path), '--config', str(context.config_path),
            '--resource-root', str(context.resource_root), '--index', str(context.index_path),
            '--state', str(context.state_path), '--transcript', str(source), script]
    if not foreign_source:
        command.insert(-1, '--recover-deferred')
    state_before = owned_state(context)
    result = invoke_hook(context, command, source, env)
    assert result.returncode == 0, result.stderr
    notes = list(context.vault_path.rglob('*.md'))
    if foreign_source:
        assert notes == []
        assert owned_state(context) == state_before
        assert result.stdout == ''
        assert json.loads(result.stderr)['code'] == 'transcript_outside_host'
    else:
        proof = json.loads(next(line.split(':', 1)[1] for line in result.stderr.splitlines()
                                if line.startswith('NATIVE_CONTEXT_PROOF:')))
        assert proof['host'] == context.host
        assert proof['native_session_id'] == context.native_session_id
        assert proof['mutation_contexts'], result.stderr
        snapshots = [path for path in notes if '-snapshot-' in path.name]
        parents = [path for path in notes if path not in snapshots]
        expects_snapshot = event == 'pre_compact' or (context.host == 'codex' and event == 'session_end')
        assert len(parents) == 1
        assert len(snapshots) == int(expects_snapshot)
        if context.host == 'codex' and event == 'session_end':
            snapshot = snapshots[0].read_text()
            assert 'trigger: "session_end"' in snapshot
            assert 'trigger: "clear"' not in snapshot
            assert 'source_session: "' + context.native_session_id + '"' in snapshot
            assert 'agent_session_id: "' + context.native_session_id + '"' in snapshot
            assert 'parent_session: "[[' + parents[0].stem + ']]"' in snapshot
            assert 'Capture the first synthetic fact.' in snapshot
        assert all('foreign-session' not in path.read_text() and 'foreign-thread' not in path.read_text()
                   for path in notes)
    assert source.read_bytes() == context.transcript_path.read_bytes()


@pytest.mark.parametrize('recover', [False, True])
def test_expired_start_retains_owned_input_then_next_stop_publishes(selected_host_context, recover):
    context = selected_host_context
    before = context.transcript_path.read_bytes()
    command = [sys.executable, str(ROOT / 'tests' / 'native_hook_test_driver.py'),
        '--host', context.host, '--client', context.client, '--session-id', context.native_session_id,
        '--cwd', str(context.worktree), '--vault', str(context.vault_path), '--config', str(context.config_path),
        '--resource-root', str(context.resource_root), '--index', str(context.index_path),
        '--state', str(context.state_path), '--transcript', str(context.transcript_path),
        '--expire-first-capture', 'obsidian_session_hint.py']
    if recover:
        command.insert(-1, '--recover-deferred')
    result = invoke_hook(context, command, context.transcript_path, dict(os.environ))
    assert result.returncode == 0, result.stderr
    proof = json.loads(next(line.split(':', 1)[1] for line in result.stderr.splitlines()
                            if line.startswith('NATIVE_CONTEXT_PROOF:')))
    assert proof['initial_mutation_count'] == 0 and proof['recovery_attempted'] is recover
    assert proof['source_path'] == str(context.transcript_path)
    if not recover:
        assert proof['capture_results'][0]['status'] == 'pending'
        assert proof['capture_results'][0]['warnings'] == ['Capture deadline reached']
        assert proof['capture_results'][0]['context']['native_session_id'] == context.native_session_id
        assert proof['mutation_contexts'] == []
        assert list(context.vault_path.rglob('*.md')) == []
        assert context.transcript_path.read_bytes() == before
        return
    first, last = proof['capture_results']
    assert first['status'] == 'pending' and first['warnings'] == ['Capture deadline reached']
    assert last['event'] == 'stop' and last['status'] == 'complete' and last['applied_revision']
    assert first['context'] == last['context'] == proof['mutation_contexts'][0]
    assert first['context']['native_session_id'] == context.native_session_id
    assert first['context']['host'] == context.host and first['context']['client'] == context.client
    assert first['source_revision'] == last['source_revision'] == proof['source_revision']
    notes = list(context.vault_path.rglob('*.md'))
    assert len(notes) == 1
    assert 'Capture the first synthetic fact.' in notes[0].read_text()
    assert 'Keep all synthetic facts.' in notes[0].read_text()
    assert context.transcript_path.read_bytes() == before


def test_early_transcript_deadline_replays_precompact_fixture(selected_host_context):
    context = selected_host_context
    before = context.transcript_path.read_bytes()
    command = [sys.executable, str(ROOT / 'tests/native_hook_test_driver.py'),
        '--host', context.host, '--client', context.client, '--session-id', context.native_session_id,
        '--cwd', str(context.worktree), '--vault', str(context.vault_path), '--config', str(context.config_path),
        '--resource-root', str(context.resource_root), '--index', str(context.index_path),
        '--state', str(context.state_path), '--transcript', str(context.transcript_path),
        '--expire-first-transcript', '--recover-deferred', 'obsidian_context_snapshot.py']
    result = invoke_hook(context, command, context.transcript_path, dict(os.environ))
    assert result.returncode == 0, result.stderr
    proof = json.loads(next(line.split(':', 1)[1] for line in result.stderr.splitlines()
                            if line.startswith('NATIVE_CONTEXT_PROOF:')))
    first, last = proof['capture_results']
    assert first['status'] == 'pending' and first['event'] == 'pre_compact'
    assert set(first['warnings']) == {'Transcript deadline reached before reading',
                                      'Native transcript identity is unverified'}
    assert proof['initial_mutation_count'] == 0 and proof['recovery_attempted']
    assert proof['replay_scope'] == 'unit-fixture-event-replay-not-native-L3'
    assert last['event'] == 'pre_compact' and last['status'] == 'complete'
    assert last['applied_revision'] and proof['mutation_contexts']
    snapshots = list(context.vault_path.rglob('*snapshot*.md'))
    parents = [p for p in context.vault_path.rglob('*.md') if p not in snapshots]
    assert len(snapshots) == len(parents) == 1
    text = snapshots[0].read_text()
    assert 'source_session: "' + context.native_session_id + '"' in text
    assert 'parent_session: "[[' + parents[0].stem + ']]"' in text
    assert 'Keep all synthetic facts.' in text
    assert context.transcript_path.read_bytes() == before


@pytest.mark.parametrize('change', ['source', 'config', 'context', 'input-loss', 'unsupported', 'unverified-only'])
def test_fixture_replay_refuses_changed_or_unqualified_input(selected_host_context, change):
    from native_hook_test_driver import fixture_replay_baseline, qualify_fixture_replay
    context = selected_host_context
    baseline = fixture_replay_baseline(context)
    pending = {'status': 'pending', 'pending_sources': 1, 'loss_of_input': False,
               'applied_revision': None, 'warnings': ['Transcript deadline reached before reading',
                    'Native transcript identity is unverified'], 'context': dict(baseline['context']),
               'source_revision': hashlib.sha256(baseline['source'][0]).hexdigest()}
    if change == 'source':
        context.transcript_path.write_bytes(context.transcript_path.read_bytes() + b'\n')
    elif change == 'config':
        context.config_path.write_bytes(context.config_path.read_bytes() + b'\n')
    elif change == 'context':
        pending['context']['native_session_id'] = 'another-full-session'
    elif change == 'input-loss':
        pending['loss_of_input'] = True
    elif change == 'unsupported':
        pending['warnings'].append('Unknown substantive transcript record')
    else:
        pending['warnings'] = ['Native transcript identity is unverified']
    with pytest.raises(AssertionError):
        qualify_fixture_replay(context, pending, baseline)
    assert not list(context.vault_path.rglob('*.md'))


@pytest.mark.parametrize('damage', ['wrong-id', 'malformed'])
def test_fixture_replay_requires_actual_complete_native_identity(selected_host_context, damage):
    from native_hook_test_driver import fixture_replay_baseline
    context = selected_host_context
    if damage == 'wrong-id':
        text = context.transcript_path.read_text().replace(context.native_session_id, 'another-full-session')
        context.transcript_path.write_text(text)
    else:
        context.transcript_path.write_text(context.transcript_path.read_text() + '{unfinished')
    with pytest.raises(AssertionError):
        fixture_replay_baseline(context)
    assert not list(context.vault_path.rglob('*.md'))
