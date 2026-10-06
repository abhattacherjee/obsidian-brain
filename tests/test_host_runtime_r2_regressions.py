"""Runtime review regressions use disposable selected actors."""
import contextlib
import json
import sqlite3
import time
from dataclasses import replace

import capture
import note_transactions
import transcripts
from test_host_capture_review_regressions import source_batch, checkpoint


def test_stop_only_complete_sources_do_not_report_unscanned_sources_pending(selected_host_context, monkeypatch):
    for number in range(10):
        actor=replace(selected_host_context,native_session_id='stop-only-'+str(number))
        monkeypatch.setattr(transcripts,'read_records',lambda ctx,*a: source_batch(ctx,[]))
        assert checkpoint(actor).status == 'complete'
    result=capture.recover_registered(selected_host_context,8,time.monotonic()+2,include_active=True)
    assert result.status == 'complete' and result.pending_sources == 0


def test_native_status_and_tags_index_as_legacy_yaml(selected_host_context,monkeypatch):
    from vault_index import ensure_index
    monkeypatch.setattr(transcripts,'read_records',lambda ctx,*a: source_batch(ctx,
        [transcripts.SourceRecord('fact','user','Own fact',1)]))
    note=selected_host_context.vault_path/'claude-sessions'/'native.md'
    note.parent.mkdir()
    assert checkpoint(selected_host_context,note).status == 'complete'
    text=note.read_text()
    assert '\nstatus: auto-logged\n' in text
    assert '\ntags:\n- claude/session\n' in text
    ensure_index(selected_host_context.vault_path,["claude-sessions"],db_path=selected_host_context.index_path)
    import sqlite3
    with contextlib.closing(sqlite3.connect(selected_host_context.index_path)) as connection:
        tags=connection.execute('SELECT tags FROM notes WHERE path=?',(str(note),)).fetchone()[0]
    assert set(tags.split(',')) >= {'claude/session','claude/project/project','claude/auto'}


def test_planned_note_name_survives_below_threshold(selected_host_context,monkeypatch):
    from runtime_context import using_runtime_context
    from obsidian_utils import get_session_context
    actor=replace(selected_host_context,config=dict(selected_host_context.config,min_messages=3))
    monkeypatch.setattr(transcripts,'read_records',lambda ctx,*a: source_batch(ctx,
        [transcripts.SourceRecord('fact','user','Own fact',1,'2026-01-01T00:00:00Z')]))
    with using_runtime_context(actor):
        assert capture.capture_checkpoint(actor,capture.CaptureEvent('stop'),time.monotonic()+2).status == 'complete'
        name=get_session_context()['session_note_name']
        assert name.startswith('2026-01-01-')
        assert not list(actor.vault_path.rglob('*.md'))
        monkeypatch.setattr(transcripts,'read_records',lambda ctx,*a: source_batch(ctx,
            [transcripts.SourceRecord(str(i),'user','Own '+str(i),i) for i in range(3)]))
        result=capture.capture_checkpoint(actor,capture.CaptureEvent('stop'),time.monotonic()+2)
        assert result.status == 'complete'
        assert get_session_context()['session_note_name'] == name


def test_latest_unindexed_registered_session_wins_hint_with_same_slug(selected_host_context,monkeypatch):
    import native_lifecycle
    from vault_index import ensure_index
    project=selected_host_context.worktree.parent/'My.Repo'
    project.mkdir()
    actor=replace(selected_host_context,canonical_project_root=project,worktree=project)
    for number,date in enumerate(('2026-01-01','2026-01-02')):
        session=replace(actor,native_session_id='hint-'+str(number))
        monkeypatch.setattr(transcripts,'read_records',lambda ctx,*a,date=date: source_batch(ctx,
            [transcripts.SourceRecord('fact','user','Own fact',1,date+'T00:00:00Z')]))
        assert checkpoint(session).status == 'complete'
        if number == 0:
            ensure_index(actor.vault_path,['claude-sessions'],db_path=actor.index_path)
    hint=native_lifecycle._context_hint(actor,time.monotonic()+1)['hookSpecificOutput']['additionalContext']
    assert 'my-repo' in hint and '2026-01-02' in hint and '2026-01-01' not in hint


