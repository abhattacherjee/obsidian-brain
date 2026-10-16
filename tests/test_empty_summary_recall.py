"""An empty summary scaffold must leave its deferred AI request eligible."""
import json

import pytest
import ai_backend
import obsidian_utils
from note_transactions import NoteMutation, apply_mutations, read_revision


@pytest.mark.parametrize('body', [
    '## Summary\n',
    '## Summary\n \t\n## Key Decisions\nOther section.\n',
    '## Summary\n<!-- placeholder -->\n## Conversation (raw)\nUser: Fact.\n',
    '## Summary details\nThis is not the summary section.\n',
    '## Summary\nAI summary unavailable — raw extraction below.\n',
    '## Summary\nOld legacy summary.\n'
    '<!-- obsidian-brain:summary:start -->\n## Summary\n\n'
    '<!-- obsidian-brain:summary:end -->\n',
])
def test_empty_or_placeholder_summary_remains_queued_unchanged(selected_host_context, body):
    context = selected_host_context
    folder = context.vault_path / 'sessions'; folder.mkdir()
    note = folder / 'legacy.md'
    note.write_text('---\ntype: claude-session\nproject: project\n'
                    'session_id: seeded\nstatus: auto-logged\n---\n' + body)
    before = note.read_bytes()
    result = json.loads(obsidian_utils.find_unsummarized_notes(
        str(context.vault_path), 'sessions', 'project', include_aged=True))
    assert result['unsummarized'] == [str(note)]
    assert result['auto_fixed'] == 0
    assert note.read_bytes() == before


def test_real_legacy_summary_can_fix_stale_status(selected_host_context):
    context = selected_host_context
    folder = context.vault_path / 'sessions'; folder.mkdir()
    note = folder / 'legacy.md'
    before = ('---\ntype: claude-session\nproject: project\n'
              'status: auto-logged\n---\n## Summary\nReal legacy fact.\n'
              '## Conversation (raw)\nAI summary unavailable was discussed.\n')
    note.write_text(before)
    result = json.loads(obsidian_utils.find_unsummarized_notes(
        str(context.vault_path), 'sessions', 'project', include_aged=True))
    assert result['unsummarized'] == []
    assert result['auto_fixed'] == 1
    assert note.read_text() == before.replace('status: auto-logged', 'status: summarized')


def test_empty_scaffold_triggers_bound_ai_only_after_success(selected_host_context, monkeypatch):
    context = selected_host_context
    folder = context.vault_path / 'sessions'; folder.mkdir()
    note = folder / 'native.md'
    note.write_text('---\ntype: claude-session\nproject: project\n'
                    'session_id: seeded\nstatus: auto-logged\n---\n'
                    '# Private fixture\n## Summary\n\n## Key Decisions\n')
    apply_mutations(context, [NoteMutation(note, read_revision(context, note),
                    {'capture': 'User: Preserve the seeded fact.\nAssistant: Fact retained.\n'},
                    'fixture-capture')])
    before = note.read_bytes()
    seen = []
    def execute(bound, operation, request):
        assert bound is context
        assert operation == 'session_summary'
        assert 'Preserve the seeded fact.' in request.input
        seen.append(request.input_revision)
        if len(seen) == 1:
            return ai_backend.AIResult('cancelled')
        return ai_backend.AIResult('ok', '## Summary\nSeeded fact preserved.\n',
                                  request.input_revision, backend=context.host, model='fixture-model')
    monkeypatch.setattr(ai_backend, 'execute_ai', execute)
    monkeypatch.setattr(obsidian_utils, 'find_transcript_jsonl',
                        lambda *args: pytest.fail('retained facts must not use foreign transcripts'))
    queue = json.loads(obsidian_utils.find_unsummarized_notes(
        str(context.vault_path), 'sessions', 'project', include_aged=True))
    assert queue['unsummarized'] == [str(note)]
    assert note.read_bytes() == before
    result = obsidian_utils.upgrade_unsummarized_note(str(note), str(context.vault_path), 'sessions', 'project')
    assert result[0].startswith('Failed:')
    assert note.read_bytes() == before
    result = obsidian_utils.upgrade_unsummarized_note(str(note), str(context.vault_path), 'sessions', 'project')
    assert result[0].startswith('Upgraded '), result
    after = note.read_text()
    assert 'Preserve the seeded fact.' in after
    assert 'Seeded fact preserved.' in after
    assert obsidian_utils.parse_frontmatter_field(after, 'summary_revision') == seen[-1]
    assert obsidian_utils.parse_frontmatter_field(after, 'status') == 'summarized'
    assert seen[0] == seen[1]


