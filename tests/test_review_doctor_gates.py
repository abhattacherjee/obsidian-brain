"""Review regressions use only disposable vault and package bytes."""
import datetime
import importlib.util
import json
from pathlib import Path
import pytest
from selected_legacy_vault import selected_host_context

ROOT = Path(__file__).resolve().parents[1]


def load(relative):
    spec = importlib.util.spec_from_file_location('review_' + Path(relative).stem, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dry_run_never_invokes_capture_recovery(selected_host_context, monkeypatch, capsys):
    import sys
    from scripts import vault_doctor
    from types import SimpleNamespace
    monkeypatch.setattr(vault_doctor, '_recover_runtime_pending', lambda: pytest.fail('dry-run recovery wrote'))
    monkeypatch.setattr(vault_doctor.vault_doctor_checks, 'all_checks', lambda: [SimpleNamespace(
        NAME='read-only', DEFAULT_WINDOW_DAYS=30, scan=lambda *a, **k: [])])
    monkeypatch.setattr(sys, 'argv', ['doctor', '--json'])
    before = {p: p.read_bytes() for p in selected_host_context.vault_path.rglob('*') if p.is_file()}
    assert vault_doctor.main() == 0
    assert json.loads(capsys.readouterr().out)['total_issues'] == 0
    assert {p: p.read_bytes() for p in selected_host_context.vault_path.rglob('*') if p.is_file()} == before


def test_unrelated_invalid_note_and_symlink_do_not_block_snapshot_links(selected_host_context, tmp_path_factory):
    from scripts.vault_doctor_checks.snapshot_migration import _rewrite_wikilinks_in_vault
    vault = selected_host_context.vault_path
    corrupt = vault / 'unrelated.md'; corrupt.write_bytes(b'\xff\xfe')
    outside = tmp_path_factory.mktemp('unrelated-external') / 'private.md'
    outside.write_bytes(b'private external bytes')
    (vault / 'external.md').symlink_to(outside)
    link = vault / 'link.md'; link.write_text('Keep [[old-snapshot]] and manual prose.\n')
    result = _rewrite_wikilinks_in_vault(str(vault), 'old-snapshot', 'new-snapshot')
    assert result[0] == 1
    assert link.read_text() == 'Keep [[new-snapshot]] and manual prose.\n'
    assert corrupt.read_bytes() == b'\xff\xfe'
    assert outside.read_bytes() == b'private external bytes'


def test_frontmatter_repair_preserves_crlf_body():
    from scripts.vault_doctor_checks.source_sessions import _rewrite_frontmatter
    body = '\r\n# Manual body\r\nKeep raw bytes.\r\n'
    original = '---\r\nsource_session: old\r\nsource_session_note: "[[old]]"\r\n---\r\n' + body
    result = _rewrite_frontmatter(original, 'new-id', 'new-note')
    assert 'source_session: new-id\r\n' in result
    assert result.endswith('---\r\n' + body)


@pytest.mark.parametrize('body', [
    '<!--\n- [x] Shared behavior affects Claude Code and Codex\n-->\nImpact evidence: hidden choice',
    '- [x] Shared behavior affects Claude Code and Codex\nImpact evidence:\n# Another heading',
    '- [x] Shared behavior affects Claude Code and Codex\nImpact evidence: <!-- hidden -->',
])
def test_impact_requires_visible_choice_and_same_line_evidence(body):
    with pytest.raises(ValueError):
        load('scripts/ci-checks/check-codex-impact.py').validate(body)


def test_version_bump_preserves_unicode_and_updates_architecture_date(tmp_path):
    version = load('scripts/dev-test/version_sync.py')
    files = {'.claude-plugin/plugin.json': {'name':'brain','version':'1.2.3'},
             '.codex-plugin/plugin.json': {'name':'brain','version':'1.2.3'},
             '.claude-plugin/marketplace.json': {'plugins':[{'name':'brain','version':'1.2.3'}]},
             'docs/architecture/architecture.json': {'version':'1.2.3','lastUpdated':'2000-01-01','title':'Memory → notes ✓'}}
    for name, data in files.items():
        p = tmp_path / name; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    version.run(tmp_path, 'patch')
    raw = (tmp_path / 'docs/architecture/architecture.json').read_text()
    data = json.loads(raw)
    assert data['version'] == '1.2.4'
    assert data['lastUpdated'] == datetime.date.today().isoformat()
    assert 'Memory → notes ✓' in raw and '\\u' not in raw


def test_packaged_doctor_has_its_shared_state_module(tmp_path):
    package = load('scripts/dev-test/package_tree.py')
    package.copy_runtime(ROOT, tmp_path / 'package')
    assert (tmp_path / 'package/scripts/doctor_repair_state.py').read_bytes() == (ROOT / 'scripts/doctor_repair_state.py').read_bytes()
    spec = importlib.util.spec_from_file_location('packaged_doctor_state', tmp_path / 'package/scripts/doctor_repair_state.py')
    state = importlib.util.module_from_spec(spec); spec.loader.exec_module(state)
    assert state.REPAIRS.get() is None and state.INVOCATION.get() is None


def test_dry_pending_audit_reports_existing_intents_without_creating_state(selected_host_context):
    from scripts.vault_doctor import _inspect_runtime_pending
    from note_transactions import session_state_path, coordination_location
    context = selected_host_context
    journal = coordination_location(context) / 'state.sqlite3'
    assert not journal.exists()
    assert _inspect_runtime_pending()['status'] == 'complete'
    assert not journal.parent.exists()
    pending = session_state_path(context) / 'pending'; pending.mkdir()
    (pending / 'intent.json').write_text('{"synthetic":"pending"}')
    before = {p:p.read_bytes() for p in context.state_path.rglob('*') if p.is_file()}
    result = _inspect_runtime_pending()
    assert result['status'] == 'pending' and result['pending_mutations'] == 1
    assert {p:p.read_bytes() for p in context.state_path.rglob('*') if p.is_file()} == before
    assert not journal.parent.exists()


def test_dry_pending_audit_never_claims_clean_from_live_wal(selected_host_context):
    from scripts.vault_doctor import _inspect_runtime_pending
    from note_transactions import coordination_path
    root = coordination_path(selected_host_context)
    wal = root / 'state.sqlite3-wal'; wal.write_bytes(b'live-writer-data')
    result = _inspect_runtime_pending()
    assert result['status'] == 'unavailable'
    assert 'WAL' in result['warnings'][0]
    assert wal.read_bytes() == b'live-writer-data'
    assert not (root / 'state.sqlite3').exists()


def test_session_coverage_filters_old_sources_before_native_cap(selected_host_context, monkeypatch):
    import os
    import time
    from scripts.vault_doctor_checks import session_coverage
    context = selected_host_context
    root = context.native_home / ('projects/project' if context.host == 'claude' else 'sessions')
    root.mkdir(parents=True, exist_ok=True)
    for index in range(1025):
        path = root / ('old-%s.jsonl' % index)
        path.write_text('deliberately not parsed\n')
        os.utime(path, (1,1))
    assert session_coverage._scan_selected(context, str(context.vault_path), 'sessions', 'insights', 1) == []


def test_claude_auxiliary_jsonl_does_not_block_session_audit(selected_host_context):
    from runtime_adapters import metrics_path
    from scripts.vault_doctor_checks import session_coverage
    context = selected_host_context
    metrics = metrics_path(context)
    metrics.parent.mkdir(parents=True, exist_ok=True)
    metrics.write_text('{"metric":1}\n')
    if context.host == 'claude':
        context.native_home.mkdir(parents=True, exist_ok=True)
        (context.native_home / 'obsidian-brain-summarizer-metrics.jsonl').write_text('{"metric":1}\n')
        root = context.native_home / 'projects/project'; root.mkdir(parents=True, exist_ok=True)
        for folder, name in [('subagents', 'agent.jsonl'), ('vercel-plugin', 'skill-injections.jsonl')]:
            nested = root / folder / name
            nested.parent.mkdir()
            nested.write_text('unrelated auxiliary data\n')
    assert session_coverage._scan_selected(context, str(context.vault_path), 'sessions', 'insights', 1) == []
    assert metrics.read_text() == '{"metric":1}\n'


def test_unknown_top_level_jsonl_keeps_session_audit_incomplete(selected_host_context):
    from scripts.vault_doctor_checks import session_coverage
    context = selected_host_context
    root = context.native_home / ('projects/project' if context.host == 'claude' else 'sessions')
    root.mkdir(parents=True, exist_ok=True)
    source = root / 'unknown-source.jsonl'
    source.write_text('{"metric":1}\n')
    source_before = source.read_bytes()
    vault_before = {p: p.read_bytes() for p in context.vault_path.rglob('*') if p.is_file()}
    issues = session_coverage._scan_selected(
        context, str(context.vault_path), 'sessions', 'insights', 1, reconstruct=True)
    assert len(issues) == 1
    assert issues[0].extra['signal_class'] == 'session-coverage-incomplete'
    assert issues[0].confidence == 0
    assert issues[0].extra['unresolved'] is True
    assert issues[0].extra['audit_complete'] is False
    assert source.read_bytes() == source_before
    assert {p: p.read_bytes() for p in context.vault_path.rglob('*') if p.is_file()} == vault_before


def test_shared_reachable_script_and_reference_injections_are_linted(tmp_path):
    lint = load('scripts/ci-checks/host_neutral_lint.py')
    for name in ('scripts/vault_doctor_checks/bad.py', 'scripts/vault_doctor.py', 'skills/recall/references/shared.md'):
        p = tmp_path / name; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('def bad():\n    return "~/.claude/projects"\n' if p.suffix == '.py' else 'Use ~/.claude/projects')
    paths = {path for path, _, _, _ in lint.lint_repository(tmp_path, {})}
    assert paths == {'scripts/vault_doctor_checks/bad.py', 'scripts/vault_doctor.py', 'skills/recall/references/shared.md'}


def test_script_resources_follow_loaded_tree_and_refuse_unrelated_directory(tmp_path, monkeypatch):
    resolver = load('scripts/dev-test/loaded_resource_root.py')
    root = tmp_path / 'loaded tree with spaces'
    for name in ('scripts/test-security.sh', 'hooks/obsidian_utils.py', '.codex-plugin/plugin.json'):
        p = root / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text('{}')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('HOME', str(tmp_path / 'poisoned-home'))
    assert resolver.resolve(root / 'scripts/test-security.sh') == root
    with pytest.raises(ValueError, match='absolute'):
        resolver.resolve('scripts/test-security.sh')
    (root / 'hooks/obsidian_utils.py').unlink()
    with pytest.raises(ValueError, match='sentinel'):
        resolver.resolve(root / 'scripts/test-security.sh')
    for name in ('test-security.sh','test-phase1-manual.sh'):
        source = (ROOT / 'scripts' / name).read_text()
        assert 'loaded_resource_root.py' in source
        assert 'known_marketplaces.json' not in source
        assert 'glob.glob' not in source


@pytest.mark.parametrize('mutation', ['version', 'name', 'missing'])
def test_preflight_rejects_codex_manifest_drift(tmp_path, monkeypatch, mutation):
    import sys
    source = (ROOT / 'scripts/commit-preflight.sh').read_text()
    start = source.index('import json, sys, traceback\n')
    code = source[start:source.index('\nPY\n)', start)]
    paths = [tmp_path / name for name in ('claude.json','market.json','codex.json')]
    paths[0].write_text(json.dumps({'name':'obsidian-brain','version':'1.2.3'}))
    paths[1].write_text(json.dumps({'plugins':[{'name':'obsidian-brain','version':'1.2.3'}]}))
    codex = {'name':'obsidian-brain','version':'1.2.3'}
    if mutation != 'missing':
        codex[mutation] = 'different'
        paths[2].write_text(json.dumps(codex))
    monkeypatch.setattr(sys, 'argv', ['check'] + list(map(str, paths)))
    with pytest.raises(SystemExit) as error:
        exec(compile(code, 'preflight-manifests', 'exec'), {})
    assert error.value.code == 2


def test_dry_pending_audit_surfaces_blocked_source_without_replay(selected_host_context):
    import sqlite3
    from scripts.vault_doctor import _inspect_runtime_pending
    from note_transactions import coordination_path
    journal = coordination_path(selected_host_context) / 'state.sqlite3'
    with sqlite3.connect(journal) as connection:
        connection.execute('CREATE TABLE identity(vault TEXT PRIMARY KEY)')
        connection.execute('INSERT INTO identity VALUES (?)',(str(selected_host_context.vault_path.resolve()),))
        connection.execute('CREATE TABLE source_sessions(scope TEXT, completeness TEXT, cursor TEXT, descriptor TEXT)')
        cursor = {'parser_state': {'_deferred_source_rows': [{'reason': 'unknown_schema:user-note'}]}}
        descriptor = {'host': selected_host_context.host}
        connection.execute('INSERT INTO source_sessions VALUES (?,?,?,?)',
                           ('synthetic-blocked', 'partial', json.dumps(cursor), json.dumps(descriptor)))
    before = journal.read_bytes()
    result = _inspect_runtime_pending()
    assert result['status'] == 'pending' and result['pending_sources'] == 1
    assert any('type=user-note' in warning for warning in result['warnings'])
    assert journal.read_bytes() == before
    assert not Path(str(journal) + '-shm').exists()


def test_distribution_probes_require_exact_validated_package(tmp_path, monkeypatch):
    resolver = load('scripts/dev-test/loaded_resource_root.py')
    root = tmp_path / 'selected package with spaces'
    for name in ('hooks/obsidian_utils.py', 'hooks/hooks.json', 'hooks/codex-hooks.json',
                 'scripts/vault_doctor_checks/snapshot_integrity.py', 'skills/recall/SKILL.md'):
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('{}')
    for name in ('.claude-plugin', '.codex-plugin'):
        path = root / name / 'plugin.json'; path.parent.mkdir(parents=True)
        data = {'name': 'obsidian-brain', 'version': '3.8.1'}
        if name == '.codex-plugin': data['hooks'] = './hooks/codex-hooks.json'
        path.write_text(json.dumps(data))
    monkeypatch.setenv('HOME', str(tmp_path / 'unrelated-home'))
    assert resolver.selected_cache(str(root)) == root
    for bad in ('', 'relative'):
        with pytest.raises(ValueError): resolver.selected_cache(bad)
    alias = tmp_path / 'alias'; alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'): resolver.selected_cache(str(alias))
    descriptor = root / '.codex-plugin/plugin.json'
    original = descriptor.read_text()
    descriptor.write_text(json.dumps({'name': 'obsidian-brain', 'version': '3.8.2'}))
    with pytest.raises(ValueError, match='versions differ'): resolver.selected_cache(str(root))
    descriptor.write_text(original)
    sentinel = root / 'hooks/obsidian_utils.py'; sentinel.unlink(); sentinel.symlink_to(tmp_path / 'outside.py')
    with pytest.raises(ValueError, match='sentinel'): resolver.selected_cache(str(root))
    for name in ('test-issue-101-manual.sh', 'test-issue-105-manual.sh',
                 'test-snapshots-manual.sh', 'test-vault-doctor-snapshots-manual.sh'):
        source = (ROOT / 'scripts/dev-test' / name).read_text()
        assert '--cache-path "${OB_CACHE_PATH:-}"' in source
        assert 'find ~/.claude/plugins/cache' not in source
        assert 'sort -V' not in source


@pytest.mark.parametrize('status,loss,expected', [('pending',False,1),('unavailable',False,2),('pending',True,2)])
def test_report_only_pending_input_keeps_error_exit_distinct(selected_host_context,monkeypatch,capsys,status,loss,expected):
    import sys
    from scripts import vault_doctor
    from types import SimpleNamespace
    monkeypatch.setattr(vault_doctor,'_inspect_runtime_pending',lambda:{'status':status,'pending_sources':1,'pending_mutations':0,'loss_of_input':loss,'warnings':['safe diagnostic']})
    monkeypatch.setattr(vault_doctor.vault_doctor_checks,'all_checks',lambda:[SimpleNamespace(NAME='read-only',DEFAULT_WINDOW_DAYS=30,scan=lambda *a,**k:[])])
    monkeypatch.setattr(sys,'argv',['doctor','--json'])
    assert vault_doctor.main()==expected
    report=json.loads(capsys.readouterr().out)
    assert report['capture_recovery']['pending_sources']==1
    assert report['capture_recovery']['warnings']==['safe diagnostic']


@pytest.mark.parametrize("body", [
    '```\n- [x] No Codex impact; explain why\n```\nImpact evidence: docs only\n',
    '~~~markdown\n- [x] No Codex impact; explain why\n~~~\nImpact evidence: docs only\n',
    '- [x] No Codex impact; explain why\n<!-- unfinished\nImpact evidence: hidden\n',
    '```\n- [x] No Codex impact; explain why\nImpact evidence: hidden\n',
])
def test_hidden_markdown_cannot_supply_pr_impact_evidence(body):
    import importlib.util
    from pathlib import Path
    script = Path(__file__).parents[1] / 'scripts/ci-checks/check-codex-impact.py'
    spec = importlib.util.spec_from_file_location('review_impact_visibility', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError):
        module.validate(body)


def test_visible_pr_impact_survives_unrelated_fenced_example():
    import importlib.util
    from pathlib import Path
    script = Path(__file__).parents[1] / 'scripts/ci-checks/check-codex-impact.py'
    spec = importlib.util.spec_from_file_location('review_impact_visibility_ok', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.validate('```python\nprint("example")\n```\n'
                    '- [x] No Codex impact; explain why\n'
                    'Impact evidence: changed prose only\n')


@pytest.mark.parametrize('marker', ['`<!--`', '``literal ` <!--``'])
def test_inline_comment_literal_keeps_visible_impact_choice(marker):
    checker = load('scripts/ci-checks/check-codex-impact.py')
    checker.validate('The parser handles ' + marker + '.\n'
                     '- [x] Shared behavior affects Claude Code and Codex\n'
                     'Impact evidence: both host capture contracts\n')
    with pytest.raises(ValueError):
        checker.validate('The parser handles ' + marker + '.\n<!--\n'
                         '- [x] Shared behavior affects Claude Code and Codex\n'
                         'Impact evidence: hidden contracts\n-->')


def test_retained_conflict_has_exact_private_ack_reference_on_every_audit(selected_host_context):
    import hashlib
    from note_transactions import NoteMutation, apply_mutations
    from scripts import vault_doctor
    context = selected_host_context
    note = context.vault_path / 'manual-ack-reference.md'
    note.write_text('manual content stays')
    pending = apply_mutations(context, [NoteMutation(
        note, 'stale-revision', {'document': 'private proposed replacement'}, 'reference-control')])
    raw = pending.pending_path.read_bytes()
    expected = {'path': str(pending.pending_path), 'sha256': hashlib.sha256(raw).hexdigest()}
    for _ in range(2):
        audit = vault_doctor._inspect_runtime_pending()
        assert audit['status'] == 'pending'
        assert audit['pending_mutations'] == 1
        assert audit['pending_intents'] == [expected]
        assert audit['warnings']
        assert 'private proposed replacement' not in json.dumps(audit)
        recovered = vault_doctor._recover_runtime_pending()
        assert recovered['status'] == 'pending'
        assert recovered['pending_intents'] == [expected]
        assert recovered['warnings']
        assert note.read_text() == 'manual content stays'
        assert pending.pending_path.read_bytes() == raw


def test_pending_fingerprint_refuses_symlink_and_keeps_target_private(selected_host_context):
    import time
    import note_transactions as transactions
    target = selected_host_context.state_path / 'private-target.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('private content')
    target.chmod(0o600)
    link = target.with_name('link.json')
    link.symlink_to(target)
    with pytest.raises(ValueError, match='symbolic link'):
        transactions._pending_intent_reference(link, time.monotonic() + 1)
    assert target.read_text() == 'private content'


@pytest.mark.parametrize('supplied', [['--discard-pending','intent.json'],
                                    ['--expected-pending-sha256','0'*64],
                                    ['--discard-pending','intent.json','--expected-pending-sha256','0'*64]])
def test_pending_discard_requires_complete_explicit_ack_before_any_recovery(selected_host_context, monkeypatch, supplied):
    import sys
    from scripts import vault_doctor
    monkeypatch.setattr(vault_doctor, '_recover_runtime_pending', lambda: pytest.fail('invalid acknowledgment reached recovery'))
    monkeypatch.setattr(vault_doctor, '_inspect_runtime_pending', lambda: pytest.fail('invalid acknowledgment reached audit'))
    monkeypatch.setattr(sys, 'argv', ['doctor','--yes'] + supplied)
    assert vault_doctor.main() == 3


def test_explicit_pending_discard_matches_bytes_and_never_edits_note(selected_host_context, monkeypatch, capsys):
    import hashlib
    import sys
    from types import SimpleNamespace
    from scripts import vault_doctor
    from note_transactions import _pending
    context = selected_host_context
    note = context.vault_path / 'manual.md'; note.write_text('Keep manual content')
    path = _pending(context,'synthetic-ack',json.dumps({'operation':'synthetic-ack','path':str(note),'expected':'prior','changes':{'document':'Unapproved change'}}))
    payload = path.read_bytes(); before = note.read_bytes()
    monkeypatch.setattr(vault_doctor.vault_doctor_checks,'all_checks',lambda:[SimpleNamespace(NAME='synthetic',DEFAULT_WINDOW_DAYS=7,scan=lambda *a,**k:[])])
    monkeypatch.setattr(vault_doctor,'_recover_runtime_pending',lambda:pytest.fail('explicit acknowledgment replayed unrelated intents'))
    arguments = ['doctor','--apply','--yes','--discard-pending',str(path),'--expected-pending-sha256']
    monkeypatch.setattr(sys,'argv',arguments+['0'*64])
    assert vault_doctor.main()==2
    assert path.read_bytes()==payload and note.read_bytes()==before
    monkeypatch.setattr(sys,'argv',arguments+[hashlib.sha256(payload).hexdigest()])
    assert vault_doctor.main()==0
    assert not path.exists() and note.read_bytes()==before
    assert 'destination note unchanged' in capsys.readouterr().err


def test_admin_migration_finishes_before_fresh_bounded_recovery(selected_host_context, monkeypatch):
    import time
    from types import SimpleNamespace
    import capture
    import note_transactions as transactions
    from scripts import vault_doctor
    observed = {}
    def connect(context):
        assert context is selected_host_context
        observed['budget'] = transactions._migration_seconds.get()
        class Connection:
            def close(self):
                observed['closed'] = time.monotonic()
        return Connection()
    def writes(context, max_operations, deadline):
        assert observed['closed'] < deadline
        assert .9 < deadline - observed['closed'] <= 1.1
        assert max_operations == 8
        observed['deadline'] = deadline
        return transactions.WriteResult('unchanged')
    def captures(context, max_sources, deadline, include_active, replay_retired):
        assert deadline == observed['deadline'] and max_sources == 8 and include_active and replay_retired
        return SimpleNamespace(status='complete', applied_revision=None, pending_sources=0, loss_of_input=False, warnings=())
    monkeypatch.setattr(transactions,'connect_coordination',connect)
    monkeypatch.setattr(transactions,'recover_pending_mutations',writes)
    monkeypatch.setattr(capture,'recover_registered',captures)
    assert vault_doctor._recover_runtime_pending()['status']=='complete'
    assert observed['budget']==60
    assert transactions._migration_seconds.get()==.5


def test_dry_pending_audit_counts_registered_sessions_once_without_replay(selected_host_context):
    from dataclasses import replace
    from note_transactions import _pending
    from scripts.vault_doctor import _inspect_runtime_pending
    context = selected_host_context
    later = replace(context, native_session_id=context.native_session_id + '-later')
    first = _pending(context,'selected-intent',json.dumps({'operation':'selected-intent','path':str(context.vault_path/'manual.md'),'expected':'prior','changes':{'document':'pending'}}))
    second = _pending(later,'later-intent',json.dumps({'operation':'later-intent','path':str(context.vault_path/'other.md'),'expected':'prior','changes':{'document':'pending'}}))
    before = (first.read_bytes(), second.read_bytes())
    result = _inspect_runtime_pending()
    assert result['status']=='pending' and result['pending_mutations']==2
    assert (first.read_bytes(), second.read_bytes())==before


@pytest.mark.parametrize('capture_status,write_status', [('unavailable','pending'),('complete','unavailable')])
def test_pending_notes_cannot_hide_actual_recovery_error(selected_host_context, monkeypatch, capture_status, write_status):
    from types import SimpleNamespace
    import capture
    import note_transactions
    from scripts import vault_doctor
    monkeypatch.setattr(capture,'recover_registered',lambda *a,**k:SimpleNamespace(
        status=capture_status,applied_revision=None,pending_sources=1,loss_of_input=False,warnings=('Synthetic recovery diagnostic',)))
    monkeypatch.setattr(note_transactions,'recover_pending_mutations',lambda *a,**k:note_transactions.WriteResult(write_status))
    assert vault_doctor._recover_runtime_pending()['status']=='unavailable'


def test_dry_audit_rejects_journal_for_another_vault_without_writes(selected_host_context):
    import sqlite3
    from note_transactions import coordination_path
    from scripts.vault_doctor import _inspect_runtime_pending
    journal = coordination_path(selected_host_context) / 'state.sqlite3'
    with sqlite3.connect(journal) as connection:
        connection.execute('CREATE TABLE identity(vault TEXT PRIMARY KEY)')
        connection.execute('INSERT INTO identity VALUES (?)',('/synthetic/unrelated-vault',))
    before = journal.read_bytes()
    result = _inspect_runtime_pending()
    assert result['status']=='unavailable'
    assert 'The coordination journal vault identity is unverified' in result['warnings']
    assert journal.read_bytes()==before
    assert not Path(str(journal)+'-shm').exists()


def test_runtime_copy_refuses_missing_doctor_dependency_before_target_write(tmp_path):
    package = load('scripts/dev-test/package_tree.py')
    source = tmp_path / 'synthetic-source'
    for name in ('hooks/brain_cli.py','scripts/vault_doctor.py','scripts/test-dev-skill.sh',
                 'scripts/vault_doctor_checks/__init__.py'):
        path = source / name; path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# synthetic')
    target = tmp_path / 'target'
    with pytest.raises(ValueError,match='Runtime package is incomplete'):
        package.copy_runtime(source,target)
    assert not target.exists()


def test_dry_audit_accepts_verified_physical_vault_after_rename(selected_host_context):
    import sqlite3
    from note_transactions import coordination_path
    from scripts.vault_doctor import _inspect_runtime_pending
    context=selected_host_context
    journal=coordination_path(context)/'state.sqlite3'
    details=context.vault_path.stat()
    with sqlite3.connect(journal) as connection:
        connection.execute('CREATE TABLE identity(vault TEXT PRIMARY KEY)')
        connection.execute('INSERT INTO identity VALUES (?)',('/synthetic/prior-vault-name',))
        connection.execute('CREATE TABLE physical_identity(vault_id TEXT PRIMARY KEY)')
        connection.execute('INSERT INTO physical_identity VALUES (?)',(str(details.st_dev)+':'+str(details.st_ino),))
    before=journal.read_bytes()
    assert _inspect_runtime_pending()['status']=='complete'
    assert journal.read_bytes()==before


def test_manual_snapshot_probes_isolate_coordination_state():
    for name in ('test-snapshots-manual.sh', 'test-vault-doctor-snapshots-manual.sh'):
        source = (ROOT / 'scripts/dev-test' / name).read_text()
        assert 'export XDG_STATE_HOME="$HOME/.local/state"' in source
        assert 'mkdir -p "$CLAUDE_CONFIG_DIR" "$CODEX_HOME" "$XDG_STATE_HOME"' in source
        assert source.index('export XDG_STATE_HOME=') < source.index('from vault_doctor_checks' if name.startswith('test-vault-doctor') else 'from vault_index import ensure_index')


def test_security_preflight_source_does_not_depend_on_working_directory():
    source = (ROOT / 'scripts/test-security.sh').read_text()
    block = source.split('# --- Test 8:', 1)[1].split('# --- Test 9:', 1)[0]
    assert 'with open(sys.argv[1])' in block
    assert '\" "$RESOURCE_ROOT/scripts/commit-preflight.sh"' in block
    assert "open('scripts/commit-preflight.sh')" not in block
