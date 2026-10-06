"""Two operation runs replay only unchanged semantic classification input."""
import copy
import hashlib
import json
import time
import uuid

import pytest
import check_items_cache as cache
import skill_procedures as procedures
from check_items_test_helpers import selected_host_context, native_ai_context
from check_items_cli import CLASSIFIER_PROMPT


def group(identifier, revision='source-sha'):
    return {'group_id':identifier, 'project':'demo', 'representative':'Fix bug #87',
            'canonical_hash':cache.canonical_hash('Fix bug #87'),
            'members':[{'file':'session.md','line':8,'text':'Fix bug #87','mtime':100,'source_revision':revision}]}


def test_new_random_group_id_reuses_durable_verdict_and_source_change_misses(selected_host_context):
    first = group(uuid.uuid4().hex[:8])
    evidence = {'demo':{'git_commits':[{'sha':'abc1234','message':'Fix bug #87'}]}}
    planned = cache.build_classifier_provenance(selected_host_context,[first],evidence)
    model = selected_host_context.config['classifier_model' if selected_host_context.host == 'claude' else 'codex_ai_model']
    verdict = {'group_id':first['group_id'], 'canonical_hash':first['canonical_hash'],
        'canonical_text':first['representative'], 'members':first['members'],
        'classification':'DONE','confidence':'HIGH','evidence_citation':'commit abc1234',
        'classifier_source':'agent','ai_backend':selected_host_context.host,'ai_model':model,
        'ai_prompt_version':'check-items-classifier-v3',
        'ai_prompt_sha256':hashlib.sha256(CLASSIFIER_PROMPT.encode()).hexdigest(),
        'classified_ts':time.time()}
    proved = cache.build_classifier_provenance(selected_host_context,[first],evidence,[verdict])
    with cache.locked_cache() as state:
        cache.update_cache(state,'demo',[first],[verdict],'head',provenance=proved)
    second = group(uuid.uuid4().hex[:8]); second['members'][0]['mtime'] += .1
    fresh = cache.build_classifier_provenance(selected_host_context,[second],evidence)
    hits, missing = cache.partition([second],cache.load_cache(),'demo','head',provenance=fresh)
    assert len(hits) == 1 and missing == []
    assert hits[0]['group_id'] == second['group_id']
    from dataclasses import replace
    from runtime_context import using_runtime_context
    first_path=cache._cache_path()
    later=replace(selected_host_context,native_session_id='later-native-session')
    assert later.session_key!=selected_host_context.session_key
    with using_runtime_context(later):
        assert cache._cache_path()==first_path
        later_hits,later_missing=cache.partition([second],cache.load_cache(),'demo','head',provenance=fresh)
        assert len(later_hits)==1 and later_missing==[]
        assert later_hits[0]['group_id']==second['group_id']
    changed = group(uuid.uuid4().hex[:8],revision='later-source-sha')
    newer = cache.build_classifier_provenance(selected_host_context,[changed],evidence)
    hits, missing = cache.partition([changed],cache.load_cache(),'demo','head',provenance=newer)
    assert not hits and missing[0]['_reason'] == 'provenance_changed'


def test_stage04_keeps_covered_results_and_names_missing_groups(selected_host_context,monkeypatch):
    import os
    from operation_state import operation_directory, store_artifact, read_artifact
    context = selected_host_context
    identifier,directory = operation_directory(context)
    groups=[group('covered'), dict(group('missing'),representative='Other unfinished work',canonical_hash=cache.canonical_hash('Other unfinished work'))]
    data={'scope.json':{'no_cache':False},'partition.json':{'heads':{}},
          'merged.json':{'merged_by_proj':{'demo':groups}},'evidence.json':{}}
    for name,value in data.items(): store_artifact(context,identifier,name,json.dumps(value))
    monkeypatch.setenv('SCOPE_PATH',str(directory/'scope.json'))
    monkeypatch.setenv('MERGED_PATH',str(directory/'merged.json'))
    monkeypatch.setenv('EVIDENCE_PATH',str(directory/'evidence.json'))
    import open_item_dedup
    monkeypatch.setattr(open_item_dedup,'classify_groups_with_agent',lambda *a:[{'group_id':'covered','classification':'DONE','confidence':'HIGH','canonical_text':'Fix bug #87','evidence_citation':'commit abc1234','action_required':None}])
    monkeypatch.setattr(open_item_dedup,'get_last_classifier_mode',lambda:'partial')
    procedures._check_items_stage_04(context,{'operation_id':identifier})
    result=json.loads(read_artifact(context,identifier,'classifications.json'))
    assert result['status']=='partial' and result['unclassified_group_ids']==['missing']
    assert result['counts']=={'requested':2,'classified':1,'unclassified':1}
    assert [record['group_id'] for record in result['classifications']]==['covered']
    assert result['warnings']


