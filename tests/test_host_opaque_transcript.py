"""Opaque oversized source rows retain later explicitly owned messages."""
import json
import time
from dataclasses import replace

import pytest
from runtime_context import using_runtime_context
from transcripts import SourceCursor, read_records


def _case(context, size=5*1024*1024, tail=True):
    path = context.native_home / ('projects' if context.host == 'claude' else 'sessions') / 'opaque.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    native = context.native_session_id
    if context.host == 'claude':
        header = {'type': 'mode', 'mode': 'normal', 'sessionId': native}
        following = {'type': 'user', 'sessionId': native, 'uuid': 'own-after',
                     'message': {'role': 'user', 'content': 'verified after opaque'}}
    else:
        header = {'type': 'session_meta', 'payload': {'id': native}}
        following = {'type': 'response_item', 'payload': {'type': 'message', 'id': 'own-after', 'role': 'user',
            'content': [{'type': 'input_text', 'text': 'verified after opaque'}], 'thread_id': native, 'turn_id': 'own-turn'}}
    first = json.dumps(header).encode()+b'\n'
    opaque = b'{"type":"future_record","private":"'+b'x'*size+b'"}'
    path.write_bytes(first+opaque+(b'\n'+json.dumps(following).encode()+b'\n' if tail else b''))
    return replace(context, transcript_path=path), path, len(first)


def _next(batch):
    return SourceCursor(batch.source_generation, batch.consumed_offset, batch.source_identity,
        batch.anchor_digest, batch.parser_state, known_size=batch.source_size,
        exhausted=batch.source_complete)


def _read(context, cursor=SourceCursor()):
    with using_runtime_context(context):
        return read_records(context, cursor, time.monotonic()+2)


def test_opaque_cross_budget_retains_later_owned_dialogue(selected_host_context):
    context, path, start = _case(selected_host_context)
    batch = _read(context)
    assert start < batch.consumed_offset < path.stat().st_size
    assert not batch.records and batch.status == 'partial'
    assert len(json.dumps(batch.parser_state)) < 12000
    texts = []
    for _ in range(8):
        batch = _read(context, _next(batch))
        texts.extend(record.text for record in batch.records)
        if texts:
            break
    assert texts == ['verified after opaque']
    assert batch.status == 'partial' and not batch.source_complete
    refs = batch.parser_state['_deferred_source_rows']
    assert refs[0]['reason'] == 'oversized:unrecognized'
    assert refs[0]['offset'] == start
    assert 'xxxxx' not in json.dumps(batch.parser_state)


def test_opaque_rewrite_cannot_publish_following_dialogue(selected_host_context):
    context, path, start = _case(selected_host_context)
    batch = _read(context)
    with path.open('r+b') as stream:
        stream.seek(start+100)
        stream.write(b'changed')
    later = _read(context, _next(batch))
    assert not later.records
    assert later.status == 'partial'
    assert any('pending' in warning for warning in later.warnings)


@pytest.mark.parametrize('damage', ['bool', 'overflow', 'hash'])
def test_corrupt_opaque_state_never_advances(selected_host_context, damage):
    context, _, _ = _case(selected_host_context)
    batch = _read(context)
    state = dict(batch.parser_state)
    drain = dict(state['_opaque_drain'])
    if damage == 'bool':
        drain['cursor'] = True
    elif damage == 'overflow':
        drain['chunk_lengths'] = [1024*1024]*65
    else:
        drain['chunk_sha256'] = ['private secret']
    state['_opaque_drain'] = drain
    cursor = replace(_next(batch), parser_state=state)
    later = _read(context, cursor)
    assert later.consumed_offset == cursor.offset
    assert not later.records and later.status == 'partial'


def test_opaque_truncated_tail_remains_pending(selected_host_context):
    context, path, _ = _case(selected_host_context, tail=False)
    batch = _read(context)
    for _ in range(5):
        batch = _read(context, _next(batch))
    assert not batch.records and not batch.source_complete
    assert batch.parser_state['_opaque_drain']['complete'] is False
    with path.open('ab') as stream:
        stream.write(b'\n')
    for _ in range(5):
        batch = _read(context, _next(batch))
    assert batch.status == 'partial' and not batch.source_complete
    assert batch.parser_state['_deferred_source_rows'][0]['reason'] == 'oversized:unrecognized'


