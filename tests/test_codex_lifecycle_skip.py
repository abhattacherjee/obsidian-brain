"""Explicit native lifecycle selection resists inherited markers and foreign roots."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from test_snapshot_e2e import selected_host_context

ROOT = Path(__file__).resolve().parents[1]


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
    result = subprocess.run(command, input=json.dumps({
        'session_id': context.native_session_id, 'cwd': str(context.worktree),
        'transcript_path': str(source), 'trigger': 'manual'}),
        text=True, capture_output=True, env=env, cwd=context.worktree, timeout=5)
    assert result.returncode == 0, result.stderr
    notes = list(context.vault_path.rglob('*.md'))
    if foreign_source:
        assert notes == []
        assert result.stdout == ''
        assert json.loads(result.stderr)['code'] == 'transcript_outside_host'
    else:
        proof = json.loads(next(line.split(':', 1)[1] for line in result.stderr.splitlines()
                                if line.startswith('NATIVE_CONTEXT_PROOF:')))
        assert proof['host'] == context.host
        assert proof['native_session_id'] == context.native_session_id
        assert proof['mutation_contexts']
        assert len(notes) == (2 if event == 'pre_compact' else 1)
        assert all('foreign-session' not in path.read_text() and 'foreign-thread' not in path.read_text()
                   for path in notes)
    assert source.read_bytes() == context.transcript_path.read_bytes()
