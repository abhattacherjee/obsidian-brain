"""Source revisions protect callers that prepare edits before publication."""
import hashlib
import obsidian_utils


def test_summary_rejects_change_after_model_input(sample_unsummarized_note, tmp_vault, monkeypatch):
    note = sample_unsummarized_note
    original = note.read_bytes()
    revision = hashlib.sha256(original).hexdigest()
    edited = original + b'\nA manual edit while the model runs.\n'
    note.write_bytes(edited)
    status = obsidian_utils.upgrade_note_with_summary(
        str(note), '## Summary\nA useful summary.\n', str(tmp_vault),
        'claude-sessions', 'test-project', expected_revision=revision,
    )
    assert status.startswith('Failed:')
    assert note.read_bytes() == edited


def test_status_flip_preserves_crlf_and_mode(tmp_path):
    note = tmp_path / 'note.md'
    note.write_bytes(b'---\r\nstatus: auto-logged\r\n---\r\nBody\r\n')
    note.chmod(0o644)
    assert obsidian_utils.flip_note_status(str(note), 'auto-logged', 'summarized', vault_path=str(tmp_path))
    assert note.read_bytes() == b'---\r\nstatus: summarized\r\n---\r\nBody\r\n'
    assert note.stat().st_mode & 0o777 == 0o644


def test_status_flip_rejects_edit_during_preparation(tmp_path, monkeypatch):
    import note_transactions
    note = tmp_path / 'note.md'
    original = '---\nstatus: auto-logged\n---\nBody\n'
    note.write_text(original)
    register = note_transactions.record_read

    def edit_after_read(context, path, content):
        revision = register(context, path, content)
        note.write_text(original + 'Manual addition\n')
        return revision

    monkeypatch.setattr(note_transactions, 'record_read', edit_after_read)
    assert not obsidian_utils.flip_note_status(
        str(note), 'auto-logged', 'summarized', vault_path=str(tmp_path),
    )
    assert note.read_text() == original + 'Manual addition\n'


def test_preparation_hashes_exact_source_before_ai(sample_unsummarized_note, tmp_vault, monkeypatch):
    note = sample_unsummarized_note
    original = note.read_bytes().replace(b'\n', b'\r\n')
    note.write_bytes(original)
    monkeypatch.setattr(obsidian_utils, 'find_transcript_jsonl', lambda sid: None)
    prep = obsidian_utils._prepare_note_for_summary(
        str(note), str(tmp_vault), 'claude-sessions', 'test-project',
    )
    assert prep['ok']
    assert prep['expected_revision'] == hashlib.sha256(original).hexdigest()


def test_solo_summary_passes_pre_model_revision(sample_unsummarized_note, tmp_vault, monkeypatch):
    note = sample_unsummarized_note
    original = note.read_bytes()
    monkeypatch.setattr(obsidian_utils, 'find_transcript_jsonl', lambda sid: None)

    def model(*args, **kwargs):
        note.write_bytes(original + b'\nManual edit during AI.\n')
        return ('## Summary\nUseful result.\n', None)

    monkeypatch.setattr(obsidian_utils, 'generate_summary', model)
    result = obsidian_utils.upgrade_unsummarized_note(
        str(note), str(tmp_vault), 'claude-sessions', 'test-project',
    )
    assert result[0].startswith('Failed: summary publication conflict')
    assert note.read_bytes() == original + b'\nManual edit during AI.\n'


def test_status_flip_rejects_path_outside_configured_vault(tmp_path, monkeypatch):
    vault = tmp_path / 'vault'
    vault.mkdir()
    note = tmp_path / 'outside.md'
    content = '---\nstatus: auto-logged\n---\nBody\n'
    note.write_text(content)
    monkeypatch.setattr(obsidian_utils, 'load_config', lambda: {'vault_path': str(vault)})
    assert not obsidian_utils.flip_note_status(str(note), 'auto-logged', 'summarized')
    assert note.read_text() == content


def test_orphan_summary_repair_rejects_concurrent_edit(tmp_vault, monkeypatch):
    import json
    import note_transactions
    note = tmp_vault / 'claude-sessions' / 'orphan.md'
    original = '---\nstatus: auto-logged\nproject: test-project\n---\n## Summary\nExisting summary.\n'
    note.write_text(original)
    register = note_transactions.record_read

    def edit_after_read(context, path, content):
        revision = register(context, path, content)
        note.write_text(original + 'Manual addition\n')
        return revision

    monkeypatch.setattr(note_transactions, 'record_read', edit_after_read)
    result = json.loads(obsidian_utils.find_unsummarized_notes(
        str(tmp_vault), 'claude-sessions', 'test-project', include_aged=True,
    ))
    assert result['auto_fixed'] == 0
    assert note.read_text() == original + 'Manual addition\n'
