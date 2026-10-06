"""Inline check-items requests publish only validated host-owned results."""
from types import SimpleNamespace
import json
import pytest
import check_items_cli as cli
from check_items_test_helpers import native_ai_context, private_output
_REAL_REQUEST_AI = cli._request_ai


@pytest.fixture(autouse=True)
def no_live_ai(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('test attempted live AI')
    monkeypatch.setattr(cli, '_request_ai', blocked)


def groups():
    return [{'group_id':'one','project':'p'}, {'group_id':'two','project':'p'}]


def verdict(gid):
    return {'group_id':gid,'classification':'ACTIVE','confidence':'MED',
            'canonical_text':'work','evidence_citation':None,'action_required':None}


def result(data):
    return SimpleNamespace(data=data,backend='codex',model='configured-model')


@pytest.mark.parametrize('merges,before,after', [
    ([{'canonical_group_id':'one','absorbed_group_ids':['unknown'],'reasoning':'same'}],2,1),
    ([{'canonical_group_id':'one','absorbed_group_ids':['one'],'reasoning':'same'}],2,1),
    ([{'canonical_group_id':'one','absorbed_group_ids':['two','two'],'reasoning':'same'}],2,0),
    ([],2,1),
])
def test_invalid_merges_leave_existing_output(monkeypatch,tmp_path,merges,before,after):
    output=private_output('existing.json');output.write_text('previous')
    monkeypatch.setattr(cli,'_request_ai',lambda *args:(0,result({
        'merges':merges,'total_groups_before':before,'total_groups_after':after})))
    assert cli.run_semantic_merge(json.dumps({'groups':groups()}),str(output))==4
    assert output.read_text()=='previous'


def test_merge_publication_is_private_and_model_has_no_output_path(monkeypatch,tmp_path):
    output=private_output('out.json');seen=[]
    merged={'merges':[{'canonical_group_id':'one','absorbed_group_ids':['two'],'reasoning':'same task'}],
            'total_groups_before':2,'total_groups_after':1}
    def execute(*args):
        seen.append(args);return 0,result(merged)
    monkeypatch.setattr(cli,'_request_ai',execute)
    assert cli.run_semantic_merge(json.dumps({'groups':groups()}),str(output))==0
    assert json.loads(output.read_text())==merged
    assert output.stat().st_mode & 0o777 == 0o600
    assert str(output) not in json.dumps(seen)
    assert '<input-json-path>' not in cli.SEMANTIC_MERGE_PROMPT
    assert '<output-json-path>' not in cli.SEMANTIC_MERGE_PROMPT


@pytest.mark.parametrize('returned', [[verdict('one')],[verdict('one'),verdict('one')],
                                     [verdict('one'),verdict('unknown')]])
def test_classifier_rejects_missing_duplicate_unknown_ids(monkeypatch,tmp_path,returned):
    monkeypatch.setattr(cli,'_request_ai',lambda *args:(0,result(returned)))
    rc,data=cli._dispatch_classifier_chunk(groups(),{},'haiku',str(tmp_path/'out.json'))
    assert rc==4 and data==[]


def test_classifier_rejects_cross_project_merge():
    inputs=groups();inputs[1]['project']='q'
    value={'merges':[{'canonical_group_id':'one','absorbed_group_ids':['two'],'reasoning':'same'}],
           'total_groups_before':2,'total_groups_after':1}
    assert not cli._validate_merge_payload(value,inputs)


def test_cancelled_classifier_preserves_previous_output(monkeypatch,tmp_path):
    output=private_output('out.json');output.write_text('previous')
    monkeypatch.setattr(cli,'_request_ai',lambda *args:(7,None))
    monkeypatch.setattr(__import__('check_items_prefilter'),'is_prefilter_enabled',lambda:False)
    assert cli.run_classifier(json.dumps({'groups':groups(),'evidence':{}}),str(output))==7
    assert output.read_text()=='previous'


def test_exhausted_chunk_cannot_publish_partial_results(monkeypatch,tmp_path):
    output=private_output('out.json');output.write_text('previous')
    monkeypatch.setattr(cli,'CLASSIFIER_CHUNK_SIZE',1)
    monkeypatch.setattr(__import__('check_items_prefilter'),'is_prefilter_enabled',lambda:False)
    calls=[]
    def execute(operation,prompt,payload,model,requested):
        calls.append(requested[0]['group_id'])
        if requested[0]['group_id']=='one':
            return 0,result([verdict('one')])
        return 4,None
    monkeypatch.setattr(cli,'_request_ai',execute)
    assert cli.run_classifier(json.dumps({'groups':groups(),'evidence':{}}),str(output))==4
    assert calls==['one','two','two']
    assert output.read_text()=='previous'


def test_native_default_model_cannot_authorize_cache_replay(tmp_path):
    import check_items_cache as cache
    from runtime_context import using_runtime_context
    context=SimpleNamespace(host='codex',config={},vault_path=tmp_path/'vault',
                            canonical_project_root=tmp_path/'project')
    group={'group_id':'one','canonical_hash':'h','canonical_text':'work'}
    with using_runtime_context(context):
        assert cache._provenance(group,'p',{'complete':True,'evidence':{},'classifier_model':'haiku'}) is None


def test_cache_fingerprint_uses_effective_classifier_model_and_prompt(tmp_path,monkeypatch):
    import check_items_cache as cache
    from runtime_context import using_runtime_context
    context=SimpleNamespace(host='claude',config={'summary_model':'haiku'},vault_path=tmp_path/'vault',
                            canonical_project_root=tmp_path/'project')
    group={'group_id':'one','canonical_hash':'h','canonical_text':'work'}
    with using_runtime_context(context):
        context.config['classifier_model']='claude-haiku-4-5-20251001'
        small=cache._provenance(group,'p',{'complete':True,'evidence':{},'classifier_model':'claude-haiku-4-5-20251001'})
        context.config['classifier_model']='claude-sonnet-4-5-20250929'
        large=cache._provenance(group,'p',{'complete':True,'evidence':{},'classifier_model':'claude-sonnet-4-5-20250929'})
        assert small and large and small != large
        monkeypatch.setattr(cli,'CLASSIFIER_PROMPT',cli.CLASSIFIER_PROMPT+'\nnew contract')
        assert cache._provenance(group,'p',{'complete':True,'evidence':{},'classifier_model':'claude-sonnet-4-5-20250929'}) != large


@pytest.mark.parametrize('unsafe', ['markdown', 'outside', 'symlink', 'public_mode', 'private_json'])
def test_private_result_boundary_preserves_notes_even_with_state_in_vault(
        native_ai_context, tmp_path, unsafe):
    context=native_ai_context
    context.state_path=context.vault_path
    output=private_output('result.md' if unsafe=='markdown' else 'result.json')
    output.write_text('previous')
    if unsafe=='outside':
        output=context.vault_path/'outside.json'
        output.write_text('previous');output.chmod(0o600)
    elif unsafe=='symlink':
        note=context.vault_path/'manual.md';note.write_text('manual edit')
        output.unlink();output.symlink_to(note)
    elif unsafe=='public_mode':
        output.chmod(0o644)
    with pytest.raises(ValueError):
        cli._publish_private_json(str(output),{'replacement':'unsafe'})
    assert output.read_text() == ('manual edit' if unsafe=='symlink' else 'previous')


@pytest.mark.parametrize('host,client,requested_model',[('claude','claude-code','sonnet'),
                                                        ('codex','codex-cli',None)])
def test_request_binds_actual_host_inline_json_and_revision(
        native_ai_context,monkeypatch,host,client,requested_model):
    import ai_backend
    from ai_backend import AIResult
    context=native_ai_context;context.host=host;context.client=client
    context.config.pop("classifier_model", None)
    seen=[]
    def execute(selected,operation,request):
        seen.append((selected,operation,request))
        return AIResult('ok',verdict('one'),request.input_revision,'',host,'native-model')
    monkeypatch.setattr(cli, "_request_ai", _REAL_REQUEST_AI)
    # Re-establish a strict transport guard after replacing the fixture's AI mock.
    import subprocess
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('live AI subprocess'))
    monkeypatch.setattr(ai_backend,'execute_ai',execute)
    rc,response=cli._request_ai('classify_items',cli.CLASSIFIER_PROMPT,
                               {'groups':groups(),'evidence':{}},'sonnet',groups())
    assert rc==0 and response.backend==host
    selected,operation,request=seen[0]
    assert selected is context and operation=='classify_items'
    assert request.model==requested_model
    assert tuple(request.options['expected_ids'])==('one','two')
    assert request.options['project_by_id']=={'one':'p','two':'p'}
    assert len(request.input_revision)==64
    assert 'Input JSON:' in request.input and '<input-json-path>' not in request.input


