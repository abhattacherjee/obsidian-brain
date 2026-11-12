"""load_config's session cache must not outlive the _DEFAULTS that wrote it (#409).

Repro: a session cached its config with pre-3.8.0 code, whose _DEFAULTS had no
`wiki_folder`. After the plugin updated mid-session, load_config() kept
returning that dict, so /vault-ask's `c.get("wiki_folder")` read None and the
wiki was silently turned off for the rest of the session.

#59: the cache also keeps the config file's (st_ino, st_mtime_ns, st_size),
taken before the read, and trusts the cached config only while the file still
matches. The config and both signatures are one cache value, "config_entry",
so two processes cannot leave an old dict beside a new file's signature.
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


def _entry(config, **over):
    entry = {
        "config": config,
        "defaults_sig": obsidian_utils._defaults_signature(),
        "file_sig": obsidian_utils._config_file_sig(),
    }
    entry.update(over)
    return entry


def test_cache_written_by_older_code_is_not_trusted(cfg):
    # The literal repro: old code cached "config" with no signature key, and
    # its dict lacked the wiki_folder default added in 3.8.0. The old keys
    # are ignored, even a "config" that looks current.
    obsidian_utils.cache_set(SID, "config", dict(obsidian_utils._DEFAULTS, vault_path="/stale"))
    obsidian_utils.cache_set(SID, "config_defaults_sig", obsidian_utils._defaults_signature())
    loaded = obsidian_utils.load_config()
    assert loaded.get("wiki_folder") == "claude-wiki"
    assert loaded["vault_path"] == "/vault"


def test_entry_with_wrong_defaults_sig_is_not_trusted(cfg):
    obsidian_utils.cache_set(SID, "config_entry", _entry(
        dict(obsidian_utils._DEFAULTS, vault_path="/stale"), defaults_sig="not-the-sig"))
    assert obsidian_utils.load_config()["vault_path"] == "/vault"


def test_entry_missing_a_default_key_is_not_trusted(cfg):
    short = {k: v for k, v in obsidian_utils._DEFAULTS.items() if k != "wiki_folder"}
    short["vault_path"] = "/stale"
    obsidian_utils.cache_set(SID, "config_entry", _entry(short))
    loaded = obsidian_utils.load_config()
    assert loaded["vault_path"] == "/vault"
    assert loaded["wiki_folder"] == "claude-wiki"


def test_config_and_signatures_are_one_cache_value(cfg, monkeypatch):
    keys = []
    real_set = obsidian_utils.cache_set

    def record(sid, key, value):
        keys.append(key)
        return real_set(sid, key, value)

    monkeypatch.setattr(obsidian_utils, "cache_set", record)
    obsidian_utils.load_config()
    assert keys == ["config_entry"]


def test_stale_entry_under_old_file_sig_is_rejected(cfg):
    obsidian_utils.load_config()
    old_sig = obsidian_utils._config_file_sig()
    cfg.write_text(json.dumps({"vault_path": "/new-value-longer"}))
    assert obsidian_utils._config_file_sig() != old_sig
    obsidian_utils.cache_set(SID, "config_entry", _entry(
        dict(obsidian_utils._DEFAULTS, vault_path="/stale"), file_sig=old_sig))
    assert obsidian_utils.load_config()["vault_path"] == "/new-value-longer"


def test_defaults_change_mid_session_is_picked_up(cfg, monkeypatch):
    assert "new_default_409" not in obsidian_utils.load_config()
    monkeypatch.setitem(obsidian_utils._DEFAULTS, "new_default_409", "x")
    assert obsidian_utils.load_config()["new_default_409"] == "x"


def test_rewritten_config_is_read_in_the_same_session(cfg):
    assert obsidian_utils.load_config()["vault_path"] == "/vault"
    cfg.write_text(json.dumps({"vault_path": "/elsewhere"}))
    assert obsidian_utils.load_config()["vault_path"] == "/elsewhere"
    assert obsidian_utils.load_config(fresh=True)["vault_path"] == "/elsewhere"


def test_invalidating_config_entry_still_forces_a_reread(cfg):
    obsidian_utils.load_config()
    cfg.write_text(json.dumps({"vault_path": "/elsewhere"}))
    obsidian_utils.cache_invalidate(SID, "config_entry")
    assert obsidian_utils.load_config()["vault_path"] == "/elsewhere"


def test_signature_is_not_a_config_key(cfg):
    # The signature lives beside the config in the cache entry, never inside
    # it, so /vault-config never shows it as a setting.
    assert "defaults_sig" not in obsidian_utils.load_config()
    assert "defaults_sig" not in obsidian_utils.load_config()


def test_old_code_rewriting_config_under_a_new_signature_is_not_trusted(cfg):
    # Review finding on PR #411. Old hooks keep running the pre-update install
    # for the whole session; an old load_config re-caches "config" without
    # touching anything else. The new code reads only "config_entry", so that
    # old dict is never served.
    assert obsidian_utils.load_config()["wiki_folder"] == "claude-wiki"  # 1
    obsidian_utils.cache_invalidate(SID, "config_entry")                 # 2
    old = {k: v for k, v in obsidian_utils._DEFAULTS.items() if k != "wiki_folder"}
    old["vault_path"] = "/vault"
    obsidian_utils.cache_set(SID, "config", old)                         # 3
    obsidian_utils.cache_set(SID, "config_defaults_sig", obsidian_utils._defaults_signature())
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


def test_entry_without_file_sig_is_not_trusted(tmp_path, monkeypatch):
    monkeypatch.setattr(obsidian_utils, "_CONFIG_PATH", str(tmp_path / "missing-config.json"))
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda *a, **k: SID)
    obsidian_utils.cache_set(SID, "config_entry", {
        "config": dict(obsidian_utils._DEFAULTS, vault_path="/stale"),
        "defaults_sig": obsidian_utils._defaults_signature(),
    })
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
    assert "file_sig" not in obsidian_utils.load_config()


def test_same_length_in_place_edit_with_new_mtime_is_seen(cfg):
    assert obsidian_utils.load_config()["vault_path"] == "/vault"
    before = cfg.stat()
    cfg.write_text(json.dumps({"vault_path": "/vaulY"}))
    os.utime(cfg, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    after = cfg.stat()
    assert after.st_ino == before.st_ino
    assert after.st_size == before.st_size
    assert after.st_mtime_ns != before.st_mtime_ns
    assert obsidian_utils.load_config()["vault_path"] == "/vaulY"


def test_different_length_edit_with_old_mtime_is_seen(cfg):
    assert obsidian_utils.load_config()["vault_path"] == "/vault"
    before = cfg.stat()
    cfg.write_text(json.dumps({"vault_path": "/vault-much-longer"}))
    os.utime(cfg, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = cfg.stat()
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_size != before.st_size
    assert obsidian_utils.load_config()["vault_path"] == "/vault-much-longer"
