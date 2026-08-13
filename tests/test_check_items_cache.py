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


def test_ttl_for_review_matches_active():
    """REVIEW is a nudge for human judgement, not a settled result like
    DONE/STALE — it must re-evaluate on the same (longer) cadence as ACTIVE,
    not be cached on the short DONE/NEEDS-ACTION/STALE cycle (#264 Task 1)."""
    from check_items_cache import (
        _ttl_for, TTL_ACTIVE, TTL_DONE, TTL_STALE, TTL_NEEDS_ACTION,
    )
    assert _ttl_for("REVIEW") == TTL_ACTIVE
    # Pin intent, not just the value: REVIEW must land on the long/settled
    # cadence, never on one of the short DONE/NEEDS-ACTION/STALE cycles. This
    # guards against a future edit that repoints TTL_ACTIVE itself onto one
    # of the short-cycle constants (which the bare equality assertion above
    # would not catch, since it would still hold).
    assert _ttl_for("REVIEW") not in (TTL_DONE, TTL_STALE, TTL_NEEDS_ACTION)


def test_partition_rejects_future_dated_entry_in_the_same_run(capsys):
    """#302 round 4: before this guard, a cached entry whose classified_ts is
    ahead of `now` (clock skew, a hand-edit, a cache synced from a faster
    machine) came back `known` on partition()'s READ side for the CURRENT
    run -- the old one-sided `now - ts > ttl` check is negative for a future
    timestamp, so it can never exceed the TTL. And because a replayed verdict
    is stamped classifier_source='cache', which is inside assign_tier's
    _HIGH_TRUST_SOURCES, that `known` result could preselect the item for
    auto-checkoff. _freeze_classification's write-side clamp did not help
    here: it only fires on the NEXT run's write, so only the next run's
    partition() call saw ttl_expired -- this test pins that the very FIRST
    partition() call on a future-dated entry already routes to `needs`, with
    no update_cache round-trip in between. #302 round 5: a negative age is
    reported as 'unusable_ts', not the ordinary 'ttl_expired', with a stderr
    warning -- it's an environmental fault (clock skew, hand-edit, external
    writer), not routine expiry."""
    from check_items_cache import partition
    now = 1735100000.0
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(now),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry(
                    "h1", classification="DONE", classified_ts=now + 3600,
                )],
            }
        },
    }
    known, needs = partition([_make_group("h1")], cache, project="obsidian-brain",
                             head_sha="abc1234", now=now)
    assert len(known) == 0
    assert len(needs) == 1
    assert needs[0]["_reason"] == "unusable_ts"
    assert "unusable classified_ts" in capsys.readouterr().err


def test_partition_rejects_nan_classified_ts(capsys):
    """#302 round 4: a cached entry with classified_ts = NaN must route to
    `needs` on the very first partition() call. Every comparison against NaN
    is False, so the old one-sided `now - classified_ts > ttl` check never
    fired for a NaN age -- the entry was un-expirable for all time, not
    merely until the next run. The symmetric range check catches it because
    `0 <= _age` is also False for a NaN age. #302 round 5: that catch is
    reported as 'unusable_ts', not 'ttl_expired' -- a NaN stamp is not a
    valid past time at all, so labeling it as routine expiry would hide the
    corruption, and a stderr warning is emitted."""
    from check_items_cache import partition
    now = 1735100000.0
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(now),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry(
                    "h1", classification="DONE", classified_ts=float("nan"),
                )],
            }
        },
    }
    known, needs = partition([_make_group("h1")], cache, project="obsidian-brain",
                             head_sha="abc1234", now=now)
    assert len(known) == 0
    assert len(needs) == 1
    assert needs[0]["_reason"] == "unusable_ts"
    assert "unusable classified_ts" in capsys.readouterr().err


def test_partition_genuinely_old_entry_reports_ttl_expired_silently(capsys):
    """#302 round 5: a discriminating control for the unusable_ts/ttl_expired
    split. An entry whose age is TTL_DONE + 60 is genuinely old -- NOT
    corrupt, NOT clock-skewed, just past its TTL -- and must still report
    the ordinary 'ttl_expired' with NO warning. Without this control, a
    future edit could collapse both labels into one (or make the routine
    path noisy) and nothing would notice: this test pins both the label AND
    the silence of the routine path."""
    from check_items_cache import partition, TTL_DONE
    now = 1735100000.0
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(now),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry(
                    "h1", classification="DONE",
                    classified_ts=now - (TTL_DONE + 60),
                )],
            }
        },
    }
    known, needs = partition([_make_group("h1")], cache, project="obsidian-brain",
                             head_sha="abc1234", now=now)
    assert len(known) == 0
    assert len(needs) == 1
    assert needs[0]["_reason"] == "ttl_expired"
    assert capsys.readouterr().err == ""


