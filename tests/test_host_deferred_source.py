"""Deferred source rows retain bytes and publish only verified facts."""
import json
import time
from dataclasses import replace

import transcripts


def source(context):
    home = context.native_home
    root = home / ('projects' if context.host == 'claude' else 'sessions')
    root.mkdir(parents=True, exist_ok=True)
    return replace(context, transcript_path=root / 'deferred.jsonl')


def cursor(batch):
    return transcripts.SourceCursor(batch.source_generation, batch.consumed_offset,
        batch.source_identity, batch.anchor_digest, batch.parser_state, known_size=batch.source_size)


def test_deferred_row_retains_only_reference_and_replays_after_proof(selected_host_context):
    ctx = source(selected_host_context)
    raw = b'{"header":true}\n{"text":"before"}\n{"text":"private-deferred"}\n{"text":"after"}\n'
    ctx.transcript_path.write_bytes(raw)
    proven = False
    def parser(context, rows, header, state):
        records, deferred = [], []
        for row in rows:
            text = row.data.get('text')
            if text == 'private-deferred' and not proven:
                deferred.append(transcripts.DeferredRow(row.offset, row.end_offset, 'ambiguous_ownership'))
            elif text:
                records.append(transcripts.SourceRecord(str(row.offset), 'user', text, row.offset))
        return transcripts.ParsedRecords(tuple(records), status='partial' if deferred else 'ok',
            parser_state={'safe':True}, deferred_rows=tuple(deferred))
    first = transcripts.read_source(ctx, transcripts.SourceCursor(), time.monotonic()+2, parser)
    assert first.status == 'partial' and first.consumed_offset == len(raw)
    assert [r.text for r in first.records] == ['before', 'after']
    refs = first.parser_state['_deferred_source_rows']
    assert len(refs) == 1 and 'private-deferred' not in json.dumps(refs)
    assert ctx.transcript_path.read_bytes() == raw and not first.loss_of_input
    pending = transcripts.read_source(ctx, cursor(first), time.monotonic()+2, parser)
    assert pending.status == 'partial' and pending.parser_state['_deferred_source_rows'] == refs
    proven = True
    resolved = transcripts.read_source(ctx, cursor(pending), time.monotonic()+2, parser)
    assert [r.text for r in resolved.records] == ['private-deferred']
    assert resolved.status == 'ok' and resolved.source_complete
    assert not resolved.parser_state.get('_deferred_source_rows')


def test_changed_deferred_bytes_are_never_replayed(selected_host_context):
    ctx = source(selected_host_context)
    raw = b'{"header":true}\n{"text":"private-deferred"}\n' + b'{"padding":"' + b'x'*100 + b'"}\n'
    ctx.transcript_path.write_bytes(raw)
    seen = []
    def parser(context, rows, header, state):
        seen.extend(row.data.get('text') for row in rows)
        deferred = tuple(transcripts.DeferredRow(r.offset,r.end_offset,'unknown_schema:future_record')
                         for r in rows if r.data.get('text'))
        return transcripts.ParsedRecords(status='partial' if deferred else 'ok', deferred_rows=deferred)
    first = transcripts.read_source(ctx, transcripts.SourceCursor(), time.monotonic()+2, parser)
    ctx.transcript_path.write_bytes(raw.replace(b'private-deferred', b'changed-deferred'))
    seen.clear()
    following = transcripts.read_source(ctx, cursor(first), time.monotonic()+2, parser)
    assert 'changed-deferred' not in seen
    assert following.status == 'partial' and following.parser_state['_deferred_source_rows']
    assert any('changed' in w.lower() for w in following.warnings)


