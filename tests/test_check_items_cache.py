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


import time


def _make_group(canonical_hash, project="obsidian-brain", text="Item X", members=None):
    return {
        "canonical_hash": canonical_hash,
        "project": project,
        "canonical_text": text,
        "members": members or [{"file": "n.md", "line": 1, "mtime": 1735000000}],
    }


def _make_cached_entry(canonical_hash, classification="DONE", classified_ts=None,
                      members=None):
    return {
        "canonical_hash": canonical_hash,
        "canonical_text": "Item X",
        "members": members or [{"file": "n.md", "line": 1, "mtime": 1735000000}],
        "classification": classification,
        "confidence": "HIGH",
        "evidence_citation": "test",
        "classified_ts": classified_ts if classified_ts is not None else int(time.time()) - 60,
    }


def test_cache_hit_skips_reclassification():
    """Test 15 - fresh cache with matching hash + mtime + HEAD returns known=N, needs=0."""
    from check_items_cache import partition
    head = "abc1234"
    groups = [_make_group("h1"), _make_group("h2")]
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(time.time()),
                "project_head_at_classify": head,
                "groups": [_make_cached_entry("h1"), _make_cached_entry("h2")],
            }
        },
    }
    known, needs = partition(groups, cache, project="obsidian-brain", head_sha=head)
    assert len(known) == 2
    assert len(needs) == 0


def test_head_change_triggers_full_reclassify_phase1():
    """Test 17 - cached HEAD != current HEAD invalidates all groups (Phase 1 coarse)."""
    from check_items_cache import partition
    groups = [_make_group("h1"), _make_group("h2")]
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(time.time()),
                "project_head_at_classify": "OLDHEAD",
                "groups": [_make_cached_entry("h1"), _make_cached_entry("h2")],
            }
        },
    }
    known, needs = partition(groups, cache, project="obsidian-brain", head_sha="NEWHEAD")
    assert len(known) == 0
    assert len(needs) == 2
    assert all(g.get("_reason") == "head_changed" for g in needs)


def test_force_flag_invalidates_everything():
    """Test 19 - force=True always returns known=0, needs=N."""
    from check_items_cache import partition
    groups = [_make_group("h1"), _make_group("h2")]
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(time.time()),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry("h1"), _make_cached_entry("h2")],
            }
        },
    }
    known, needs = partition(groups, cache, project="obsidian-brain",
                             head_sha="abc1234", force=True)
    assert len(known) == 0
    assert len(needs) == 2
    assert all(g.get("_reason") == "force" for g in needs)


def test_new_group_triggers_reclassify():
    """Cache-miss canonical_hash routes to needs with _reason='new'."""
    from check_items_cache import partition
    groups = [_make_group("h_new")]
    cache = {"schema_version": 1, "runs": {}}
    known, needs = partition(groups, cache, project="obsidian-brain", head_sha="abc1234")
    assert len(known) == 0
    assert len(needs) == 1
    assert needs[0].get("_reason") == "new"


def test_mtime_bump_triggers_partial_reclassify():
    """Test 16 - one member mtime bumped -> that group reclassifies; the other stays."""
    from check_items_cache import partition
    groups = [
        _make_group("h1", members=[{"file": "a.md", "line": 1, "mtime": 1735000000}]),
        _make_group("h2", members=[{"file": "b.md", "line": 1, "mtime": 1735000100}]),
    ]
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(time.time()),
                "project_head_at_classify": "abc1234",
                "groups": [
                    _make_cached_entry("h1", members=[
                        {"file": "a.md", "line": 1, "mtime": 1735000000}
                    ]),
                    _make_cached_entry("h2", members=[
                        {"file": "b.md", "line": 1, "mtime": 1735000050}
                    ]),
                ],
            }
        },
    }
    known, needs = partition(groups, cache, project="obsidian-brain", head_sha="abc1234")
    assert len(known) == 1
    assert len(needs) == 1
    assert needs[0]["canonical_hash"] == "h2"
    assert needs[0]["_reason"] == "mtime_changed"


def test_mtime_one_second_tolerance():
    """+/- 1s mtime tolerance per spec; FS noise must not invalidate."""
    from check_items_cache import partition
    groups = [_make_group("h1", members=[{"file": "a.md", "line": 1, "mtime": 1735000000.5}])]
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(time.time()),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry("h1", members=[
                    {"file": "a.md", "line": 1, "mtime": 1735000000.0}
                ])],
            }
        },
    }
    known, needs = partition(groups, cache, project="obsidian-brain", head_sha="abc1234")
    assert len(known) == 1
    assert len(needs) == 0


def test_ttl_expires_for_done_at_24h():
    """Test 18 - DONE expires at TTL_DONE; under-TTL stays known."""
    from check_items_cache import partition, TTL_DONE
    now = 1735100000.0
    cache_within = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(now),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry(
                    "h1", classification="DONE",
                    classified_ts=now - (TTL_DONE - 300)
                )],
            }
        },
    }
    known, needs = partition([_make_group("h1")], cache_within,
                             project="obsidian-brain", head_sha="abc1234", now=now)
    assert len(known) == 1
    assert len(needs) == 0

    cache_expired = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(now),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry(
                    "h1", classification="DONE",
                    classified_ts=now - (TTL_DONE + 60)
                )],
            }
        },
    }
    known2, needs2 = partition([_make_group("h1")], cache_expired,
                               project="obsidian-brain", head_sha="abc1234", now=now)
    assert len(known2) == 0
    assert len(needs2) == 1
    assert needs2[0]["_reason"] == "ttl_expired"