def test_partition_ttl_boundary_is_inclusive():
    """#302 round 4: pins that the symmetric range check in CHANGE 1 did not
    shift the TTL boundary. The old check expired when `age > ttl`, so
    `age == ttl` stayed known. An age of exactly TTL_DONE must still be
    known; TTL_DONE + 1 must be ttl_expired. Both halves are asserted in one
    test so a boundary-shifting edit fails immediately on whichever side it
    moved -- but only for the UPPER boundary of `0 <= _age <= ttl`; see
    test_partition_zero_age_entry_is_known for the LOWER boundary, which
    this test does not reach (mutating `0 <= _age` to `0 < _age` survives
    this test unchanged)."""
    from check_items_cache import partition, TTL_DONE
    now = 1735100000.0
    cache_at_boundary = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(now),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry(
                    "h1", classification="DONE", classified_ts=now - TTL_DONE,
                )],
            }
        },
    }
    known, needs = partition([_make_group("h1")], cache_at_boundary,
                             project="obsidian-brain", head_sha="abc1234", now=now)
    assert len(known) == 1
    assert len(needs) == 0

    cache_past_boundary = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(now),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry(
                    "h1", classification="DONE", classified_ts=now - (TTL_DONE + 1),
                )],
            }
        },
    }
    known2, needs2 = partition([_make_group("h1")], cache_past_boundary,
                               project="obsidian-brain", head_sha="abc1234", now=now)
    assert len(known2) == 0
    assert len(needs2) == 1
    assert needs2[0]["_reason"] == "ttl_expired"


def test_partition_zero_age_entry_is_known():
    """#302 round 4: the LOWER bound of partition()'s `0 <= _age <= ttl`
    range, which test_partition_ttl_boundary_is_inclusive does not reach --
    it pins both sides of the UPPER bound only. An age of exactly 0
    (classified_ts == now, i.e. an entry written by update_cache in this same
    run, or two calls inside one clock tick) is a valid just-verified stamp,
    NOT an unusable future one, so it must come back `known`. Mirrors
    test_prior_ts_equal_to_now_is_not_clamped on the write side."""
    from check_items_cache import partition
    now = 1735100000.0
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(now),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry("h1", classification="DONE",
                                              classified_ts=now)],
            }
        },
    }
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
            "classifier_source": "agent",
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


def test_cache_replay_does_not_refresh_ttl():
    """#302: a classifier_source="cache" replay must inherit the prior
    on-disk classified_ts, not the caller's stamp — otherwise a group's TTL
    clock resets every time it is merely *read* back out of the cache, and
    ttl_expired never fires on a repo whose HEAD is static."""
    from check_items_cache import partition, update_cache, TTL_DONE

    t0 = 1735100000.0
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(t0),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry("h1", classification="DONE", classified_ts=t0)],
            }
        },
    }

    # Still within TTL at t1: comes back as a known (cache) hit.
    t1 = t0 + 100
    known, needs = partition([_make_group("h1")], cache, project="obsidian-brain",
                             head_sha="abc1234", now=t1)
    assert len(known) == 1
    assert len(needs) == 0

    # Build the replay payload the way SKILL.md Step 10 actually builds it:
    # classifier_source="cache", carrying the *refreshed* t1 stamp. This is
    # exactly what the real caller sends, so the fixture must include it —
    # a fixture that omitted classified_ts here would pass even with the
    # fix reverted.
    replay = _make_fresh("h1", classifier_source="cache", classification="DONE",
                         classified_ts=int(t1))
    updated = update_cache(cache=cache, project="obsidian-brain",
                           all_groups=[_make_group("h1")],
                           fresh_classifications=[replay],
                           head_sha="abc1234", now=t1)
    persisted = updated["runs"]["obsidian-brain"]["groups"][0]
    assert persisted["classified_ts"] == t0, (
        "cache replay must inherit the prior classified_ts, not the caller's stamp"
    )

    # t2 is chosen to discriminate old vs new behavior: with the old
    # refresh-on-replay behavior the stored ts would be t1, making the age
    # at t2 equal TTL_DONE - 40 — still within TTL, so the group would
    # wrongly come back as known. With the fix, the stored ts stays t0, so
    # the age at t2 is TTL_DONE + 60, which is past TTL_DONE.
    t2 = t0 + TTL_DONE + 60
    known2, needs2 = partition([_make_group("h1")], updated, project="obsidian-brain",
                               head_sha="abc1234", now=t2)
    assert len(needs2) == 1
    assert needs2[0]["_reason"] == "ttl_expired"


def test_fresh_agent_verdict_still_refreshes_ttl():
    """Excluding negative control for #302: a genuinely re-derived
    (classifier_source="agent") verdict MUST still refresh its
    classified_ts — the inheritance rule must not be over-applied to
    non-replay sources."""
    from check_items_cache import update_cache

    t0 = 1735100000.0
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(t0),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry("h1", classification="DONE", classified_ts=t0)],
            }
        },
    }

    t1 = t0 + 100
    fresh = _make_fresh("h1", classifier_source="agent", classification="DONE",
                        classified_ts=int(t1))
    updated = update_cache(cache=cache, project="obsidian-brain",
                           all_groups=[_make_group("h1")],
                           fresh_classifications=[fresh],
                           head_sha="abc1234", now=t1)
    persisted = updated["runs"]["obsidian-brain"]["groups"][0]
    assert persisted["classified_ts"] == int(t1)