def test_opaque_append_during_verification_keeps_pending(selected_host_context, monkeypatch):
    import transcripts
    context, path, _ = _case(selected_host_context)
    batch = _read(context)
    original = transcripts._source_version
    fired = []
    def append_after_version(info):
        value = original(info)
        if not fired:
            with path.open('ab') as stream:
                stream.write(b'\n')
            fired.append(True)
        return value
    monkeypatch.setattr(transcripts, '_source_version', append_after_version)
    later = _read(context, _next(batch))
    assert fired and not later.records and not later.source_complete
    # A stable subsequent version can finish verification; no append is assumed
    # to prove that the already-consumed source prefix stayed unchanged.
    monkeypatch.setattr(transcripts, '_source_version', original)
    texts = []
    for _ in range(8):
        later = _read(context, _next(later))
        texts.extend(record.text for record in later.records)
        if texts:
            break
    assert texts == ['verified after opaque']


def test_opaque_untrusted_row_cannot_establish_implicit_ownership(selected_host_context):
    context, path, _ = _case(selected_host_context, size=2*1024*1024)
    native = context.native_session_id
    if context.host == 'claude':
        ambiguous = {'type': 'user', 'uuid': 'ambiguous-after',
                     'message': {'role': 'user', 'content': 'unproven dialogue'}}
    else:
        ambiguous = {'type': 'response_item', 'payload': {'type': 'message', 'id': 'ambiguous-after',
            'role': 'user', 'content': [{'type': 'input_text', 'text': 'unproven dialogue'}],
            'internal_chat_message_metadata_passthrough': {'turn_id': 'unproven-turn', 'content_item_kinds': ['user.text'], 'create_time': 1}}}
    data = path.read_bytes()
    last = data.rfind(b'\n', 0, len(data)-1)+1
    path.write_bytes(data[:last]+json.dumps(ambiguous).encode()+b'\n'+data[last:])
    texts = []
    batch = _read(context)
    for _ in range(6):
        texts.extend(record.text for record in batch.records)
        batch = _read(context, _next(batch))
    assert texts == ['verified after opaque']
    assert any(ref['reason'] == 'ambiguous_ownership' for ref in batch.parser_state['_deferred_source_rows'])


def test_opaque_deadline_and_maximum_are_visible_pending(selected_host_context, monkeypatch):
    import transcripts
    context, _, _ = _case(selected_host_context)
    batch = _read(context)
    with using_runtime_context(context):
        expired = read_records(context, _next(batch), time.monotonic()-1)
    assert expired.consumed_offset == batch.consumed_offset and not expired.records
    monkeypatch.setattr(transcripts, 'MAX_OPAQUE_BYTES', 4*1024*1024)
    cursor = _next(batch)
    for _ in range(4):
        batch = _read(context, cursor)
        cursor = _next(batch)
    assert not batch.records and not batch.source_complete
    assert batch.parser_state['_opaque_drain']['complete'] is False
    assert any('pending' in warning for warning in batch.warnings)


def test_oversized_header_never_establishes_identity(selected_host_context):
    context, path, _ = _case(selected_host_context)
    path.write_bytes(b'{"sessionId":"'+context.native_session_id.encode()+b'","private":"'+b'x'*1048576+b'"}\n')
    batch = _read(context)
    assert batch.status == 'unsupported' and batch.consumed_offset == 0
    assert not batch.source_identity and not batch.records


def test_completed_opaque_reference_rechecks_changed_prefix(selected_host_context):
    context, path, start = _case(selected_host_context, size=2*1024*1024)
    batch = _read(context)
    for _ in range(4):
        batch = _read(context, _next(batch))
    with path.open('r+b') as stream:
        stream.seek(start+100)
        stream.write(b'changed')
    changed = _read(context, _next(batch))
    assert changed.status == 'partial'
    assert any('source changed' in warning for warning in changed.warnings)
    assert changed.parser_state['_deferred_source_rows']


