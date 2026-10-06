"""Real native adapters retain covered chunks after ordinary child failures."""
import json
import subprocess
from pathlib import Path

import pytest
import ai_backend as backend
import check_items_cli as cli
from check_items_test_helpers import selected_host_context,native_ai_context,private_output,verdicts

_EXECUTE_AI=backend.execute_ai
_NATIVE_POPEN=subprocess.Popen


@pytest.mark.parametrize('failed_group', ['g1','g2'])
@pytest.mark.parametrize('failures', [1,2])
def test_ordinary_native_exit_retries_chunk_and_preserves_covered_results(selected_host_context,monkeypatch,capsys,failed_group,failures):
    from ai_adapters import codex
    context=selected_host_context
    groups=[{'group_id':f'g{number}','project':'demo','representative':f'Fix bug #{number}'} for number in range(1,4)]
    calls=[]
    def child(command,prompt,**kwargs):
        inputs=json.loads(prompt.split('Input JSON:\n',1)[1].split(backend.ANALYSIS_INSTRUCTION,1)[0])
        requested=inputs['groups'];gid=requested[0]['group_id'];calls.append(gid)
        if gid==failed_group and calls.count(gid)<=failures:
            return 1,b'',b'transient native overload'
        data={"items":verdicts(requested)}
        if context.host=='claude':
            return 0,json.dumps({'structured_output':data,'is_error':False,
                'modelUsage':{context.config['classifier_model']:{}}}).encode(),b''
        target=Path(command[command.index('--output-last-message')+1])
        target.write_text(json.dumps(data));target.chmod(0o600)
        rows=[{'type':'thread.started','thread_id':'isolated-analysis'}, {'type':'turn.started'},
              {'type':'item.completed','item':{'type':'agent_message','text':'Completed'}}, {'type':'turn.completed'}]
        return 0,b'\n'.join(json.dumps(row).encode() for row in rows),b''
    monkeypatch.setattr(backend,'execute_ai',_EXECUTE_AI)
    monkeypatch.setattr(backend,'_run_bounded',child)
    monkeypatch.setattr(codex,'_run_bounded',child)
    monkeypatch.setattr(codex,'discover_restrictions',lambda *args:(codex.restrictions(),context.config['codex_ai_model']))
    monkeypatch.setattr(cli,'CLASSIFIER_CHUNK_SIZE',1)
    monkeypatch.setenv('CHECK_ITEMS_PREFILTER','off')
    output=private_output('classified.json');output.write_text('previous output')
    assert cli.run_classifier(json.dumps({'groups':groups,'evidence':{}}),str(output))==0
    expected=['g1','g2','g3'] if failures==1 else [group['group_id'] for group in groups if group['group_id']!=failed_group]
    assert [row['group_id'] for row in json.loads(output.read_text())]==expected
    assert calls.count(failed_group)==2 and calls[-1]=='g3'
    telemetry=capsys.readouterr().err
    if failures==2:
        assert 'failed_chunks=1 unclassified=1' in telemetry
    else:
        assert 'subagent=3' in telemetry and 'unclassified=' not in telemetry
    assert output.stat().st_mode & 0o777==0o600


@pytest.mark.parametrize('stderr', [b'401 authentication required',b'permission denied',None])
def test_native_auth_policy_and_missing_executable_abort_without_retry(selected_host_context,monkeypatch,stderr):
    from ai_adapters import codex
    context=selected_host_context;calls=[]
    def child(*args,**kwargs):
        calls.append(args)
        if stderr is None:
            raise backend._BackendFailure('unavailable','executable_missing')
        return 1,b'',stderr
    monkeypatch.setattr(backend,'execute_ai',_EXECUTE_AI)
    monkeypatch.setattr(backend,'_run_bounded',child)
    monkeypatch.setattr(codex,'_run_bounded',child)
    monkeypatch.setattr(codex,'discover_restrictions',lambda *args:(codex.restrictions(),context.config['codex_ai_model']))
    monkeypatch.setenv('CHECK_ITEMS_PREFILTER','off')
    output=private_output('blocked.json');output.write_text('previous output')
    assert cli.run_classifier(json.dumps({'groups':[{'group_id':'g1','project':'demo'}],'evidence':{}}),str(output))==8
    assert len(calls)==1 and output.read_text()=='previous output'


