"""Tests for hooks/capture_state.py (#425)."""

import json
import os
import stat
import threading

import pytest

import capture_state as cs

SID = "11111111-2222-3333-4444-555555555555"


DEFAULTS = {"offset": 0, "transcript_path": None, "project": None, "last_batch_at": 0.0,
            "pending": None, "checked_size": -1}


def test_state_roundtrip_and_defaults():
    assert cs.get_state(SID) == DEFAULTS
    cs.set_state(SID, offset=42, project="demo")
    cs.set_state(SID, last_batch_at=7.5)
    assert cs.get_state(SID) == {**DEFAULTS, "offset": 42, "project": "demo", "last_batch_at": 7.5}
    assert SID in cs.known_sessions()


def test_state_file_mode_0600_and_dir_0700():
    cs.set_state(SID, offset=1)
    path = cs.capture_dir() / f"{SID}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(cs.capture_dir().stat().st_mode) == 0o700


def test_state_rejects_unknown_fields_and_bad_sid():
    with pytest.raises(ValueError):
        cs.set_state(SID, nonsense=1)
    for bad in ["../x", "a/b", "", "x" * 200, "a b"]:
        with pytest.raises(ValueError):
            cs.get_state(bad)


def test_corrupt_state_reads_as_defaults():
    (cs.capture_dir() / f"{SID}.json").write_text("{not json")
    assert cs.get_state(SID)["offset"] == 0


def test_claim_exclusive():
    token = cs.claim(SID)
    assert token and cs.claim(SID) is None
    assert cs.claim_is_live(SID) is True
    cs.release(SID, token)
    assert cs.claim_is_live(SID) is False
    assert cs.claim(SID)


def _age_claim(seconds):
    path = cs.capture_dir() / f"{SID}.claim"
    old = path.stat().st_mtime - seconds
    os.utime(path, (old, old))


def test_claim_stale_after_ttl():
    first = cs.claim(SID)
    _age_claim(1000)
    assert cs.claim_is_live(SID, ttl=900) is False
    second = cs.claim(SID, ttl=900)  # taken over
    assert second and second != first
    assert cs.claim(SID, ttl=900) is None  # and now live again


def test_release_and_renew_touch_only_your_own_claim():
    first = cs.claim(SID)
    _age_claim(1000)
    second = cs.claim(SID, ttl=900)
    cs.release(SID, first)  # the stale worker finishing late
    assert cs.claim_is_live(SID) is True
    assert cs.renew(SID, first) is False
    cs.release(SID, second)
    assert cs.claim_is_live(SID) is False


def test_renew_pushes_expiry_back():
    token = cs.claim(SID)
    _age_claim(800)
    assert cs.renew(SID, token) is True
    _age_claim(200)
    assert cs.claim_is_live(SID, ttl=900) is True


def test_flush_pop_once():
    assert cs.pop_flush(SID) is False and cs.has_flush(SID) is False
    cs.set_flush(SID)
    assert cs.has_flush(SID) is True and cs.has_flush(SID) is True  # peeking keeps it
    assert cs.pop_flush(SID) is True
    assert cs.pop_flush(SID) is False and cs.has_flush(SID) is False


def test_ledger_append_list_newest_first():
    a = cs.ledger_append({"session_id": SID, "title": "a", "ts": 1.0})
    b = cs.ledger_append({"session_id": SID, "title": "b", "ts": 2.0})
    cs.ledger_append({"session_id": "other", "title": "c", "ts": 3.0})
    assert [r["id"] for r in cs.ledger_list(session=SID)] == [b, a]
    assert [r["title"] for r in cs.ledger_list()] == ["c", "b", "a"]
    assert len(cs.ledger_list(limit=1)) == 1
    assert stat.S_IMODE((cs.capture_dir() / cs.LEDGER_NAME).stat().st_mode) == 0o600


def test_ledger_update_supersedes_and_keeps_first_ts():
    cid = cs.ledger_append({"session_id": SID, "status": "active", "ts": 5.0})
    merged = cs.ledger_update(cid, status="rejected")
    assert merged["status"] == "rejected"
    rows = cs.ledger_list()
    assert len(rows) == 1 and rows[0]["status"] == "rejected" and rows[0]["ts"] == 5.0
    with pytest.raises(KeyError):
        cs.ledger_update("nope", status="x")


def test_ledger_skips_bad_lines():
    cs.ledger_append({"session_id": SID})
    with open(cs.capture_dir() / cs.LEDGER_NAME, "a") as fh:
        fh.write("garbage\n[1,2]\n")
    assert len(cs.ledger_list()) == 1


def test_ledger_concurrent_appends():
    def writer(tag):
        for i in range(50):
            cs.ledger_append({"session_id": SID, "title": f"{tag}-{i}"})

    threads = [threading.Thread(target=writer, args=(t,)) for t in ("x", "y")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (cs.capture_dir() / cs.LEDGER_NAME).read_text().splitlines()
    assert len(lines) == 100
    assert all(json.loads(line)["session_id"] == SID for line in lines)


def test_installed_at_stable():
    first = cs.installed_at()
    assert first > 0
    assert cs.installed_at() == first


def test_append_capture_log_line(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "_log_dir", lambda: tmp_path / ".claude")
    cs.append_capture_log("demo project", SID, "saved:2", "batch one")
    line = (tmp_path / ".claude" / "obsidian-brain-hook.log").read_text().strip()
    assert " Capture project=demo_project sid=11111111 outcome=saved:2 detail=batch_one" in line
