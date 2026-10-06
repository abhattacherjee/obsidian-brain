"""Behavioral controls back private publication and shared error contracts."""
import hashlib
import json
import time
from pathlib import Path

import pytest
import ai_backend
from ai_adapters import claude,codex
from test_host_codex_ai_contract import codex_context


def test_native_json_recursion_returns_structured_failure(selected_host_context,monkeypatch):
    adapter=claude if selected_host_context.host=='claude' else codex
    def nested(*args):
        return json.loads('['*10000+']'*10000),None
    monkeypatch.setattr(adapter,'execute',nested)
    result=ai_backend.execute_ai(selected_host_context,'session_summary',
                                 ai_backend.AIRequest('Synthetic input',input_revision='source-sha'))
    assert result.status=='invalid_output' and result.error_code=='output_invalid'
    assert result.backend==selected_host_context.host and result.input_revision=='source-sha'


def test_config_publication_refuses_manual_edit_at_late_cas(selected_host_context,monkeypatch):
    import skill_procedures
    context=selected_host_context
    context.config_path.chmod(0o600)
    before=hashlib.sha256(context.config_path.read_bytes()).hexdigest()
    manual=b'{"manual":"keep this edit"}\n'
    def edit(point):
        assert point=='before_config_cas'
        context.config_path.write_bytes(manual)
    monkeypatch.setattr(skill_procedures,'_config_publication_fault',edit)
    with pytest.raises(ValueError,match='changed during publication'):
        skill_procedures._publish_config(context,{'vault_path':str(context.vault_path)},before)
    assert context.config_path.read_bytes()==manual
    assert not list(context.config_path.parent.glob('.config-*'))


@pytest.mark.host_only('codex',reason='codex-record-format',capability='codex_native_format')
def test_codex_wire_directory_755_is_refused_before_native_discovery(codex_context,tmp_path,monkeypatch):
    directory=tmp_path/'wire';directory.mkdir(mode=0o755);directory.chmod(0o755)
    monkeypatch.setattr(codex,'discover_restrictions',lambda *a:pytest.fail('Unsafe wire directory reached native discovery'))
    with pytest.raises(ai_backend._BackendFailure) as error:
        codex.execute(codex_context,'Synthetic input',{},None,time.monotonic()+1,{},directory)
    assert error.value.code=='native_private_directory_invalid'
    assert list(directory.iterdir())==[]


@pytest.mark.host_only('codex',reason='codex-record-format',capability='codex_native_format')
@pytest.mark.parametrize('malformed',['plugins','plugin','servers','server'])
def test_codex_effective_plugin_config_shapes_fail_closed(codex_context,monkeypatch,malformed):
    from test_host_codex_ai_contract import native_config,fake_inventory,discover
    restricted=native_config(True)
    if malformed=='plugins':restricted['plugins']=[]
    elif malformed=='plugin':restricted['plugins']['tools@local']=False
    elif malformed=='servers':restricted['plugins']['tools@local']['mcp_servers']=[]
    else:restricted['plugins']['tools@local']['mcp_servers']['one']=False
    fake_inventory(monkeypatch,restricted=restricted)
    with pytest.raises(ai_backend._BackendFailure) as error:
        discover(codex_context)
    assert error.value.code=='native_config_invalid'


def test_summary_reader_ignores_older_outer_heading_and_markers(selected_host_context):
    import obsidian_utils
    directory = selected_host_context.vault_path / 'claude-sessions'
    directory.mkdir()
    note = directory / '2026-10-06-session.md'
    note.write_text('---\nproject: project\ndate: 2026-10-06\n---\n## Summary\n\n'
                    '<!-- obsidian-brain:summary:start -->\n## Summary\nActual useful summary.\n'
                    '## Open Questions / Next Steps\nKeep the real next step.\n'
                    '<!-- obsidian-brain:summary:end -->\n')
    result = obsidian_utils.find_latest_session(str(selected_host_context.vault_path), 'claude-sessions', 'project')
    assert result['summary'] == 'Actual useful summary.'
    assert result['next_steps'] == 'Keep the real next step.'