def test_cache_replay_without_prior_entry_uses_now(capsys):
    """#302: a classifier_source="cache" replay with no prior on-disk entry
    (evicted mid-run by the #297 branch, or a concurrent run between this
    run's two load_cache() calls) has nothing to inherit, so
    _freeze_classification's `int(now)` DEFAULT is used — and update_cache
    warns, because that entry's TTL silently restarts. The replay payload
    deliberately carries no classified_ts: with one, the assertion would be
    satisfied through fc.get("classified_ts", ...) and the default would
    never be exercised."""
    from check_items_cache import update_cache

    cache = {"schema_version": 1, "runs": {}}
    t1 = 1735100100.0
    replay = _make_fresh("h1", classifier_source="cache", classification="DONE")
    del replay["classified_ts"]
    updated = update_cache(cache=cache, project="obsidian-brain",
                           all_groups=[_make_group("h1")],
                           fresh_classifications=[replay],
                           head_sha="abc1234", now=t1)
    persisted = updated["runs"]["obsidian-brain"]["groups"][0]
    assert persisted["classified_ts"] == int(t1)
    assert "no prior on-disk" in capsys.readouterr().err


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
        "classifier_source": "agent",
    }]
    updated = update_cache(cache, project="p", all_groups=all_groups,
                           fresh_classifications=fresh, head_sha="h")
    cached_entry = updated["runs"]["p"]["groups"][0]
    assert cached_entry["action_required"] == 'gh issue close 534 --comment "Fixed"'

    # Round-trip through partition
    known, _ = partition(all_groups, updated, project="p", head_sha="h")
    assert len(known) == 1
    assert known[0]["_cached_action_required"] == 'gh issue close 534 --comment "Fixed"'


def test_cached_heuristic_verdict_is_not_replayed_as_known():
    """#297 legacy-cache gap: a cached entry explicitly stamped
    classifier_source='heuristic' must never be replayed as `known`, even
    when it matches HEAD, mtime, and TTL — everything that would otherwise
    make it a cache hit. Routing it to `needs` re-classifies it with real
    evidence instead of letting SKILL.md stamp a trusted 'cache' source onto
    a heuristic verdict."""
    from check_items_cache import partition
    head = "abc1234"
    groups = [_make_group("h1")]
    entry = _make_cached_entry("h1")
    entry["classifier_source"] = "heuristic"
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(time.time()),
                "project_head_at_classify": head,
                "groups": [entry],
            }
        },
    }
    known, needs = partition(groups, cache, project="obsidian-brain", head_sha=head)
    assert known == []
    assert len(needs) == 1
    assert needs[0]["_reason"] == "heuristic_cached"


def test_legacy_heuristic_citation_is_not_replayed_as_known():
    """Live-data case: entries written before #297 carry no classifier_source
    field at all, so the heuristic must be detected from the evidence
    citation's shape instead. classify_groups_heuristic always emits
    "heuristic: token '<t>' near completion phrase '<p>'", so that prefix is
    the tell for a pre-#297 entry that was actually heuristic-derived."""
    from check_items_cache import partition
    head = "abc1234"
    groups = [_make_group("h1")]
    entry = _make_cached_entry("h1")
    entry.pop("classifier_source", None)  # predates #297; no field at all
    entry["evidence_citation"] = "heuristic: token 'v0.4.0' near completion phrase 'release'"
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(time.time()),
                "project_head_at_classify": head,
                "groups": [entry],
            }
        },
    }
    known, needs = partition(groups, cache, project="obsidian-brain", head_sha=head)
    assert known == []
    assert len(needs) == 1
    assert needs[0]["_reason"] == "heuristic_cached"


def test_legacy_agent_entry_still_replays_as_known():
    """Contrast for the two tests above: 369 of the 373 live cached entries
    have no classifier_source but are legitimately agent-derived. A
    provenance-less entry whose citation does NOT look heuristic must still
    replay as `known` — otherwise the heuristic_cached guard would be
    indistinguishable from "invalidate every legacy entry", which would
    throw away all of that agent-derived work and suppress preselection for
    days until everything re-classifies."""
    from check_items_cache import partition
    head = "abc1234"
    groups = [_make_group("h1")]
    entry = _make_cached_entry("h1")
    entry.pop("classifier_source", None)  # predates #297; no field at all
    entry["evidence_citation"] = "Merged as 5dfaf98; PR #68 closed."
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(time.time()),
                "project_head_at_classify": head,
                "groups": [entry],
            }
        },
    }
    known, needs = partition(groups, cache, project="obsidian-brain", head_sha=head)
    assert needs == []
    assert len(known) == 1
    assert known[0]["_cached_classification"] == entry["classification"]


def test_update_cache_allowlist_rejects_unknown_source(capsys):
    """update_cache must persist only classifier_source values in
    {"agent", "prefilter", "cache"} — an allowlist, not a denylist. An
    absent, empty, or misspelled source (e.g. "Heuristic") must be refused
    exactly like an explicit "heuristic" would be, since a caller that has
    not been updated to thread provenance should degrade to "never
    persisted" rather than "persisted by default"."""
    from check_items_cache import update_cache
    cache = {"schema_version": 1, "runs": {}}
    all_groups = [
        _make_group("agent-h"), _make_group("heuristic-h"),
        _make_group("empty-h"), _make_group("none-h"), _make_group("mixed-case-h"),
    ]
    fresh_classifications = [
        _make_fresh("agent-h", classifier_source="agent"),
        _make_fresh("heuristic-h", classifier_source="heuristic"),
        _make_fresh("empty-h", classifier_source=""),
        _make_fresh("none-h", classifier_source=None),
        _make_fresh("mixed-case-h", classifier_source="Heuristic"),
    ]
    updated = update_cache(cache, project="obsidian-brain", all_groups=all_groups,
                           fresh_classifications=fresh_classifications, head_sha="h")
    hashes = {g["canonical_hash"] for g in updated["runs"]["obsidian-brain"]["groups"]}
    assert hashes == {"agent-h"}
    err = capsys.readouterr().err
    assert "refusing to cache 4 verdict" in err


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


