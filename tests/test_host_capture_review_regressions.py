"""Capture review regressions preserve ownership, privacy and legacy usability."""
import contextlib
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
import capture
import note_transactions
import transcripts


def source_batch(context, records, offset=100):
    return transcripts.TranscriptBatch('ok', tuple(records), 'review-source', offset,
        metadata={'native_session_id': context.native_session_id}, source_identity='review-source',
        source_complete=True, source_size=offset)


def checkpoint(context, note=None, kind='stop'):
    return capture.capture_checkpoint(context, capture.CaptureEvent(kind, note_path=note, min_messages=1),
                                     time.monotonic()+2)


def test_unverified_source_does_not_register_or_publish(selected_host_context, monkeypatch):
    context = selected_host_context
    batch = replace(source_batch(context, [transcripts.SourceRecord('m', 'user', 'Visible fact', 1)]), metadata={})
    monkeypatch.setattr(transcripts, 'read_records', lambda *a: batch)
    result = checkpoint(context)
    assert result.status == 'pending' and result.pending_sources == 1
    assert not list(context.vault_path.rglob('*.md'))
    with contextlib.closing(note_transactions.connect_coordination(context)) as connection:
        assert connection.execute('SELECT COUNT(*) FROM source_sessions').fetchone()[0] == 0


def test_foreign_note_owner_preserves_bytes(selected_host_context, monkeypatch):
    context = selected_host_context
    note = context.vault_path / 'foreign.md'
    raw = ('---\ntype: claude-session\nagent_provider: '+context.host+'\nagent_session_id: other-native-id\n---\nManual prose.\n').encode()
    note.write_bytes(raw)
    monkeypatch.setattr(transcripts, 'read_records', lambda *a: source_batch(context,
        [transcripts.SourceRecord('m', 'user', 'Visible fact', 1)]))
    assert checkpoint(context, note).status == 'conflict'
    assert note.read_bytes() == raw


def test_retained_session_cannot_switch_note(selected_host_context, monkeypatch):
    context = selected_host_context
    first, second = context.vault_path/'first.md', context.vault_path/'second.md'
    monkeypatch.setattr(transcripts, 'read_records', lambda *a: source_batch(context,
        [transcripts.SourceRecord('m', 'user', 'Visible fact', 1)]))
    assert checkpoint(context, first).status == 'complete'
    raw = first.read_bytes()
    assert checkpoint(context, second).status == 'conflict'
    assert first.read_bytes() == raw and not second.exists()


def test_raw_tool_inputs_and_results_never_enter_capture_or_private_facts(selected_host_context, monkeypatch):
    context = selected_host_context
    secret = 'DATABASE_URL=postgres://admin:PrivatePass@db.internal/prod\n{"client_secret":"private-client-secret"}'
    records = [transcripts.SourceRecord('m', 'user', 'Visible fact', 1),
        transcripts.SourceRecord('call', 'assistant', json.dumps({'command':'cat .env','content':secret}),
                                 2, tool_name='Bash', tool_category='shell'),
        transcripts.SourceRecord('result', 'tool', secret, 3, tool_name='Bash', tool_category='shell')]
    monkeypatch.setattr(transcripts, 'read_records', lambda *a: source_batch(context, records))
    note = context.vault_path/'session.md'
    assert checkpoint(context, note).status == 'complete'
    text = note.read_text()
    assert 'Visible fact' in text and 'shell' in text
    for value in ('PrivatePass','private-client-secret','cat .env'):
        assert value not in text
    with contextlib.closing(note_transactions.connect_coordination(context)) as connection:
        for table, column in [('capture_events','text'),('native_events','content')]:
            retained = repr(connection.execute('SELECT '+column+' FROM '+table).fetchall())
            assert 'PrivatePass' not in retained and 'private-client-secret' not in retained


def test_published_session_is_discoverable_without_reindex(selected_host_context, monkeypatch):
    context = selected_host_context
    monkeypatch.setattr(transcripts, 'read_records', lambda *a: source_batch(context,
        [transcripts.SourceRecord('m','user','Visible fact',1)]))
    assert checkpoint(context).status == 'complete'
    from session_lookup import find_existing_session
    note = find_existing_session(context, time.monotonic()+1)
    assert note is not None and note.is_file()
    assert not context.index_path.exists()
    from obsidian_utils import get_session_context
    assert get_session_context()['session_note_name'] == note.stem