def test_unscoped_legacy_cache_remains_unchanged_and_cold(selected_host_context,monkeypatch):
    context=selected_host_context
    legacy=context.user_home/'legacy-global-classifications.json'
    original=json.dumps({'schema_version':1,'runs':{'same-project-name':{'entries':[{'classification':'DONE'}]}}}).encode()
    legacy.write_bytes(original);legacy.chmod(0o600)
    monkeypatch.setattr(cache,'CACHE_PATH',legacy)
    assert cache._cache_path()!=legacy
    assert cache.load_cache()['runs']=={}
    with cache.locked_cache() as scoped:
        assert 'same-project-name' not in scoped['runs']
    assert legacy.read_bytes()==original


def test_json_flag_keeps_scope_defaults_and_unknown_arguments(monkeypatch):
    import check_items_args
    monkeypatch.setattr(check_items_args,'_known_projects',lambda:set())
    monkeypatch.setattr(check_items_args,'_vault_known_projects',lambda:set())
    parsed=check_items_args.parse_scope(['--json','--dry-run','nonsense'])
    assert parsed.json_output and parsed.dry_run
    assert parsed.unknown_tokens==['nonsense']
    assert check_items_args.Scope().json_output is False


def test_stage01_second_operation_skips_merge_with_fresh_evidence(selected_host_context,monkeypatch):
    import os
    from types import SimpleNamespace
    import subprocess
    import open_item_dedup
    from operation_state import operation_directory,store_artifact,read_artifact
    context=selected_host_context
    project=context.canonical_project_root.name
    (context.canonical_project_root/'.git').mkdir(exist_ok=True)
    folder=context.vault_path/'claude-sessions';folder.mkdir()
    note=folder/'2099-01-01-session.md'
    note.write_text('---\nproject: '+project+'\ntype: claude-session\n---\n## Open Questions / Next Steps\n- [ ] Fix bug #87\n')
    revision=hashlib.sha256(note.read_bytes()).hexdigest()
    evidence={project:{'git_commits':[{'sha':'abc1234','message':'Fix bug #87'}]}}
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout='head\n'))
    pipeline_calls=[]
    def collect(**kwargs):
        pipeline_calls.append(kwargs['projects_json'])
        store_artifact(context,kwargs['operation_id'],'pipeline_evidence.json',json.dumps({'evidence':evidence,'evidence_gaps':{}}))
        return 'OK: synthetic verified evidence'
    monkeypatch.setattr(open_item_dedup,'deep_analysis_pipeline',collect)
    def semantic_merge(values):
        merged=copy.deepcopy(values)
        for items in merged.values():
            for item in items:
                item['group_id']='remapped-'+item['group_id']
        return merged
    monkeypatch.setattr(open_item_dedup,'merge_groups_semantically',semantic_merge)
    monkeypatch.setattr(open_item_dedup,'get_last_semantic_merge_mode',lambda:'ok')
    def prepare():
        identifier,directory=operation_directory(context)
        values={'scope.json':{'mode':'current','no_cache':False,'window_days':14}}
        for name,value in values.items():store_artifact(context,identifier,name,json.dumps(value))
        procedures._check_collect(context,{'operation_id':identifier,'scope_path':str(directory/'scope.json')})
        raw=json.loads(read_artifact(context,identifier,'raw_items.json'))
        assert len(raw)==1 and raw[0]['source_revision']==revision
        monkeypatch.setenv('SCOPE_PATH',str(directory/'scope.json'))
        monkeypatch.setenv('RAW_PATH',str(directory/'raw_items.json'))
        procedures._check_items_stage_01(context,{'operation_id':identifier})
        return identifier,directory,json.loads(read_artifact(context,identifier,'partition.json'))
    first_id,first_dir,first=prepare()
    assert len(pipeline_calls)==1
    monkeypatch.setenv('PART_PATH',str(first_dir/'partition.json'))
    procedures._check_items_stage_02(context,{'operation_id':first_id})
    remapped=json.loads(read_artifact(context,first_id,'merged.json'))
    assert remapped['merged_by_proj'][project][0]['group_id'].startswith('remapped-')
    monkeypatch.setenv('MERGED_PATH',str(first_dir/'merged.json'))
    procedures._check_items_stage_03(context,{'operation_id':first_id})
    assert len(pipeline_calls)==1
    assert json.loads(read_artifact(context,first_id,'evidence.json'))==evidence
    assert json.loads(read_artifact(context,first_id,'gaps.json'))=={}
    changed=json.loads(read_artifact(context,first_id,'merged.json'))
    changed['merged_by_proj'][project][0]['representative']='Changed semantic group'
    store_artifact(context,first_id,'merged.json',json.dumps(changed))
    procedures._check_items_stage_03(context,{'operation_id':first_id})
    assert len(pipeline_calls)==2
    assert len(first['needs'])==1
    initial=first['needs'][0]
    model=context.config['classifier_model' if context.host=='claude' else 'codex_ai_model']
    verdict={'group_id':initial['group_id'],'canonical_hash':initial['canonical_hash'],'members':initial['members'],
        'canonical_text':initial['representative'],'classification':'DONE','confidence':'HIGH','classifier_source':'agent',
        'ai_backend':context.host,'ai_model':model,'ai_prompt_version':'check-items-classifier-v3',
        'ai_prompt_sha256':hashlib.sha256(CLASSIFIER_PROMPT.encode()).hexdigest(),'classified_ts':time.time()}
    proof=cache.build_classifier_provenance(context,[initial],evidence,[verdict])
    with cache.locked_cache() as state:cache.update_cache(state,project,[initial],[verdict],'head',provenance=proof)
    second_id,second_dir,second=prepare()
    assert second_id!=first_id and second['flat_groups'][0]['group_id']!=initial['group_id']
    assert len(second['known'])==1 and second['needs']==[]
    monkeypatch.setenv('PART_PATH',str(second_dir/'partition.json'))
    monkeypatch.setattr(open_item_dedup,'merge_groups_semantically',lambda *a:pytest.fail('cache hit invoked semantic AI'))
    procedures._check_items_stage_02(context,{'operation_id':second_id})
    note.write_text('- [ ] Manual edited item\n');before=note.read_bytes()
    monkeypatch.setenv('MERGED_PATH',str(second_dir/'merged.json'))
    with pytest.raises(ValueError,match='Source changed'):
        procedures._check_items_stage_03(context,{'operation_id':second_id})
    assert note.read_bytes()==before