def test_partition_handles_non_numeric_classified_ts():
    """Corrupted cache with string classified_ts must not crash partition.

    Regression test for R5 Finding D. When classified_ts is non-numeric
    (e.g. manual cache edit, partial corruption), the coercion must default
    to 0.0, which forces ttl_expired and routes the group to needs.
    """
    from check_items_cache import partition
    cache = {
        "schema_version": 1,
        "runs": {
            "p": {
                "last_run_ts": int(time.time()),
                "project_head_at_classify": "h",
                "groups": [{
                    "canonical_hash": "h1",
                    "canonical_text": "x",
                    "members": [{"file": "n.md", "line": 1, "mtime": 1735000000}],
                    "classification": "DONE",
                    "confidence": "HIGH",
                    "evidence_citation": "c",
                    "action_required": None,
                    "classified_ts": "not-a-number",  # corrupt — should not crash
                }],
            }
        },
    }
    groups = [_make_group("h1")]
    # Must not raise TypeError from `now - classified_ts`; corrupt ts → ttl_expired.
    known, needs = partition(groups, cache, project="p", head_sha="h")
    assert len(known) == 0, "corrupted classified_ts must force reclassification"
    assert len(needs) == 1
    assert needs[0].get("_reason") == "ttl_expired"


# ---------------------------------------------------------------------------
# Task 5 (#297): cache refuses heuristic verdicts and evicts stale entries
# they would otherwise shadow.
# ---------------------------------------------------------------------------

def _make_fresh(canonical_hash, classifier_source="agent", classification="ACTIVE",
                 confidence="LOW", members=None, classified_ts=None):
    return {
        "canonical_hash": canonical_hash,
        "canonical_text": "Item X",
        "members": members or [{"file": "n.md", "line": 1, "mtime": 1735000000}],
        "classification": classification,
        "confidence": confidence,
        "evidence_citation": "test",
        "action_required": None,
        "classifier_source": classifier_source,
        "classified_ts": classified_ts if classified_ts is not None else int(time.time()),
    }


def test_update_cache_refuses_heuristic_verdicts(capsys):
    """Heuristic-derived verdicts must never be persisted (#297 defect 5) —
    only the agent-sourced verdicts in the same run survive, and stderr
    says how many were refused."""
    from check_items_cache import update_cache
    cache = {"schema_version": 1, "runs": {}}
    all_groups = [_make_group("h1"), _make_group("h2"), _make_group("h3")]
    fresh_classifications = [
        _make_fresh("h1", classifier_source="agent"),
        _make_fresh("h2", classifier_source="agent"),
        _make_fresh("h3", classifier_source="heuristic"),
    ]
    updated = update_cache(cache, project="obsidian-brain", all_groups=all_groups,
                           fresh_classifications=fresh_classifications, head_sha="H1")
    hashes = {g["canonical_hash"] for g in updated["runs"]["obsidian-brain"]["groups"]}
    assert hashes == {"h1", "h2"}

    captured = capsys.readouterr()
    assert "refusing to cache 1" in captured.err


def test_heuristic_verdict_evicts_stale_cached_entry():
    """A heuristic verdict for a hash that already has a cached (trusted)
    entry must evict that entry outright, not merely fail to overwrite it —
    otherwise the unconditional project_head_at_classify bump in
    update_cache would let this run's HEAD revalidate a verdict it never
    actually confirmed (the second-order hazard #297 calls out)."""
    from check_items_cache import update_cache
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": 1734000000,
                "project_head_at_classify": "OLDHEAD",
                "groups": [_make_cached_entry("H", classification="DONE")],
            }
        },
    }
    all_groups = [_make_group("H")]
    fresh_classifications = [_make_fresh("H", classifier_source="heuristic")]
    updated = update_cache(cache, project="obsidian-brain", all_groups=all_groups,
                           fresh_classifications=fresh_classifications, head_sha="NEWHEAD")
    hashes = {g["canonical_hash"] for g in updated["runs"]["obsidian-brain"]["groups"]}
    assert "H" not in hashes


def test_evicted_group_reclassifies_next_run():
    """The anti-revalidation assertion at the level a user experiences it:
    after a heuristic verdict evicts a stale cached entry, partition() must
    NOT replay a cached verdict for that hash on the next run. Seeded
    identically to test_heuristic_verdict_evicts_stale_cached_entry — without
    eviction this group would come back `known` at the bumped HEAD, since
    project_head_at_classify is stamped unconditionally by update_cache."""
    from check_items_cache import update_cache, partition
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": 1734000000,
                "project_head_at_classify": "OLDHEAD",
                "groups": [_make_cached_entry("H", classification="DONE")],
            }
        },
    }
    all_groups = [_make_group("H")]
    fresh_classifications = [_make_fresh("H", classifier_source="heuristic")]
    updated = update_cache(cache, project="obsidian-brain", all_groups=all_groups,
                           fresh_classifications=fresh_classifications, head_sha="NEWHEAD")

    known, needs = partition([_make_group("H")], updated, project="obsidian-brain",
                             head_sha="NEWHEAD")
    assert len(known) == 0
    assert len(needs) == 1
    assert needs[0]["canonical_hash"] == "H"
    assert needs[0]["_reason"] == "new"