def test_removed_registered_subdirectory_recovers_only_exact_original_cwd(selected_host_context,monkeypatch):
    import shutil
    import pytest
    from runtime_context import resolve_runtime_context,RuntimeContextError
    root=selected_host_context.worktree
    (root/'.git').mkdir()
    sub=root/'sub'
    sub.mkdir()
    def resolve(cwd):
        return resolve_runtime_context(selected_host_context.host,selected_host_context.client,
            {'session_id':selected_host_context.native_session_id,'cwd':str(cwd)},
            {'config_path':selected_host_context.config_path,'resource_root':selected_host_context.resource_root})
    actor=resolve(sub)
    assert actor.worktree == root
    monkeypatch.setattr(transcripts,'read_records',lambda ctx,*a: source_batch(ctx,
        [transcripts.SourceRecord('fact','user','Own fact',1)]))
    assert checkpoint(actor).status == 'complete'
    shutil.rmtree(root)
    recovered=resolve(sub)
    assert recovered.worktree == root and recovered.invocation_cwd == sub
    monkeypatch.setattr(transcripts,'read_records',lambda ctx,*a: source_batch(ctx,
        [transcripts.SourceRecord('later','user','After worktree removed',2)]))
    assert checkpoint(recovered,kind='session_end').status == 'complete'
    assert any('After worktree removed' in note.read_text() for note in actor.vault_path.rglob('*.md'))
    with pytest.raises(RuntimeContextError,match='no verified registration'):
        resolve(root/'different-missing-subdirectory')


def test_manual_block_tag_change_conflicts_before_metadata_replacement(selected_host_context):
    note=selected_host_context.vault_path/'manual-tags.md'
    note.write_text('---\nstatus: auto-logged\ntags:\n- claude/session\n---\nHuman prose.\n')
    expected=note_transactions.read_revision(selected_host_context,note)
    note.write_text(note.read_text().replace('- claude/session','- human/edited'))
    result=note_transactions.apply_mutations(selected_host_context,[note_transactions.NoteMutation(
        note,expected,{'metadata':json.dumps({'tags':['claude/auto']})},'replace-stale-tags')])
    assert result.status == 'conflict' and '- human/edited' in note.read_text()


def test_lazy_missing_source_start_is_quiet_without_masking_stop(selected_host_context,capsys):
    import native_lifecycle
    actor=replace(selected_host_context,transcript_path=selected_host_context.native_home/
        ('projects' if selected_host_context.host=='claude' else 'sessions')/'not-created.jsonl')
    native_lifecycle.dispatch(actor,'session_start',{},time.monotonic())
    assert capsys.readouterr().err == ''
    native_lifecycle.dispatch(actor,'stop',{},time.monotonic())
    assert 'capture pending' in capsys.readouterr().err


def test_readonly_doctor_names_retained_partial_type(selected_host_context,monkeypatch):
    import vault_doctor
    from runtime_context import using_runtime_context
    batch=source_batch(selected_host_context,[transcripts.SourceRecord('fact','user','Own fact',1)])
    batch=replace(batch,status='partial',source_complete=False,parser_state={'_deferred_source_rows':[
        {'reason':'unknown_schema:future-thing','offset':0,'end_offset':1,'row_sha256':'a'*64}]})
    monkeypatch.setattr(transcripts,'read_records',lambda *a:batch)
    assert checkpoint(selected_host_context).status == 'pending'
    with using_runtime_context(selected_host_context):
        report=vault_doctor._inspect_runtime_pending()
    assert report['status']=='pending' and report['pending_sources']==1
    assert any('type=future-thing' in warning for warning in report['warnings'])


def test_native_snapshot_keeps_slugged_project_and_legacy_tags(selected_host_context,monkeypatch):
    project=selected_host_context.worktree.parent/'My.Repo'
    project.mkdir()
    actor=replace(selected_host_context,canonical_project_root=project,worktree=project)
    monkeypatch.setattr(transcripts,'read_records',lambda ctx,*a: source_batch(ctx,
        [transcripts.SourceRecord('fact','user','Own fact',1)]))
    assert checkpoint(actor,kind='pre_compact').status == 'complete'
    snapshots=[note for note in actor.vault_path.rglob('*.md') if capture._note_identity(note)['type']=='claude-snapshot']
    assert len(snapshots)==1
    fields=capture._note_identity(snapshots[0])
    assert fields['project']=='my-repo'
    assert set(fields['tags']) == {'claude/snapshot','claude/project/my-repo','claude/auto'}
    snapshot = snapshots[0]
    assert '-my-repo-' in snapshot.name
    parents = [note for note in actor.vault_path.rglob('*.md')
               if capture._note_identity(note)['type'] == 'claude-session']
    assert len(parents) == 1
    parent = parents[0]
    assert fields['source_session_note'] == '[[' + parent.stem + ']]'
    assert fields['parent_session'] == fields['source_session_note']
    from runtime_context import using_runtime_context
    from obsidian_utils import find_snapshots_for_session
    from vault_index import ensure_index, _parent_session_for_snapshot
    from vault_doctor_checks import snapshot_integrity
    with using_runtime_context(actor):
        for indexed in (False, True):
            assert find_snapshots_for_session(parent.parent, actor.native_session_id,
                fields['date'], 'My.Repo', use_index=indexed) == ['[[' + snapshot.stem + ']]']
        database = ensure_index(str(actor.vault_path), ['claude-sessions'], db_path=str(actor.index_path))
        assert _parent_session_for_snapshot(str(snapshot), database) == str(parent)
        with contextlib.closing(sqlite3.connect(database)) as connection:
            assert connection.execute('SELECT type,source_note FROM notes WHERE path=?',
                (str(snapshot),)).fetchone() == ('claude-snapshot', parent.stem)
        issues = snapshot_integrity.scan(str(actor.vault_path), 'claude-sessions', 'claude-insights', 9999)
    assert not [issue for issue in issues if issue.check in {'snapshot-orphan', 'snapshot-broken-backlink'}]