def test_json_dashboard_reports_partial_status_real_counts_and_published_path(selected_host_context,monkeypatch,capsys):
    from operation_state import operation_directory,store_artifact,read_artifact
    context=selected_host_context
    identifier,directory=operation_directory(context)
    values={'scope.json':{'mode':'current','window_days':14,'dry_run':True,'json_output':True},
        'raw.json':[],'partition.json':{'flat_groups':[]},'merged.json':{'merged_by_proj':{},'mode':'ok'},
        'classifications.json':{'classifications':[],'classifier_mode':'partial','status':'partial',
            'warnings':['One group remains unclassified'],'unclassified_group_ids':['missing'],
            'counts':{'requested':1,'classified':0,'unclassified':1}},
        'buckets.json':{'review':[],'dashboard_only':[]},'gaps.json':{}}
    for name,value in values.items():store_artifact(context,identifier,name,json.dumps(value))
    for key,name in {'SCOPE_PATH':'scope.json','RAW_PATH':'raw.json','PART_PATH':'partition.json',
        'MERGED_PATH':'merged.json','CLASSIFICATIONS_PATH':'classifications.json','BUCKETS_PATH':'buckets.json',
        'GAPS_PATH':'gaps.json'}.items():monkeypatch.setenv(key,str(directory/name))
    procedures._check_items_stage_07(context,{'operation_id':identifier})
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='partial' and result['warnings']==['One group remains unclassified']
    assert result['unclassified_group_ids']==['missing']
    assert result['counts']['requested']==1 and result['counts']['classified']==0
    assert result['counts']['applied']==0 and result['counts']['cascaded']==0
    from pathlib import Path
    report=Path(result['report'])
    report.resolve().relative_to(context.vault_path.resolve())
    assert report.is_file()
    assert json.loads(read_artifact(context,identifier,'outcome.json'))==result


def test_evidence_repo_resolver_prefers_bound_canonical_repo(selected_host_context,monkeypatch):
    import open_item_dedup
    context=selected_host_context
    (context.canonical_project_root/'.git').mkdir()
    monkeypatch.setattr(open_item_dedup,'get_workspace_roots',lambda:[])
    assert open_item_dedup._resolve_project_paths()[context.canonical_project_root.name]==str(context.canonical_project_root)


