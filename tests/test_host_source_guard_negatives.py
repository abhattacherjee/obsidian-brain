"""Deferral references cannot bypass their reason or generation contracts."""
import json
import time
from dataclasses import replace

import pytest
from runtime_context import using_runtime_context
from transcripts import DeferredRow, ParsedRecords, SourceCursor, read_records, read_source


def _source(context):
    path = context.native_home / ('projects' if context.host=='claude' else 'sessions') / 'guard.jsonl'
    path.parent.mkdir(parents=True,exist_ok=True)
    header = ({'type':'mode','mode':'normal','sessionId':context.native_session_id} if context.host=='claude'
              else {'type':'session_meta','payload':{'id':context.native_session_id}})
    prefix = json.dumps(header).encode()+b'\n'
    path.write_bytes(prefix+b'{"type":"future_record","accountUuid":"PRIVATE ACCOUNT"}\n')
    return replace(context,transcript_path=path),path,len(prefix)


def _cursor(batch):
    return SourceCursor(batch.source_generation,batch.consumed_offset,batch.source_identity,
        batch.anchor_digest,batch.parser_state,known_size=batch.source_size)


@pytest.mark.parametrize('reason',['invented','unknown_schema:future\nsecret','unknown_schema:'+'x'*49,'oversized:unrecognized'])
def test_unverified_deferral_reason_cannot_acknowledge_row(selected_host_context,reason):
    context,path,start = _source(selected_host_context)
    def unsupported_reason(context,rows,header,state):
        return ParsedRecords(deferred_rows=tuple(DeferredRow(row.offset,row.end_offset,reason)
            for row in rows if row.offset==start))
    with using_runtime_context(context):
        batch = read_source(context,SourceCursor(),time.monotonic()+2,unsupported_reason)
    assert batch.status == 'partial' and batch.consumed_offset == start
    assert not batch.parser_state.get('_deferred_source_rows')
    assert not batch.records and any('Unverified' in warning for warning in batch.warnings)


def test_deferred_generation_mismatch_retains_reference_and_later_own_fact(selected_host_context):
    context,path,_ = _source(selected_host_context)
    with using_runtime_context(context):
        first = read_records(context,SourceCursor(),time.monotonic()+2)
    state = dict(first.parser_state)
    refs = [dict(ref) for ref in state['_deferred_source_rows']]
    refs[0]['source_generation'] = '0'*64
    state['_deferred_source_rows'] = refs
    if context.host=='claude':
        own = {'type':'user','uuid':'own-fresh','sessionId':context.native_session_id,
               'message':{'role':'user','content':'Verified own fact'}}
    else:
        own = {'type':'response_item','payload':{'type':'message','id':'own-fresh','role':'user',
            'thread_id':context.native_session_id,'content':[{'type':'input_text','text':'Verified own fact'}]}}
    with path.open('ab') as stream:
        stream.write(json.dumps(own).encode()+b'\n')
    with using_runtime_context(context):
        second = read_records(context,replace(_cursor(first),parser_state=state),time.monotonic()+2)
    assert [record.text for record in second.records] == ['Verified own fact']
    assert second.status == 'partial' and not second.source_complete
    assert second.parser_state['_deferred_source_rows'][0]['source_generation'] == '0'*64
    assert any('source changed' in warning for warning in second.warnings)
    assert 'PRIVATE ACCOUNT' not in json.dumps(second.parser_state)


@pytest.mark.parametrize('damage',['sum','identity','generation','cap'])
def test_opaque_state_guards_reject_before_reading_private_bytes(selected_host_context,tmp_path,damage):
    import hashlib
    import transcripts
    count=transcripts.MAX_OPAQUE_CHUNKS+1 if damage=='cap' else 2
    path=tmp_path/'opaque-unit';path.write_bytes(b'a'*count)
    value={'source_identity':'identity','generation':'generation','start':0,'cursor':count,
        'chunk_lengths':[1]*count,'chunk_sha256':[hashlib.sha256(b'a').hexdigest()]*count,
        'complete':True,'end':count,'verification_cursor':0,'verification_version':None}
    if damage=='sum':value['start']=1
    elif damage=='identity':value['source_identity']='other'
    elif damage=='generation':value['generation']='other'
    calls=[]
    with path.open('rb') as source:
        class NoRead:
            def fileno(self):return source.fileno()
            def seek(self,*args):calls.append(True);raise AssertionError('Invalid opaque state reached source IO')
        with pytest.raises(ValueError,match='Invalid opaque drain state'):
            transcripts._drain_opaque(NoRead(),value,'identity','generation',count,time.monotonic()+1,1024)
    assert not calls


def test_regular_batch_budget_retains_unread_owned_rows(selected_host_context,monkeypatch):
    import transcripts
    context,path,_=_source(selected_host_context)
    native=context.native_session_id
    rows=[]
    for number in range(30):
        if context.host=='claude':
            row={'type':'user','sessionId':native,'uuid':f'budget-{number}',
                'message':{'role':'user','content':f'owned-{number}'}}
        else:
            row={'type':'response_item','payload':{'type':'message','id':f'budget-{number}',
                'role':'user','thread_id':native,'turn_id':f'turn-{number}',
                'content':[{'type':'input_text','text':f'owned-{number}'}]}}
        rows.append(json.dumps(row).encode()+b'\n')
    header=path.read_bytes().split(b'\n',1)[0]+b'\n';path.write_bytes(header+b''.join(rows))
    budget=len(header)+sum(map(len,rows[:3]))
    monkeypatch.setattr(transcripts,'MAX_BATCH_BYTES',budget)
    import os
    original=os.fdopen
    read_positions=[]
    class BoundedReads:
        def __init__(self,stream):self.stream=stream
        def __enter__(self):return self
        def __exit__(self,*args):return self.stream.__exit__(*args)
        def __getattr__(self,name):return getattr(self.stream,name)
        def readline(self,*args):
            read_positions.append(self.stream.tell())
            return self.stream.readline(*args)
    monkeypatch.setattr(os,'fdopen',lambda *args,**kwargs:BoundedReads(original(*args,**kwargs)))
    with using_runtime_context(context):
        first=read_records(context,SourceCursor(),time.monotonic()+2)
    assert first.status=='partial' and 0<len(first.records)<30
    assert first.consumed_offset==budget<path.stat().st_size
    assert all(position<budget for position in read_positions)