def _decision_fixture(context, name, date='2026-10-08', project='project',
                      status='active', note_type='claude-decision'):
    folder = context.vault_path / 'insights'
    folder.mkdir(exist_ok=True)
    path = folder / (name + '.md')
    path.write_text(f'---\ntype: {note_type}\nproject: {project}\nstatus: {status}\n'
                    f'date: {date}\ntitle: {name}\nsource_session: unrelated-seed\n---\n'
                    f'# {name}\n{name} explains the selected storage choice.\n')
    return path


def test_recall_includes_unlinked_active_project_decision(selected_host_context):
    context = selected_host_context
    _decision_fixture(context, 'SQLite decision')
    brief = obsidian_utils.build_context_brief(str(context.vault_path), 'sessions', 'insights', 'project')
    assert 'SQLite decision' in brief
    import vault_index
    assert vault_index.query_related_notes(db_path=str(context.index_path), project='project',
        session_ids=[], session_tags=[], session_summary='', limit=20) == []


def test_recall_project_decision_fallback_excludes_other_status_types_projects(selected_host_context):
    context = selected_host_context
    _decision_fixture(context, 'Chosen active decision')
    for name, options in [
        ('Archived decision', {'status': 'archived'}),
        ('Superseded decision', {'status': 'superseded'}),
        ('Foreign decision', {'project': 'project-collision'}),
        ('Unlinked insight', {'note_type': 'claude-insight'}),
    ]:
        _decision_fixture(context, name, **options)
    brief = obsidian_utils.build_context_brief(str(context.vault_path), 'sessions', 'insights', 'project')
    assert 'Chosen active decision' in brief
    for name in ['Archived decision', 'Superseded decision', 'Foreign decision', 'Unlinked insight']:
        assert name not in brief


def test_recall_project_decisions_latest_three_preserve_ranked_priority(selected_host_context, monkeypatch):
    context = selected_host_context
    ranked = _decision_fixture(context, 'Contextual hit', date='2026-01-01')
    for day in range(1, 5):
        _decision_fixture(context, f'Fallback {day}', date=f'2026-10-0{day}')
    import vault_index
    monkeypatch.setattr(vault_index, 'query_related_notes',
                        lambda **kwargs: [{'path': str(ranked), 'title': 'Contextual hit'}])
    brief = obsidian_utils.build_context_brief(str(context.vault_path), 'sessions', 'insights', 'project')
    assert 'Fallback 1' not in brief
    assert brief.index('Contextual hit') < brief.index('Fallback 4') < brief.index('Fallback 3') < brief.index('Fallback 2')
    assert 'insight_count: 4' in brief


def test_recall_project_decisions_keep_twenty_total_budget(selected_host_context, monkeypatch):
    context = selected_host_context
    hit = _decision_fixture(context, 'Ranked hit')
    for day in range(1, 5):
        _decision_fixture(context, f'Fallback {day}', date=f'2026-10-0{day}')
    import vault_index
    monkeypatch.setattr(vault_index, 'query_related_notes', lambda **kwargs:
                        [{'path': str(hit), 'title': f'Ranked {i}'} for i in range(19)])
    brief = obsidian_utils.build_context_brief(str(context.vault_path), 'sessions', 'insights', 'project')
    assert 'Fallback 4' in brief
    assert all(f'Fallback {i}' not in brief for i in range(1, 4))


def test_recall_project_decisions_reject_indexed_symlink_escape(selected_host_context):
    context = selected_host_context
    path = _decision_fixture(context, 'Escaping decision')
    import vault_index
    vault_index.ensure_index(str(context.vault_path), ['insights'], db_path=str(context.index_path))
    outside = context.worktree / 'outside-decision.md'
    outside.write_text('Private external sentinel')
    path.unlink()
    path.symlink_to(outside)
    assert obsidian_utils._recall_project_decisions(str(context.index_path),
        context.vault_path, context.vault_path / 'insights', 'project', []) == []