def test_persistent_native_failure_does_not_repeat_exhausted_classifier(selected_host_context,monkeypatch,capsys):
    from ai_adapters import codex
    import open_item_dedup as dedup
    context=selected_host_context
    groups=[{'group_id':f'g{number}','project':'demo','representative':f'Fix bug #{number}'} for number in range(1,4)]
    calls=[]
    def child(command,prompt,**kwargs):
        inputs=json.loads(prompt.split('Input JSON:\n',1)[1].split(backend.ANALYSIS_INSTRUCTION,1)[0])
        calls.append(inputs['groups'][0]['group_id'])
        return 1,b'',b'transient native overload'
    monkeypatch.setattr(backend,'execute_ai',_EXECUTE_AI)
    monkeypatch.setattr(backend,'_run_bounded',child)
    monkeypatch.setattr(codex,'_run_bounded',child)
    monkeypatch.setattr(codex,'discover_restrictions',lambda *args:(codex.restrictions(),context.config['codex_ai_model']))
    monkeypatch.setattr(cli,'CLASSIFIER_CHUNK_SIZE',1)
    monkeypatch.setenv('CHECK_ITEMS_PREFILTER','off')
    note=context.vault_path/'manual.md';note.write_text('Manual body remains unchanged')
    assert dedup.classify_groups_with_agent(groups,{})==[]
    assert calls==['g1','g1','g2','g2','g3','g3']
    diagnostic=capsys.readouterr().err
    assert 'operation remains pending; no classification was published' in diagnostic
    assert 'falling back to' not in diagnostic
    assert note.read_text()=='Manual body remains unchanged'

@pytest.mark.parametrize('failed_group', ['g1','g2'])
@pytest.mark.parametrize('failures', [1,2])
def test_wrapper_preserves_native_chunk_retries_and_partial_results(selected_host_context,monkeypatch,capsys,failed_group,failures):
    from ai_adapters import codex
    import open_item_dedup as dedup
    context=selected_host_context
    groups=[{'group_id':f'g{number}','project':'demo','representative':f'Fix bug #{number}'} for number in range(1,4)]
    calls=[]
    def child(command,prompt,**kwargs):
        inputs=json.loads(prompt.split('Input JSON:\n',1)[1].split(backend.ANALYSIS_INSTRUCTION,1)[0])
        requested=inputs['groups'];gid=requested[0]['group_id'];calls.append(gid)
        if gid==failed_group and calls.count(gid)<=failures:
            return 1,b'',b'transient native overload'
        data={"items":verdicts(requested)}
        if context.host=='claude':
            return 0,json.dumps({'structured_output':data,'is_error':False,
                'modelUsage':{context.config['classifier_model']:{}}}).encode(),b''
        target=Path(command[command.index('--output-last-message')+1])
        target.write_text(json.dumps(data));target.chmod(0o600)
        rows=[{'type':'thread.started','thread_id':'isolated-analysis'}, {'type':'turn.started'},
              {'type':'item.completed','item':{'type':'agent_message','text':'Completed'}}, {'type':'turn.completed'}]
        return 0,b'\n'.join(json.dumps(row).encode() for row in rows),b''
    monkeypatch.setattr(backend,'execute_ai',_EXECUTE_AI)
    monkeypatch.setattr(backend,'_run_bounded',child)
    monkeypatch.setattr(codex,'_run_bounded',child)
    monkeypatch.setattr(codex,'discover_restrictions',lambda *args:(codex.restrictions(),context.config['codex_ai_model']))
    monkeypatch.setattr(cli,'CLASSIFIER_CHUNK_SIZE',1)
    monkeypatch.setenv('CHECK_ITEMS_PREFILTER','off')
    output=private_output('classified.json');output.write_text('previous output')
    results=dedup.classify_groups_with_agent(groups,{})
    expected=['g1','g2','g3'] if failures==1 else [group['group_id'] for group in groups if group['group_id']!=failed_group]
    assert [row['group_id'] for row in results]==expected
    assert calls.count(failed_group)==2 and calls[-1]=='g3'
    telemetry=capsys.readouterr().err
    if failures==2:
        assert 'failed_chunks=1 unclassified=1' in telemetry
    else:
        assert 'subagent=3' in telemetry and 'unclassified=' not in telemetry
    assert output.read_text()=='previous output'