def test_new_session_keeps_dashboard_metadata_and_scaffold(selected_host_context, monkeypatch):
    context = selected_host_context
    monkeypatch.setattr(transcripts, 'read_records', lambda *a: source_batch(context, [
        transcripts.SourceRecord('m','user','Visible fact',1,timestamp='2026-10-06T00:00:00Z'),
        transcripts.SourceRecord('a','assistant','Visible reply',2,timestamp='2026-10-06T00:03:00Z')]))
    note=context.vault_path/'session.md'
    assert checkpoint(context,note).status == 'complete'
    fields=capture._note_identity(note)
    assert fields['project_path'] == str(context.canonical_project_root)
    assert fields['git_branch'] == ''
    assert fields['duration_minutes'] == 3
    assert fields['project'] == 'project'
    assert {'claude/session','claude/project/project','claude/auto'} <= set(fields['tags'])
    for heading in ('# Session','## Summary','## Key Decisions','## Changes Made'):
        assert heading in note.read_text()


def test_committed_checkpoint_storage_grows_with_body_not_turn_count(selected_host_context):
    context=selected_host_context
    note=context.vault_path/'session.md'
    events=[]
    for turn in range(30):
        events.append(('event-'+str(turn), 'Visible reply '+str(turn)+' '+('x'*8192)))
        assert capture.publish_events(context,'size-source','generation',turn+1,events,note).status == 'complete'
    with contextlib.closing(note_transactions.connect_coordination(context)) as connection:
        checkpoint_bytes=connection.execute('SELECT COALESCE(SUM(length(body)),0) FROM checkpoints').fetchone()[0]
        operation_bytes=connection.execute('SELECT COALESCE(SUM(length(document)+length(payload)),0) FROM operations').fetchone()[0]
    assert checkpoint_bytes <= len(note.read_bytes()) * 2
    assert operation_bytes <= len(note.read_bytes()) * 3


def test_complete_ended_sources_do_not_create_perpetual_recovery_queue(selected_host_context, monkeypatch):
    context=selected_host_context
    for number in range(10):
        actor=replace(context,native_session_id='complete-'+str(number))
        monkeypatch.setattr(transcripts,'read_records',lambda ctx,*a: source_batch(ctx, []))
        assert checkpoint(actor,kind='session_end').status == 'complete'
    monkeypatch.setattr(transcripts,'read_records',lambda *a: pytest.fail('Completed source was reread'))
    result=capture.recover_registered(context,8,time.monotonic()+1,include_active=True)
    assert result.status == 'complete' and result.pending_sources == 0


@pytest.mark.parametrize('status,revision,enabled,outcome',[
    ('complete','revision',True,'ok_raw_note_only'),
    ('complete',None,True,'skipped_below_threshold'),
    ('pending',None,True,'pending_capture'),
    ('conflict',None,True,'write_failed'),
    ('complete',None,False,'skipped_auto_log_off'),
])
def test_native_sessionend_emits_one_bound_outcome(selected_host_context,monkeypatch,status,revision,enabled,outcome):
    from types import MappingProxyType
    import native_lifecycle
    from session_auxiliary_state import directory
    context=replace(selected_host_context,config=MappingProxyType(dict(selected_host_context.config,auto_log_enabled=enabled)))
    monkeypatch.setattr(capture,'capture_checkpoint',lambda *a: capture.CaptureResult(status,revision))
    native_lifecycle.dispatch(context,'session_end',{},time.monotonic())
    path=directory(context,'logs')/'obsidian-brain-hook.log'
    rows=path.read_text().splitlines()
    assert len(rows) == 1
    assert 'outcome='+outcome in rows[0]
    assert path.stat().st_mode & 0o777 == 0o600



def test_native_sessionend_exception_emits_one_bound_exception_outcome(selected_host_context, monkeypatch, capsys):
    import native_lifecycle
    from session_auxiliary_state import directory
    def fail_capture(*args):
        raise OSError("private exception detail must not enter telemetry")
    monkeypatch.setattr(capture, "capture_checkpoint", fail_capture)
    native_lifecycle.dispatch(selected_host_context, "session_end", {}, time.monotonic())
    path = directory(selected_host_context, "logs") / "obsidian-brain-hook.log"
    rows = path.read_text().splitlines()
    assert len(rows) == 1
    assert "outcome=exception" in rows[0]
    assert "ok_raw_note_only" not in rows[0]
    assert "private exception detail" not in rows[0]
    assert path.stat().st_mode & 0o777 == 0o600
    assert "capture failed" in capsys.readouterr().err