def test_bound_pipeline_ignores_age_only_cache_after_source_and_head_change(selected_host_context, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    import open_item_dedup as pipeline
    import vault_index
    from operation_state import operation_directory, read_artifact
    context = selected_host_context
    folder = context.vault_path / 'claude-sessions'; folder.mkdir(exist_ok=True)
    (context.vault_path / 'claude-insights').mkdir(exist_ok=True)
    note = folder / 'source.md'; note.write_text('Original source facts')
    projects = json.dumps(['demo'])
    key = pipeline._cache_key(['source.md'], projects, str(context.vault_path),
                              'claude-sessions', 'claude-insights', str(context.index_path))
    stale = (time.time(), 'OK:stale', json.dumps({'evidence': {'demo': {'commits': ['old-head']}}}))
    monkeypatch.setattr(pipeline, '_PIPELINE_EVIDENCE_CACHE', {key: stale})
    monkeypatch.setattr(vault_index, 'ensure_index', lambda *a, **k: str(context.index_path))
    monkeypatch.setattr(pipeline, 'collect_open_items', lambda *a, **k: [])
    monkeypatch.setattr(pipeline, '_resolve_project_paths', lambda: {'demo': str(context.canonical_project_root)})
    head = ['new-head']; calls = []
    def fake_run(args, **kwargs):
        calls.append(tuple(args))
        text = head[0] if args[:2] == ['git', 'log'] else ('[]' if args[0] == 'gh' else '')
        return SimpleNamespace(returncode=0, stdout=text, stderr='')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    def collect():
        identifier, directory = operation_directory(context)
        assert pipeline.deep_analysis_pipeline(['source.md'], projects, str(directory / 'pipeline.json'),
            str(context.vault_path), 'claude-sessions', 'claude-insights', db_path=str(context.index_path),
            operation_id=identifier).startswith('OK:')
        return json.loads(read_artifact(context, identifier, 'pipeline.json'))
    assert collect()['evidence']['demo']['commits'] == ['new-head']
    first_calls = len(calls)
    note.write_text('Later source facts'); head[0] = 'later-head'
    assert collect()['evidence']['demo']['commits'] == ['later-head']
    assert len(calls) > first_calls
    assert pipeline._PIPELINE_EVIDENCE_CACHE == {key: stale}


@pytest.mark.parametrize('artifact', ['evidence.json', 'gaps.json', 'crash'])
def test_evidence_reuse_rejects_replaced_artifacts_and_interrupted_publication(selected_host_context,monkeypatch,artifact):
    import open_item_dedup
    from operation_state import operation_directory,store_artifact,read_artifact
    context=selected_host_context
    identifier,directory=operation_directory(context)
    note=context.vault_path/'source.md';note.write_text('Captured source')
    raw=[{'path':str(note),'source_revision':hashlib.sha256(note.read_bytes()).hexdigest()}]
    merged={'merged_by_proj':{'demo':[group('original')]}}
    for name,value in {'scope.json':{},'raw_items.json':raw,'merged.json':merged}.items():
        store_artifact(context,identifier,name,json.dumps(value))
    monkeypatch.setenv('SCOPE_PATH',str(directory/'scope.json'))
    monkeypatch.setenv('MERGED_PATH',str(directory/'merged.json'))
    monkeypatch.setattr(open_item_dedup,'_resolve_project_paths',lambda:{})
    calls=[]
    def collect(**kwargs):
        calls.append(len(calls)+1)
        store_artifact(context,identifier,'pipeline_evidence.json',json.dumps({
            'evidence':{'demo':{'generation':calls[-1]}},'evidence_gaps':{'demo':[f'gap-{calls[-1]}']}}))
        return 'OK: isolated evidence'
    monkeypatch.setattr(open_item_dedup,'deep_analysis_pipeline',collect)
    payload={'operation_id':identifier}
    procedures._check_items_stage_03(context,payload)
    if artifact=='crash':
        changed=copy.deepcopy(merged);changed['merged_by_proj']['demo'][0]['representative']='Different task'
        store_artifact(context,identifier,'merged.json',json.dumps(changed))
        original=procedures._store_json
        def interrupted(ctx,value,path,data):
            if str(path).endswith('/gaps.json'):
                raise OSError('isolated crash after evidence publication')
            return original(ctx,value,path,data)
        monkeypatch.setattr(procedures,'_store_json',interrupted)
        with pytest.raises(OSError,match='isolated crash'):
            procedures._check_items_stage_03(context,payload)
        monkeypatch.setattr(procedures,'_store_json',original)
        store_artifact(context,identifier,'merged.json',json.dumps(merged))
    else:
        store_artifact(context,identifier,artifact,json.dumps({'demo':'replaced unapproved artifact'}))
    procedures._check_items_stage_03(context,payload)
    expected=3 if artifact=='crash' else 2
    assert len(calls)==expected
    assert json.loads(read_artifact(context,identifier,'evidence.json'))=={'demo':{'generation':expected}}
    assert json.loads(read_artifact(context,identifier,'gaps.json'))=={'demo':[f'gap-{expected}']}
