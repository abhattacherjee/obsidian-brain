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
    from obsidian_utils import parse_frontmatter_field
    assert parse_frontmatter_field(text.split('---')[1], 'author_host') == context.host
    assert parse_frontmatter_field(text.split('---')[1], 'operation_id') == identity
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


@pytest.mark.parametrize('unsafe', ['public_mode', 'hardlink'])
def test_unsafe_acted_cache_does_not_hide_published_edit(context, monkeypatch, capsys, unsafe):
    from pathlib import Path
    note = context.vault_path / 'cache-failure.md'
    note.write_text('- [ ] Task.\n')
    identity, revisions = prepared(context, [note])
    with using_runtime_context(context):
        cache = Path(deep_cli._acted_items_path())
        cache.write_text('[]')
        cache.chmod(0o600)
        if unsafe == 'public_mode':
            cache.chmod(0o644)
        else:
            import os
            os.link(cache, cache.with_name('second-link.json'))
        assert deep_cli._load_acted_items() == set()
        assert edit(context, monkeypatch, [[str(note), '- [ ] Task.', '- [x] Task.']], revisions, identity) == 0
    assert note.read_text() == '- [x] Task.\n'
    output = capsys.readouterr()
    assert 'Applied 1/1 edits' in output.out
    assert 'warning: could not' in output.err
    assert cache.read_text() == '[]'


@pytest.mark.parametrize('value', [[1, ['private-cache-secret']], [1, 'private-cache-secret'],
                                  {'private-cache-secret': 1}, None, 'private-cache-secret'])
def test_wrong_shape_acted_cache_does_not_hide_published_edit(context, monkeypatch, capsys, value):
    from pathlib import Path
    note = context.vault_path / 'cache-shape.md'
    note.write_text('- [ ] Task.\n')
    identity, revisions = prepared(context, [note])
    with using_runtime_context(context):
        cache = Path(deep_cli._acted_items_path())
        cache.write_text(json.dumps(value))
        cache.chmod(0o600)
        assert deep_cli._load_acted_items() == set()
        assert edit(context, monkeypatch, [[str(note), '- [ ] Task.', '- [x] Task.']], revisions, identity) == 0
        retained = json.loads(cache.read_text())
        assert isinstance(retained, list) and all(isinstance(item, str) for item in retained)
        assert 'private-cache-secret' not in retained
    assert note.read_text() == '- [x] Task.\n'
    output = capsys.readouterr()
    assert 'Applied 1/1 edits' in output.out
    assert 'warning: could not load acted items' in output.err
    assert 'private-cache-secret' not in output.out + output.err


def test_invalid_new_acted_items_warn_without_changing_private_cache(context, capsys):
    from pathlib import Path
    with using_runtime_context(context):
        cache = Path(deep_cli._acted_items_path())
        cache.write_text('["Existing task"]')
        cache.chmod(0o600)
        before = cache.read_bytes()
        deep_cli._save_acted_items({1, 'private-cache-secret'})
        assert cache.read_bytes() == before
    output = capsys.readouterr()
    assert 'warning: could not save acted items' in output.err
    assert 'private-cache-secret' not in output.err


@pytest.mark.parametrize('form', ['basename', 'vault_relative', 'absolute'])
def test_deep_checkoffs_resolves_selected_vault_paths_without_writing(context, monkeypatch, capsys, form):
    note = context.vault_path / 'claude-sessions' / 'path-target.md'
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text('- [ ] Selected task.\n')
    requested = {'basename': note.name, 'vault_relative': 'claude-sessions/' + note.name,
                 'absolute': str(note)}[form]
    monkeypatch.chdir(context.worktree)
    monkeypatch.setattr(deep_cli.sys, 'stdin', io.StringIO(json.dumps([
        {'file': requested, 'line': 99, 'text': 'Selected task.'}])))
    with using_runtime_context(context):
        deep_cli.run_build_checkoffs()
    result = json.loads(capsys.readouterr().out)
    assert result == {'edits': [[str(note), '- [ ] Selected task.', '- [x] Selected task.']], 'skipped': []}
    assert note.read_text() == '- [ ] Selected task.\n'


@pytest.mark.parametrize('form', ['outside_absolute', 'escaping_relative', 'internal_traversal', 'symlink_escape'])
def test_deep_checkoffs_refuses_unapproved_paths(context, tmp_path, monkeypatch, capsys, form):
    outside = tmp_path / 'outside.md'
    outside.write_text('- [ ] Private outside task.\n')
    folder = context.vault_path / 'claude-sessions'
    folder.mkdir(parents=True, exist_ok=True)
    inside = folder / 'inside.md'
    inside.write_text('- [ ] Private outside task.\n')
    symlink = folder / 'linked.md'
    symlink.symlink_to(outside)
    requested = {'outside_absolute': str(outside), 'escaping_relative': '../outside.md',
                 'internal_traversal': 'claude-sessions/../claude-sessions/inside.md',
                 'symlink_escape': 'claude-sessions/linked.md'}[form]
    monkeypatch.setattr(deep_cli.sys, 'stdin', io.StringIO(json.dumps([
        {'file': requested, 'line': 1, 'text': 'Private outside task.'}])))
    with using_runtime_context(context):
        deep_cli.run_build_checkoffs()
    result = json.loads(capsys.readouterr().out)
    assert result['edits'] == []
    assert result['skipped'][0]['reason'] == 'containment'
    assert outside.read_text() == inside.read_text() == '- [ ] Private outside task.\n'