def test_continuous_append_during_verification_is_visible_liveness_limit(selected_host_context, monkeypatch):
    import transcripts
    context, path, _ = _case(selected_host_context)
    batch = _read(context)
    original = transcripts._source_version
    appends = []
    def changing_version(info):
        version = original(info)
        with path.open('ab') as stream:
            stream.write(b' ')
        appends.append(True)
        return version
    monkeypatch.setattr(transcripts, '_source_version', changing_version)
    for _ in range(4):
        batch = _read(context, _next(batch))
        assert batch.status == 'partial' and not batch.records
        assert not batch.source_complete
    assert appends and batch.parser_state['_opaque_drain']
    assert any('pending' in warning for warning in batch.warnings)


@pytest.mark.parametrize('kind', ['user', 'assistant', 'malformed'])
def test_oversized_body_is_never_decoded_or_trusted(selected_host_context, monkeypatch, kind):
    import transcripts
    context, path, _ = _case(selected_host_context, size=2*1024*1024)
    source = path.read_bytes().replace(b'"future_record"', json.dumps(kind).encode(), 1)
    # This deliberately resembles native identity inside an opaque body. Neither
    # its identity nor its shape is consulted, even when the JSON is malformed.
    source = source.replace(b'"private":"', b'"sessionId":"foreign-private-session","private":"', 1)
    if kind == 'malformed':
        header_end = source.index(b'\n')+1
        source = source[:header_end]+source[header_end:].replace(b'"}\n', b'not-json}\n', 1)
    path.write_bytes(source)
    original = transcripts._strict_json
    decoded_lengths = []
    def bounded_json(line):
        decoded_lengths.append(len(line))
        assert len(line) <= transcripts.MAX_RECORD_BYTES
        return original(line)
    monkeypatch.setattr(transcripts, '_strict_json', bounded_json)
    texts = []
    batch = _read(context)
    for _ in range(6):
        texts.extend(record.text for record in batch.records)
        batch = _read(context, _next(batch))
    assert texts == ['verified after opaque']
    assert decoded_lengths and batch.status == 'partial'
    assert 'foreign-private-session' not in json.dumps(batch.parser_state)
    assert batch.parser_state['_deferred_source_rows'][0]['reason'] == 'oversized:unrecognized'


@pytest.mark.parametrize('size',[10*1024*1024,70*1024*1024])
def test_opaque_progress_between_appends_keeps_later_owned_turns(selected_host_context,size):
    context,path,_=_case(selected_host_context,size=size)
    batch=_read(context)
    texts=[]
    for number in range(24):
        if context.host=='claude':
            row={'type':'user','sessionId':context.native_session_id,'uuid':f'tick-{number}',
                 'message':{'role':'user','content':f'owned tick {number}'}}
        else:
            row={'type':'response_item','payload':{'type':'message','id':f'tick-{number}',
                'role':'user','content':[{'type':'input_text','text':f'owned tick {number}'}],
                'thread_id':context.native_session_id,'turn_id':f'tick-turn-{number}'}}
        with path.open('ab') as stream:stream.write(json.dumps(row).encode()+b'\n')
        before=batch.consumed_offset
        batch=_read(context,_next(batch))
        assert 0 <= batch.consumed_offset-before <= 4*1024*1024
        texts.extend(record.text for record in batch.records)
        if 'verified after opaque' in texts:break
    assert 'verified after opaque' in texts and any(text.startswith('owned tick ') for text in texts)
    assert batch.status=='partial' and not batch.source_complete
    assert 'xxxxx' not in json.dumps(batch.parser_state)


