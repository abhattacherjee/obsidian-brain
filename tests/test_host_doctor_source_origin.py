"""Historical source matching keeps the invoking actor and full origin ID."""
import json
import os
from pathlib import Path

import pytest
from runtime_context import current_runtime_context
from vault_doctor_checks import source_sessions
from test_host_behavior_conformance import native_rows


@pytest.mark.parametrize('origin', ['claude', 'codex'])
def test_source_backlink_repair_uses_full_historical_origin(selected_host_context, origin):
    actor = selected_host_context
    sid = 'full-source-native-id'
    home = Path(os.environ['CLAUDE_CONFIG_DIR' if origin == 'claude' else 'CODEX_HOME'])
    directory = home / ('projects/demo' if origin == 'claude' else 'sessions')
    directory.mkdir(parents=True)
    filename = sid + '.jsonl' if origin == 'claude' else 'rollout-unrelated-filename.jsonl'
    path = directory / filename
    rows = native_rows(origin, sid)
    visible = [row for row in rows if row.get('type') in ('user', 'assistant', 'response_item')
               and row.get('timestamp')]
    visible[-1]['timestamp'] = '2026-10-05T01:00:00Z'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    os.utime(path, (1791162000, 1791162000))  # 2026-10-05 01:00 UTC.
    sessions = actor.vault_path / 'claude-sessions'
    insights = actor.vault_path / 'claude-insights'
    sessions.mkdir()
    insights.mkdir()
    session = sessions / '2026-10-05-demo-correct.md'
    session.write_text(f'---\ntype: claude-session\ndate: 2026-10-05\nproject: demo\n'
                       f'session_id: {sid}\nagent_session_id: {sid}\nagent_provider: {origin}\n---\n')
    note = insights / '2026-10-05-decision.md'
    note.write_text(f'---\ntype: claude-insight\ndate: 2026-10-05\nproject: demo\n'
                   f'source_session: {sid}\nagent_session_id: {sid}\nagent_provider: {origin}\n'
                   'source_session_note: "[[wrong-basename]]"\n---\nManual decision.\n')
    original = note.read_bytes()
    issues = source_sessions.scan(str(actor.vault_path), 'claude-sessions', 'claude-insights', 9999)
    assert len(issues) == 1
    assert issues[0].proposed_source == '[[2026-10-05-demo-correct]]'
    assert issues[0].confidence == .99
    assert note.read_bytes() == original
    assert current_runtime_context() is actor


def test_unknown_source_origin_is_unverified_and_preserved(selected_host_context):
    actor = selected_host_context
    insights = actor.vault_path / 'claude-insights'
    insights.mkdir()
    note = insights / '2026-10-05-decision.md'
    note.write_text('---\ndate: 2026-10-05\nproject: demo\nsource_session: full-id\n'
                   'agent_provider: unknown\nsource_session_note: "[[original]]"\n---\nManual prose.\n')
    original = note.read_bytes()
    issues = source_sessions.scan(str(actor.vault_path), 'claude-sessions', 'claude-insights', 9999)
    assert len(issues) == 1 and issues[0].extra['signal_class'] == 'source-origin-unverified'
    assert issues[0].proposed_source == '' and issues[0].confidence == 0
    assert note.read_bytes() == original


def test_doctor_native_memory_public_dispatch_uses_selected_actor(selected_host_context, monkeypatch, capsys):
    from vault_doctor_checks import memory_index
    actor = selected_host_context
    claude_store = Path(os.environ['CLAUDE_CONFIG_DIR']) / 'projects/demo/memory'
    claude_store.mkdir(parents=True)
    note = claude_store / 'orphan.md'
    note.write_text('Explicit Claude native memory.\n')
    before = note.read_bytes()
    monkeypatch.setenv('HOME', str(actor.canonical_project_root / 'ambient-home'))
    issues = memory_index.scan(str(actor.vault_path), 'claude-sessions', 'claude-insights', 9999)
    if actor.host == 'claude':
        assert len(issues) == 1 and issues[0].extra['signal_class'] == 'index-missing'
    else:
        assert issues == []
        assert 'no equivalent native memory-file API' in capsys.readouterr().err
    assert note.read_bytes() == before
    assert current_runtime_context() is actor