def test_inter_agent_message_keeps_nonhuman_summary_without_raw_payload(selected_host_context, monkeypatch):
    context = selected_host_context
    secret = 'Coordination private token and complete secret file contents'
    records = [transcripts.SourceRecord('user', 'user', 'Visible fact', 1),
        transcripts.SourceRecord('agent', 'agent', secret, 2, kind='inter_agent_message',
                                 source_actor='/root/worker', source_recipient='/root')]
    monkeypatch.setattr(transcripts, 'read_records', lambda *a: source_batch(context, records))
    note = context.vault_path / 'session.md'
    assert checkpoint(context, note).status == 'complete'
    text = note.read_text()
    assert 'Coordination message' in text
    assert secret not in text and 'User: Coordination' not in text
    with contextlib.closing(note_transactions.connect_coordination(context)) as connection:
        retained = repr(connection.execute('SELECT text FROM capture_events').fetchall())
        assert secret not in retained
        assert connection.execute("SELECT role FROM native_events WHERE event='agent'").fetchone() == ('agent',)


def test_inter_agent_identity_change_cannot_relabel_committed_fact(selected_host_context, monkeypatch):
    context = selected_host_context
    records = [transcripts.SourceRecord('user', 'user', 'Visible fact', 1),
               transcripts.SourceRecord('agent', 'agent', 'Private coordination payload', 2,
                   kind='inter_agent_message', source_actor='/root/worker', source_recipient='/root')]
    monkeypatch.setattr(transcripts, 'read_records', lambda *a: source_batch(context, records))
    note = context.vault_path / 'session.md'
    assert checkpoint(context, note).status == 'complete'
    before = note.read_bytes()
    records[1] = replace(records[1], source_actor='/root/other')
    assert checkpoint(context, note).status == 'conflict'
    assert note.read_bytes() == before


@pytest.mark.parametrize('role', ['user', 'assistant'])
@pytest.mark.parametrize('payload,credential', [
    ('DATABASE_URL=postgres://admin:S3cr3tPass@db.internal:5432/prod', 'S3cr3tPass'),
    ('{"client_secret": "a8f9d7e6c5b4a3"}', 'a8f9d7e6c5b4a3'),
    ('-----BEGIN RSA PRIVATE KEY-----\nVGhpc0lzU3ludGhldGljUHJpdmF0ZUtleU1hdGVyaWFs\n-----END RSA PRIVATE KEY-----',
     'VGhpc0lzU3ludGhldGljUHJpdmF0ZUtleU1hdGVyaWFs'),
])
def test_visible_pasted_credentials_never_enter_vault_or_coordination(selected_host_context, monkeypatch, role, payload, credential):
    context = selected_host_context
    records = [transcripts.SourceRecord('visible', 'user', 'Visible ordinary context.', 1),
               transcripts.SourceRecord('pasted', role, payload, 2)]
    monkeypatch.setattr(transcripts, 'read_records', lambda *a: source_batch(context, records))
    note = context.vault_path / 'session.md'
    assert checkpoint(context, note).status == 'complete'
    assert 'Visible ordinary context.' in note.read_text()
    assert credential not in note.read_text()
    with contextlib.closing(note_transactions.connect_coordination(context)) as connection:
        for table, column in [('capture_events', 'text'), ('native_events', 'content'), ('checkpoints', 'body')]:
            retained = repr(connection.execute('SELECT ' + column + ' FROM ' + table).fetchall())
            assert credential not in retained
        for table in ('operations',):
            assert credential not in repr(connection.execute('SELECT * FROM ' + table).fetchall())


def test_partial_published_capture_names_safe_type_without_failure_stderr(selected_host_context, monkeypatch, capsys):
    import native_lifecycle
    record = transcripts.SourceRecord('own','user','Known fact survives.',1)
    warning = 'Unknown substantive '+selected_host_context.host.capitalize()+' transcript record (type=future_record)'
    batch = replace(source_batch(selected_host_context,[record]), status='partial',
                    warnings=(warning,'Unknown substantive Claude transcript record (type=sk-secret)',
                              'private raw payload'), source_complete=False)
    monkeypatch.setattr(transcripts,'read_records',lambda *a:batch)
    note = selected_host_context.vault_path/'partial-status.md'
    result = checkpoint(selected_host_context,note)
    assert result.status == 'pending' and result.applied_revision
    text = note.read_text()
    assert 'partial' in text and 'future_record' in text
    assert 'sk-secret' not in text and 'private raw payload' not in text
    native_lifecycle.dispatch(selected_host_context,'session_end',{},time.monotonic())
    stderr = capsys.readouterr().err
    assert 'capture partial' in stderr and 'capture failed' not in stderr


