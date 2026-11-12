"""load_config's session cache must not outlive the _DEFAULTS that wrote it (#409).

Repro: a session cached its config with pre-3.8.0 code, whose _DEFAULTS had no
`wiki_folder`. After the plugin updated mid-session, load_config() kept
returning that dict, so /vault-ask's `c.get("wiki_folder")` read None and the
wiki was silently turned off for the rest of the session.
"""
import builtins
import json
import os

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


def test_rewritten_config_is_read_in_the_same_session(cfg):
    assert obsidian_utils.load_config()["vault_path"] == "/vault"
    cfg.write_text(json.dumps({"vault_path": "/elsewhere"}))
    assert obsidian_utils.load_config()["vault_path"] == "/elsewhere"
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


def test_old_code_rewriting_config_under_a_new_signature_is_not_trusted(cfg):
    # Review finding on PR #411. Old hooks keep running the pre-update install
    # for the whole session; an old load_config re-caches "config" without
    # touching "config_defaults_sig", so the signature alone would bless it.
    assert obsidian_utils.load_config()["wiki_folder"] == "claude-wiki"  # 1
    obsidian_utils.cache_invalidate(SID, "config")                       # 2
    old = {k: v for k, v in obsidian_utils._DEFAULTS.items() if k != "wiki_folder"}
    old["vault_path"] = "/vault"
    obsidian_utils.cache_set(SID, "config", old)                         # 3
    assert obsidian_utils.load_config().get("wiki_folder") == "claude-wiki"  # 4


def test_unchanged_file_serves_the_cache(cfg, monkeypatch):
    assert obsidian_utils.load_config()["vault_path"] == "/vault"
    reads = []
    real_load = obsidian_utils.json.load

    def count_load(fh, *args, **kwargs):
        if fh.name == str(cfg):
            reads.append(fh.name)
        return real_load(fh, *args, **kwargs)

    monkeypatch.setattr(obsidian_utils.json, "load", count_load)
    assert obsidian_utils.load_config()["vault_path"] == "/vault"
    assert reads == []


def test_atomic_rename_is_seen(cfg):
    assert obsidian_utils.load_config()["vault_path"] == "/vault"
    replacement = cfg.with_name("replacement.json")
    replacement.write_text(json.dumps({"vault_path": "/vaulX"}))
    assert replacement.stat().st_size == cfg.stat().st_size
    # Preserve mtime too, so the inode is what distinguishes these files.
    original_stat = cfg.stat()
    os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    os.rename(replacement, cfg)
    assert obsidian_utils.load_config()["vault_path"] == "/vaulX"


def test_missing_file_cached_then_created_file_is_read(tmp_path, monkeypatch):
    path = tmp_path / "missing-config.json"
    monkeypatch.setattr(obsidian_utils, "_CONFIG_PATH", str(path))
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda *a, **k: SID)
    assert obsidian_utils.load_config()["vault_path"] == obsidian_utils._DEFAULTS["vault_path"]
    opens = []
    real_open = builtins.open

    def count_open(file, *args, **kwargs):
        if str(file) == str(path):
            opens.append(file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", count_open)
    assert obsidian_utils.load_config()["vault_path"] == obsidian_utils._DEFAULTS["vault_path"]
    assert opens == []
    path.write_text(json.dumps({"vault_path": "/created"}))
    assert obsidian_utils.load_config()["vault_path"] == "/created"


def test_cache_from_older_code_without_file_sig_is_not_trusted(tmp_path, monkeypatch):
    monkeypatch.setattr(obsidian_utils, "_CONFIG_PATH", str(tmp_path / "missing-config.json"))
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda *a, **k: SID)
    obsidian_utils.cache_set(SID, "config_defaults_sig", obsidian_utils._defaults_signature())
    obsidian_utils.cache_set(SID, "config", dict(obsidian_utils._DEFAULTS, vault_path="/stale"))
    assert obsidian_utils.load_config()["vault_path"] != "/stale"


def test_sig_taken_before_read(cfg, monkeypatch):
    # A write lands just after load_config parsed the old file. The cache then
    # holds the old dict; its signature must be the old file's, so the next
    # call re-reads. A signature taken after the read would bless the old dict
    # with the new file's signature and pin it for the session.
    real_load = obsidian_utils.json.load
    raced = []

    def load_then_race(fh, *args, **kwargs):
        data = real_load(fh, *args, **kwargs)
        if fh.name == str(cfg) and not raced:
            raced.append(True)
            cfg.write_text(json.dumps({"vault_path": "/raced-longer"}))
        return data

    monkeypatch.setattr(obsidian_utils.json, "load", load_then_race)
    assert obsidian_utils.load_config()["vault_path"] == "/vault"
    assert raced
    assert obsidian_utils.load_config()["vault_path"] == "/raced-longer"


def test_file_sig_is_not_a_config_key(cfg):
    assert "config_file_sig" not in obsidian_utils.load_config()
