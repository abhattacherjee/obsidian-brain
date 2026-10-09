"""Summaries bind capture revisions and preserve user-owned text."""
import hashlib
import json

import pytest

from runtime_context import using_runtime_context
from note_transactions import NoteMutation, apply_mutations, read_revision
from obsidian_utils import upgrade_note_with_summary


@pytest.fixture
def context(selected_host_context):
    return selected_host_context


def seeded_note(context):
    note = context.vault_path / 'session.md'
    note.write_text('---\ntype: claude-session\nstatus: auto-logged\nmy_field: keep\nagent_provider: claude\n---\n# My title\nUser prose.\n')
    apply_mutations(context, [NoteMutation(note, read_revision(context, note),
                    {'capture': 'First captured fact.\n'}, 'initial-capture')])
    return note


def test_summary_binds_capture_and_preserves_user_prose(context):
    note = seeded_note(context)
    baseline = read_revision(context, note)
    with note.open('a') as stream:
        stream.write('\nUser addition after AI started.\n')
    with using_runtime_context(context):
        result = upgrade_note_with_summary(str(note), '## Summary\nA captured fact.\n',
                        str(context.vault_path), 'sessions', 'project', expected_revision=baseline)
    assert result.startswith('Upgraded ')
    text = note.read_text()
    assert 'my_field: keep' in text
    assert 'agent_provider: claude' in text
    assert 'author_host: ' + json.dumps(context.host) in text
    assert 'operation_id:' in text
    assert '# My title\nUser prose.' in text
    assert 'User addition after AI started.' in text
    assert 'First captured fact.' in text
    from note_transactions import _regions
    capture_hash = _regions(text)['capture']
    assert 'summary_revision: ' + json.dumps(capture_hash) in text
    assert capture_hash != hashlib.sha256(text.encode()).hexdigest()


def test_summary_rejects_new_capture_without_clobber(context):
    note = seeded_note(context)
    baseline = read_revision(context, note)
    apply_mutations(context, [NoteMutation(note, baseline,
                    {'capture': 'Second captured fact.\n'}, 'next-capture')])
    before = note.read_text()
    with using_runtime_context(context):
        result = upgrade_note_with_summary(str(note), '## Summary\nStale fact.\n',
                        str(context.vault_path), 'sessions', 'project', expected_revision=baseline)
    assert result.startswith('Failed: summary publication conflict')
    assert note.read_text() == before

def test_later_capture_is_queued_for_new_summary(context):
    from obsidian_utils import find_unsummarized_notes
    sessions = context.vault_path / 'sessions'; sessions.mkdir()
    note = sessions / 'session.md'
    note.write_text('---\ntype: claude-session\nproject: project\nstatus: auto-logged\n---\nUser prose.\n')
    apply_mutations(context, [NoteMutation(note, read_revision(context, note), {'capture': 'First.\n'}, 'seed')])
    with using_runtime_context(context):
        assert upgrade_note_with_summary(str(note), '## Summary\nFirst.\n', str(context.vault_path), 'sessions', 'project').startswith('Upgraded ')
        assert json.loads(find_unsummarized_notes(str(context.vault_path), 'sessions', 'project'))['unsummarized'] == []
        baseline = read_revision(context, note)
        apply_mutations(context, [NoteMutation(note, baseline, {'capture': 'First.\nSecond.\n'}, 'advance')])
        queued = json.loads(find_unsummarized_notes(str(context.vault_path), 'sessions', 'project'))
    assert queued['unsummarized'] == [str(note)]
    assert 'User prose.' in note.read_text()

def test_native_summary_prepares_retained_capture_without_foreign_transcript(context, monkeypatch):
    import obsidian_utils
    note = context.vault_path / 'native.md'
    note.write_text('---\ntype: claude-session\nproject: project\nsession_id: original-native-id\nstatus: auto-logged\n---\nUser prose.\n')
    apply_mutations(context, [NoteMutation(note, read_revision(context, note),
                    {'capture': 'User: Request.\nAssistant: Done.\nTool: native evidence.\n'}, 'native-input')])
    monkeypatch.setattr(obsidian_utils, 'find_transcript_jsonl', lambda *args: pytest.fail('no foreign or newer transcript read'))
    with using_runtime_context(context):
        prep = obsidian_utils._prepare_note_for_summary(str(note), str(context.vault_path), 'sessions', 'project')
    assert prep['ok']
    assert prep['source'] == 'retained native capture'
    assert 'Tool: native evidence.' in '\n'.join(prep['user_msgs'])
    assert 'User prose.' not in '\n'.join(prep['user_msgs'])


