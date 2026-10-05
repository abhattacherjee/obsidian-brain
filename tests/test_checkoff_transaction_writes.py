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


def test_batch_edit_preserves_concurrent_manual_edit(tmp_vault, monkeypatch, capsys):
    note = tmp_vault / 'claude-sessions' / 'note.md'
    note.write_text('- [ ] Fix importer\n')
    monkeypatch.setattr(obsidian_utils, 'load_config', lambda: {'vault_path': str(tmp_vault)})
    monkeypatch.setattr(deep_cli, '_save_acted_items', lambda items: None)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps([
        [str(note), '- [ ] Fix importer', '- [x] Fix importer'],
    ])))
    edited = _race(monkeypatch, note)
    deep_cli.run_batch_edit()
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
    note = tmp_vault / 'claude-sessions' / 'note.md'
    note.write_text('- [ ] Fix importer\n')
    edited = _race(monkeypatch, note)
    result = open_item_dedup.cascade_group_members([
        {'members': [{'file': str(note), 'line': 1, 'text': 'Fix importer'}]},
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


def test_batch_edit_preserves_crlf(tmp_vault, monkeypatch, capsys):
    note = tmp_vault / 'claude-sessions' / 'note.md'
    note.write_bytes(b'- [ ] Fix importer\r\nUnrelated text\r\n')
    monkeypatch.setattr(obsidian_utils, 'load_config', lambda: {'vault_path': str(tmp_vault)})
    monkeypatch.setattr(deep_cli, '_save_acted_items', lambda items: None)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps([
        [str(note), '- [ ] Fix importer', '- [x] Fix importer'],
    ])))
    deep_cli.run_batch_edit()
    assert 'Applied 1/1 edits' in capsys.readouterr().out
    assert note.read_bytes() == b'- [x] Fix importer\r\nUnrelated text\r\n'