def test_transient_earlier_block_cannot_save_ahead_of_acknowledged_cursor(selected_host_context):
    from transcripts import read_source,ParsedRecords
    from transcripts.claude import parse_rows as claude_parser
    from transcripts.codex import parse_rows as codex_parser
    context,path,start=_case(selected_host_context)
    native=claude_parser if context.host=='claude' else codex_parser
    calls=[]
    def transient(ctx,rows,header,state):
        calls.append(True)
        if len(calls)==1:
            return ParsedRecords(status='partial',blocked_offset=header.offset,parser_state=state)
        return native(ctx,rows,header,state)
    with using_runtime_context(context):
        batch=read_source(context,SourceCursor(),time.monotonic()+2,transient)
        assert batch.consumed_offset==0 and '_opaque_drain' not in batch.parser_state
        texts=[]
        for _ in range(5):
            batch=read_source(context,_next(batch),time.monotonic()+2,transient)
            texts.extend(record.text for record in batch.records)
        assert texts==['verified after opaque']


@pytest.mark.parametrize('at_limit', [False, True])
def test_chunk_ceiling_does_not_acknowledge_257th_chunk(selected_host_context, at_limit):
    import hashlib
    from transcripts import _drain_opaque,MAX_OPAQUE_CHUNKS
    path=selected_host_context.native_home/'chunk-ceiling';path.parent.mkdir(parents=True,exist_ok=True)
    count=MAX_OPAQUE_CHUNKS if at_limit else MAX_OPAQUE_CHUNKS-1
    raw=b'x'*count+b'\n';path.write_bytes(raw)
    value=dict(source_identity='bound-source',generation='a'*64,start=0,cursor=count,
        chunk_sha256=[hashlib.sha256(b'x').hexdigest()]*count,
        chunk_lengths=[1]*count,complete=False,end=None,
        verification_cursor=0,verification_version=None)
    with path.open('rb') as stream:
        saved,used,complete=_drain_opaque(stream,value,'bound-source','a'*64,
            count,time.monotonic()+2,1)
    assert complete is (not at_limit) and used==int(not at_limit)
    assert saved['cursor']==count+used and len(saved['chunk_lengths'])==count+used
    assert path.read_bytes()==raw


def test_adapter_failure_drops_incompatible_drain_and_retry_recovers_dialogue(selected_host_context):
    from transcripts import read_source
    from transcripts.claude import parse_rows as claude
    from transcripts.codex import parse_rows as codex
    from runtime_context import using_runtime_context
    ctx,path,start=_case(selected_host_context);raw=path.read_bytes()
    batch=_read(ctx);assert '_opaque_drain' in batch.parser_state
    native=claude if ctx.host=='claude' else codex
    def rejected(*args):raise ValueError('controlled adapter failure')
    with using_runtime_context(ctx):
        rejected_batch=read_source(ctx,_next(batch),time.monotonic()+2,rejected)
        assert rejected_batch.status=='unsupported' and not rejected_batch.records
        assert rejected_batch.consumed_offset==start
        assert '_opaque_drain' not in rejected_batch.parser_state
        batch=rejected_batch;texts=[]
        for _ in range(5):
            batch=read_source(ctx,_next(batch),time.monotonic()+2,native)
            texts.extend(r.text for r in batch.records)
        assert texts==['verified after opaque']
        assert batch.status=='partial' and not batch.source_complete
    assert path.read_bytes()==raw


def test_opaque_row_keeps_reader_partial_even_if_adapter_reports_ok(selected_host_context):
    from transcripts import read_source,ParsedRecords,SourceRecord,SourceCursor
    from runtime_context import using_runtime_context
    ctx,path,start=_case(selected_host_context,size=2*1024*1024)
    def recognized(ctx,rows,header,state):
        following=next(row for row in rows if row.offset>start)
        return ParsedRecords(records=(SourceRecord('own-after','user','verified after opaque',following.offset),),
                             metadata={'native_session_id':ctx.native_session_id},parser_state=state)
    with using_runtime_context(ctx):
        batch=read_source(ctx,SourceCursor(),time.monotonic()+2,recognized)
    assert [r.text for r in batch.records]==['verified after opaque']
    assert batch.consumed_offset==path.stat().st_size
    assert batch.status=='partial' and not batch.source_complete
    assert batch.parser_state['_deferred_source_rows'][0]['reason']=='oversized:unrecognized'
    assert 'xxxxx' not in str(batch.parser_state)
