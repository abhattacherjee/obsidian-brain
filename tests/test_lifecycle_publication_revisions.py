"""Native lifecycle publishers preserve manual edits and release OS ownership."""
import time
from concurrent.futures import ThreadPoolExecutor

import capture
import note_transactions
import obsidian_session_log as session_log
import obsidian_context_snapshot as snapshot
import obsidian_session_reaper as reaper
import transcripts
from native_capture_test_helpers import selected_host_context, native_batch, checkpoint, session_note


def _edit_during_publication(context, monkeypatch, native_batch):
    assert checkpoint(context).status == 'complete'
    note = session_note(context)
    native_batch.append(transcripts.SourceRecord('second-fact', 'user', 'Second native fact.', 100))
    manual = note.read_text().replace('First native fact.', 'Manual managed-region edit.')
    def edit(point):
        if point == 'after_checkpoint':
            note.write_text(manual)
    monkeypatch.setattr(capture, '_fault', edit)
    return note, manual


def test_session_end_preserves_manual_edit_during_build(selected_host_context, native_batch, monkeypatch, capsys):
    note, manual = _edit_during_publication(selected_host_context, monkeypatch, native_batch)
    session_log._run(payload={})
    assert note.read_text() == manual
    assert 'capture failed:' in capsys.readouterr().err


def test_snapshot_preserves_manual_edit_during_build(selected_host_context, native_batch, monkeypatch, capsys):
    note, manual = _edit_during_publication(selected_host_context, monkeypatch, native_batch)
    snapshot._run(payload={'trigger': 'manual'})
    assert note.read_text() == manual
    assert list(selected_host_context.vault_path.rglob('*snapshot*.md')) == []
    assert 'capture failed:' in capsys.readouterr().err


def test_reaper_preserves_new_stop_note_during_build(selected_host_context, native_batch, monkeypatch):
    context = selected_host_context
    note, manual = _edit_during_publication(context, monkeypatch, native_batch)
    assert checkpoint(context, 'session_end').status == 'conflict'
    monkeypatch.setattr(capture, '_fault', lambda point: None)
    result = reaper._reap_orphaned_sessions(context.canonical_project_root.name,
        str(context.vault_path), 'claude-sessions', dict(context.config))
    assert result.reaped == 0
    assert result.recovery_status != 'complete'
    assert note.read_text() == manual


def test_snapshot_releases_claim_when_body_build_fails(selected_host_context, native_batch, monkeypatch, capsys):
    context = selected_host_context
    def failed_body(point):
        if point == 'before_checkpoint':
            raise RuntimeError('Synthetic body build failure')
    monkeypatch.setattr(capture, '_fault', failed_body)
    snapshot._run(payload={'trigger': 'manual'})
    assert 'capture failed:' in capsys.readouterr().err
    assert list(context.vault_path.rglob('*.md')) == []
    def other_writer():
        with note_transactions.ownership_lock(context, deadline=time.monotonic() + 0.2):
            return True
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(other_writer).result(timeout=1) is True
    monkeypatch.setattr(capture, '_fault', lambda point: None)
    assert checkpoint(context, 'pre_compact').status == 'complete'
    assert len(list(context.vault_path.rglob('*snapshot*.md'))) == 1
