"""Current native identity must survive inherited host markers and worktrees."""
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))


@pytest.fixture
def runtime_case(tmp_path, monkeypatch):
    home = tmp_path / "home with spaces"
    codex_home = home / "custom codex"
    worktree = tmp_path / "project worktree"
    vault = tmp_path / "shared vault"
    resources = tmp_path / "installed plugin"
    for path in (home, codex_home, worktree, vault, resources / "hooks", resources / ".codex-plugin", resources / ".claude-plugin"):
        path.mkdir(parents=True, exist_ok=True)
    (resources / ".codex-plugin" / "plugin.json").write_text('{"name":"test-brain"}')
    (resources / ".claude-plugin" / "plugin.json").write_text('{"name":"test-brain"}')
    config = tmp_path / "selected config.json"
    config.write_text(json.dumps({"vault_path": str(vault)}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("OBSIDIAN_BRAIN_CONFIG", raising=False)
    monkeypatch.delenv("OBSIDIAN_BRAIN_DB", raising=False)
    monkeypatch.delenv("OBSIDIAN_BRAIN_STATE_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    return {
        "home": home, "codex_home": codex_home, "worktree": worktree,
        "vault": vault, "resource_root": resources, "config_path": config,
    }


def resolve(case, host="codex", sid="native-thread", client="codex-cli", **payload):
    module = importlib.import_module("runtime_context")
    native = {"session_id": sid, "cwd": str(case["worktree"])}
    native.update(payload)
    return module.resolve_runtime_context(host, client, native, {
        "config_path": case["config_path"], "resource_root": case["resource_root"],
    })


def test_codex_retro_does_not_link_a_concurrent_claude_session(runtime_case, monkeypatch):
    case = runtime_case
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "concurrent-claude-session")
    recent = case["home"] / ".claude" / "projects" / "some-other-project"
    recent.mkdir(parents=True)
    (recent / "concurrent-claude-session.jsonl").write_text('{"sessionId":"concurrent-claude-session"}\n')
    context = resolve(case)
    assert context.host == "codex"
    assert context.native_session_id == "native-thread"
    assert context.transcript_path is None
    assert context.state_path == case["codex_home"] / "obsidian-brain" / "state"


def test_explicit_claude_host_wins_over_inherited_codex_thread(runtime_case, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-codex-thread")
    context = resolve(runtime_case, host="claude", client="claude-code", sid="nested-claude-session")
    assert context.host == "claude"
    assert context.native_session_id == "nested-claude-session"
    assert context.state_path == runtime_case["home"] / ".claude" / "obsidian-brain"


def test_equal_ids_have_separate_host_cache_keys(runtime_case):
    codex = resolve(runtime_case, sid="same/opaque id")
    claude = resolve(runtime_case, host="claude", client="claude-code", sid="same/opaque id")
    assert codex.session_key != claude.session_key
    assert "/" not in codex.session_key


def test_resume_keeps_identity_and_fork_changes_it(runtime_case):
    first = resolve(runtime_case, sid="parent", source="startup")
    resumed = resolve(runtime_case, sid="parent", source="resume")
    fork = resolve(runtime_case, sid="child", source="fork")
    assert first.session_key == resumed.session_key
    assert first.session_key != fork.session_key


def test_worktree_move_keeps_canonical_project_root(runtime_case):
    case = runtime_case
    main = case["worktree"].parent / "main repository"
    gitdir = main / ".git" / "worktrees" / "linked"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    (case["worktree"] / ".git").write_text("gitdir: " + str(gitdir) + "\n")
    first = resolve(case)
    moved = case["worktree"].with_name("moved worktree")
    case["worktree"].rename(moved)
    case["worktree"] = moved
    second = resolve(case)
    assert first.canonical_project_root == second.canonical_project_root == main
    assert second.worktree == moved


def test_independent_gitdir_keeps_checkout_as_project_root(runtime_case):
    case = runtime_case
    gitdir = case["home"] / "separate Git metadata"
    gitdir.mkdir()
    (case["worktree"] / ".git").write_text("gitdir: " + str(gitdir) + "\n")
    assert resolve(case).canonical_project_root == case["worktree"]


def test_deleted_worktree_never_falls_back_to_unrelated_cwd(runtime_case, monkeypatch, tmp_path):
    case = runtime_case
    deleted = case["worktree"]
    deleted.rmdir()
    monkeypatch.chdir(tmp_path)
    module = importlib.import_module("runtime_context")
    with pytest.raises(module.RuntimeContextError) as error:
        resolve(case)
    assert error.value.code == "project_missing"


def test_missing_native_id_does_not_use_other_host_marker(runtime_case, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "wrong-host")
    module = importlib.import_module("runtime_context")
    with pytest.raises(module.RuntimeContextError) as error:
        resolve(runtime_case, sid="")
    assert error.value.code == "session_missing"


def test_tool_shell_uses_only_the_selected_hosts_identity(runtime_case, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "current-tool-thread")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "inherited-claude")
    assert resolve(runtime_case, sid=None).native_session_id == "current-tool-thread"


def test_host_and_client_mismatch_is_rejected(runtime_case):
    module = importlib.import_module("runtime_context")
    with pytest.raises(module.RuntimeContextError) as error:
        resolve(runtime_case, host="claude", client="codex-cli")
    assert error.value.code == "client_mismatch"


def test_native_payload_rejects_invalid_path_types(runtime_case):
    module = importlib.import_module("runtime_context")
    with pytest.raises(module.RuntimeContextError) as error:
        resolve(runtime_case, cwd=["not", "a", "path"])
    assert error.value.code == "path_invalid"


def test_empty_native_cwd_does_not_select_process_cwd(runtime_case, monkeypatch):
    monkeypatch.chdir(runtime_case["worktree"])
    module = importlib.import_module("runtime_context")
    with pytest.raises(module.RuntimeContextError) as error:
        resolve(runtime_case, cwd="")
    assert error.value.code == "path_invalid"


def test_legacy_utilities_use_bound_context_without_identity_scanning(runtime_case, monkeypatch):
    module = importlib.import_module("runtime_context")
    utils = importlib.import_module("obsidian_utils")
    index = importlib.import_module("vault_index")
    context = resolve(runtime_case)

    def forbidden_scan(*args, **kwargs):
        raise AssertionError("Bound contexts must not scan for another session")

    monkeypatch.setattr(utils, "_get_session_id_fast", forbidden_scan)
    with module.using_runtime_context(context):
        assert utils.load_config()["vault_path"] == str(context.vault_path)
        assert index._default_db_path() == str(context.index_path)
        utils.cache_set(context.native_session_id, "selected", context.host)
        assert utils.cache_get(context.native_session_id, "selected") == "codex"
    assert module.current_runtime_context() is None
    assert (context.state_path / ("cache-" + context.session_key + ".json")).exists()
    assert not (runtime_case["home"] / ".claude").exists()


def test_equal_host_ids_do_not_share_legacy_cache(runtime_case):
    utils = importlib.import_module("obsidian_utils")
    codex = resolve(runtime_case, sid="equal")
    claude = resolve(runtime_case, host="claude", client="claude-code", sid="equal")
    utils.cache_set("equal", "owner", "codex", context=codex)
    utils.cache_set("equal", "owner", "claude", context=claude)
    assert utils.cache_get("equal", "owner", context=codex) == "codex"
    assert utils.cache_get("equal", "owner", context=claude) == "claude"


def test_dedup_claims_do_not_use_claude_state_for_codex(runtime_case):
    utils = importlib.import_module("obsidian_utils")
    runtime = importlib.import_module("runtime_context")
    context = resolve(runtime_case)
    with runtime.using_runtime_context(context):
        assert utils.claim_hook_run("SessionStart", context.native_session_id)
        assert not utils.claim_hook_run("SessionStart", context.native_session_id)
        utils.release_hook_run("SessionStart", context.native_session_id)
        assert utils.claim_hook_run("SessionStart", context.native_session_id)
    assert not (runtime_case["home"] / ".claude").exists()


def test_retro_gate_keys_include_host_when_state_is_shared(runtime_case, monkeypatch):
    utils = importlib.import_module("obsidian_utils")
    runtime = importlib.import_module("runtime_context")
    monkeypatch.setenv("OBSIDIAN_BRAIN_STATE_DIR", str(runtime_case["home"] / "selected shared state"))
    codex = resolve(runtime_case, sid="equal")
    claude = resolve(runtime_case, host="claude", client="claude-code", sid="equal")
    with runtime.using_runtime_context(codex):
        codex_path = utils.mark_retro_classification_pending("equal", "codex-retro.md")
    with runtime.using_runtime_context(claude):
        assert utils.get_retro_classification_pending("equal") is None
        claude_path = utils.mark_retro_classification_pending("equal", "claude-retro.md")
        assert utils.get_retro_classification_pending("equal")["retro_path"] == "claude-retro.md"
    with runtime.using_runtime_context(codex):
        assert utils.get_retro_classification_pending("equal")["retro_path"] == "codex-retro.md"
    assert codex_path != claude_path


def test_unwritable_cache_does_not_break_note_reads(runtime_case, monkeypatch, capsys):
    utils = importlib.import_module("obsidian_utils")
    context = resolve(runtime_case)

    def denied(*args, **kwargs):
        raise PermissionError("synthetic cache permission failure")

    monkeypatch.setattr(utils.tempfile, "mkstemp", denied)
    utils.cache_set(context.native_session_id, "metadata", {"title": "readable note"}, context=context)
    assert "cache write failed" in capsys.readouterr().err
