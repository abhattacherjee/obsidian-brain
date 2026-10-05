"""Resolve host configuration without borrowing another host's state."""
import hashlib
import importlib
import json
from pathlib import Path

import pytest

from test_runtime_context import runtime_case


def context(case, **overrides):
    module = importlib.import_module("runtime_context")
    selected = {"resource_root": case["resource_root"]}
    selected.update(overrides)
    return module.resolve_runtime_context("codex", "codex-cli", {
        "session_id": "current-thread", "cwd": str(case["worktree"]),
    }, selected)


def write_config(path, vault, **values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(vault_path=str(vault), **values)))


def test_config_precedence_is_cli_then_explicit_env_then_native_home(runtime_case, monkeypatch):
    case = runtime_case
    native = case["codex_home"] / "obsidian-brain-config.json"
    write_config(native, case["vault"], selection="native")
    assert context(case).config["selection"] == "native"
    environment = case["home"] / "environment config.json"
    write_config(environment, case["vault"], selection="environment")
    monkeypatch.setenv("OBSIDIAN_BRAIN_CONFIG", str(environment))
    assert context(case).config["selection"] == "environment"
    write_config(case["config_path"], case["vault"], selection="cli")
    chosen = context(case, config_path=case["config_path"])
    assert chosen.config["selection"] == "cli"
    assert chosen.config_path == case["config_path"]


def test_config_relative_paths_do_not_depend_on_unrelated_cwd(runtime_case, monkeypatch, tmp_path):
    case = runtime_case
    relative_vault = case["config_path"].parent / "relative vault"
    relative_vault.mkdir()
    case["config_path"].write_text('{"vault_path":"relative vault"}')
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    chosen = context(case, config_path=case["config_path"])
    assert chosen.vault_path == relative_vault
    assert chosen.resource_root == case["resource_root"]


def test_existing_shared_index_is_kept_without_rewriting_it(runtime_case):
    case = runtime_case
    legacy = case["home"] / ".claude" / "obsidian-brain-vault.db"
    legacy.parent.mkdir()
    legacy.write_bytes(b"existing shared index")
    chosen = context(case, config_path=case["config_path"])
    assert chosen.index_path == legacy
    assert legacy.read_bytes() == b"existing shared index"


def test_new_vault_default_is_shared_and_qualified_by_resolved_vault(runtime_case):
    case = runtime_case
    chosen = context(case, config_path=case["config_path"])
    key = hashlib.sha256(str(case["vault"].resolve()).encode("utf-8")).hexdigest()
    assert chosen.index_path == case["home"] / ".local/share/obsidian-brain/vaults" / key / "index.sqlite3"
    assert not chosen.index_path.exists(), "Resolving a context must not create a DB"


def test_index_precedence_preserves_cli_env_and_config(runtime_case, monkeypatch):
    case = runtime_case
    configured = case["home"] / "configured.sqlite3"
    write_config(case["config_path"], case["vault"], index_path=str(configured))
    assert context(case, config_path=case["config_path"]).index_path == configured
    environment = case["home"] / "environment.sqlite3"
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", str(environment))
    assert context(case, config_path=case["config_path"]).index_path == environment
    explicit = case["home"] / "explicit.sqlite3"
    assert context(case, config_path=case["config_path"], index_path=explicit).index_path == explicit


def test_missing_installation_is_reported_instead_of_using_cwd(runtime_case):
    module = importlib.import_module("runtime_context")
    with pytest.raises(module.RuntimeContextError) as error:
        context(runtime_case, config_path=runtime_case["config_path"], resource_root=runtime_case["home"] / "absent plugin")
    assert error.value.code == "resources_missing"


def test_codex_transcript_cannot_point_into_claude_home(runtime_case):
    module = importlib.import_module("runtime_context")
    case = runtime_case
    transcript = case["home"] / ".claude/projects/project/thread.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"sessionId":"current-thread"}\n')
    with pytest.raises(module.RuntimeContextError) as error:
        module.resolve_runtime_context("codex", "codex-cli", {
            "session_id": "current-thread", "cwd": str(case["worktree"]),
            "transcript_path": str(transcript),
        }, {"config_path": case["config_path"], "resource_root": case["resource_root"]})
    assert error.value.code == "transcript_outside_host"


def test_state_override_does_not_change_host_identity_or_index(runtime_case, monkeypatch):
    case = runtime_case
    state = case["home"] / "selected state"
    monkeypatch.setenv("OBSIDIAN_BRAIN_STATE_DIR", str(state))
    chosen = context(case, config_path=case["config_path"])
    assert chosen.state_path == state
    assert chosen.host == "codex" and chosen.native_session_id == "current-thread"
    assert chosen.index_path != state


def test_context_configuration_cannot_be_reassigned(runtime_case):
    chosen = context(runtime_case, config_path=runtime_case["config_path"])
    with pytest.raises(TypeError):
        chosen.config["vault_path"] = "/other/vault"