def test_ttl_active_longer_than_done():
    """ACTIVE TTL is 7d; an age that expires DONE keeps ACTIVE cached."""
    from check_items_cache import partition, TTL_DONE, TTL_ACTIVE
    now = 1735100000.0
    age = TTL_DONE + 3600  # 25h
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(now),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry(
                    "h1", classification="ACTIVE", classified_ts=now - age
                )],
            }
        },
    }
    assert age < TTL_ACTIVE
    known, needs = partition([_make_group("h1")], cache, project="obsidian-brain",
                             head_sha="abc1234", now=now)
    assert len(known) == 1
    assert len(needs) == 0


def test_cache_evicts_removed_items():
    """Test 20 - cached entries whose canonical_hash is not in current run are GC'd."""
    from check_items_cache import update_cache
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": 1734000000,
                "project_head_at_classify": "OLDHEAD",
                "groups": [
                    _make_cached_entry("gone1"),
                    _make_cached_entry("kept1"),
                    _make_cached_entry("kept2"),
                ],
            }
        },
    }
    all_groups = [_make_group("kept1"), _make_group("kept2"), _make_group("fresh")]
    fresh_classifications = [
        {
            "canonical_hash": "fresh",
            "canonical_text": "New item",
            "members": [{"file": "x.md", "line": 1, "mtime": 1735000000}],
            "classification": "ACTIVE",
            "confidence": "LOW",
            "evidence_citation": None,
            "classified_ts": 1735000000,
        }
    ]
    updated = update_cache(cache, project="obsidian-brain",
                           all_groups=all_groups,
                           fresh_classifications=fresh_classifications,
                           head_sha="NEWHEAD")
    hashes = {g["canonical_hash"] for g in updated["runs"]["obsidian-brain"]["groups"]}
    assert "gone1" not in hashes
    assert hashes == {"kept1", "kept2", "fresh"}
    assert updated["runs"]["obsidian-brain"]["project_head_at_classify"] == "NEWHEAD"


def test_cache_update_preserves_unchanged_classifications():
    """Cached entries surviving GC keep their classification/citation if no fresh override."""
    from check_items_cache import update_cache
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": 1734000000,
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry("h1", classification="DONE")],
            }
        },
    }
    all_groups = [_make_group("h1")]
    fresh_classifications = []
    updated = update_cache(cache, project="obsidian-brain",
                           all_groups=all_groups,
                           fresh_classifications=fresh_classifications,
                           head_sha="abc1234")
    out = updated["runs"]["obsidian-brain"]["groups"][0]
    assert out["canonical_hash"] == "h1"
    assert out["classification"] == "DONE"
    assert out["evidence_citation"] == "test"


# ---------------------------------------------------------------------------
# R3 regression tests: Finding A1 + A2
# ---------------------------------------------------------------------------

def test_cache_preserves_action_required_on_warm_runs():
    """NEEDS-ACTION groups must retain action_required across cache cycles (Finding A1)."""
    from check_items_cache import update_cache, partition
    cache = {"schema_version": 1, "runs": {}}
    all_groups = [_make_group("h1")]
    fresh = [{
        "canonical_hash": "h1",
        "canonical_text": "Close #534",
        "members": [{"file": "n.md", "line": 1, "mtime": 1735000000}],
        "classification": "NEEDS-ACTION",
        "confidence": "HIGH",
        "evidence_citation": "Story 11.12",
        "action_required": 'gh issue close 534 --comment "Fixed"',
        "classified_ts": int(time.time()) - 60,
    }]
    updated = update_cache(cache, project="p", all_groups=all_groups,
                           fresh_classifications=fresh, head_sha="h")
    cached_entry = updated["runs"]["p"]["groups"][0]
    assert cached_entry["action_required"] == 'gh issue close 534 --comment "Fixed"'

    # Round-trip through partition
    known, _ = partition(all_groups, updated, project="p", head_sha="h")
    assert len(known) == 1
    assert known[0]["_cached_action_required"] == 'gh issue close 534 --comment "Fixed"'


def test_partition_skips_corrupted_cache_entries():
    """Cache entries missing canonical_hash must not crash partition (Finding A2)."""
    from check_items_cache import partition
    cache = {
        "schema_version": 1,
        "runs": {
            "p": {
                "last_run_ts": int(time.time()),
                "project_head_at_classify": "h",
                "groups": [
                    {"no_hash_field": True},   # corrupt entry
                    _make_cached_entry("h1"),   # valid
                ],
            }
        },
    }
    groups = [_make_group("h1")]
    # Should not raise KeyError; corrupted entry is silently skipped
    known, needs = partition(groups, cache, project="p", head_sha="h")
    assert len(known) == 1
