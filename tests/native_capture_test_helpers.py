"""Native normalized fixtures for lifecycle publication races."""
import time
from dataclasses import replace
from types import MappingProxyType
from pathlib import Path
import pytest
import capture
import transcripts
from runtime_context import using_runtime_context

@pytest.fixture
def selected_host_context(selected_host_context, host):
    selected=replace(selected_host_context,resource_root=Path(__file__).resolve().parents[1],config=MappingProxyType(dict(
        selected_host_context.config,min_messages=1,min_duration_minutes=0)))
    with using_runtime_context(selected):
        yield selected

@pytest.fixture
def native_batch(selected_host_context,monkeypatch):
    records=[transcripts.SourceRecord('first-fact','user','First native fact.',0,
                                     timestamp='2026-04-18T23:55:00Z')]
    def read(ctx,cursor,deadline):
        return transcripts.TranscriptBatch('ok',tuple(records),'synthetic-generation',len(records)*100,
            metadata={'native_session_id':ctx.native_session_id},source_identity='synthetic-source',
            source_complete=True,source_size=len(records)*100)
    monkeypatch.setattr(transcripts,'read_records',read)
    return records

def checkpoint(context,kind='stop',**options):
    return capture.capture_checkpoint(context,capture.CaptureEvent(kind,**options),time.monotonic()+2)

def session_note(context):
    return next(path for path in context.vault_path.rglob('*.md')
                if 'type: "claude-session"' in path.read_text())
