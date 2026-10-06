"""Deep edits use protected pre-model revisions, never a fresh write-time baseline."""
import io
import json
from types import MappingProxyType

import pytest

import deep_cli
from operation_state import operation_directory, store_artifact
from note_transactions import record_read
from runtime_context import RuntimeContext, using_runtime_context


@pytest.fixture
def context(selected_host_context):
    return selected_host_context


def prepared(context, notes):
    identity, _ = operation_directory(context)
    revisions = {str(path.resolve()): record_read(context, path, path.read_bytes().decode()) for path in notes}
    manifest = {'host': context.host, 'session_key': context.session_key,
                'vault': str(context.vault_path), 'project': str(context.canonical_project_root),
                'operation_id': identity, 'sources': revisions}
    store_artifact(context, identity, 'source-manifest.json', json.dumps(manifest))
    return identity, revisions


def edit(context, monkeypatch, edits, revisions=None, identity=None):
    monkeypatch.setattr(deep_cli.sys, 'stdin', io.StringIO(json.dumps(edits)))
    with using_runtime_context(context):
        return deep_cli.run_batch_edit(expected_revisions=revisions, operation_id=identity)


def test_native_edit_refuses_missing_pre_model_revision(context, monkeypatch):
    note = context.vault_path / 'note.md'; note.write_text('- [ ] Task.\n')
    with pytest.raises(ValueError, match='revision|operation'):
        edit(context, monkeypatch, [[str(note), '- [ ] Task.', '- [x] Task.']])
    assert note.read_text() == '- [ ] Task.\n'


def test_manual_edit_after_analysis_cannot_be_silently_rebased(context, monkeypatch, capsys):
    note = context.vault_path / 'note.md'; note.write_text('- [ ] Task.\n')
    identity, revisions = prepared(context, [note])
    note.write_text('- [ ] Task.\nUser addition.\n')
    assert edit(context, monkeypatch, [[str(note), '- [ ] Task.', '- [x] Task.']], revisions, identity) == 1
    assert note.read_text() == '- [ ] Task.\nUser addition.\n'
    assert 'Applied 0/1 edits' in capsys.readouterr().out


def test_forged_post_model_revision_is_not_a_prepared_revision(context, monkeypatch):
    note = context.vault_path / 'note.md'; note.write_text('- [ ] Task.\n')
    identity, revisions = prepared(context, [note])
    note.write_text('- [ ] Task.\nUser addition.\n')
    revisions[str(note)] = record_read(context, note, note.read_text())
    with pytest.raises(ValueError, match='prepared'):
        assert edit(context, monkeypatch, [[str(note), '- [ ] Task.', '- [x] Task.']], revisions, identity) == 1
    assert note.read_text() == '- [ ] Task.\nUser addition.\n'


def test_multiple_selected_edits_share_original_baseline(context, monkeypatch, capsys):
    note = context.vault_path / 'note.md'; note.write_text('- [ ] First.\n- [ ] Second.\n')
    identity, revisions = prepared(context, [note])
    assert edit(context, monkeypatch, [[str(note), '- [ ] First.', '- [x] First.'],
                               [str(note), '- [ ] Second.', '- [x] Second.']], revisions, identity) == 0
    assert note.read_text() == '- [x] First.\n- [x] Second.\n'
    assert 'Applied 2/2 edits' in capsys.readouterr().out


@pytest.mark.parametrize('case', ['outside', 'unknown', 'deselected'])
def test_invalid_selection_is_rejected_before_any_publication(context, monkeypatch, case):
    note = context.vault_path / 'selected.md'; note.write_text('- [ ] First.\n')
    other = (context.vault_path.parent if case == 'outside' else context.vault_path) / 'other.md'
    other.write_text('- [ ] Other.\n')
    identity, revisions = prepared(context, [note, other] if case == 'deselected' else [note])
    edits = [[str(note), '- [ ] First.', '- [x] First.']]
    if case != 'deselected':
        edits.append([str(other), '- [ ] Other.', '- [x] Other.'])
    with pytest.raises(ValueError):
        edit(context, monkeypatch, edits, revisions, identity)
    assert note.read_text() == '- [ ] First.\n'
    assert other.read_text() == '- [ ] Other.\n'


def test_user_edit_between_two_selected_changes_is_not_rebased(context, monkeypatch, capsys):
    import note_transactions
    note = context.vault_path / 'note.md'; note.write_text('- [ ] First.\n- [ ] Second.\n')
    identity, revisions = prepared(context, [note])
    original = note_transactions.apply_mutations
    def publish_then_user_edit(*args, **kwargs):
        result = original(*args, **kwargs)
        with note.open('a') as stream:
            stream.write('User change between edits.\n')
        return result
    monkeypatch.setattr(note_transactions, 'apply_mutations', publish_then_user_edit)
    assert edit(context, monkeypatch, [[str(note), '- [ ] First.', '- [x] First.'],
                               [str(note), '- [ ] Second.', '- [x] Second.']], revisions, identity) == 1
    assert note.read_text() == '- [x] First.\n- [ ] Second.\nUser change between edits.\n'
    assert 'Applied 1/2 edits' in capsys.readouterr().out


def test_edit_records_invoking_actor_without_changing_origin(context, monkeypatch):
    note = context.vault_path / 'note.md'
    note.write_text('---\nagent_provider: claude\nagent_session_id: original-session\n---\n- [ ] First.\n')
    identity, revisions = prepared(context, [note])
    edit(context, monkeypatch, [[str(note), '- [ ] First.', '- [x] First.']], revisions, identity)
    text = note.read_text()
    assert 'agent_provider: claude' in text
    assert 'agent_session_id: original-session' in text
    assert 'author_host: ' + json.dumps(context.host) in text
    assert 'operation_id: ' + json.dumps(identity) in text
    assert '- [x] First.' in text


def test_unregistered_or_tampered_source_manifest_cannot_authorize_edits(context, monkeypatch):
    note = context.vault_path / 'note.md'; note.write_text('- [ ] Task.\n')
    identity, revisions = prepared(context, [note])
    _, directory = operation_directory(context, identity)
    manifest = directory / 'source-manifest.json'
    manifest.write_text('{}')
    with pytest.raises(ValueError, match='content changed'):
        edit(context, monkeypatch, [[str(note), '- [ ] Task.', '- [x] Task.']], revisions, identity)
    assert note.read_text() == '- [ ] Task.\n'
