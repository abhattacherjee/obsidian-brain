"""Legacy lifecycle publishers protect changes made during body construction."""
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
import pytest
import note_transactions
import obsidian_utils
import obsidian_session_log as session_log
import obsidian_context_snapshot as snapshot
import obsidian_session_reaper as reaper
from runtime_context import using_runtime_context


@pytest.fixture
def publisher_setup(tmp_vault, tmp_path, monkeypatch):
    config = {'vault_path': str(tmp_vault), 'sessions_folder': 'claude-sessions',
              'min_messages': 0, 'min_duration_minutes': 0}
    monkeypatch.setenv('HOME', str(tmp_path))
    transcript = tmp_path / '.claude' / 'projects' / 'project' / 'source.jsonl'
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{}\n')
    context = replace(note_transactions.context_for_vault(tmp_vault),
                      native_session_id='session-123', transcript_path=transcript,
                      config=MappingProxyType(config), state_path=tmp_path / 'state')
    metadata = {'project': 'project', 'git_branch': 'develop', 'duration_minutes': 5,
                'files_touched': [], 'errors': []}
    for module in (obsidian_utils, session_log, snapshot):
        for name, value in {
            'load_config': lambda: config,
            'read_transcript': lambda path: [{'type': 'user'}],
            'extract_user_messages': lambda messages: ['A user request'],
            'extract_assistant_messages': lambda messages: ['An assistant answer'],
            'extract_session_metadata': lambda messages, cwd: dict(metadata),
            'extract_tool_uses': lambda messages: [],
            'make_filename': lambda *args, **kwargs: 'note.md',
            'claim_hook_run': lambda *args: True,
            'release_hook_run': lambda *args: None,
            '_append_sessionend_log': lambda **kwargs: None,
            '_append_reaper_log': lambda **kwargs: None,
        }.items():
            if hasattr(module, name):
                monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(session_log, 'find_snapshots_for_session', lambda *args: [])
    monkeypatch.setattr(session_log, 'is_resumed_session', lambda *args, **kwargs: False)
    monkeypatch.setattr(obsidian_utils, 'canonical_project_name', lambda: 'project')
    monkeypatch.setattr(obsidian_utils, '_first_seen_date', lambda sid: '2026-10-05')
    for module in (session_log, snapshot):
        writer = module.write_vault_note

        def bound_writer(*args, _writer=writer, **kwargs):
            with using_runtime_context(context):
                return _writer(*args, **kwargs)

        monkeypatch.setattr(module, 'write_vault_note', bound_writer)
    note = tmp_vault / 'claude-sessions' / 'note.md'
    return context, config, note, transcript


def test_session_end_preserves_manual_edit_during_build(publisher_setup, monkeypatch):
    context, config, note, transcript = publisher_setup
    note.write_text('Existing reviewed note\n')
    build = session_log.build_raw_fallback

    def edited_build(*args, **kwargs):
        note.write_text('Manual edit during SessionEnd build\n')
        return build(*args, **kwargs)

    monkeypatch.setattr(session_log, 'build_raw_fallback', edited_build)
    with using_runtime_context(None):
        session_log._run(payload={'session_id': 'session-123', 'cwd': '/project',
                                 'transcript_path': str(transcript)})
    assert note.read_text() == 'Manual edit during SessionEnd build\n'


def test_snapshot_preserves_manual_edit_during_build(publisher_setup, monkeypatch):
    context, config, note, transcript = publisher_setup
    note.write_text('Existing snapshot\n')
    build = snapshot._build_snapshot_body

    def edited_build(*args, **kwargs):
        note.write_text('Manual edit during snapshot build\n')
        return build(*args, **kwargs)

    monkeypatch.setattr(snapshot, '_build_snapshot_body', edited_build)
    with using_runtime_context(None):
        snapshot._run(payload={'session_id': 'session-123', 'source': 'compact',
                              'transcript_path': str(transcript)})
    assert note.read_text() == 'Manual edit during snapshot build\n'


def test_reaper_preserves_new_stop_note_during_build(publisher_setup, monkeypatch):
    context, config, note, transcript = publisher_setup
    monkeypatch.setattr(reaper, '_resolve_project_jsonl_dir', lambda project: transcript.parent)
    monkeypatch.setattr(reaper, '_read_watermark', lambda path: 0)
    monkeypatch.setattr(reaper, '_write_watermark_atomic', lambda *args: None)
    monkeypatch.setattr(reaper, '_build_existing_sid_set', lambda *args: set())
    monkeypatch.setattr(reaper, '_permission_canary', lambda *args: True)
    monkeypatch.setattr(reaper, '_log_summary', lambda *args: None)
    build = obsidian_utils.build_raw_fallback

    def stop_note_during_build(*args, **kwargs):
        note.write_text('Stop already saved this session\n')
        return build(*args, **kwargs)

    monkeypatch.setattr(obsidian_utils, 'build_raw_fallback', stop_note_during_build)
    with using_runtime_context(context):
        result = reaper._reap_orphaned_sessions('project', str(context.vault_path), 'claude-sessions', config)
    assert result.reaped == 0
    assert note.read_text() == 'Stop already saved this session\n'


def test_snapshot_releases_claim_when_body_build_fails(publisher_setup, monkeypatch):
    context, config, note, transcript = publisher_setup
    released = []
    monkeypatch.setattr(snapshot, 'release_hook_run', lambda *args: released.append(args))

    def failed_body(*args, **kwargs):
        raise RuntimeError('body rendering failed')

    monkeypatch.setattr(snapshot, '_build_snapshot_body', failed_body)
    with using_runtime_context(None), pytest.raises(RuntimeError, match='body rendering failed'):
        snapshot._run(payload={'session_id': 'session-123', 'source': 'compact',
                              'transcript_path': str(transcript)})
    assert released == [('PreCompact', 'session-123')]
    assert not note.exists()