@pytest.mark.parametrize('returncode',[3,4,7,8])
def test_bound_wrapper_nonzero_is_terminal(selected_host_context,monkeypatch,capsys,returncode):
    import open_item_dedup as dedup
    calls=[]
    def exhausted(*args):calls.append(args);return returncode
    monkeypatch.setattr(cli,'run_classifier',exhausted)
    assert dedup.classify_groups_with_agent([{'group_id':'g1','project':'demo'}],{})==[]
    assert len(calls)==1
    assert 'operation remains pending' in capsys.readouterr().err


def test_bound_wrapper_retries_successful_but_invalid_envelope(selected_host_context,monkeypatch):
    import open_item_dedup as dedup
    groups=[{'group_id':'g1','project':'demo'}];calls=[]
    def local_output(payload,output):
        calls.append(payload)
        Path(output).write_text(json.dumps([{'invalid':True}] if len(calls)==1 else verdicts(groups)))
        return 0
    monkeypatch.setattr(cli,'run_classifier',local_output)
    assert [row['group_id'] for row in dedup.classify_groups_with_agent(groups,{})]==['g1']
    assert len(calls)==2

@pytest.mark.parametrize('failures', [1,2])
def test_single_chunk_wrapper_preserves_native_retry_budget(selected_host_context,monkeypatch,capsys,failures):
    failed_group="g1"
    from ai_adapters import codex
    import open_item_dedup as dedup
    context=selected_host_context
    groups=[{'group_id':f'g{number}','project':'demo','representative':f'Fix bug #{number}'} for number in range(1,2)]
    calls=[]
    def child(command,prompt,**kwargs):
        inputs=json.loads(prompt.split('Input JSON:\n',1)[1].split(backend.ANALYSIS_INSTRUCTION,1)[0])
        requested=inputs['groups'];gid=requested[0]['group_id'];calls.append(gid)
        if gid==failed_group and calls.count(gid)<=failures:
            return 1,b'',b'transient native overload'
        data={"items":verdicts(requested)}
        if context.host=='claude':
            return 0,json.dumps({'structured_output':data,'is_error':False,
                'modelUsage':{context.config['classifier_model']:{}}}).encode(),b''
        target=Path(command[command.index('--output-last-message')+1])
        target.write_text(json.dumps(data));target.chmod(0o600)
        rows=[{'type':'thread.started','thread_id':'isolated-analysis'}, {'type':'turn.started'},
              {'type':'item.completed','item':{'type':'agent_message','text':'Completed'}}, {'type':'turn.completed'}]
        return 0,b'\n'.join(json.dumps(row).encode() for row in rows),b''
    monkeypatch.setattr(backend,'execute_ai',_EXECUTE_AI)
    monkeypatch.setattr(backend,'_run_bounded',child)
    monkeypatch.setattr(codex,'_run_bounded',child)
    monkeypatch.setattr(codex,'discover_restrictions',lambda *args:(codex.restrictions(),context.config['codex_ai_model']))
    monkeypatch.setattr(cli,'CLASSIFIER_CHUNK_SIZE',1)
    monkeypatch.setenv('CHECK_ITEMS_PREFILTER','off')
    output=private_output('classified.json');output.write_text('previous output')
    results=dedup.classify_groups_with_agent(groups,{})
    expected=['g1'] if failures==1 else [group['group_id'] for group in groups if group['group_id']!=failed_group]
    assert [row['group_id'] for row in results]==expected
    assert calls.count(failed_group)==2 and calls[-1]=='g1'
    telemetry=capsys.readouterr().err
    if failures==2:
        assert 'operation remains pending' in telemetry
    else:
        assert 'subagent=1' in telemetry and 'unclassified=' not in telemetry
    assert output.read_text()=='previous output'