def test_rejected_hash_is_not_reinserted_by_the_second_loop():
    """Final round: loop 1 evicts a hash carrying a classifier_source=
    "heuristic" verdict (the #297 guard) via a bare `continue` that
    deliberately does NOT add it to `seen`. Without the fix, loop 2's guard
    is only `h in seen or h not in current_hashes`, so when the SAME
    canonical_hash also has a trusted classifier_source="cache" record in
    this run's fresh_classifications, loop 2 re-inserts it immediately --
    undoing the eviction loop 1 just performed, in the same function call,
    and defeating both #297's "an unverified verdict must not be replayed"
    guard and #302's inheritance (a fresh insert here restarts the TTL at
    `now`). The fix adds `h in _rejected_hashes` to loop 2's guard. Test both
    orderings of the two fresh records for h1, since fresh_by_hash is
    last-write-wins -- a fix that only works for one ordering is not a
    fix."""
    from check_items_cache import update_cache, partition

    for order in ("heuristic_then_cache", "cache_then_heuristic"):
        cache = {
            "schema_version": 1,
            "runs": {
                "obsidian-brain": {
                    "last_run_ts": 1734000000,
                    "project_head_at_classify": "OLDHEAD",
                    "groups": [_make_cached_entry("h1", classification="DONE")],
                }
            },
        }
        heuristic_fc = _make_fresh("h1", classifier_source="heuristic")
        cache_fc = _make_fresh("h1", classifier_source="cache", classification="DONE")
        fresh_classifications = (
            [heuristic_fc, cache_fc] if order == "heuristic_then_cache"
            else [cache_fc, heuristic_fc]
        )
        updated = update_cache(
            cache, project="obsidian-brain", all_groups=[_make_group("h1")],
            fresh_classifications=fresh_classifications, head_sha="NEWHEAD",
        )
        hashes = {g["canonical_hash"] for g in updated["runs"]["obsidian-brain"]["groups"]}
        assert "h1" not in hashes, f"order={order}: h1 must be absent, not re-inserted"

        # Must be re-derived from scratch on the next run, exactly like
        # test_evicted_group_reclassifies_next_run.
        known, needs = partition([_make_group("h1")], updated, project="obsidian-brain",
                                 head_sha="NEWHEAD")
        assert len(known) == 0, f"order={order}"
        assert len(needs) == 1, f"order={order}"
        assert needs[0]["canonical_hash"] == "h1", f"order={order}"
        assert needs[0]["_reason"] == "new", f"order={order}"


def test_agent_verdicts_unaffected():
    """A run with no heuristic verdicts produces the same cache content as
    today's (pre-#297-Task-5) behaviour: GC, overwrite-by-hash, and the HEAD
    bump are all unaffected by the new filtering/eviction logic."""
    from check_items_cache import update_cache
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": 1734000000,
                "project_head_at_classify": "OLDHEAD",
                "groups": [_make_cached_entry("gone1"), _make_cached_entry("kept1")],
            }
        },
    }
    all_groups = [_make_group("kept1"), _make_group("fresh")]
    fresh_classifications = [
        _make_fresh("kept1", classifier_source="agent", classification="NEEDS-ACTION"),
        _make_fresh("fresh", classifier_source="agent"),
    ]
    updated = update_cache(cache, project="obsidian-brain", all_groups=all_groups,
                           fresh_classifications=fresh_classifications, head_sha="NEWHEAD")
    hashes = {g["canonical_hash"] for g in updated["runs"]["obsidian-brain"]["groups"]}
    assert hashes == {"kept1", "fresh"}
    kept1 = next(g for g in updated["runs"]["obsidian-brain"]["groups"]
                 if g["canonical_hash"] == "kept1")
    assert kept1["classification"] == "NEEDS-ACTION"
    assert updated["runs"]["obsidian-brain"]["project_head_at_classify"] == "NEWHEAD"


def test_cache_replay_inherits_its_own_prior_not_a_neighbours():
    """#302 cross-talk guard: when several groups are replayed in one run,
    each must inherit the classified_ts of *its own* prior entry. A single-
    group fixture cannot tell `prior=g` apart from `prior=existing_groups[0]`."""
    from check_items_cache import update_cache

    t_old, t_mid = 1735100000.0, 1735150000.0
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(t_mid),
                "project_head_at_classify": "abc1234",
                "groups": [
                    _make_cached_entry("h1", classification="DONE", classified_ts=t_old),
                    _make_cached_entry("h2", classification="ACTIVE", classified_ts=t_mid),
                ],
            }
        },
    }
    t1 = t_mid + 100
    updated = update_cache(
        cache=cache, project="obsidian-brain",
        all_groups=[_make_group("h1"), _make_group("h2")],
        fresh_classifications=[
            _make_fresh("h1", classifier_source="cache", classification="DONE",
                        classified_ts=int(t1)),
            _make_fresh("h2", classifier_source="cache", classification="ACTIVE",
                        classified_ts=int(t1)),
        ],
        head_sha="abc1234", now=t1,
    )
    by_hash = {g["canonical_hash"]: g for g in updated["runs"]["obsidian-brain"]["groups"]}
    assert by_hash["h1"]["classified_ts"] == t_old
    assert by_hash["h2"]["classified_ts"] == t_mid


