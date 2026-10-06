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
    before = source.read_bytes()
    with pytest.raises(ValueError, match='incomplete|duplicated'):
        session_coverage.scan(str(actor.vault_path), 'claude-sessions', 'claude-insights', 9999)
    assert source.read_bytes() == before
    assert not list((actor.vault_path / 'claude-sessions').iterdir())