def test_recognized_metadata_resolves_without_invented_chat(selected_host_context):
    ctx = source(selected_host_context)
    ctx.transcript_path.write_bytes(b'{"header":true}\n{"metadata":"future"}\n')
    supported = False
    def parser(context, rows, header, state):
        refs = tuple(transcripts.DeferredRow(r.offset,r.end_offset,'unknown_schema:future_record')
                     for r in rows if 'metadata' in r.data and not supported)
        return transcripts.ParsedRecords(status='partial' if refs else 'ok', deferred_rows=refs)
    pending = transcripts.read_source(ctx, transcripts.SourceCursor(), time.monotonic()+2, parser)
    assert pending.parser_state['_deferred_source_rows']
    supported = True
    following = transcripts.read_source(ctx, cursor(pending), time.monotonic()+2, parser)
    assert following.status == 'ok' and not following.records
    assert not following.parser_state.get('_deferred_source_rows')


def test_ref_cap_keeps_unretained_row_unacknowledged(selected_host_context, monkeypatch):
    ctx = source(selected_host_context)
    ctx.transcript_path.write_bytes(b'{"header":true}\n{"unknown":1}\n{"unknown":2}\n')
    monkeypatch.setattr(transcripts,'MAX_DEFERRED_ROWS',1)
    def parser(context, rows, header, state):
        return transcripts.ParsedRecords(status='partial', deferred_rows=tuple(
            transcripts.DeferredRow(r.offset,r.end_offset,'unknown_schema:future_record')
            for r in rows if 'unknown' in r.data))
    batch = transcripts.read_source(ctx, transcripts.SourceCursor(), time.monotonic()+2, parser)
    assert len(batch.parser_state['_deferred_source_rows']) == 1
    assert batch.consumed_offset == ctx.transcript_path.read_bytes().index(b'{"unknown":2}')
    assert not batch.source_complete and any('reference limit' in w for w in batch.warnings)


def test_late_fact_replay_reorders_capture_and_preserves_manual_prose(selected_host_context, monkeypatch):
    import capture
    import note_transactions
    import contextlib
    ctx = source(selected_host_context)
    raw = b'{"header":true}\n{"text":"before"}\n{"text":"middle"}\n{"text":"after"}\n'
    ctx.transcript_path.write_bytes(raw)
    proven = False
    def parser(context, rows, header, state):
        records, refs = [], []
        for row in rows:
            text = row.data.get('text')
            if text == 'middle' and not proven:
                refs.append(transcripts.DeferredRow(row.offset,row.end_offset,'ambiguous_ownership'))
            elif text:
                records.append(transcripts.SourceRecord(str(row.offset),'user',text,row.offset))
        return transcripts.ParsedRecords(tuple(records), {'native_session_id':context.native_session_id},
            status='partial' if refs else 'ok', deferred_rows=tuple(refs))
    monkeypatch.setattr(transcripts,'read_records',lambda context, retained, deadline:
        transcripts.read_source(ctx,retained,deadline,parser))
    note = selected_host_context.vault_path/'late.md'
    event = capture.CaptureEvent('stop',note_path=note,min_messages=1)
    first = capture.capture_checkpoint(selected_host_context,event,time.monotonic()+2)
    assert first.status == 'pending' and first.pending_sources == 1
    note.write_text(note.read_text()+'\nManual outside capture.\n')
    proven = True
    following = capture.capture_checkpoint(selected_host_context,event,time.monotonic()+2)
    assert following.status == 'complete'
    text = note.read_text()
    assert text.index('User: before') < text.index('User: middle') < text.index('User: after')
    assert 'Manual outside capture.' in text and ctx.transcript_path.read_bytes() == raw
    with contextlib.closing(note_transactions.connect_coordination(selected_host_context)) as connection:
        retained = connection.execute('SELECT cursor FROM source_sessions').fetchone()[0]
        assert '_deferred_source_rows' not in retained


