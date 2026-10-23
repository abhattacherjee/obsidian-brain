"""In-process memo for the per-session cache file (#447).

The cache file is the truth; the memo is a copy keyed by the file's
(st_ino, st_mtime_ns, st_size). These tests pin the three properties that
make that safe: one parse per unchanged file, an external rewrite is seen,
and a caller cannot change the memo through a returned value.
"""

from __future__ import annotations

import json
import os
from unittest import mock

import pytest

import obsidian_utils

SID = "memo-test-sid"


@pytest.fixture(autouse=True)
def _clear_memo():
    obsidian_utils._CACHE_MEMO.clear()
    obsidian_utils._CACHE_DIRTY.clear()
    yield
    obsidian_utils._CACHE_MEMO.clear()
    obsidian_utils._CACHE_DIRTY.clear()


def _cache_path(sid: str = SID) -> str:
    return f"{obsidian_utils._CACHE_PREFIX}{sid}.json"


def _external_write(data: dict, sid: str = SID) -> None:
    """Rewrite the cache file the way another process would (temp + rename)."""
    path = _cache_path(sid)
    tmp = path + ".ext"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def test_cache_get_parses_file_once_over_1000_lookups():
    obsidian_utils.cache_set(SID, "a", {"x": 1})
    obsidian_utils._CACHE_MEMO.clear()  # start cold: the first get must parse
    with mock.patch.object(obsidian_utils.json, "load", wraps=json.load) as load:
        for _ in range(1000):
            assert obsidian_utils.cache_get(SID, "a") == {"x": 1}
    assert load.call_count == 1


def test_cache_set_does_not_reparse_its_own_write():
    obsidian_utils.cache_set(SID, "a", 1)
    with mock.patch.object(obsidian_utils.json, "load", wraps=json.load) as load:
        for i in range(50):
            obsidian_utils.cache_set(SID, f"k{i}", i)
            assert obsidian_utils.cache_get(SID, f"k{i}") == i
    assert load.call_count == 0
    with open(_cache_path()) as f:
        on_disk = json.load(f)
    assert on_disk["k49"] == 49 and on_disk["a"] == 1


def test_external_rewrite_is_seen():
    obsidian_utils.cache_set(SID, "a", "old")
    assert obsidian_utils.cache_get(SID, "a") == "old"
    _external_write({"a": "new-value-from-another-process", "b": 2})
    assert obsidian_utils.cache_get(SID, "a") == "new-value-from-another-process"
    assert obsidian_utils.cache_get(SID, "b") == 2


def test_same_size_external_rewrite_with_new_mtime_is_seen():
    """Same size, different bytes, different mtime_ns: size alone would not
    tell the files apart, so the mtime must."""
    obsidian_utils.cache_set(SID, "a", "AAAA")
    assert obsidian_utils.cache_get(SID, "a") == "AAAA"
    path = _cache_path()
    st = os.stat(path)
    with open(path, "r+") as f:  # same inode, same length
        raw = f.read()
        assert "AAAA" in raw
        f.seek(0)
        f.write(raw.replace("AAAA", "BBBB"))
    assert os.stat(path).st_size == st.st_size
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert obsidian_utils.cache_get(SID, "a") == "BBBB"


def test_cache_set_merges_over_external_rewrite():
    obsidian_utils.cache_set(SID, "mine", 1)
    _external_write({"mine": 1, "theirs": "written-by-another-process"})
    obsidian_utils.cache_set(SID, "mine2", 2)
    with open(_cache_path()) as f:
        on_disk = json.load(f)
    assert on_disk == {"mine": 1, "theirs": "written-by-another-process", "mine2": 2}


def test_mutating_returned_value_does_not_leak_into_memo():
    obsidian_utils.cache_set(SID, "meta", {"tags": ["a"], "type": "x"})
    got = obsidian_utils.cache_get(SID, "meta")
    got["tags"].append("leaked")
    got["type"] = "changed"
    assert obsidian_utils.cache_get(SID, "meta") == {"tags": ["a"], "type": "x"}


def test_mutating_value_after_set_does_not_leak_into_memo():
    value = {"tags": ["a"]}
    obsidian_utils.cache_set(SID, "meta", value)
    value["tags"].append("leaked")
    assert obsidian_utils.cache_get(SID, "meta") == {"tags": ["a"]}


def test_set_keeps_json_round_trip_semantics():
    """Today a value goes through json.dump/json.load, so a tuple comes back
    as a list and an int dict key as a str. The memo must not change that."""
    obsidian_utils.cache_set(SID, "t", {"pair": (1, 2), 3: "int-key"})
    assert obsidian_utils.cache_get(SID, "t") == {"pair": [1, 2], "3": "int-key"}