def test_context_brief_marks_native_summary_stale_after_new_capture(context):
    import obsidian_utils
    sessions = context.vault_path / 'sessions'; sessions.mkdir()
    note = sessions / '2026-10-05-session.md'
    note.write_text('---\ntype: claude-session\nproject: project\ndate: 2026-10-05\nstatus: auto-logged\n---\nUser prose.\n')
    apply_mutations(context, [NoteMutation(note, read_revision(context, note), {'capture': 'First.\n'}, 'brief-seed')])
    with using_runtime_context(context):
        assert upgrade_note_with_summary(str(note), '## Summary\nOld captured result.\n', str(context.vault_path), 'sessions', 'project').startswith('Upgraded ')
        fresh = obsidian_utils.build_context_brief(str(context.vault_path), 'sessions', 'insights', 'project')
        assert 'Summary stale' not in fresh
        baseline = read_revision(context, note)
        apply_mutations(context, [NoteMutation(note, baseline, {'capture': 'First.\nLater fact.\n'}, 'brief-advance')])
        pending = obsidian_utils.build_context_brief(str(context.vault_path), 'sessions', 'insights', 'project')
    assert 'Summary stale; refresh pending' in pending
    assert 'Historical summary:' in pending
    assert 'Old captured result.' in pending
    assert '| Old captured result. |' not in pending


def test_stale_summary_cannot_rank_insights_or_close_older_open_item(context, monkeypatch):
    import obsidian_utils
    import vault_index
    import open_item_dedup
    sessions = context.vault_path / 'sessions'
    sessions.mkdir()
    older = sessions / '2026-10-04-older.md'
    older.write_text('---\ntype: claude-session\nproject: project\ndate: 2026-10-04\n---\n## Open Questions / Next Steps\n- [ ] Deploy feature.\n')
    note = sessions / '2026-10-05-latest.md'
    note.write_text('---\ntype: claude-session\nproject: project\ndate: 2026-10-05\nstatus: auto-logged\n---\n')
    apply_mutations(context, [NoteMutation(note, read_revision(context, note), {'capture': 'Deployed feature.\n'}, 'evidence-seed')])
    with using_runtime_context(context):
        assert upgrade_note_with_summary(str(note), '## Summary\nDeployed feature.\n', str(context.vault_path), 'sessions', 'project').startswith('Upgraded ')
    apply_mutations(context, [NoteMutation(note, read_revision(context, note), {'capture': 'Deployed feature.\nDeployment was rolled back.\n'}, 'evidence-advance')])
    summaries = []
    monkeypatch.setattr(vault_index, 'ensure_index', lambda *args: str(context.index_path))
    def rank(**kwargs):
        summaries.append(kwargs['session_summary'])
        return []
    monkeypatch.setattr(vault_index, 'query_related_notes', rank)
    monkeypatch.setattr(open_item_dedup, 'collect_open_items', lambda *args: [(str(older), 8, 'Deploy feature.')])
    monkeypatch.setattr(obsidian_utils, 'match_items_against_evidence',
                        lambda *args: pytest.fail('stale summary must not become completion evidence'))
    with using_runtime_context(context):
        brief = obsidian_utils.build_context_brief(str(context.vault_path), 'sessions', 'insights', 'project')
    assert summaries == ['']
    assert 'Historical summary:' in brief
    assert '<<<OB_OPEN_ITEM_CANDIDATES>>>\nNO_CANDIDATES' in brief