def test_native_sessionend_logs_retained_message_count_and_duration(selected_host_context, monkeypatch):
    import native_lifecycle
    from session_auxiliary_state import directory
    records = [transcripts.SourceRecord('one','user','One',1,'2026-01-01T00:00:00Z'),
               transcripts.SourceRecord('two','user','Two',2,'2026-01-01T00:03:00Z')]
    monkeypatch.setattr(transcripts,'read_records',lambda *a:source_batch(selected_host_context,records))
    note = selected_host_context.vault_path/'statistics.md'
    assert checkpoint(selected_host_context,note).status == 'complete'
    native_lifecycle.dispatch(selected_host_context,'session_end',{},time.monotonic())
    text = (directory(selected_host_context,'logs')/'obsidian-brain-hook.log').read_text()
    assert 'msgs=2' in text and 'dur=3.0' in text


def test_native_child_dialogue_capture_and_lookup_preserve_manual_prose(selected_host_context):
    """The observed Codex replay boundary excludes inherited parent dialogue."""
    from runtime_context import using_runtime_context
    from session_lookup import find_existing_session
    selected = selected_host_context
    native = selected.native_session_id
    if selected.host == 'codex':
        source = selected.native_home/'sessions'/'child-rollout.jsonl'
        header = {'type':'session_meta','ordinal':0,'payload':{
            'id':native,'cwd':str(selected.worktree),'session_id':'parent',
            'forked_from_id':'parent','parent_thread_id':'parent',
            'subagent_history_start_ordinal':4,'source':{'subagent':{'thread_spawn':{
                'parent_thread_id':'parent','depth':1,'agent_path':'/root/child',
                'agent_nickname':'Synthetic child','agent_role':None}}}}}
        def start(turn):
            return {'type':'event_msg','payload':{'type':'task_started','turn_id':turn,
                'started_at':1,'collaboration_mode_kind':'default','model_context_window':None}}
        rows = [header,
            {'type':'session_meta','payload':{'id':'parent','cwd':str(selected.worktree)}},
            start('parent-turn'),
            {'type':'response_item','payload':{'type':'message','id':'parent-question','role':'user',
                'content':[{'type':'input_text','text':'Inherited parent question'}],
                'internal_chat_message_metadata_passthrough':{'turn_id':'parent-turn'}}},
            {'type':'event_msg','payload':{'type':'thread_settings_applied','thread_id':native,'thread_settings':{}}},
            start('child-turn'),
            {'type':'event_msg','payload':{'type':'user_message','message':'Own child question',
                'local_images':[],'local_audio':[],'text_elements':[]}},
            {'type':'event_msg','payload':{'type':'agent_message','message':'Own child answer',
                'phase':None,'memory_citation':None}}]
        for ordinal,row in enumerate(rows):
            row['ordinal'] = ordinal
        later = {'type':'event_msg','ordinal':8,'payload':{'type':'agent_message',
                 'message':'Next own answer','phase':None,'memory_citation':None}}
    else:
        source = selected.native_home/'projects'/'synthetic'/'own-session.jsonl'
        rows = [{'type':role,'sessionId':native,'uuid':identifier,
                 'message':{'role':role,'content':text}}
                for role,identifier,text in [('user','question','Own child question'),
                                             ('assistant','answer','Own child answer')]]
        later = {'type':'assistant','sessionId':native,'uuid':'next-answer',
                 'message':{'role':'assistant','content':'Next own answer'}}
    source.parent.mkdir(parents=True,exist_ok=True)
    raw = ''.join(json.dumps(row)+'\n' for row in rows).encode()
    source.write_bytes(raw)
    actor = replace(selected,transcript_path=source)
    with using_runtime_context(actor):
        first = checkpoint(actor)
        assert first.status == 'complete', first
        note = find_existing_session(actor,time.monotonic()+1)
        assert note is not None and not actor.index_path.exists()
        note.write_text(note.read_text()+'\nHuman prose outside capture.\n')
        with source.open('ab') as stream:
            stream.write((json.dumps(later)+'\n').encode())
        second = checkpoint(actor)
        assert second.status == 'complete', second
        assert find_existing_session(actor,time.monotonic()+1) == note
    text = note.read_text()
    assert 'Inherited parent question' not in text
    assert text.count('Own child question') == text.count('Own child answer') == 1
    assert 'Next own answer' in text and 'Human prose outside capture.' in text
    assert text.index('Own child question') < text.index('Own child answer') < text.index('Next own answer')
    assert source.read_bytes().startswith(raw)