def test_cache_replay_with_unusable_prior_ts():
    """#302: the inheritance guard's two defensive conditions. A prior entry
    with no classified_ts at all falls back to the caller's stamp (there is
    nothing to inherit); a prior entry with a non-numeric classified_ts is
    round-tripped verbatim rather than 'healed' to now — partition() reads a
    non-numeric ts as ancient, so verbatim is fail-safe while healing would
    re-introduce #302."""
    from check_items_cache import update_cache, partition

    t1 = 1735100100.0

    missing = _make_cached_entry("h1", classification="DONE")
    del missing["classified_ts"]
    corrupt = _make_cached_entry("h2", classification="DONE",
                                 classified_ts="not-a-number")
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(t1),
                "project_head_at_classify": "abc1234",
                "groups": [missing, corrupt],
            }
        },
    }
    updated = update_cache(
        cache=cache, project="obsidian-brain",
        all_groups=[_make_group("h1"), _make_group("h2")],
        fresh_classifications=[
            _make_fresh("h1", classifier_source="cache", classification="DONE",
                        classified_ts=int(t1)),
            _make_fresh("h2", classifier_source="cache", classification="DONE",
                        classified_ts=int(t1)),
        ],
        head_sha="abc1234", now=t1,
    )
    by_hash = {g["canonical_hash"]: g for g in updated["runs"]["obsidian-brain"]["groups"]}
    assert by_hash["h1"]["classified_ts"] == int(t1)
    assert by_hash["h2"]["classified_ts"] == "not-a-number"

    # The corrupt entry must re-derive on the next run rather than be replayed.
    _known, needs = partition([_make_group("h2")], updated, project="obsidian-brain",
                              head_sha="abc1234", now=t1)
    assert [n["_reason"] for n in needs] == ["ttl_expired"]


def test_future_dated_prior_ts_is_clamped_and_expires(capsys):
    """#302 follow-up: an inherited classified_ts ahead of wall clock can never
    satisfy partition()'s `now - ts > ttl`. WITHOUT the clamp this entry is
    un-expirable for as long as the skew lasts — the old refresh-on-replay
    stamp self-healed clock skew in a single run, and inheritance removed
    that self-heal, so the clamp must do it explicitly. The clamp target is
    0 (ancient), not `now`: this run did not verify the verdict, so it must
    not be granted a fresh full TTL either — partition() must therefore see
    ttl_expired immediately, at the SAME `now` the clamp happened at, with no
    need to advance the clock."""
    from check_items_cache import update_cache, partition

    t0 = 1735100000.0
    ten_years_ahead = t0 + 10 * 365 * 86400
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(t0),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry("h1", classification="DONE",
                                              classified_ts=ten_years_ahead)],
            }
        },
    }
    updated = update_cache(
        cache=cache, project="obsidian-brain", all_groups=[_make_group("h1")],
        fresh_classifications=[_make_fresh("h1", classifier_source="cache",
                                           classification="DONE",
                                           classified_ts=int(t0))],
        head_sha="abc1234", now=t0,
    )
    persisted = updated["runs"]["obsidian-brain"]["groups"][0]
    assert persisted["classified_ts"] == 0
    assert "not a usable past timestamp" in capsys.readouterr().err

    # The round-trip is the load-bearing half: clamped to 0 (ancient) the
    # group is immediately ttl_expired at the very `now` the clamp happened.
    _known, needs = partition([_make_group("h1")], updated, project="obsidian-brain",
                              head_sha="abc1234", now=t0)
    assert [n["_reason"] for n in needs] == ["ttl_expired"]


def test_prior_ts_equal_to_now_is_not_clamped(capsys):
    """Boundary fixture for the clamp's strict `>`: a prior classified_ts
    EXACTLY equal to `now` is not in the future, so it is preserved verbatim
    and emits no warning. `now` is deliberately non-integral: with an
    integral `now`, a `>=`-instead-of-`>` mutation clamps to a numerically
    identical value, so a value-only assertion is vacuous; a non-integral
    `now` makes both the stored value AND the absence of the warning
    discriminate the mutation."""
    from check_items_cache import update_cache

    now = 1735100000.5
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(now),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry("h1", classification="DONE",
                                              classified_ts=now)],
            }
        },
    }
    updated = update_cache(
        cache=cache, project="obsidian-brain", all_groups=[_make_group("h1")],
        fresh_classifications=[_make_fresh("h1", classifier_source="cache",
                                           classification="DONE",
                                           classified_ts=int(now))],
        head_sha="abc1234", now=now,
    )
    persisted = updated["runs"]["obsidian-brain"]["groups"][0]
    assert persisted["classified_ts"] == now
    assert "not a usable past timestamp" not in capsys.readouterr().err


def test_nan_prior_ts_is_clamped_and_expires(capsys):
    """#302 round 3: with the old `_numeric_ts > now` comparison, a NaN prior
    classified_ts is inherited verbatim -- every comparison against NaN is
    False, so `nan > now` is False and NaN slips past the clamp as if it were
    a valid, non-future timestamp. That is strictly worse than a future date:
    partition()'s `now - nan > ttl` is ALSO False for every possible `now`,
    since NaN poisons any arithmetic it touches, so the entry would never
    expire and never self-heal as the clock advances -- a future-dated entry
    at least self-heals once `now` catches up to it. The fix compares with
    `not (x <= now)`, which is True for both NaN and future values (NaN
    fails `<=` too), routing both into the clamp."""
    from check_items_cache import update_cache, partition

    t0 = 1735100000.0
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(t0),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry("h1", classification="DONE",
                                              classified_ts=float("nan"))],
            }
        },
    }
    updated = update_cache(
        cache=cache, project="obsidian-brain", all_groups=[_make_group("h1")],
        fresh_classifications=[_make_fresh("h1", classifier_source="cache",
                                           classification="DONE",
                                           classified_ts=int(t0))],
        head_sha="abc1234", now=t0,
    )
    persisted = updated["runs"]["obsidian-brain"]["groups"][0]
    assert persisted["classified_ts"] == 0
    assert "not a usable past timestamp" in capsys.readouterr().err

    # Round-trip at the SAME now: clamped to 0 (ancient) must be immediately
    # ttl_expired, proving the clamp -- not merely a persisted 0 -- is what
    # makes the entry re-derive.
    _known, needs = partition([_make_group("h1")], updated, project="obsidian-brain",
                              head_sha="abc1234", now=t0)
    assert [n["_reason"] for n in needs] == ["ttl_expired"]