@pytest.mark.parametrize('host,client',[('claude','claude-code'),('codex','codex-cli')])
def test_bound_legacy_orchestrators_keep_context_and_do_not_launch_nested_cli(
        native_ai_context,monkeypatch,host,client):
    import open_item_dedup as dedup
    context=native_ai_context;context.host=host;context.client=client
    monkeypatch.setattr(dedup,'_check_items_workdir',lambda:pytest.fail('legacy workdir selected'))
    def execute(operation,prompt,payload,model,requested):
        from runtime_context import current_runtime_context
        assert current_runtime_context() is context
        if operation=='semantic_merge':
            return 0,result({'merges':[{'canonical_group_id':'one','absorbed_group_ids':['two'],
                                       'reasoning':'same task'}],
                             'total_groups_before':2,'total_groups_after':1})
        return 0,result([verdict(g['group_id']) for g in requested])
    monkeypatch.setattr(cli,'_request_ai',execute)
    monkeypatch.setenv('CHECK_ITEMS_PREFILTER','off')
    inputs=[{'group_id':'one','project':'p','representative':'one','members':[]},
            {'group_id':'two','project':'p','representative':'two','members':[]}]
    merged=dedup.merge_groups_semantically(inputs)
    assert len(merged)==1 and merged[0]['group_id']=='one'
    classified=dedup.classify_groups_with_agent(inputs,{})
    assert {r['group_id'] for r in classified}=={'one','two'}
    assert all(r['classifier_source']=='agent' for r in classified)