def test_native_partial_note_and_stderr_name_unknown_type_without_payload(selected_host_context, capsys, monkeypatch):
    import native_lifecycle
    from runtime_context import using_runtime_context
    selected = selected_host_context
    if selected.host == 'claude':
        source = selected.native_home/'projects'/'synthetic'/'partial.jsonl'
        header = {'type':'user','sessionId':selected.native_session_id,'uuid':'before',
                  'message':{'role':'user','content':'Known before.'}}
        later = {'type':'assistant','sessionId':selected.native_session_id,'uuid':'after',
                 'message':{'role':'assistant','content':'Known after.'}}
    else:
        source = selected.native_home/'sessions'/'partial.jsonl'
        header = {'type':'session_meta','payload':{'id':selected.native_session_id,'cwd':str(selected.worktree)}}
        later = {'type':'event_msg','payload':{'type':'item_completed','thread_id':selected.native_session_id,
                 'turn_id':'own-turn','item':{'type':'UserMessage','id':'after',
                                            'content':[{'type':'text','text':'Known after.'}]}}}
    unknown = {'type':'foo-state','payload':{'account_id':'private-account','content':'private-unknown-body'}}
    source.parent.mkdir(parents=True,exist_ok=True)
    source.write_text(''.join(json.dumps(row)+'\n' for row in (header,unknown,later)))
    actor = replace(selected,transcript_path=source)
    with using_runtime_context(actor):
        result = checkpoint(actor)
        assert result.status == 'pending' and result.applied_revision and result.pending_sources == 1
        from session_lookup import find_existing_session
        note = find_existing_session(actor,time.monotonic()+1)
        assert note is not None
        native_lifecycle.dispatch(actor,'session_end',{},time.monotonic())
    text = note.read_text()
    assert 'Known after.' in text and 'partial' in text and 'foo-state' in text
    stderr = capsys.readouterr().err
    assert 'capture partial' in stderr and 'type=foo-state' in stderr and 'capture failed' not in stderr
    for value in ('private-account','private-unknown-body',str(source),selected.native_session_id):
        assert value not in stderr
    for value in ('private-account','private-unknown-body'):
        assert value not in text
    original_reader = transcripts.read_records
    calls = []
    def track_reader(*args):
        calls.append(args)
        return original_reader(*args)
    monkeypatch.setattr(transcripts, 'read_records', track_reader)
    with using_runtime_context(actor):
        unchanged = capture.recover_registered(actor, 8, time.monotonic()+2, include_active=True)
        assert calls == [] and unchanged.pending_sources == 0
        assert 'type=foo-state' in ' '.join(unchanged.warnings)
        appended = json.loads(json.dumps(later))
        if actor.host == 'claude':
            appended['uuid'] = 'late-answer'
            appended['message']['content'] = 'Known late append.'
        else:
            appended['payload']['item']['id'] = 'late-answer'
            appended['payload']['item']['content'][0]['text'] = 'Known late append.'
        with source.open('a') as stream:
            stream.write(json.dumps(appended)+'\n')
        changed = capture.recover_registered(actor, 8, time.monotonic()+2, include_active=True)
        assert calls and changed.status == 'pending'
        assert 'Known late append.' in note.read_text()


def test_ended_partial_skips_unchanged_recovery_but_replays_appended_source(selected_host_context, monkeypatch):
    context = selected_host_context
    source = context.native_home / 'synthetic-ended-source.jsonl'
    context = replace(context, transcript_path=source)
    from runtime_context import using_runtime_context
    with using_runtime_context(context):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b'original retained source\n')
        refs = [{'reason': 'unknown_schema:future_record', 'offset': 0, 'end_offset': 1}]
        batch = replace(source_batch(context, [transcripts.SourceRecord('own-before', 'user', 'Own before', 1)],
                                     offset=source.stat().st_size), status='partial',
                        parser_state={'_deferred_source_rows': refs})
        calls = []
        monkeypatch.setattr(transcripts, 'read_records', lambda *a: calls.append(a) or batch)
        note = context.vault_path / 'ended-partial.md'
        result = checkpoint(context, note, 'session_end')
        assert result.status == 'pending'
        assert 'partial' in note.read_text()
        calls.clear()
        for _ in range(3):
            recovery = capture.recover_registered(context, 8, time.monotonic()+2, include_active=True)
            assert recovery.pending_sources == 0
            assert 'type=future_record' in ' '.join(recovery.warnings)
        assert calls == []
        # Administrative replay does not discard the retained uncertainty.
        explicit = capture.recover_registered(context, 8, time.monotonic()+2, include_active=True,
                                             replay_retired=True)
        assert explicit.status == 'pending' and explicit.pending_sources == 1
        assert len(calls) == 1
        calls.clear()
        source.write_bytes(source.read_bytes() + b'new own row\n')
        batch = replace(batch, records=(*batch.records,
            transcripts.SourceRecord('own-after', 'assistant', 'Own after', 25)),
            consumed_offset=source.stat().st_size, source_size=source.stat().st_size)
        result = capture.recover_registered(context, 8, time.monotonic()+2, include_active=True)
        assert calls and result.status == 'pending'
        assert 'Own after' in note.read_text()
        from runtime_context import using_runtime_context
        import vault_doctor
        with using_runtime_context(context):
            audit = vault_doctor._inspect_runtime_pending()
        assert audit['status'] == 'pending' and audit['pending_sources'] == 1
        assert audit['loss_of_input'] is False
        assert 'type=future_record' in ' '.join(audit['warnings'])
        with contextlib.closing(note_transactions.connect_coordination(context)) as connection:
            cursor = json.loads(connection.execute('SELECT cursor FROM source_sessions').fetchone()[0])
            assert cursor['parser_state']['_deferred_source_rows'] == refs


