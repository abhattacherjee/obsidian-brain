import os
import sys
import pytest

HOOKS_DIR = os.path.join(os.path.dirname(__file__), "..", "hooks")
if HOOKS_DIR not in sys.path:
    sys.path.insert(0, HOOKS_DIR)


def test_canonical_hash_stable_across_whitespace():
    """Test 13 - whitespace and markdown punctuation do not change the hash."""
    from check_items_cache import canonical_hash
    a = canonical_hash("Fix #68")
    b = canonical_hash("  Fix  #68  ")
    c = canonical_hash("**Fix** #68")
    d = canonical_hash("Fix #68\n")
    assert a == b == c == d
    assert len(a) == 16
    assert all(ch in "0123456789abcdef" for ch in a)


def test_canonical_hash_changes_on_rename():
    """Test 14 - real content rename produces a different hash."""
    from check_items_cache import canonical_hash
    a = canonical_hash("Fix #68")
    b = canonical_hash("Fix issue 68")
    c = canonical_hash("Fix #69")
    assert a != b
    assert a != c
    assert b != c


import json


def test_cache_corrupted_json_falls_back_to_empty(tmp_path, monkeypatch):
    """Test 21 - corrupted JSON loads as empty cache, no exception."""
    from check_items_cache import load_cache, SCHEMA_VERSION
    fake_cache = tmp_path / "check-items-classifications.json"
    fake_cache.write_text("{not valid json at all", encoding="utf-8")
    monkeypatch.setattr("check_items_cache.CACHE_PATH", fake_cache)

    cache = load_cache()
    assert cache == {"schema_version": SCHEMA_VERSION, "runs": {}}

    fake_cache.write_text(json.dumps({"schema_version": 99, "runs": {"x": {}}}), encoding="utf-8")
    cache2 = load_cache()
    assert cache2 == {"schema_version": SCHEMA_VERSION, "runs": {}}


def test_cache_save_then_load_roundtrip(tmp_path, monkeypatch):
    """save_cache writes atomically with 0o600 perms; load_cache returns the same shape."""
    from check_items_cache import load_cache, save_cache, SCHEMA_VERSION
    fake_cache = tmp_path / "check-items-classifications.json"
    monkeypatch.setattr("check_items_cache.CACHE_PATH", fake_cache)
    monkeypatch.setattr("check_items_cache.CACHE_DIR", tmp_path)

    data = {
        "schema_version": SCHEMA_VERSION,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": 1735000000,
                "project_head_at_classify": "abc1234",
                "groups": [
                    {
                        "canonical_hash": "0123456789abcdef",
                        "canonical_text": "Test item",
                        "members": [],
                        "classification": "DONE",
                        "confidence": "HIGH",
                        "evidence_citation": "test",
                        "classified_ts": 1734999000,
                    }
                ],
            }
        },
    }
    save_cache(data)
    assert fake_cache.exists()
    mode = fake_cache.stat().st_mode & 0o777
    assert mode == 0o600

    loaded = load_cache()
    assert loaded == data