def test_refresh_restores_summary_freshness_without_losing_capture_or_user_text(context):
    import obsidian_utils
    sessions = context.vault_path / 'sessions'
    sessions.mkdir()
    note = sessions / '2026-10-05-latest.md'
    note.write_text('---\ntype: claude-session\nproject: project\ndate: 2026-10-05\nstatus: auto-logged\n---\nUser text stays.\n')
    apply_mutations(context, [NoteMutation(note, read_revision(context, note), {'capture': 'First fact.\n'}, 'refresh-first')])
    with using_runtime_context(context):
        assert upgrade_note_with_summary(str(note), '## Summary\nFirst summary.\n', str(context.vault_path), 'sessions', 'project').startswith('Upgraded ')
        apply_mutations(context, [NoteMutation(note, read_revision(context, note), {'capture': 'First fact.\nLater fact.\n'}, 'refresh-later')])
        assert json.loads(obsidian_utils.find_unsummarized_notes(str(context.vault_path), 'sessions', 'project'))['unsummarized'] == [str(note)]
        assert upgrade_note_with_summary(str(note), '## Summary\nBoth captured facts.\n', str(context.vault_path), 'sessions', 'project').startswith('Upgraded ')
        assert json.loads(obsidian_utils.find_unsummarized_notes(str(context.vault_path), 'sessions', 'project'))['unsummarized'] == []
        brief = obsidian_utils.build_context_brief(str(context.vault_path), 'sessions', 'insights', 'project')
    assert 'Summary stale' not in brief
    assert 'Both captured facts.' in brief
    assert 'User text stays.' in note.read_text()
    assert 'Later fact.' in note.read_text()
    assert 'First summary.' not in note.read_text()


@pytest.mark.parametrize('summary', ['No trusted heading.', '## Summary\n### Only a heading\n'])
def test_invalid_summary_leaves_capture_and_frontmatter_unchanged(context, summary):
    note = seeded_note(context)
    before = note.read_bytes()
    with using_runtime_context(context):
        result = upgrade_note_with_summary(str(note), summary, str(context.vault_path), 'sessions', 'project')
    assert result.startswith('Failed: malformed summary')
    assert note.read_bytes() == before



def test_native_auth_failure_remains_pending_without_model_escalation(context,monkeypatch):
    import obsidian_utils as utils
    import ai_backend
    note=seeded_note(context)
    original=note.read_text().replace('status: auto-logged\n','status: auto-logged\nsession_id: '+context.native_session_id+'\n')
    note.write_text(original);calls=[]
    def denied(actor,operation,request):
        calls.append((actor,operation,request.model))
        return ai_backend.AIResult('auth_error',None,request.input_revision,'native_auth_error',
                                   actor.host,None,'native_auth_error')
    monkeypatch.setattr(ai_backend,'execute_ai',denied)
    status,elapsed,model,reason=utils.upgrade_unsummarized_note(str(note),str(context.vault_path),'sessions','project')
    assert reason=='native_auth_error' and model is None
    assert 'authentication required' in status and 'returned empty' not in status
    assert len(calls)==1 and calls[0][0] is context
    assert note.read_text()==original


@pytest.mark.parametrize('error_code, expected_calls', [
    ('native_auth_error', 1), ('native_execution_failed', 2),
])
def test_public_recall_batch_skips_only_auth_solo_retry(context, monkeypatch, error_code, expected_calls):
    import io
    import ai_backend
    from skill_procedures import run_operation

    note = seeded_note(context)
    note.write_text(note.read_text().replace('status: auto-logged\n',
                    'status: auto-logged\nsession_id: ' + context.native_session_id + '\n'))
    original = note.read_bytes()
    calls = []

    def failed(actor, operation, request):
        calls.append((actor, operation))
        return ai_backend.AIResult(
            'auth_error' if error_code == 'native_auth_error' else 'error',
            None, request.input_revision, error_code, actor.host, None, error_code,
        )

    monkeypatch.setattr(ai_backend, 'execute_ai', failed)
    stdout, stderr = io.StringIO(), io.StringIO()
    result = run_operation(context, 'recall', 'upgrade-batch',
                           {'paths': [str(note)], 'project': 'project'}, stdout, stderr)
    assert result == 0, stderr.getvalue()
    rows = json.loads(stdout.getvalue())
    assert len(calls) == expected_calls and all(actor is context for actor, _ in calls)
    assert calls[0][1] == 'session_summaries'
    assert note.read_bytes() == original
    assert rows[0]['fallback_reason'] == ('native_auth_error' if error_code == 'native_auth_error'
                                         else 'haiku_subprocess_error')
    if error_code == 'native_auth_error':
        assert 'authentication required' in rows[0]['status']
        assert 'returned empty' not in rows[0]['status']