@pytest.mark.parametrize('unsafe', ['ownership', 'opaque', 'loss', 'not_eof'])
def test_ended_unresolved_input_remains_in_recovery_queue(selected_host_context, monkeypatch, unsafe):
    context = selected_host_context
    source = context.native_home / 'synthetic-ended-source.jsonl'
    context = replace(context, transcript_path=source)
    from runtime_context import using_runtime_context
    with using_runtime_context(context):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b'raw source remains\n')
        state = {'_deferred_source_rows': [{'reason': 'unknown_schema:future_record'}]}
        if unsafe == 'ownership':
            state['_deferred_source_rows'].append({'reason': 'ambiguous_ownership'})
        if unsafe == 'opaque':
            state['_opaque_drain'] = {'start': 0}
        batch = replace(source_batch(context, [transcripts.SourceRecord('own', 'user', 'Owned', 1)],
            offset=source.stat().st_size), status='partial', parser_state=state,
            loss_of_input=unsafe == 'loss', source_complete=unsafe != 'not_eof',
            consumed_offset=source.stat().st_size - int(unsafe == 'not_eof'))
        calls = []
        monkeypatch.setattr(transcripts, 'read_records', lambda *a: calls.append(a) or batch)
        checkpoint(context, context.vault_path / 'partial.md', 'session_end')
        calls.clear()
        result = capture.recover_registered(context, 8, time.monotonic()+2, include_active=True)
        assert calls and result.status == 'pending' and result.pending_sources == 1
        if unsafe == 'loss':
            from runtime_context import using_runtime_context
            import vault_doctor
            with using_runtime_context(context):
                audit = vault_doctor._inspect_runtime_pending()
            assert audit['loss_of_input'] is True and audit['status'] != 'complete'


@pytest.mark.parametrize('warning,visible', [
    ('Transcript batch limit reached', True),
    ('Transcript header exceeds the record limit', True),
    ('Native transcript identity is unverified', True),
    ('Selected Codex session metadata is pending', True),
    ('Transcript header exceeds the record limit /private/raw-secret', False),
    ('Native transcript identity is unverified /private/raw-secret', False),
    ('Selected Codex session metadata is pending /private/raw-secret', False),
    ('Existing session index lookup is pending: OperationalError', True),
    ('Registered session lookup is pending: PermissionError', True),
    ('Existing session index lookup is pending: OperationalError /private/secret-token', False),
    ('Transcript batch limit reached /private/raw-secret', False),
    ('arbitrary private raw exception', False),
])
def test_pending_capture_reason_reports_only_fixed_safe_diagnostics(selected_host_context, monkeypatch, capsys,
                                                                   warning, visible):
    import native_lifecycle
    context = selected_host_context
    partial = replace(source_batch(context, []), status='partial', warnings=(warning,), source_complete=False)
    monkeypatch.setattr(transcripts, 'read_records', lambda *a: partial)
    result = checkpoint(context)
    assert result.status == 'pending'
    native_lifecycle._report_capture_result('capture', result)
    stderr = capsys.readouterr().err
    if visible:
        assert warning in stderr
    else:
        assert warning not in stderr and 'Source input remains pending.' in stderr
    assert '/private/' not in stderr and 'raw-secret' not in stderr and 'secret-token' not in stderr