def test_corrupt_file_reads_as_miss_and_set_resets_it(capsys):
    path = _cache_path()
    obsidian_utils._ensure_secure_dir()
    with open(path, "w") as f:
        f.write("{not json")
    assert obsidian_utils.cache_get(SID, "a") is None
    obsidian_utils.cache_set(SID, "a", 1)
    assert "cache corrupted, resetting" in capsys.readouterr().err
    with open(path) as f:
        assert json.load(f) == {"a": 1}


def test_invalidate_all_drops_memo():
    obsidian_utils.cache_set(SID, "a", 1)
    obsidian_utils.cache_invalidate(SID)
    assert not os.path.exists(_cache_path())
    assert obsidian_utils.cache_get(SID, "a") is None


def test_invalidate_keys_updates_memo_and_disk():
    obsidian_utils.cache_set(SID, "a", 1)
    obsidian_utils.cache_set(SID, "b", 2)
    obsidian_utils.cache_invalidate(SID, "a")
    assert obsidian_utils.cache_get(SID, "a") is None
    assert obsidian_utils.cache_get(SID, "b") == 2
    with open(_cache_path()) as f:
        assert json.load(f) == {"b": 2}


def test_failed_write_does_not_leave_value_in_memo(monkeypatch):
    obsidian_utils.cache_set(SID, "a", 1)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(obsidian_utils.os, "replace", boom)
    obsidian_utils.cache_set(SID, "b", 2)
    monkeypatch.undo()
    # The write failed, so the file (the truth) has no "b"; the memo must
    # not answer with it either.
    assert obsidian_utils.cache_get(SID, "b") is None


# --- cache_deferred --------------------------------------------------------


def test_deferred_writes_once_and_get_sees_pending_keys():
    obsidian_utils.cache_set(SID, "seed", 0)
    real_replace = os.replace
    with mock.patch.object(obsidian_utils.os, "replace", side_effect=real_replace) as rep:
        with obsidian_utils.cache_deferred():
            for i in range(100):
                obsidian_utils.cache_set(SID, f"k{i}", i)
                assert obsidian_utils.cache_get(SID, f"k{i}") == i
            assert rep.call_count == 0
        assert rep.call_count == 1
    with open(_cache_path()) as f:
        on_disk = json.load(f)
    assert on_disk["k99"] == 99 and on_disk["seed"] == 0


def test_deferred_merges_external_key_written_mid_block():
    obsidian_utils.cache_set(SID, "seed", 0)
    with obsidian_utils.cache_deferred():
        obsidian_utils.cache_set(SID, "mine", 1)
        _external_write({"seed": 0, "theirs": "from-another-process"})
        # A get after the external rewrite re-reads the file; the pending
        # key set inside the block must still be visible.
        assert obsidian_utils.cache_get(SID, "mine") == 1
        assert obsidian_utils.cache_get(SID, "theirs") == "from-another-process"
        obsidian_utils.cache_set(SID, "mine2", 2)
    with open(_cache_path()) as f:
        on_disk = json.load(f)
    assert on_disk == {"seed": 0, "theirs": "from-another-process", "mine": 1, "mine2": 2}


def test_deferred_nesting_flushes_at_outermost_exit():
    with obsidian_utils.cache_deferred():
        with obsidian_utils.cache_deferred():
            obsidian_utils.cache_set(SID, "a", 1)
        assert not os.path.exists(_cache_path())
    with open(_cache_path()) as f:
        assert json.load(f) == {"a": 1}


def test_deferred_flushes_on_exception():
    with pytest.raises(RuntimeError):
        with obsidian_utils.cache_deferred():
            obsidian_utils.cache_set(SID, "a", 1)
            raise RuntimeError("boom")
    with open(_cache_path()) as f:
        assert json.load(f) == {"a": 1}


def test_invalidate_inside_deferred_block_flushes_first():
    obsidian_utils.cache_set(SID, "a", 1)
    with obsidian_utils.cache_deferred():
        obsidian_utils.cache_set(SID, "b", 2)
        obsidian_utils.cache_set(SID, "c", 3)
        obsidian_utils.cache_invalidate(SID, "b")
        with open(_cache_path()) as f:
            assert json.load(f) == {"a": 1, "c": 3}
        assert obsidian_utils.cache_get(SID, "b") is None
    with open(_cache_path()) as f:
        assert json.load(f) == {"a": 1, "c": 3}


def test_invalidate_all_inside_deferred_block_drops_pending_keys():
    with obsidian_utils.cache_deferred():
        obsidian_utils.cache_set(SID, "a", 1)
        obsidian_utils.cache_invalidate(SID)
        assert obsidian_utils.cache_get(SID, "a") is None
    assert not os.path.exists(_cache_path())


def test_uncacheable_sid_stays_a_no_op_inside_deferred_block():
    with obsidian_utils.cache_deferred():
        obsidian_utils.cache_set("unknown", "a", 1)
        assert obsidian_utils.cache_get("unknown", "a") is None
    assert not os.path.exists(_cache_path("unknown"))