def test_overflow_prior_ts_is_treated_as_ancient(capsys):
    """#302 round 3: `float()` raises OverflowError (not ValueError) on a
    sufficiently large Python int, e.g. a 400-digit integer that could arrive
    via a hand-edited or corrupted JSON cache file. Both _freeze_classification
    and partition() must catch OverflowError alongside TypeError/ValueError so
    a huge int degrades to 'ancient' like any other unusable value, instead of
    crashing the whole cache write/read.

    Mirrors test_cache_replay_with_unusable_prior_ts's non-numeric-string
    case: _freeze_classification's guard only clamps a value it can compare
    against `now` (a real float). An OverflowError means it could not even
    get that far, so -- like the non-numeric-string case -- it round-trips
    the huge value verbatim rather than clamping it; partition() is the one
    that must independently treat it as ancient on the next read, which is
    what the OverflowError catch there is for."""
    from check_items_cache import update_cache, partition

    huge = 10**400
    t0 = 1735100000.0
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(t0),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry("h1", classification="DONE",
                                              classified_ts=huge)],
            }
        },
    }
    # Must not raise despite the OverflowError-triggering prior value.
    updated = update_cache(
        cache=cache, project="obsidian-brain", all_groups=[_make_group("h1")],
        fresh_classifications=[_make_fresh("h1", classifier_source="cache",
                                           classification="DONE",
                                           classified_ts=int(t0))],
        head_sha="abc1234", now=t0,
    )
    persisted = updated["runs"]["obsidian-brain"]["groups"][0]
    assert persisted["classified_ts"] == huge
    # The write side is deliberately silent for an OverflowError value: it
    # cannot compare the value to `now` (that comparison is what raised), so
    # it cannot call it future-dated the way the NaN/future-date clamp does.
    assert "not a usable past timestamp" not in capsys.readouterr().err

    _known, needs = partition([_make_group("h1")], updated, project="obsidian-brain",
                              head_sha="abc1234", now=t0)
    assert [n["_reason"] for n in needs] == ["ttl_expired"]

    # partition() must also tolerate reading a huge int directly from a cache
    # entry without going through update_cache's clamp first.
    direct_cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(t0),
                "project_head_at_classify": "abc1234",
                "groups": [_make_cached_entry("h1", classification="DONE",
                                              classified_ts=huge)],
            }
        },
    }
    _known2, needs2 = partition([_make_group("h1")], direct_cache, project="obsidian-brain",
                                head_sha="abc1234", now=t0)
    assert [n["_reason"] for n in needs2] == ["ttl_expired"]


def test_replay_with_prior_missing_ts_warns_and_stamps_now(capsys):
    """A prior entry that exists but carries no classified_ts is the same
    invariant violation as no prior at all: nothing to inherit, so the
    caller's stamp is used AND the diagnostic fires."""
    from check_items_cache import update_cache

    now = 1735100100.0
    prior = _make_cached_entry("h1", classification="DONE")
    del prior["classified_ts"]
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(now),
                "project_head_at_classify": "abc1234",
                "groups": [prior],
            }
        },
    }
    updated = update_cache(
        cache=cache, project="obsidian-brain", all_groups=[_make_group("h1")],
        fresh_classifications=[_make_fresh("h1", classifier_source="cache",
                                           classification="DONE",
                                           classified_ts=int(now))],
        head_sha="abc1234", now=now,
    )
    persisted = updated["runs"]["obsidian-brain"]["groups"][0]
    assert persisted["classified_ts"] == int(now)
    assert "no prior on-disk" in capsys.readouterr().err


def test_update_cache_tolerates_non_dict_cached_entry():
    """A corrupt cache file whose `groups` list holds a non-dict entry must
    degrade to a skipped entry rather than crash the whole cache write with
    AttributeError — mirrors partition()'s isinstance(g, dict) filter. The
    valid entries in the same list still round-trip."""
    from check_items_cache import update_cache

    t0 = 1735100000.0
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(t0),
                "project_head_at_classify": "abc1234",
                "groups": [
                    "not-a-dict",
                    None,
                    _make_cached_entry("h1", classification="DONE", classified_ts=t0),
                ],
            }
        },
    }
    updated = update_cache(cache=cache, project="obsidian-brain",
                           all_groups=[_make_group("h1")],
                           fresh_classifications=[],
                           head_sha="abc1234", now=t0 + 100)
    groups = updated["runs"]["obsidian-brain"]["groups"]
    assert [g["canonical_hash"] for g in groups] == ["h1"]
    assert groups[0]["classified_ts"] == t0


# ---------------------------------------------------------------------------
# #305: a surviving entry (no fresh classification this run) must be pinned
# to the head it was actually verified at, not laundered onto the run-level
# project_head_at_classify that gets bumped unconditionally.
# ---------------------------------------------------------------------------

