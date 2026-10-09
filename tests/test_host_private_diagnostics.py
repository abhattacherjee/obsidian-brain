"""Bound diagnostics follow selected native storage despite ambient mutations."""
import io
from dataclasses import replace

import hook_bootstrap
import summarizer_metrics
import transcripts
from note_transactions import session_state_path
from runtime_context import using_runtime_context
from transcripts import SourceCursor


def test_bound_metrics_ignore_legacy_sink_override(selected_host_context, monkeypatch, tmp_path):
    context = selected_host_context
    poisoned = tmp_path / 'unselected' / 'metrics.jsonl'
    monkeypatch.setattr(summarizer_metrics, 'METRICS_PATH', poisoned)
    summarizer_metrics.append_metrics_record({'event': 'synthetic'})
    expected = session_state_path(context) / 'logs' / 'obsidian-brain-summarizer-metrics.jsonl'
    assert expected.is_file()
    assert not poisoned.exists()


def test_bootstrap_bound_home_ignores_other_host_and_changed_environment(selected_host_context, monkeypatch, tmp_path):
    context = selected_host_context
    changed = tmp_path / 'changed'
    monkeypatch.setenv('CODEX_HOME', str(changed))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(changed))
    monkeypatch.setattr(hook_bootstrap.sys, 'stdin', io.StringIO('{}'))
    hook_bootstrap.log_import_failure('Stop', TypeError('synthetic'))
    expected = context.native_home / 'obsidian-brain-hook.log'
    assert expected.is_file()
    assert not changed.exists()
    assert str(expected) in hook_bootstrap.session_start_notice('synthetic')


def test_transcript_read_uses_bound_native_home_after_environment_changes(selected_host_context, monkeypatch, tmp_path):
    context = selected_host_context
    directory = context.native_home / ('projects' if context.host == 'claude' else 'sessions')
    directory.mkdir(parents=True)
    source = directory / 'synthetic.jsonl'
    if context.host == 'claude':
        source.write_text('{"type":"user","sessionId":"synthetic-session","uuid":"m1","message":{"role":"user","content":"synthetic fact"}}\n')
    else:
        source.write_text('{"type":"session_meta","payload":{"id":"synthetic-session"}}\n{"type":"response_item","payload":{"type":"message","id":"m1","role":"user","content":[{"type":"input_text","text":"synthetic fact"}],"internal_chat_message_metadata_passthrough":{"turn_id":"synthetic-turn"}}}\n')
    bound = replace(context, transcript_path=source)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'changed'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'changed'))
    import time
    with using_runtime_context(bound):
        result = transcripts.read_records(bound, SourceCursor(), time.monotonic() + 2)
    assert result.status == 'ok', result.warnings
    assert [record.text for record in result.records] == ['synthetic fact']


def test_changed_environment_cannot_authorize_another_transcript_root(selected_host_context, monkeypatch, tmp_path):
    context = selected_host_context
    changed = tmp_path / 'unselected'
    directory = changed / ('projects' if context.host == 'claude' else 'sessions')
    directory.mkdir(parents=True)
    source = directory / 'unselected.jsonl'
    source.write_text('{}\n')
    monkeypatch.setenv('CODEX_HOME', str(changed))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(changed))
    import time
    result = transcripts.read_records(replace(context, transcript_path=source), SourceCursor(),
                                      time.monotonic() + 2)
    assert result.status == 'unavailable'
    assert not result.records