def test_replay_byte_budget_rotates_and_preserves_unresolved_refs(selected_host_context, monkeypatch):
    ctx = source(selected_host_context)
    ctx.transcript_path.write_bytes(b'{"header":true}\n' + b''.join(
        ('{"unknown":'+str(index)+'}\n').encode() for index in range(4)))
    def parser(context, rows, header, state):
        return transcripts.ParsedRecords(status='partial', deferred_rows=tuple(
            transcripts.DeferredRow(r.offset,r.end_offset,'unknown_schema:future_record')
            for r in rows if 'unknown' in r.data))
    pending = transcripts.read_source(ctx,transcripts.SourceCursor(),time.monotonic()+2,parser)
    seen = set()
    monkeypatch.setattr(transcripts,'MAX_DEFERRED_REPLAY_BYTES',16)
    def observe(context, rows, header, state):
        seen.update(r.data['unknown'] for r in rows if 'unknown' in r.data)
        return parser(context,rows,header,state)
    for _ in range(4):
        pending = transcripts.read_source(ctx,cursor(pending),time.monotonic()+2,observe)
        assert len(pending.parser_state['_deferred_source_rows']) == 4
        assert pending.status == 'partial' and not pending.loss_of_input
    assert seen == {0,1,2,3}


def test_old_unknown_replay_cannot_erase_later_verified_native_proof(selected_host_context):
    ctx = source(selected_host_context)
    if ctx.host == 'claude':
        header = {'type':'user','sessionId':ctx.native_session_id,'uuid':'initial',
                  'message':{'role':'user','content':'Initial own fact.'}}
        proof = {'type':'user','sessionId':ctx.native_session_id,'uuid':'proof',
                 'message':{'role':'user','content':'Renewed own proof.'}}
        later = {'type':'assistant','uuid':'later',
                 'message':{'role':'assistant','content':'Later proven answer.'}}
    else:
        header = {'type':'session_meta','payload':{'id':ctx.native_session_id,'cwd':str(ctx.worktree)}}
        proof = {'type':'event_msg','payload':{'type':'task_started','turn_id':'own-turn',
                 'started_at':1,'collaboration_mode_kind':'default','model_context_window':None}}
        later = {'type':'event_msg','payload':{'type':'agent_message','message':'Later proven answer.',
                 'phase':None,'memory_citation':None}}
    unknown = {'type':'future_record','payload':{'content':'Uninterpreted private data'}}
    ctx.transcript_path.write_text(''.join(json.dumps(row)+'\n' for row in (header,unknown,proof)))
    first = transcripts.read_records(ctx,transcripts.SourceCursor(),time.monotonic()+2)
    assert first.status == 'partial' and first.parser_state['_deferred_source_rows']
    with ctx.transcript_path.open('a') as stream:
        stream.write(json.dumps(later)+'\n')
    following = transcripts.read_records(ctx,cursor(first),time.monotonic()+2)
    assert any(record.text == 'Later proven answer.' for record in following.records)
    assert following.status == 'partial'
    assert len(following.parser_state['_deferred_source_rows']) == 1


def test_deadline_after_new_parse_cannot_ack_unparsed_deferred_ref(selected_host_context, monkeypatch):
    ctx = source(selected_host_context)
    ctx.transcript_path.write_bytes(b'{"header":true}\n{"unknown":true}\n')
    def unknown(context, rows, header, state):
        return transcripts.ParsedRecords(status='partial',deferred_rows=tuple(
            transcripts.DeferredRow(row.offset,row.end_offset,'unknown_schema:future_record')
            for row in rows if 'unknown' in row.data))
    pending = transcripts.read_source(ctx,transcripts.SourceCursor(),time.monotonic()+2,unknown)
    deadline = time.monotonic()+2
    def new_only(context, rows, header, state):
        assert not rows
        monkeypatch.setattr(time,'monotonic',lambda:deadline+1)
        return transcripts.ParsedRecords()
    following = transcripts.read_source(ctx,cursor(pending),deadline,new_only)
    assert following.status == 'partial' and not following.source_complete
    assert following.parser_state['_deferred_source_rows'] == pending.parser_state['_deferred_source_rows']
