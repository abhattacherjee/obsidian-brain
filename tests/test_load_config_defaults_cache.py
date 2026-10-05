"""load_config's session cache must not outlive the _DEFAULTS that wrote it (#409).

Repro: a session cached its config with pre-3.8.0 code, whose _DEFAULTS had no
`wiki_folder`. After the plugin updated mid-session, load_config() kept
returning that dict, so /vault-ask's `c.get("wiki_folder")` read None and the
wiki was silently turned off for the rest of the session.
"""
import json

import pytest

import obsidian_utils

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "obsidian-brain-config.json"
    path.write_text(json.dumps({"vault_path": "/vault"}))
    path.chmod(0o600)
    monkeypatch.setattr(obsidian_utils, "_CONFIG_PATH", str(path))
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda *a, **k: SID)
    return path


def test_cache_written_by_older_code_is_not_trusted(cfg):
    # The literal repro: old code cached "config" with no signature key, and
    # its dict lacked the wiki_folder default added in 3.8.0.
    stale = {k: v for k, v in obsidian_utils._DEFAULTS.items() if k != "wiki_folder"}
    stale["vault_path"] = "/vault"
    obsidian_utils.cache_set(SID, "config", stale)
    assert obsidian_utils.load_config().get("wiki_folder") == "claude-wiki"


def test_defaults_change_mid_session_is_picked_up(cfg, monkeypatch):
    assert "new_default_409" not in obsidian_utils.load_config()
    monkeypatch.setitem(obsidian_utils._DEFAULTS, "new_default_409", "x")
    assert obsidian_utils.load_config()["new_default_409"] == "x"


def test_cache_still_used_while_defaults_are_unchanged(cfg):
    assert obsidian_utils.load_config()["vault_path"] == "/vault"
    cfg.write_text(json.dumps({"vault_path": "/elsewhere"}))
    # Same session, same defaults: the cached view wins, as before.
    assert obsidian_utils.load_config()["vault_path"] == "/vault"
    assert obsidian_utils.load_config(fresh=True)["vault_path"] == "/elsewhere"


def test_invalidating_config_key_still_forces_a_reread(cfg):
    obsidian_utils.load_config()
    cfg.write_text(json.dumps({"vault_path": "/elsewhere"}))
    obsidian_utils.cache_invalidate(SID, "config")
    assert obsidian_utils.load_config()["vault_path"] == "/elsewhere"


def test_signature_is_not_a_config_key(cfg):
    # The signature lives beside the config in the cache, never inside it,
    # so /vault-config never shows it as a setting.
    assert "config_defaults_sig" not in obsidian_utils.load_config()
    assert "config_defaults_sig" not in obsidian_utils.load_config()
