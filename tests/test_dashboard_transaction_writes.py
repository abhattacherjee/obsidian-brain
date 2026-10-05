"""Dashboard regeneration refuses a source changed during rendering."""
import pytest
import check_items_report
import obsidian_utils


def test_dashboard_preserves_manual_edit_during_render(tmp_path, monkeypatch):
    vault = tmp_path / 'vault'
    folder = vault / 'claude-check-items'
    folder.mkdir(parents=True)
    note = folder / 'check-items-project-2026-10-05.md'
    note.write_text('Reviewed dashboard\n')
    monkeypatch.setattr(obsidian_utils, 'load_config', lambda: {})
    body = check_items_report._body

    def edited_body(*args, **kwargs):
        note.write_text('Manual edit while report renders\n')
        return body(*args, **kwargs)

    monkeypatch.setattr(check_items_report, '_body', edited_body)
    with pytest.raises(OSError, match='conflict'):
        check_items_report.write_check_items_dashboard(
            vault_path=str(vault), scope_name='project', date_str='2026-10-05',
            window_days=14, raw_count=0, group_count=0, classifications=[],
            applied=0, cascaded=0, merges=[], semantic_merge_mode='ok',
            classifier_mode='ok', dry_run=True,
        )
    assert note.read_text() == 'Manual edit while report renders\n'