def test_session_coverage_repair_uses_selected_native_capture(selected_host_context, monkeypatch):
    from vault_doctor_checks import session_coverage
    actor = selected_host_context
    sid = 'doctor-recovery-full-native-id'
    directory = actor.native_home / ('projects/demo' if actor.host == 'claude' else 'sessions')
    directory.mkdir(parents=True)
    path = directory / (sid + '.jsonl' if actor.host == 'claude' else 'rollout-unrelated.jsonl')
    rows = native_rows(actor.host, sid)
    import copy
    template = next(row for row in rows if row.get('type') == 'user' or row.get('payload', {}).get('role') == 'user')
    for index in (2, 3):
        extra = copy.deepcopy(template)
        if actor.host == 'claude':
            extra['uuid'] = f'user-{index}'
        else:
            extra['payload']['id'] = f'user-{index}'
        rows.append(extra)
    # Record the real project identity, independent of native file naming.
    for row in rows:
        if actor.host == 'claude':
            row['cwd'] = str(actor.canonical_project_root)
        elif row.get('type') == 'session_meta':
            row['payload']['cwd'] = str(actor.canonical_project_root)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    monkeypatch.setattr(session_coverage.subprocess, 'run', lambda *a, **k: pytest.fail('bound recovery launched legacy child'))
    sessions = actor.vault_path / 'claude-sessions'
    sessions.mkdir()
    issues = session_coverage.scan(str(actor.vault_path), 'claude-sessions', 'claude-insights', 9999,
                                   reconstruct=True)
    assert len(issues) == 1
    assert issues[0].extra['sid'] == sid
    before = path.read_bytes()
    results = session_coverage.apply(issues, str(actor.state_path / 'backup'))
    assert results[0].status == 'applied', results[0].error
    note = Path(results[0].note_path).read_text()
    metadata = source_sessions._parse_frontmatter(note)
    assert metadata['agent_provider'] == actor.host and metadata['agent_session_id'] == sid
    assert path.read_bytes() == before
    assert current_runtime_context() is actor
    assert session_coverage.scan(str(actor.vault_path), 'claude-sessions', 'claude-insights', 9999) == []


def test_conflicting_codex_historical_ids_are_unverified(selected_host_context):
    home = Path(os.environ['CODEX_HOME']) / 'sessions'
    home.mkdir(parents=True)
    (home / 'rollout.jsonl').write_text(json.dumps({'type':'session_meta',
        'payload':{'id':'one-id','session_id':'another-id'}}) + '\n')
    with pytest.raises(ValueError, match='Conflicting native source IDs'):
        source_sessions._find_jsonl_anywhere('another-id', provider='codex')


@pytest.mark.parametrize('intervention', ['source-change', 'manual-note'])
def test_session_coverage_preserves_intervening_bytes(selected_host_context, intervention):
    from vault_doctor_checks import session_coverage
    from test_vault_doctor_session_coverage import _write_jsonl
    actor = selected_host_context
    roots = actor.native_home / ('projects' if actor.host == 'claude' else 'sessions')
    source = _write_jsonl(roots, 'demo', 'source-cas-full-id', str(actor.canonical_project_root), n_user=5)
    (actor.vault_path / 'claude-sessions').mkdir()
    issues = session_coverage.scan(str(actor.vault_path), 'claude-sessions', 'claude-insights', 9999,
                                   reconstruct=True)
    assert len(issues) == 1
    note = Path(issues[0].note_path)
    if intervention == 'source-change':
        source.write_bytes(source.read_bytes() + b'{"manual":"source changed"}\n')
        expected = source.read_bytes()
    else:
        note.write_text('Manual note added after the audit.\n')
        expected = note.read_bytes()
    result = session_coverage.apply(issues, str(actor.state_path / 'backup'))[0]
    if intervention == 'source-change':
        assert result.status == 'error' and 'changed since' in result.error
        assert source.read_bytes() == expected and not note.exists()
    else:
        assert result.status == 'skipped' and note.read_bytes() == expected