def test_status_and_summary_helpers_return_failure_when_ownership_busy(selected_host_context, monkeypatch):
    import obsidian_utils, note_transactions
    note = selected_host_context.vault_path / 'note.md'
    original = '---\ntype: claude-session\nstatus: auto-logged\n---\nManual text.\n'
    note.write_text(original)
    def busy(*args):
        raise note_transactions.LockBusy('Held by another writer')
    monkeypatch.setattr(note_transactions, 'record_read', busy)
    assert obsidian_utils.flip_note_status(str(note), 'auto-logged', 'summarized') is False
    result = obsidian_utils.upgrade_note_with_summary(str(note), '## Summary\nActual summary.',
                str(selected_host_context.vault_path), 'claude-sessions', 'project')
    assert result.startswith('Failed:')
    assert note.read_text() == original


def test_append_uses_shared_ownership_without_legacy_pid_gate(selected_host_context, monkeypatch):
    import note_writer
    note = selected_host_context.vault_path / 'note.md'
    note.write_text('---\ntype: claude-session\n---\nManual text.\n')
    monkeypatch.setattr(note_writer, '_acquire_lock', lambda *a: pytest.fail('Redundant PID lock entered'))
    assert note_writer.run_append_update(str(selected_host_context.vault_path), str(note), '## Update\nNew fact.') == 0
    assert 'Manual text.' in note.read_text() and 'New fact.' in note.read_text()


def test_packaged_lifecycle_events_match_selected_host_descriptor(selected_host_context):
    import shlex
    root = Path(__file__).resolve().parents[1] / 'hooks'
    expected = {'SessionStart': 'session_start', 'Stop': 'stop',
                'PreCompact': 'pre_compact', 'SessionEnd': 'session_end'}
    filename = 'hooks.json' if selected_host_context.host == 'claude' else 'codex-hooks.json'
    descriptor = json.loads((root / filename).read_text())['hooks']
    assert set(descriptor) == set(expected)
    for event, native_event in expected.items():
        assert len(descriptor[event]) == 1
        entry = descriptor[event][0]
        assert entry.get('matcher') == ('startup|resume|clear|compact' if event == 'SessionStart' else None)
        assert len(entry['hooks']) == 1 and entry['hooks'][0]['type'] == 'command'
        command = shlex.split(entry['hooks'][0]['command'])
        assert command[:2] == ['python3', '${CLAUDE_PLUGIN_ROOT}/hooks/native_entry.py']
        assert command[command.index('--host') + 1] == selected_host_context.host
        assert command[command.index('--event') + 1] == native_event
        if selected_host_context.host == 'claude':
            assert command[command.index('--client') + 1] == 'claude-code'
        else:
            assert '--client' not in command


def test_registered_identity_scenario_accepts_only_full_frozen_actor(selected_host_context, host_identity_scenario):
    from dataclasses import replace
    context = selected_host_context
    host_identity_scenario.register(context)
    assert host_identity_scenario.permits('capture.recover_pending', replace(context))
    for changed in (replace(context, native_session_id='other'),
                    replace(context, native_home=context.native_home.parent),
                    replace(context, user_home=context.user_home.parent),
                    replace(context, config={**context.config, 'manual': True})):
        assert not host_identity_scenario.permits('capture.recover_pending', changed)
    assert not host_identity_scenario.permits('ai_backend.execute_ai', context)


def test_index_hint_reads_actual_managed_summary_after_long_capture(selected_host_context):
    import sqlite3, contextlib, note_transactions, native_lifecycle
    context = selected_host_context
    directory = context.vault_path / 'claude-sessions'
    directory.mkdir()
    note = directory / 'session.md'
    note.write_text('---\ntype: claude-session\n---\n')
    with contextlib.closing(note_transactions.connect_coordination(context)):
        pass
    context.index_path.parent.mkdir(parents=True, exist_ok=True)
    body = ('## Summary\n\n' + 'Long retained capture. ' * 1000 + '\n'
            '<!-- obsidian-brain:summary:start -->\n## Summary\nActual indexed summary.\n'
            '<!-- obsidian-brain:summary:end -->\n')
    with contextlib.closing(sqlite3.connect(context.index_path)) as connection, connection:
        connection.execute('CREATE TABLE notes(path TEXT,date TEXT,body TEXT,project TEXT,type TEXT)')
        connection.execute('INSERT INTO notes VALUES (?,?,?,?,?)',
                           (str(note), '2026-10-06', body, 'project', 'claude-session'))
    result = native_lifecycle._context_hint(context, time.monotonic() + 1)
    hint = result['hookSpecificOutput']['additionalContext']
    assert 'Actual indexed summary.' in hint
    assert 'Long retained capture.' not in hint and '<!--' not in hint
