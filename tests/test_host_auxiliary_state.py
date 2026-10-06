"""Shared caches and calendar markers cannot escape selected session state."""
from dataclasses import replace
import json
import stat

import pytest

from runtime_context import using_runtime_context
import obsidian_utils
from session_auxiliary_state import cache_get, cache_update, directory, first_seen_date


def test_bound_cache_invalidation_ignores_legacy_paths(selected_host_context, monkeypatch, tmp_path):
    context = selected_host_context
    foreign = tmp_path / "foreign-cache.json"
    foreign.write_text('{"keep":true}')
    monkeypatch.setattr(obsidian_utils, "_CACHE_PREFIX", str(foreign))
    obsidian_utils.cache_set(context.native_session_id, "one", 1)
    obsidian_utils.cache_set(context.native_session_id, "two", 2)
    assert obsidian_utils.cache_get(context.native_session_id, "one") == 1
    obsidian_utils.cache_invalidate(context.native_session_id, "one")
    assert obsidian_utils.cache_get(context.native_session_id, "one") is None
    assert obsidian_utils.cache_get(context.native_session_id, "two") == 2
    obsidian_utils.cache_invalidate(context.native_session_id)
    assert obsidian_utils.cache_get(context.native_session_id, "two") is None
    assert foreign.read_text() == '{"keep":true}'


@pytest.mark.parametrize("dimension", ["host", "vault", "project", "session"])
def test_equal_native_ids_do_not_share_auxiliary_state(selected_host_context, tmp_path, dimension, host_identity_scenario):
    context = selected_host_context
    cache_update(context, "selected", "original")
    if dimension == "host":
        other_host = "codex" if context.host == "claude" else "claude"
        other = replace(context, host=other_host, client="codex-cli" if other_host == "codex" else "claude-code")
    elif dimension == "vault":
        other = replace(context, vault_path=tmp_path / "other-vault")
    elif dimension == "project":
        other = replace(context, canonical_project_root=tmp_path / "other-project")
    else:
        other = replace(context, native_session_id=context.native_session_id + "-other")
    host_identity_scenario.register(context, other)
    assert directory(context, "cache") != directory(other, "cache")
    assert cache_get(other, "selected") is None
    cache_update(other, "selected", "other")
    assert cache_get(context, "selected") == "original"


def test_calendar_and_gate_paths_are_private_and_scoped(selected_host_context):
    context = selected_host_context
    date = first_seen_date(context)
    assert obsidian_utils._first_seen_date("arbitrary/untrusted/id") == date
    assert first_seen_date(context) == date
    gate = obsidian_utils._retro_gate_dir()
    lock = obsidian_utils._lock_path("stop", context.native_session_id)
    assert gate == directory(context, "retro-gate")
    assert str(directory(context, "locks")) in lock
    marker = directory(context, "calendar") / "first-seen.json"
    assert json.loads(marker.read_text()) == {"first_seen_date": date}
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert stat.S_IMODE(marker.parent.stat().st_mode) == 0o700


def test_auxiliary_state_refuses_symlink_or_vault_storage(selected_host_context, tmp_path):
    context = selected_host_context
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked-state"
    link.symlink_to(outside, target_is_directory=True)
    for root in (link, context.vault_path / "state"):
        with pytest.raises(ValueError):
            directory(replace(context, state_path=root), "cache")
    assert list(outside.iterdir()) == []


def test_cache_does_not_follow_a_symlink(selected_host_context, tmp_path):
    context = selected_host_context
    target = tmp_path / "private-user-file.json"
    target.write_text('{"keep":true}')
    target.chmod(0o600)
    (directory(context, "cache") / "values.json").symlink_to(target)
    assert cache_get(context, "keep") is None
    with pytest.raises(OSError):
        cache_update(context, "overwrite", True)
    assert target.read_text() == '{"keep":true}'


def test_session_helpers_use_bound_identity_and_project(selected_host_context, monkeypatch, tmp_path):
    context = selected_host_context
    unrelated = tmp_path / "process-cwd"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "foreign-native-id")
    monkeypatch.setenv("CODEX_THREAD_ID", "foreign-native-id")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(unrelated))
    assert obsidian_utils._resolve_session_id() == context.native_session_id
    assert obsidian_utils._resolve_project_basename_with_source() == (context.canonical_project_root.name, "context")
    assert obsidian_utils._current_session_cwd() == str(context.worktree)
    selected = obsidian_utils.get_session_context()
    assert selected["session_id"] == context.native_session_id
    assert selected["project"] == context.canonical_project_root.name
    assert selected["cwd"] == str(context.worktree)
    assert selected["session_note_name"] == ""
    assert obsidian_utils._default_plugin_install_paths() == [str(context.resource_root)]
    with pytest.raises(ValueError):
        obsidian_utils.get_session_context(vault_path=str(unrelated))
    with pytest.raises(ValueError):
        obsidian_utils.get_session_context(sessions_folder="unrelated-folder")


def test_private_auxiliary_helpers_reject_vault_path(selected_host_context):
    import session_auxiliary_state as state
    import pytest
    directory = selected_host_context.vault_path / "cache"
    directory.mkdir()
    note = directory / "values.json"
    note.write_text("Human bytes")
    with pytest.raises(ValueError):
        state._write(selected_host_context, note, {"lost": "edit"})
    with pytest.raises(ValueError):
        with state._locked(selected_host_context, note):
            pytest.fail("A vault path cannot become a private lock")
    assert note.read_text() == "Human bytes"
