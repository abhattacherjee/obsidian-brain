"""Delayed checkoff and dedup edits cannot overwrite a changed source."""
import io
import json
import deep_cli
import note_transactions
import obsidian_utils
import open_item_dedup


def _race(monkeypatch, note):
    original = note.read_bytes()
    register = note_transactions.record_read

    def edit_after_read(context, path, content):
        revision = register(context, path, content)
        note.write_bytes(original + b'Manual addition\n')
        return revision

    monkeypatch.setattr(note_transactions, 'record_read', edit_after_read)
    return original + b'Manual addition\n'


def test_batch_edit_preserves_concurrent_manual_edit(tmp_vault, selected_host_context, monkeypatch, capsys):
    note = tmp_vault / 'claude-sessions' / 'note.md'
    note.write_text('- [ ] Fix importer\n')
    monkeypatch.setattr(obsidian_utils, 'load_config', lambda: {'vault_path': str(tmp_vault)})
    monkeypatch.setattr(deep_cli, '_save_acted_items', lambda items: None)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps([
        [str(note), '- [ ] Fix importer', '- [x] Fix importer'],
    ])))
    from test_host_deep_edit_revisions import prepared
    identity, revisions = prepared(selected_host_context, [note])
    original = note.read_bytes()
    edited = original + b'Manual addition\n'
    # The race happens after the protected model-input baseline is retained.
    note.write_bytes(edited)
    assert deep_cli.run_batch_edit(expected_revisions=revisions, operation_id=identity) == 1
    assert 'Applied 0/1 edits' in capsys.readouterr().out
    assert note.read_bytes() == edited


def test_dedup_preserves_concurrent_manual_edit(tmp_vault, monkeypatch):
    note = tmp_vault / 'claude-sessions' / 'note.md'
    note.write_text('## Open Questions / Next Steps\n- [ ] Fix importer\n')
    monkeypatch.setattr(open_item_dedup, 'collect_open_items', lambda *a, **k: ['existing'])
    monkeypatch.setattr(open_item_dedup, 'find_duplicates', lambda *a, **k: [('other', 1, 'Fix importer', 'high')])
    edited = _race(monkeypatch, note)
    assert open_item_dedup.dedup_note_open_items(str(tmp_vault), 'claude-sessions', 'project', str(note)) == []
    assert note.read_bytes() == edited


def test_cascade_group_preserves_concurrent_manual_edit(tmp_vault, monkeypatch):
    import hashlib
    note = tmp_vault / 'claude-sessions' / 'note.md'
    note.write_text('- [ ] Fix importer\n')
    original = note.read_bytes()
    pre_ai_revision = hashlib.sha256(original).hexdigest()
    edited = original + b'Manual addition\n'
    register = note_transactions.record_raw_read
    def edit_after_raw_read(context, path, raw):
        assert raw == original
        revision = register(context, path, raw)
        assert revision == pre_ai_revision
        note.write_bytes(edited)
        return revision
    monkeypatch.setattr(note_transactions, 'record_raw_read', edit_after_raw_read)
    result = open_item_dedup.cascade_group_members([
        {'members': [{'file': str(note), 'line': 1, 'text': 'Fix importer',
                      'source_revision': pre_ai_revision}]},
    ], vault_path=str(tmp_vault))
    assert 'WRITE FAILED' in result
    assert note.read_bytes() == edited


def test_batch_cascade_preserves_concurrent_manual_edit(tmp_vault, monkeypatch):
    note = tmp_vault / 'claude-sessions' / 'note.md'
    note.write_text('- [ ] Fix importer\n')
    monkeypatch.setattr(open_item_dedup, 'collect_open_items', lambda *a, **k: ['existing'])
    monkeypatch.setattr(open_item_dedup, 'cascade_checkoff', lambda *a, **k: [(str(note), 1, 'Fix importer', 'high')])
    edited = _race(monkeypatch, note)
    result = open_item_dedup.batch_cascade_checkoff(str(tmp_vault), 'claude-sessions', 'project', ['Fix importer'])
    assert 'WRITE FAILED' in result
    assert note.read_bytes() == edited


def test_batch_edit_preserves_crlf(tmp_vault, selected_host_context, monkeypatch, capsys):
    note = tmp_vault / 'claude-sessions' / 'note.md'
    note.write_bytes(b'- [ ] Fix importer\r\nUnrelated text\r\n')
    monkeypatch.setattr(obsidian_utils, 'load_config', lambda: {'vault_path': str(tmp_vault)})
    monkeypatch.setattr(deep_cli, '_save_acted_items', lambda items: None)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps([
        [str(note), '- [ ] Fix importer', '- [x] Fix importer'],
    ])))
    from test_host_deep_edit_revisions import prepared
    identity, revisions = prepared(selected_host_context, [note])
    assert deep_cli.run_batch_edit(expected_revisions=revisions, operation_id=identity) == 0
    assert 'Applied 1/1 edits' in capsys.readouterr().out
    assert note.read_bytes() == b'- [x] Fix importer\r\nUnrelated text\r\n'


# Every scoped operation uses the same selected temporary vault.
from selected_legacy_vault import selected_host_context, native_ai_frontend  # noqa: F401,E402
import pytest

pytestmark = pytest.mark.usefixtures("selected_host_context")
