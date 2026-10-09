"""Shared recovery publishes retained native sources with the selected actor."""
from dataclasses import replace
from pathlib import Path
import time
import pytest
import capture
import transcripts
from runtime_context import using_runtime_context
from obsidian_session_reaper import reap_registered_sessions
from parity_test_helpers import host, selected_host_context


def test_native_reaper_publishes_retained_source_once(selected_host_context, monkeypatch):
    original = selected_host_context
    folder = original.native_home / ('projects/test-project' if original.host == 'claude' else 'sessions/2026/10/06')
    folder.mkdir(parents=True)
    source = folder / (original.native_session_id + '.jsonl')
    source.write_text('{}\n')
    actor = replace(original, transcript_path=source,
                    config=dict(original.config, min_messages=1, min_duration_minutes=0))
    record = transcripts.SourceRecord('retained-fact', 'user', 'Recover this native fact once.', 0)
    def read(context, cursor, deadline):
        return transcripts.TranscriptBatch('ok', (record,), 'generation-one', 100,
            metadata={'native_session_id': context.native_session_id}, source_identity='source-one',
            source_complete=True, source_size=100)
    monkeypatch.setattr(transcripts, 'read_records', read)
    def crash_after_retention(point):
        if point == 'after_source_retention':
            raise RuntimeError('synthetic retained-source crash')
    monkeypatch.setattr(capture, '_fault', crash_after_retention)
    with using_runtime_context(actor), pytest.raises(RuntimeError, match='retained-source crash'):
        capture.capture_checkpoint(actor, capture.CaptureEvent('session_end'), time.monotonic() + 2)
    assert not list(actor.vault_path.rglob('*.md'))
    monkeypatch.setattr(capture, '_fault', lambda point: None)
    with using_runtime_context(actor):
        first = reap_registered_sessions(actor, deadline=time.monotonic() + 2)
        second = reap_registered_sessions(actor, deadline=time.monotonic() + 2)
    assert first.status == second.status == 'complete'
    notes = list(actor.vault_path.rglob('*.md'))
    assert len(notes) == 1
    text = notes[0].read_text()
    assert text.count('Recover this native fact once.') == 1
    assert actor.native_session_id in text
    assert 'agent_provider: ' + actor.host in text or 'agent_provider: "' + actor.host + '"' in text