def test_native_oversized_row_keeps_later_owned_dialogue_and_visible_partial(selected_host_context):
    import native_lifecycle
    import vault_doctor
    from runtime_context import using_runtime_context
    selected=selected_host_context
    source=selected.native_home/('projects' if selected.host=='claude' else 'sessions')/'oversized.jsonl'
    if selected.host=='claude':
        before={'type':'user','sessionId':selected.native_session_id,'uuid':'before',
                'message':{'role':'user','content':'Own before oversized'}}
        after={'type':'assistant','sessionId':selected.native_session_id,'uuid':'after',
               'message':{'role':'assistant','content':'Own after oversized'}}
    else:
        before={'type':'session_meta','payload':{'id':selected.native_session_id,'cwd':str(selected.worktree)}}
        after={'type':'event_msg','payload':{'type':'item_completed','thread_id':selected.native_session_id,
               'turn_id':'own-after','item':{'type':'UserMessage','id':'after',
               'content':[{'type':'text','text':'Own after oversized'}]}}}
    oversized={'type':'future-state','payload':{'content':'PRIVATE_OVERSIZED_BODY'+('x'*(1024*1024))}}
    source.parent.mkdir(parents=True,exist_ok=True)
    raw=''.join(json.dumps(row)+'\n' for row in (before,oversized,after)).encode()
    source.write_bytes(raw)
    actor=replace(selected,transcript_path=source)
    with using_runtime_context(actor):
        result=checkpoint(actor)
        assert result.status=='pending' and result.applied_revision and result.pending_sources==1
        from session_lookup import find_existing_session
        note=find_existing_session(actor,time.monotonic()+1)
        text=note.read_text()
        assert 'Own after oversized' in text and 'partial' in text and 'oversized' in text
        assert 'PRIVATE_OVERSIZED_BODY' not in text
        hint=native_lifecycle.dispatch(actor,'session_start',{},time.monotonic())['hookSpecificOutput']['additionalContext']
        assert 'Oversized '+actor.host.capitalize()+' transcript record retained' in hint
        assert 'PRIVATE_OVERSIZED_BODY' not in hint and str(source) not in hint
        report=vault_doctor._inspect_runtime_pending()
        assert report['status']=='pending' and any('Oversized' in warning for warning in report['warnings'])
        with contextlib.closing(note_transactions.connect_coordination(actor)) as connection:
            cursor=json.loads(connection.execute('SELECT cursor FROM source_sessions WHERE scope=?',
                                               (actor.session_key,)).fetchone()[0])
            refs=cursor['parser_state']['_deferred_source_rows']
            assert any(ref['reason']=='oversized:unrecognized' for ref in refs)
            assert 'PRIVATE_OVERSIZED_BODY' not in json.dumps(cursor)
            assert 'PRIVATE_OVERSIZED_BODY' not in repr(connection.execute('SELECT content FROM native_events').fetchall())
        assert source.read_bytes()==raw


def test_metadata_yaml_update_cannot_replace_matching_text_inside_manual_field(selected_host_context):
    note=selected_host_context.vault_path/'manual-field.md'
    original='---\nmanual: status: "auto-logged"\nstatus: "auto-logged"\n---\nHuman prose.\n'
    note.write_text(original)
    expected=note_transactions.read_revision(selected_host_context,note)
    result=note_transactions.apply_mutations(selected_host_context,[note_transactions.NoteMutation(
        note,expected,{'metadata':json.dumps({'status':'summarized'})},'status-cas')])
    assert result.status=='applied'
    assert '\nmanual: status: "auto-logged"\n' in note.read_text()
    assert '\nstatus: summarized\n' in note.read_text()


def test_unscanned_known_partial_source_keeps_safe_warning(selected_host_context,monkeypatch):
    for number in range(9):
        actor=replace(selected_host_context,native_session_id='queue-'+str(number))
        batch=source_batch(actor,[])
        if number==8:
            batch=replace(batch,status='partial',source_complete=False,parser_state={'_deferred_source_rows':[
                {'reason':'unknown_schema:future-row','offset':0,'end_offset':1,'row_sha256':'a'*64}]})
        monkeypatch.setattr(transcripts,'read_records',lambda *a,batch=batch:batch)
        checkpoint(actor)
    monkeypatch.setattr(transcripts,'read_records',lambda ctx,*a:source_batch(ctx,[]))
    result=capture.recover_registered(selected_host_context,8,time.monotonic()+2,include_active=True)
    assert result.status=='pending' and result.pending_sources==1
    assert any('type=future-row' in warning for warning in result.warnings)