def test_session_coverage_uses_frozen_threshold_and_native_home(selected_host_context, monkeypatch):
    from vault_doctor_checks import session_coverage
    from test_vault_doctor_session_coverage import _write_jsonl
    actor = selected_host_context
    roots = actor.native_home / ('projects' if actor.host == 'claude' else 'sessions')
    _write_jsonl(roots, 'demo', 'below-threshold-id', str(actor.canonical_project_root), n_user=1)
    (actor.vault_path / 'claude-sessions').mkdir()
    changed = dict(actor.config)
    changed['min_messages'] = 0
    actor.config_path.write_text(json.dumps(changed))
    monkeypatch.setenv('HOME', str(actor.canonical_project_root / 'different-home'))
    monkeypatch.setenv('CODEX_HOME', str(actor.canonical_project_root / 'different-codex'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(actor.canonical_project_root / 'different-claude'))
    assert session_coverage.scan(str(actor.vault_path), 'claude-sessions', 'claude-insights', 9999) == []


@pytest.mark.parametrize('suffix', ['partial', 'duplicate-id'])
def test_session_coverage_incomplete_source_is_not_clean(selected_host_context, suffix):
    from vault_doctor_checks import session_coverage
    from test_vault_doctor_session_coverage import _write_jsonl
    actor = selected_host_context
    roots = actor.native_home / ('projects' if actor.host == 'claude' else 'sessions')
    source = _write_jsonl(roots, 'demo', 'unverified-source-id', str(actor.canonical_project_root), n_user=5)
    (actor.vault_path / 'claude-sessions').mkdir()
    if suffix == 'partial':
        source.write_bytes(source.read_bytes() + b'{"unfinished":')
    else:
        other = roots / 'other' / source.name
        other.parent.mkdir()
        other.write_bytes(source.read_bytes())
    sources = [source] if suffix == 'partial' else [source, other]
    originals = {path: path.read_bytes() for path in sources}
    issues = session_coverage.scan(str(actor.vault_path), 'claude-sessions', 'claude-insights', 9999,
                                   reconstruct=True)
    assert len(issues) == len(sources)
    assert all(issue.extra['signal_class'] == 'session-coverage-incomplete' for issue in issues)
    assert all(issue.extra['unresolved'] and issue.confidence == 0 for issue in issues)
    assert all(issue.extra['audit_complete'] is False for issue in issues)
    assert not any(issue.extra['signal_class'] == 'session-coverage-gap' for issue in issues)
    results = session_coverage.apply(issues, str(actor.state_path / 'backup'))
    assert [result.status for result in results] == ['unresolved'] * len(sources)
    assert all(path.read_bytes() == before for path, before in originals.items())
    assert not list((actor.vault_path / 'claude-sessions').iterdir())


def test_incomplete_identity_window_cannot_offer_reconstruction(selected_host_context, monkeypatch):
    import time
    from vault_doctor_checks import session_coverage
    from test_vault_doctor_session_coverage import _write_jsonl
    actor = selected_host_context
    roots = actor.native_home / ('projects' if actor.host == 'claude' else 'sessions')
    first = _write_jsonl(roots, 'demo', 'a-unique-source', str(actor.canonical_project_root), n_user=5)
    second = _write_jsonl(roots, 'demo', 'b-unscanned-source', str(actor.canonical_project_root), n_user=5)
    (actor.vault_path / 'claude-sessions').mkdir()
    originals = {path:path.read_bytes() for path in [first, second]}
    clock = [100.0]
    first_hashes = []
    original = session_coverage._source_sha
    def expire_after_verified_first_source(path, deadline):
        result = original(path, deadline)
        if path == first:
            first_hashes.append(path)
            if len(first_hashes) == 2:
                clock[0] += 11
        return result
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(session_coverage, '_source_sha', expire_after_verified_first_source)
    issues = session_coverage.scan(str(actor.vault_path), 'claude-sessions', 'claude-insights', 9999,
                                  reconstruct=True)
    [gap] = [issue for issue in issues if issue.extra['signal_class'] == 'session-coverage-gap']
    [window] = [issue for issue in issues if 'unscanned_sources' in issue.extra]
    assert window.extra['unscanned_sources'] == 1
    assert gap.extra['unresolved'] and gap.extra['audit_complete'] is False and gap.confidence == 0
    assert all(result.status == 'unresolved' for result in session_coverage.apply(issues, str(actor.state_path/'backup')))
    assert all(path.read_bytes() == before for path,before in originals.items())
    assert not list((actor.vault_path/'claude-sessions').iterdir())


@pytest.mark.parametrize('shape', ['large-header', 'late-cwd', 'missing-cwd'])
def test_coverage_uses_recorded_project_beyond_old_prefix(selected_host_context, shape):
    import copy
    from vault_doctor_checks import session_coverage
    actor = selected_host_context
    sid = 'coverage-large-native-header'
    recorded = actor.canonical_project_root.parent / 'recorded-project'
    recorded.mkdir()
    rows = native_rows(actor.host, sid)
    user = next(row for row in rows if row.get('type') == 'user'
                or row.get('payload', {}).get('role') == 'user')
    for index in (2, 3):
        extra = copy.deepcopy(user)
        if actor.host == 'claude':
            extra['uuid'] = f'coverage-user-{index}'
        else:
            extra['payload']['id'] = f'coverage-user-{index}'
        rows.append(extra)
    for row in rows:
        if actor.host == 'claude':
            row.pop('cwd', None)
            if shape != 'missing-cwd':
                row['cwd'] = str(recorded)
        elif row.get('type') == 'session_meta':
            row['payload'].pop('cwd', None)
            row['payload']['base_instructions'] = {'text': 'x' * (96 * 1024)}
            if shape != 'missing-cwd':
                row['payload']['cwd'] = str(recorded)
    if actor.host == 'claude':
        if shape == 'late-cwd':
            rows.insert(0, {'type': 'queue-operation', 'operation': 'enqueue',
                            'content': 'x' * (96 * 1024)})
        else:
            rows[0]['padding'] = 'x' * (96 * 1024)
    folder = actor.native_home / ('projects/demo' if actor.host == 'claude' else 'sessions')
    folder.mkdir(parents=True)
    source = folder / (sid + '.jsonl' if actor.host == 'claude' else 'rollout-large.jsonl')
    source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    before = source.read_bytes()
    (actor.vault_path / 'claude-sessions').mkdir()
    issues = session_coverage.scan(str(actor.vault_path), 'claude-sessions', 'claude-insights',
                                  9999, reconstruct=True)
    assert source.read_bytes() == before
    assert len(issues) == 1
    if shape == 'missing-cwd':
        assert issues[0].extra['signal_class'] == 'session-coverage-incomplete'
        assert issues[0].extra['unresolved']
        assert not list((actor.vault_path / 'claude-sessions').iterdir())
    else:
        assert issues[0].extra['signal_class'] == 'session-coverage-gap'
        assert issues[0].extra['cwd'] == str(recorded)
        assert issues[0].project == 'recorded-project'
        result = session_coverage.apply(issues, str(actor.state_path / 'backup'))[0]
        assert result.status == 'applied', result.error
        metadata = source_sessions._parse_frontmatter(Path(result.note_path).read_text())
        assert metadata['agent_session_id'] == sid
        assert metadata['agent_provider'] == actor.host


def test_native_coverage_prioritizes_missing_notes_without_parsing_covered_sources(selected_host_context, monkeypatch):
    from vault_doctor_checks import session_coverage
    from test_vault_doctor_session_coverage import _write_jsonl
    from transcripts import read_records as real_read
    import transcripts
    actor=selected_host_context
    roots=actor.native_home/('projects' if actor.host=='claude' else 'sessions')
    sessions=actor.vault_path/'claude-sessions';sessions.mkdir()
    for i in range(1030):
        sid='a-covered-'+str(i).zfill(4)
        _write_jsonl(roots,'demo',sid,str(actor.canonical_project_root),n_user=5)
        (sessions/(sid+'.md')).write_text('---\ntype: claude-session\nagent_provider: '+actor.host+'\nagent_session_id: '+sid+'\n---\n')
    missing=_write_jsonl(roots,'demo','z-missing-source',str(actor.canonical_project_root),n_user=5)
    original=missing.read_bytes();calls=[]
    def read(ctx,cursor,deadline):
        calls.append(ctx.native_session_id)
        assert ctx.native_session_id=='z-missing-source','Covered source body was unnecessarily audited'
        return real_read(ctx,cursor,deadline)
    monkeypatch.setattr(transcripts,'read_records',read)
    issues=session_coverage.scan(str(actor.vault_path),'claude-sessions','claude-insights',9999,reconstruct=True)
    [gap]=[row for row in issues if row.extra['signal_class']=='session-coverage-gap']
    assert gap.extra['sid']=='z-missing-source' and calls==['z-missing-source']
    assert gap.extra['unresolved'] and gap.confidence==0 and gap.extra['audit_complete'] is False
    [window]=[row for row in issues if 'unscanned_sources' in row.extra]
    assert window.extra['unscanned_sources']==7
    assert missing.read_bytes()==original and len(list(sessions.iterdir()))==1030


def test_native_coverage_reads_known_large_source_to_verified_eof(selected_host_context, monkeypatch):
    from vault_doctor_checks import session_coverage
    from test_vault_doctor_session_coverage import _write_jsonl
    from transcripts import read_records as real_read
    import transcripts
    actor=selected_host_context;roots=actor.native_home/('projects' if actor.host=='claude' else 'sessions')
    source=_write_jsonl(roots,'demo','large-uncaptured-source',str(actor.canonical_project_root),n_user=5)
    rows=[json.loads(line) for line in source.read_text().splitlines()]
    header=rows[0]
    padding=({'type':'mode','mode':'x'*600000,'sessionId':'large-uncaptured-source'}
             if actor.host=='claude' else {'type':'session_meta','payload':{**header['payload'],'padding':'x'*600000}})
    source.write_text('\n'.join(map(json.dumps,[header]+[padding]*11+rows[1:]))+'\n')
    original=source.read_bytes();assert len(original)>6400000
    (actor.vault_path/'claude-sessions').mkdir();offsets=[]
    def read(ctx,cursor,deadline):
        offsets.append(cursor.offset)
        return real_read(ctx,cursor,deadline)
    monkeypatch.setattr(transcripts,'read_records',read)
    issues=session_coverage.scan(str(actor.vault_path),'claude-sessions','claude-insights',9999,reconstruct=True)
    [gap]=issues
    assert gap.extra['signal_class']=='session-coverage-gap' and gap.extra['sid']=='large-uncaptured-source'
    assert not gap.extra['unresolved'] and gap.confidence==.9
    assert len(offsets)>=2 and offsets[0]==0 and all(a<b for a,b in zip(offsets,offsets[1:]))
    assert source.read_bytes()==original and not list((actor.vault_path/'claude-sessions').iterdir())


def test_covered_identity_duplicates_are_still_unverified(selected_host_context, monkeypatch):
    from vault_doctor_checks import session_coverage
    from test_vault_doctor_session_coverage import _write_jsonl
    import transcripts
    actor=selected_host_context;roots=actor.native_home/('projects' if actor.host=='claude' else 'sessions')
    first=_write_jsonl(roots,'one','covered-duplicate',str(actor.canonical_project_root),n_user=5)
    second=_write_jsonl(roots,'two','covered-duplicate',str(actor.canonical_project_root),n_user=5)
    originals={p:p.read_bytes() for p in (first,second)}
    sessions=actor.vault_path/'claude-sessions';sessions.mkdir()
    note=sessions/'existing.md';note.write_text('---\ntype: claude-session\nagent_provider: '+actor.host+'\nagent_session_id: covered-duplicate\n---\nManual note.\n')
    before=note.read_bytes()
    monkeypatch.setattr(transcripts,'read_records',lambda *a:pytest.fail('Covered content must not be certified'))
    issues=session_coverage.scan(str(actor.vault_path),'claude-sessions','claude-insights',9999,reconstruct=True)
    assert len(issues)==2 and all(row.extra['signal_class']=='session-coverage-incomplete' for row in issues)
    assert all('duplicated' in row.reason and row.extra['unresolved'] for row in issues)
    assert note.read_bytes()==before and all(p.read_bytes()==raw for p,raw in originals.items())


def test_native_coverage_loss_cannot_become_a_verified_gap(selected_host_context, monkeypatch):
    from vault_doctor_checks import session_coverage
    from test_vault_doctor_session_coverage import _write_jsonl
    from transcripts import read_records as real_read
    from dataclasses import replace
    import transcripts
    actor=selected_host_context;roots=actor.native_home/('projects' if actor.host=='claude' else 'sessions')
    source=_write_jsonl(roots,'demo','lost-input-source',str(actor.canonical_project_root),n_user=5)
    raw=source.read_bytes();(actor.vault_path/'claude-sessions').mkdir()
    monkeypatch.setattr(transcripts,'read_records',lambda ctx,cursor,deadline:replace(real_read(ctx,cursor,deadline),loss_of_input=True))
    [issue]=session_coverage.scan(str(actor.vault_path),'claude-sessions','claude-insights',9999,reconstruct=True)
    assert issue.extra['signal_class']=='session-coverage-incomplete' and issue.extra['loss_of_input'] is True
    assert issue.extra['unresolved'] and issue.confidence==0 and issue.extra['audit_complete'] is False
    assert source.read_bytes()==raw and not list((actor.vault_path/'claude-sessions').iterdir())


def test_native_coverage_prioritizes_requested_project_within_missing_window(selected_host_context):
    from vault_doctor_checks import session_coverage
    from test_vault_doctor_session_coverage import _write_jsonl
    actor=selected_host_context;roots=actor.native_home/('projects' if actor.host=='claude' else 'sessions')
    for i in range(1030):
        _write_jsonl(roots,'a-other','a-other-'+str(i).zfill(4),'/historical/other',n_user=1)
    source=_write_jsonl(roots,actor.canonical_project_root.name,'z-selected-project',str(actor.canonical_project_root),n_user=5)
    raw=source.read_bytes();(actor.vault_path/'claude-sessions').mkdir()
    rows=session_coverage.scan(str(actor.vault_path),'claude-sessions','claude-insights',9999,reconstruct=True)
    [gap]=[row for row in rows if row.extra['signal_class']=='session-coverage-gap']
    assert gap.extra['sid']=='z-selected-project' and gap.project==actor.canonical_project_root.name
    assert gap.extra['unresolved'] and gap.extra['audit_complete'] is False and gap.confidence==0
    assert source.read_bytes()==raw and not list((actor.vault_path/'claude-sessions').iterdir())


def test_native_project_scheduling_hint_cannot_certify_or_change_project(selected_host_context, monkeypatch):
    from vault_doctor_checks import session_coverage
    from test_vault_doctor_session_coverage import _write_jsonl
    actor=selected_host_context;roots=actor.native_home/('projects' if actor.host=='claude' else 'sessions')
    source=_write_jsonl(roots,actor.canonical_project_root.name,'false-project-label','/historical/elsewhere',n_user=5)
    raw=source.read_bytes();(actor.vault_path/'claude-sessions').mkdir()
    monkeypatch.setattr(session_coverage,'_native_project_hint',lambda *a:True)
    assert session_coverage.scan(str(actor.vault_path),'claude-sessions','claude-insights',9999,
        project=actor.canonical_project_root.name,reconstruct=True)==[]
    assert source.read_bytes()==raw and not list((actor.vault_path/'claude-sessions').iterdir())
