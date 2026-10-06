"""Shared helpers must use selected storage before legacy compatibility paths."""
from pathlib import Path
import time

import capture
import check_items_cli
import note_transactions
import obsidian_session_reaper as reaper
import open_item_dedup
import vault_index
from runtime_adapters import private_workdir


def test_bound_workdirs_and_index_ignore_foreign_environment(selected_host_context, monkeypatch, tmp_path):
    context = selected_host_context
    foreign = tmp_path / 'foreign'
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(foreign))
    monkeypatch.setenv('CODEX_HOME', str(foreign))
    monkeypatch.setenv('OBSIDIAN_BRAIN_DB', str(foreign / 'index'))
    expected = note_transactions.session_state_path(context) / 'jobs'
    assert private_workdir() == expected
    assert check_items_cli._safe_workdir() == expected
    assert open_item_dedup._check_items_workdir() == expected
    assert vault_index._default_db_path() == str(context.index_path)
    assert note_transactions.context_for_vault(context.vault_path) is context
    assert not foreign.exists()


def test_bound_legacy_reaper_uses_registered_recovery_without_scanning_other_host(selected_host_context, monkeypatch):
    context = selected_host_context
    calls = []
    def registered(ctx, max_sources=8, deadline=None):
        assert ctx is context and max_sources == 8 and deadline > time.monotonic()
        calls.append(ctx.host)
        return capture.CaptureResult('pending', pending_sources=1)
    monkeypatch.setattr(reaper, 'reap_registered_sessions', registered)
    monkeypatch.setattr(reaper, '_reap_orphaned_sessions', lambda *args: (_ for _ in ()).throw(AssertionError('legacy scan')))
    result = reaper.reap_orphaned_sessions('synthetic', str(context.vault_path), 'sessions', {})
    assert calls == [context.host]
    assert result.recovery_status == 'pending'
    assert result.reaped == 0 and result.wall_ms >= 0