@pytest.mark.parametrize('field',['source_path','source_identity','source_generation','digest_kind'])
def test_opaque_replay_identity_refused_before_verification(selected_host_context,monkeypatch,field):
    import transcripts
    from test_host_opaque_transcript import _case,_read,_next
    context,path,_=_case(selected_host_context,size=1024*1024+100)
    first=_read(context)
    refs=[dict(ref) for ref in first.parser_state['_deferred_source_rows']]
    refs[0][field]='different'
    state={**first.parser_state,'_deferred_source_rows':refs}
    def forbidden(*args,**kwargs):
        raise AssertionError('Foreign opaque reference reached verification')
    monkeypatch.setattr(transcripts,'_drain_opaque',forbidden)
    later=_read(context,replace(_next(first),parser_state=state))
    assert later.status=='partial' and not later.records
    assert any('source changed' in warning for warning in later.warnings)
    assert later.parser_state['_deferred_source_rows'][0][field]=='different'


@pytest.mark.parametrize('damage',[None,'absent','foreign','malformed','wrong-parent','rewrite'])
def test_full_fork_child_metadata_after_opaque_row_remains_reachable(selected_host_context,monkeypatch,damage):
    from dataclasses import replace
    import transcripts
    from runtime_context import using_runtime_context
    context=selected_host_context
    path=context.native_home/('projects' if context.host=='claude' else 'sessions')/'long-fork.jsonl'
    path.parent.mkdir(parents=True,exist_ok=True)
    parent={'type':'session_meta','payload':{'id':'parent'}}
    child={'type':'session_meta','payload':{'id':context.native_session_id,'forked_from_id':'parent',
        'instructions':'PRIVATE CHILD INSTRUCTIONS'}}
    if damage=='foreign':child['payload']['id']='foreign-child'
    elif damage=='malformed':child['payload']=[]
    elif damage=='wrong-parent':child['payload']['forked_from_id']='unrelated-parent'
    own={'type':'response_item','payload':{'type':'message','id':'own-after','role':'user',
        'thread_id':context.native_session_id,'turn_id':'own-turn',
        'content':[{'type':'input_text','text':'child question'}]}}
    opaque=b'{"type":"future_record","private":"'+b'x'*(5*1024*1024)+b'"}\n'
    if context.host=='claude':
        parent={'type':'mode','mode':'normal','sessionId':context.native_session_id}
        before={'type':'user','uuid':'claude-before','sessionId':context.native_session_id,
                'message':{'role':'user','content':'Claude before opaque'}}
        own={'type':'user','uuid':'claude-after','sessionId':context.native_session_id,
             'message':{'role':'user','content':'Claude after opaque'}}
        def forbidden_discovery(*args,**kwargs):
            raise AssertionError('Claude source reached Codex metadata discovery')
        monkeypatch.setattr(transcripts,'_discover_codex_child_metadata',forbidden_discovery)
    prefix=json.dumps(parent).encode()+b'\n'
    before_bytes=json.dumps(before).encode()+b'\n' if context.host=='claude' else b''
    path.write_bytes(prefix+before_bytes+opaque+(b'' if damage=='absent' else json.dumps(child).encode()+b'\n')+json.dumps(own).encode()+b'\n')
    selected=replace(context,transcript_path=path)
    if damage=='rewrite' and context.host=='codex':
        original=transcripts._discover_codex_child_metadata
        changes=[]
        def changed(*args,**kwargs):
            changes.append(True)
            with path.open('r+b') as stream:
                stream.seek(len(prefix)+100);stream.write(f'diff{len(changes):03}'.encode())
            return original(*args,**kwargs)
        monkeypatch.setattr(transcripts,'_discover_codex_child_metadata',changed)
    texts=[];cursor=SourceCursor()
    with using_runtime_context(selected):
        for _ in range(5):
            batch=read_records(selected,cursor,time.monotonic()+2)
            assert batch.status!='unavailable'
            if context.host=='claude':
                assert 'Selected Codex session metadata is pending' not in batch.warnings
            texts.extend(record.text for record in batch.records)
            drain=batch.parser_state.get('_opaque_drain')
            assert drain is None or drain['cursor']==batch.consumed_offset
            assert 'PRIVATE CHILD INSTRUCTIONS' not in json.dumps(batch.parser_state)
            cursor=SourceCursor(batch.source_generation,batch.consumed_offset,batch.source_identity,
                batch.anchor_digest,batch.parser_state,known_size=batch.source_size)
    if context.host=='claude':
        # The native parser runs and retains its own dialogue around foreign
        # Codex metadata, without invoking Codex fork discovery.
        assert texts==['Claude before opaque','Claude after opaque']
    else:
        assert texts==(['child question'] if damage is None else [])