@pytest.mark.parametrize('pinned',[False,True])
def test_two_runs_use_configured_model_for_cache_with_fake_native_cli(selected_host_context,monkeypatch,tmp_path,pinned):
    import copy
    import hashlib
    import sys
    import check_items_cache as cache
    from ai_adapters import codex
    context=selected_host_context
    model='claude-haiku-4-5-20251001' if context.host=='claude' else 'gpt-native-pinned'
    for key in ('classifier_model','codex_ai_model','codex_summary_model'):
        context.config.pop(key,None)
    if pinned:
        context.config['classifier_model' if context.host=='claude' else 'codex_ai_model']=model
    log=tmp_path/'native-calls.jsonl'
    child=tmp_path/'fake-native-cli'
    source='''import sys,json,os
from pathlib import Path
prompt=sys.stdin.read()
inputs=json.loads(prompt.split('Input JSON:\\n',1)[1].split('\\n\\n',1)[0])
items=[{'group_id':g['group_id'],'classification':'ACTIVE','confidence':'LOW',
        'canonical_text':g['representative'],'evidence_citation':None,'action_required':None}
       for g in inputs['groups']]
with Path(LOG).open('a') as stream:stream.write(json.dumps({'groups':[g['group_id'] for g in inputs['groups']]})+'\\n')
if HOST=='claude':
 print(json.dumps({'structured_output':{'items':items},'is_error':False,'modelUsage':{MODEL:{}}}))
else:
 target=Path(sys.argv[sys.argv.index('--output-last-message')+1]);target.write_text(json.dumps({'items':items}));target.chmod(0o600)
 for row in [{'type':'thread.started','thread_id':'synthetic-analysis'}, {'type':'turn.started'},
             {'type':'item.completed','item':{'type':'agent_message','text':'Completed'}},{'type':'turn.completed'}]:
  print(json.dumps(row))
'''
    child.write_text('#!'+sys.executable+'\n'+f'LOG={str(log)!r}\nHOST={context.host!r}\nMODEL={model!r}\n'+source)
    child.chmod(0o700)
    context.config['claude_executable' if context.host=='claude' else 'codex_executable']=str(child)
    def controlled_child(command,*args,**kwargs):
        from runtime_context import current_runtime_context
        assert current_runtime_context() is context
        assert command[0]==str(child) and child.is_file() and not child.is_symlink()
        assert kwargs['cwd']==context.worktree
        assert kwargs['env']['XDG_STATE_HOME']==str(context.coordination_root)
        return _NATIVE_POPEN(command,*args,**kwargs)
    monkeypatch.setattr(subprocess,'Popen',controlled_child)
    monkeypatch.setattr(backend,'execute_ai',_EXECUTE_AI)
    monkeypatch.setattr(codex,'_run_bounded',backend._run_bounded)
    monkeypatch.setattr(codex,'discover_restrictions',lambda *args:(codex.restrictions(),model))
    monkeypatch.setenv('CHECK_ITEMS_PREFILTER','off')
    inputs=[{'group_id':'g1','project':'demo','representative':'Fix bug #87','members':[],
             'canonical_hash':cache.canonical_hash('Fix bug #87')}]
    evidence={};state={'runs':{}}
    status=cache.classifier_cache_replay_status(context)
    assert status['enabled']==pinned
    for run in range(2):
        provenance=cache.build_classifier_provenance(context,inputs,evidence)
        hits,needs=cache.partition(copy.deepcopy(inputs),state,'demo','head',now=100+run,provenance=provenance)
        assert len(hits)==(1 if pinned and run==1 else 0)
        if needs:
            output=private_output('actual-native.json')
            assert cli.run_classifier(json.dumps({'groups':needs,'evidence':evidence}),str(output))==0
            results=json.loads(output.read_text())
            assert results[0]['ai_backend']==context.host and results[0]['ai_model']==model
            fresh=[dict(results[0],canonical_hash=inputs[0]['canonical_hash'],members=[],classifier_source='agent')]
            observed=cache.build_classifier_provenance(context,inputs,evidence,fresh)
            cache.update_cache(state,'demo',inputs,fresh,'head',now=100+run,provenance=observed)
    calls=[json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls)==(1 if pinned else 2)
    assert all(call=={'groups':['g1']} for call in calls)