def test_ended_ten_mebibyte_opaque_row_retires_only_after_verified_eof(selected_host_context, monkeypatch, capsys):
    import native_lifecycle
    import vault_doctor
    from runtime_context import using_runtime_context
    selected = selected_host_context
    source = selected.native_home / ('projects/synthetic/opaque.jsonl' if selected.host == 'claude'
                                    else 'sessions/opaque.jsonl')
    source.parent.mkdir(parents=True, exist_ok=True)
    native = selected.native_session_id
    if selected.host == 'claude':
        def own(identifier, text):
            return {'type':'user','sessionId':native,'uuid':identifier,
                    'message':{'role':'user','content':text}}
        rows = [own('before', 'Own before opaque output.')]
    else:
        def own(identifier, text):
            return {'type':'event_msg','payload':{'type':'item_completed','thread_id':native,
                    'turn_id':'own-turn','item':{'type':'UserMessage','id':identifier,
                    'content':[{'type':'text','text':text}]}}}
        rows = [{'type':'session_meta','payload':{'id':native,'cwd':str(selected.worktree)}},
                own('before', 'Own before opaque output.')]
    rows.extend([{'type':'opaque_output','payload':'PRIVATE-OPAQUE-OUTPUT-'+('x'*(10*1024*1024))},
                 own('after', 'Own after opaque output.'), own('more', 'Own further question.')])
    source.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    actor = replace(selected, transcript_path=source)
    note = actor.vault_path/'opaque-session.md'
    with using_runtime_context(actor):
        for _ in range(8):
            result = capture.capture_checkpoint(actor, capture.CaptureEvent('session_end',note_path=note,min_messages=1),
                                                time.monotonic()+5)
            with contextlib.closing(note_transactions.connect_coordination(actor)) as connection:
                retired = connection.execute('SELECT COUNT(*) FROM retired_source_versions').fetchone()[0]
                stored = json.loads(connection.execute('SELECT cursor FROM source_sessions').fetchone()[0])
            if retired:
                break
        assert retired == 1 and result.status == 'pending' and result.applied_revision
        assert stored['offset'] == source.stat().st_size and not stored['parser_state'].get('_opaque_drain')
        assert stored['parser_state']['_deferred_source_rows'][0]['reason'] == 'oversized:unrecognized'
        assert 'Own after opaque output.' in note.read_text() and 'PRIVATE-OPAQUE-OUTPUT' not in note.read_text()
        original = transcripts.read_records
        visits = []
        def reader(context, cursor, deadline):
            visits.append(context.native_session_id)
            return original(context, cursor, deadline)
        monkeypatch.setattr(transcripts, 'read_records', reader)
        new_actor = replace(actor, native_session_id='next-session', transcript_path=source.parent/'not-created.jsonl')
        capsys.readouterr()
        hint = native_lifecycle.dispatch(new_actor, 'session_start', {}, time.monotonic())
        assert 'Oversized' in hint['hookSpecificOutput']['additionalContext']
        assert 'retains unverified input for review' in hint['hookSpecificOutput']['additionalContext']
        assert 'at least 1 source(s)' not in hint['hookSpecificOutput']['additionalContext']
        stderr = capsys.readouterr().err
        assert native not in visits and 'recovery pending' not in stderr and 'recovery partial' not in stderr
        assert 'capture pending' not in stderr
        with source.open('a') as stream:
            stream.write(json.dumps(own('late', 'Own late append.'))+'\n')
        visits.clear()
        replay = capture.recover_registered(actor, 8, time.monotonic()+5, include_active=True)
        assert native in visits and replay.status == 'pending'
        assert 'Own late append.' in note.read_text()
        audit = vault_doctor._inspect_runtime_pending()
        assert audit['status'] == 'pending' and audit['pending_sources'] == 1 and not audit['loss_of_input']
        assert 'Oversized' in ' '.join(audit['warnings'])


def test_oversized_unverified_header_cannot_retire(selected_host_context):
    from runtime_context import using_runtime_context
    source = selected_host_context.native_home / 'unverified-oversized.jsonl'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps({'type':'session_meta','payload':'x'*(10*1024*1024)})+'\n')
    actor = replace(selected_host_context, transcript_path=source)
    with using_runtime_context(actor):
        result = checkpoint(actor, kind='session_end')
        assert result.status == 'pending' and not result.applied_revision
        assert 'Native transcript identity is unverified' in result.warnings
        with contextlib.closing(note_transactions.connect_coordination(actor)) as connection:
            assert connection.execute('SELECT COUNT(*) FROM retired_source_versions').fetchone()[0] == 0
            assert connection.execute('SELECT COUNT(*) FROM source_sessions').fetchone()[0] == 0
    assert not list(actor.vault_path.rglob('*.md'))