def _collected_group(context):
    import open_item_dedup as dedup
    folder = context.vault_path / 'claude-sessions'
    folder.mkdir()
    note = folder / '2026-10-05-work.md'
    note.write_bytes(b'---\nproject: p\ntype: claude-session\n---\n## Open Questions / Next Steps\n- [ ] finish parser\n- [ ] write docs\n')
    records = dedup.collect_open_item_records(str(context.vault_path), 'claude-sessions', 'p')
    group = {'group_id':'one', 'project':'p', 'representative':'finish parser',
             'members':[{'file':r['path'], 'line':r['line'], 'text':r['text'],
                         'source_revision':r['source_revision']} for r in records]}
    return note, group


def test_collector_revision_hashes_the_exact_parsed_bytes(native_ai_context):
    import hashlib
    import open_item_dedup as dedup
    note, group = _collected_group(native_ai_context)
    expected = hashlib.sha256(note.read_bytes()).hexdigest()
    assert len(group['members']) == 2
    assert {m['source_revision'] for m in group['members']} == {expected}
    legacy = dedup.collect_open_items(str(native_ai_context.vault_path), 'claude-sessions', 'p')
    assert all(len(item) == 3 for item in legacy)


def test_manual_edit_during_ai_preserves_source_and_reports_conflict(native_ai_context, monkeypatch):
    import open_item_dedup as dedup
    note, group = _collected_group(native_ai_context)
    changed = note.read_bytes() + b'\nMy manual explanation.\n'
    def execute(operation, prompt, payload, model, requested):
        note.write_bytes(changed)
        value = verdict('one'); value.update(classification='DONE', confidence='HIGH', evidence_citation='commit abc1234')
        return 0, result([value])
    monkeypatch.setattr(cli, '_request_ai', execute)
    monkeypatch.setenv('CHECK_ITEMS_PREFILTER', 'off')
    classified = dedup.classify_groups_with_agent([group], {})
    assert classified[0]['classification'] == 'DONE'
    summary = dedup.cascade_group_members([group], vault_path=str(native_ai_context.vault_path))
    assert 'SOURCE REVISION CONFLICT' in summary
    assert note.read_bytes() == changed


@pytest.mark.parametrize('revision', [None, [], {}, 'not-a-sha'])
def test_bound_cascade_refuses_missing_or_malformed_source_revision(native_ai_context, revision):
    import open_item_dedup as dedup
    note, group = _collected_group(native_ai_context)
    original = note.read_bytes()
    for member in group['members']:
        member['source_revision'] = revision
    assert 'SOURCE REVISION CONFLICT' in dedup.cascade_group_members([group], vault_path=str(native_ai_context.vault_path))
    assert note.read_bytes() == original


def test_bound_cascade_accepts_matching_revision_for_multiple_lines(native_ai_context):
    import open_item_dedup as dedup
    note, group = _collected_group(native_ai_context)
    summary = dedup.cascade_group_members([group], vault_path=str(native_ai_context.vault_path))
    assert 'Cascaded 2 member-line(s)' in summary
    assert note.read_text().count('- [x] ') == 2