def test_update_cache_does_not_launder_unverified_survivor():
    """The issue's exact scenario, end to end. A cache entry verified at
    HEAD1 survives an update_cache run at HEAD2 with no fresh classification
    for it — it must carry head_at_classify == "HEAD1" afterward, and the
    next partition() at HEAD2 must route it to needs/head_changed rather than
    replaying it as known."""
    from check_items_cache import update_cache, partition

    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(time.time()),
                "project_head_at_classify": "HEAD1",
                "groups": [_make_cached_entry("h1")],
            }
        },
    }
    updated = update_cache(
        cache=cache, project="obsidian-brain",
        all_groups=[_make_group("h1")],
        fresh_classifications=[],
        head_sha="HEAD2",
    )
    survivor = updated["runs"]["obsidian-brain"]["groups"][0]
    assert survivor["head_at_classify"] == "HEAD1"
    assert updated["runs"]["obsidian-brain"]["project_head_at_classify"] == "HEAD2"

    known, needs = partition([_make_group("h1")], updated,
                             project="obsidian-brain", head_sha="HEAD2")
    assert len(known) == 0
    assert len(needs) == 1
    assert needs[0].get("_reason") == "head_changed"


def test_update_cache_survivor_keeps_original_head_across_two_bumps():
    """setdefault semantics: an entry that survives un-reclassified through
    TWO consecutive update_cache runs (HEAD2 then HEAD3) must keep the head
    it was ORIGINALLY verified at (HEAD1), not inherit HEAD2 along the way."""
    from check_items_cache import update_cache

    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(time.time()),
                "project_head_at_classify": "HEAD1",
                "groups": [_make_cached_entry("h1")],
            }
        },
    }
    after_head2 = update_cache(
        cache=cache, project="obsidian-brain",
        all_groups=[_make_group("h1")],
        fresh_classifications=[],
        head_sha="HEAD2",
    )
    assert after_head2["runs"]["obsidian-brain"]["groups"][0]["head_at_classify"] == "HEAD1"

    after_head3 = update_cache(
        cache=after_head2, project="obsidian-brain",
        all_groups=[_make_group("h1")],
        fresh_classifications=[],
        head_sha="HEAD3",
    )
    survivor = after_head3["runs"]["obsidian-brain"]["groups"][0]
    assert survivor["head_at_classify"] == "HEAD1"


def test_partition_replays_freshly_verified_entry_without_per_entry_head():
    """Positive control: an entry that DID get a fresh record this run must
    still replay as known on the next partition() at the same head — the
    per-entry stamp must not leak onto entries update_cache actually
    verified. Without this test, a mutation that stamps head_at_classify on
    every entry (not just un-reclassified survivors) would still pass the
    laundering tests above while destroying the cache's whole purpose.

    Both halves are exercised deliberately: update_cache freezes a fresh
    record at TWO call sites -- one for a brand-new canonical_hash and one
    for a hash that already had an on-disk entry -- and a fixture that only
    ever starts from an empty cache reaches the first. A stamp-everything
    mutation on the second would slip past a single-phase version of this
    test, so phase 2 below re-runs the same hash as an EXISTING entry."""
    from check_items_cache import update_cache, partition

    # Phase 1: brand-new canonical_hash (update_cache's no-prior-entry path).
    cache = {"schema_version": 1, "runs": {}}
    updated = update_cache(
        cache=cache, project="obsidian-brain",
        all_groups=[_make_group("h1")],
        fresh_classifications=[_make_fresh("h1", classifier_source="agent")],
        head_sha="HEAD1",
    )
    assert "head_at_classify" not in updated["runs"]["obsidian-brain"]["groups"][0]

    known, needs = partition([_make_group("h1")], updated,
                             project="obsidian-brain", head_sha="HEAD1")
    assert len(known) == 1
    assert len(needs) == 0

    # Phase 2: the SAME hash now has a prior on-disk entry, so this run takes
    # update_cache's has-a-prior path. It is genuinely re-verified at HEAD2,
    # so it must still carry no per-entry stamp and must still replay.
    updated = update_cache(
        cache=updated, project="obsidian-brain",
        all_groups=[_make_group("h1")],
        fresh_classifications=[_make_fresh("h1", classifier_source="agent")],
        head_sha="HEAD2",
    )
    assert "head_at_classify" not in updated["runs"]["obsidian-brain"]["groups"][0]

    known, needs = partition([_make_group("h1")], updated,
                             project="obsidian-brain", head_sha="HEAD2")
    assert len(known) == 1
    assert len(needs) == 0


def test_partition_prefers_per_entry_head_over_run_level():
    """A per-entry head_at_classify must win over the run-level
    project_head_at_classify: an entry stamped "OLD" inside a run whose
    run-level field has since moved to "NEW" is routed to needs/head_changed
    even though the run-level field matches head_sha."""
    from check_items_cache import partition

    entry = _make_cached_entry("h1")
    entry["head_at_classify"] = "OLD"
    cache = {
        "schema_version": 1,
        "runs": {
            "obsidian-brain": {
                "last_run_ts": int(time.time()),
                "project_head_at_classify": "NEW",
                "groups": [entry],
            }
        },
    }
    known, needs = partition([_make_group("h1")], cache,
                             project="obsidian-brain", head_sha="NEW")
    assert len(known) == 0
    assert len(needs) == 1
    assert needs[0].get("_reason") == "head_changed"
