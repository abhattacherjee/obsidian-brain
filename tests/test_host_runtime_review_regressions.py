"""Runtime review controls use only disposable invoking-host contexts."""
import io
import json
from pathlib import Path

import pytest
from runtime_context import RuntimeContextError, resolve_runtime_context, historical_source_roots


def resolve(context, **overrides):
    return resolve_runtime_context(context.host,context.client,
        {'session_id':context.native_session_id,'cwd':str(context.worktree)},
        {'config_path':context.config_path,'resource_root':context.resource_root,**overrides})


def test_wrong_host_only_descriptor_is_rejected(selected_host_context):
    context=selected_host_context
    own='.claude-plugin' if context.host=='claude' else '.codex-plugin'
    (context.resource_root/own/'plugin.json').unlink()
    with pytest.raises(RuntimeContextError) as error:
        resolve(context)
    assert error.value.code == 'resources_missing'


def test_cli_does_not_accept_resource_flag_abbreviations(selected_host_context):
    import brain_cli
    context=selected_host_context
    with pytest.raises(SystemExit) as error:
        brain_cli.main(['--host',context.host,'--client',context.client,'--res',str(context.resource_root),'context'],
                       stdin=io.StringIO('{}'),stdout=io.StringIO(),stderr=io.StringIO())
    assert error.value.code == 2


def test_configured_native_home_symlink_is_trusted_but_child_symlink_is_rejected(selected_host_context, tmp_path, monkeypatch):
    context=selected_host_context
    native=tmp_path/'dotfiles'/context.host
    native.mkdir(parents=True)
    home_link=tmp_path/'configured-home'
    home_link.symlink_to(native,target_is_directory=True)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR' if context.host=='claude' else 'CODEX_HOME',str(home_link))
    roots=historical_source_roots(context.host)
    expected='projects' if context.host=='claude' else 'sessions'
    assert roots[0] == native/expected
    outside=tmp_path/'outside';outside.mkdir()
    (native/expected).symlink_to(outside,target_is_directory=True)
    with pytest.raises(RuntimeContextError):
        historical_source_roots(context.host)


def test_user_home_identity_is_frozen_across_environment_changes(selected_host_context, tmp_path, monkeypatch):
    from note_transactions import coordination_location
    context=selected_host_context
    before=coordination_location(context)
    monkeypatch.setenv('HOME',str(tmp_path/'foreign-home'))
    assert coordination_location(context) == before


def test_removed_worktree_uses_only_matching_registered_identity(selected_host_context, monkeypatch):
    import capture
    import transcripts
    import time
    context=selected_host_context
    batch=transcripts.TranscriptBatch('ok',(transcripts.SourceRecord('m','user','Retained fact',1),),
        'source',100,metadata={'native_session_id':context.native_session_id},source_identity='source',source_complete=True)
    monkeypatch.setattr(transcripts,'read_records',lambda *a: batch)
    assert capture.capture_checkpoint(context,capture.CaptureEvent('stop',min_messages=1),time.monotonic()+1).status == 'complete'
    context.worktree.rmdir()
    restored=resolve(context)
    assert restored.worktree == context.worktree
    assert restored.canonical_project_root == context.canonical_project_root
    assert restored.client == context.client
    assert capture.capture_checkpoint(restored,capture.CaptureEvent('session_end',min_messages=1),time.monotonic()+1).status == 'complete'
    with pytest.raises(RuntimeContextError) as error:
        resolve(context,cwd=context.worktree.parent/'unregistered-missing-path')
    assert error.value.code == 'project_missing'