def test_conflicting_member_revisions_preserve_file_but_independent_note_saves(native_ai_context):
    import hashlib
    import open_item_dedup as dedup
    note, group = _collected_group(native_ai_context)
    original = note.read_bytes()
    group['members'][1]['source_revision'] = '0' * 64
    sibling = note.parent / 'other.md'
    sibling.write_bytes(b'- [ ] separate work\n')
    group['members'].append({'file':str(sibling), 'line':1, 'text':'separate work',
                             'source_revision':hashlib.sha256(sibling.read_bytes()).hexdigest()})
    summary = dedup.cascade_group_members([group], vault_path=str(native_ai_context.vault_path))
    assert 'SOURCE REVISION CONFLICT' in summary
    assert 'Cascaded 1 member-line(s)' in summary
    assert note.read_bytes() == original
    assert sibling.read_bytes() == b'- [x] separate work\n'


def test_private_result_rejects_parent_symlink_to_another_valid_job(native_ai_context):
    output = private_output('original.json')
    alias = output.parent / 'alias'
    alias.symlink_to(output.parent, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        cli._publish_private_json(alias / 'original.json', {'replacement':True})
    assert output.read_bytes() == b''


def test_complete_evidence_changes_cache_namespace(native_ai_context, monkeypatch):
    import check_items_cache as cache
    monkeypatch.setenv('CHECK_ITEMS_PREFILTER', 'off')
    inputs = groups()
    first = cache.build_classifier_provenance(native_ai_context, inputs, {'p':{'commits':['abc']}})
    second = cache.build_classifier_provenance(native_ai_context, inputs, {'p':{'commits':['def']}})
    assert first['classifier_model_by_id'] == {'one':'claude-haiku-4-5-20251001', 'two':'claude-haiku-4-5-20251001'}
    assert cache._provenance(inputs[0], 'p', first) != cache._provenance(inputs[0], 'p', second)
    assert cache._provenance(inputs[0], 'p') is None


def test_actual_model_must_match_approved_cache_selection(native_ai_context, monkeypatch):
    import check_items_cache as cache
    monkeypatch.setenv('CHECK_ITEMS_PREFILTER', 'off')
    observed = dict(verdict('one'), classifier_source='agent', ai_backend='claude', ai_model='sonnet')
    with pytest.raises(ValueError, match='differs'):
        cache.build_classifier_provenance(native_ai_context, groups(), {}, [observed])


def test_unknown_native_default_never_authorizes_cache_replay(native_ai_context, monkeypatch):
    import check_items_cache as cache
    native_ai_context.host = 'codex'
    native_ai_context.client = 'codex-cli'
    monkeypatch.setenv('CHECK_ITEMS_PREFILTER', 'off')
    bundle = cache.build_classifier_provenance(native_ai_context, groups(), {})
    assert bundle['classifier_model_by_id'] == {'one':None, 'two':None}
    assert cache._provenance(groups()[0], 'p', bundle) is None


def test_claude_alias_remap_never_replays_prior_actual_model(native_ai_context, monkeypatch):
    import hashlib
    import check_items_cache as cache
    native_ai_context.config.pop('classifier_model')
    monkeypatch.setenv('CHECK_ITEMS_PREFILTER', 'off')
    inputs = [dict(groups()[0], canonical_hash='hash-one', members=[])]
    evidence = {'p':{'commits':['abc']}}
    before = cache.build_classifier_provenance(native_ai_context, inputs, evidence)
    assert before['classifier_model_by_id']['one'] is None
    stored = {'runs':{}}
    for model in ('claude-haiku-model-A', 'claude-haiku-model-B'):
        observed = dict(verdict('one'), canonical_hash='hash-one', members=[],
                        classifier_source='agent', ai_backend='claude', ai_model=model,
                        ai_prompt_version='check-items-classifier-v3',
                        ai_prompt_sha256=hashlib.sha256(cli.CLASSIFIER_PROMPT.encode()).hexdigest())
        cache.update_cache(stored, 'p', inputs, [observed], 'head', now=100, provenance=before)
        entry = stored['runs']['p']['groups'][0]
        assert entry['ai_model'] == model
        assert entry['provenance'] is not None
        unchanged_policy = cache.build_classifier_provenance(native_ai_context, inputs, evidence)
        known, needs = cache.partition([dict(inputs[0])], stored, 'p', 'head', now=101,
                                       provenance=unchanged_policy)
        assert known == [] and needs[0]['_reason'] == 'provenance_changed'
        previous_observation = cache.build_classifier_provenance(native_ai_context, inputs, evidence, [observed])
        assert previous_observation['classifier_model_by_id']['one'] == model
        assert cache._provenance(inputs[0], 'p', previous_observation) is None


@pytest.mark.parametrize('host,operation,expected', [
    ('claude', 'classify_items', 'claude-sonnet-4-5-20250929'),
    ('claude', 'semantic_merge', 'haiku'),
    ('codex', 'classify_items', None),
    ('codex', 'semantic_merge', None),
])
def test_explicit_classifier_model_controls_only_claude_classifier_request(
        native_ai_context, monkeypatch, host, operation, expected):
    import ai_backend
    from ai_backend import AIResult
    context = native_ai_context
    context.host = host
    context.config['classifier_model'] = 'claude-sonnet-4-5-20250929'
    context.config['codex_ai_model'] = 'gpt-native-configured'
    seen = []
    def execute(selected, op, request):
        seen.append(request)
        actual = 'claude-sonnet-4-5-20250929' if host == 'claude' else 'gpt-native-observed'
        return AIResult('ok', [], request.input_revision, '', host, actual)
    monkeypatch.setattr(cli, '_request_ai', _REAL_REQUEST_AI)
    monkeypatch.setattr(ai_backend, 'execute_ai', execute)
    rc, response = cli._request_ai(operation, 'prompt', {'groups':groups()}, 'haiku', groups())
    assert rc == 0
    assert seen[0].model == expected
    assert response.model == ('claude-sonnet-4-5-20250929' if host == 'claude' else 'gpt-native-observed')
    if host == 'codex':
        assert ai_backend.resolve_ai_selection(context, operation, seen[0].model) == ('codex', 'gpt-native-configured')


@pytest.mark.parametrize('change', ['schema', 'policy', 'instruction'])
def test_shared_backend_contract_change_invalidates_cache(native_ai_context, monkeypatch, change):
    import copy
    import ai_backend
    import check_items_cache as cache
    group = dict(groups()[0], canonical_hash='hash-one', members=[])
    bundle = cache.build_classifier_provenance(native_ai_context, [group], {})
    before = cache._provenance(group, 'p', bundle)
    if change == 'schema':
        original = ai_backend._schema
        def revised(operation, options):
            schema = copy.deepcopy(original(operation, options))
            schema['properties']['items']['items']['properties']['confidence']['enum'].append('VERY_HIGH')
            return schema
        monkeypatch.setattr(ai_backend, '_schema', revised)
    elif change == 'policy':
        monkeypatch.setattr(ai_backend, 'VALIDATION_POLICY_REVISION', 'strict-json-semantic-v2')
    else:
        monkeypatch.setattr(ai_backend, 'ANALYSIS_INSTRUCTION', 'New bounded analysis instruction')
    assert cache._provenance(group, 'p', bundle) != before
    assert cache.build_classifier_provenance(native_ai_context, [group], {})['backend_contract_by_id'] != bundle['backend_contract_by_id']


@pytest.mark.parametrize('function', ['merge_groups_semantically', 'classify_groups_with_agent'])
def test_bound_wrapper_rejects_state_inside_vault_before_private_artifacts(native_ai_context, function):
    import open_item_dedup as dedup
    native_ai_context.state_path = native_ai_context.vault_path
    with pytest.raises(ValueError, match='outside'):
        if function == 'merge_groups_semantically':
            dedup.merge_groups_semantically(groups())
        else:
            dedup.classify_groups_with_agent(groups(), {})
    assert list(native_ai_context.vault_path.iterdir()) == []


@pytest.mark.parametrize('failure', ['flush', 'replace'])
def test_private_result_publication_failure_preserves_previous_bytes(monkeypatch, failure):
    output = private_output('publication.json')
    previous = b'{"previous":"keep exactly"}\n'
    output.write_bytes(previous)
    def fail(*args, **kwargs):
        raise OSError('injected publication failure')
    monkeypatch.setattr(cli.os, 'fsync' if failure == 'flush' else 'replace', fail)
    with pytest.raises(OSError, match='injected publication failure'):
        cli._publish_private_json(output, {'next': 'validated answer'})
    assert output.read_bytes() == previous
    assert list(output.parent.glob('.check-items-*.json')) == []