def test_native_dialogue_cannot_create_markdown_structure_or_links(selected_host_context, monkeypatch):
    context=selected_host_context
    raw='Facts before\n## Open Questions / Next Steps\n- [ ] injected task\nsee [[real-note]] and [[ -f x ]]\nFacts after'
    records=[transcripts.SourceRecord('u','user',raw,1),transcripts.SourceRecord('a','assistant',raw,2)]
    monkeypatch.setattr(transcripts,'read_records',lambda *a:source_batch(context,records))
    note=context.vault_path/'session.md'
    assert checkpoint(context,note).status=='complete'
    text=note.read_text()
    assert '\n- [ ] injected task' not in text
    assert '\n## Open Questions / Next Steps\n- [ ]' not in text
    assert '[[real-note]]' not in text and '[[ -f x ]]' not in text
    for fact in ('Facts before','Open Questions / Next Steps','injected task','real-note','-f x','Facts after'):
        assert text.count(fact)>=2
    note.write_text(text+'\nManual prose with [[deliberate-link]].\n')
    records.append(transcripts.SourceRecord('later','assistant','Later confirmed fact',3))
    monkeypatch.setattr(transcripts,'read_records',lambda *a:source_batch(context,records,offset=101))
    assert checkpoint(context,note).status=='complete'
    assert 'Later confirmed fact' in note.read_text()
    assert note.read_text().endswith('Manual prose with [[deliberate-link]].\n')


def test_native_consecutive_coordination_is_counted_without_losing_dialogue(selected_host_context, monkeypatch):
    context=selected_host_context
    records=[transcripts.SourceRecord('u','user','Visible fact',0)]
    records += [transcripts.SourceRecord('agent-'+str(i),'agent','PRIVATE-'+str(i),i+1,kind='inter_agent_message') for i in range(37)]
    records += [transcripts.SourceRecord('reply','assistant','Visible reply',38),
                transcripts.SourceRecord('last-agent','agent','PRIVATE-last',39,kind='inter_agent_message')]
    monkeypatch.setattr(transcripts,'read_records',lambda *a:source_batch(context,records))
    note=context.vault_path/'session.md'
    assert checkpoint(context,note).status=='complete'
    text=note.read_text()
    assert 'Coordination messages (37)' in text
    assert text.count('\nCoordination message\n')==1
    assert text.index('Coordination messages (37)')<text.index('Visible reply')
    assert 'PRIVATE-' not in text
    with contextlib.closing(note_transactions.connect_coordination(context)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM native_events WHERE role='agent'").fetchone()[0]==38
    assert checkpoint(context,note).status=='complete'
    assert note.read_text()==text


def test_native_snapshot_timestamp_orders_hash_names_and_survives_retry(selected_host_context, monkeypatch):
    import hashlib
    from obsidian_utils import find_snapshots_for_session,fetch_snapshot_summaries,gather_session_evidence,slugify
    context=selected_host_context;note=context.vault_path/'session.md';first='First snapshot facts\n'
    first_identity=hashlib.sha256((context.session_key+'\0'+hashlib.sha256(first.encode()).hexdigest()+'\0manual').encode()).hexdigest()
    later=next('Later snapshot facts '+str(i)+'\n' for i in range(100) if hashlib.sha256((context.session_key+'\0'+hashlib.sha256(('Later snapshot facts '+str(i)+'\n').encode()).hexdigest()+'\0auto').encode()).hexdigest()<first_identity)
    clock=iter(('2026-10-06T10:00:01+00:00','2026-10-06T11:00:02+00:00','2026-10-06T12:00:03+00:00'))
    monkeypatch.setattr(capture,'_snapshot_created_at',lambda:next(clock),raising=False)
    assert capture._snapshot(context,note,'2026-10-06',first,'manual',time.monotonic()+2).status=='complete'
    assert capture._snapshot(context,note,'2026-10-06',later,'auto',time.monotonic()+2).status=='complete'
    folder=context.vault_path/'claude-sessions';paths=list(folder.glob('*snapshot*.md'))
    by_trigger={capture._note_identity(p)['trigger']:p for p in paths}
    assert by_trigger['manual'].name>by_trigger['auto'].name
    before=by_trigger['manual'].read_bytes()
    assert capture._snapshot(context,note,'2026-10-06',first,'manual',time.monotonic()+2).status=='complete'
    assert by_trigger['manual'].read_bytes()==before
    project=slugify(context.canonical_project_root.name)
    expected=['[['+by_trigger[t].stem+']]' for t in ('manual','auto')]
    for use_index in (False,True):
        assert find_snapshots_for_session(folder,context.native_session_id,None,project,use_index=use_index)==expected
    rows=fetch_snapshot_summaries(folder,context.native_session_id,'2026-10-06',project)
    assert [(r['trigger'],r['hhmmss']) for r in rows]==[('manual','100001'),('auto','110002')]
    bundle=gather_session_evidence(str(context.vault_path),'claude-sessions','claude-insights',context.native_session_id,project)
    assert [r['trigger'] for r in bundle['snapshots']]==['manual','auto']
