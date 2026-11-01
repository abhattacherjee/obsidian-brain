"""Verifier tests use isolated caches and never invoke the real Claude CLI."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
from types import SimpleNamespace

import pytest

import lessons_verify as lv


@pytest.fixture
def cache(tmp_path):
    db = lv.VerdictCache(tmp_path / "private" / "verdicts.db")
    yield db
    db.close()


def test_rubric_is_byte_exact():
    assert len(lv.RUBRIC) == 876
    assert hashlib.sha1(lv.RUBRIC.encode("utf-8")).hexdigest() == (
        "f27dcc2dfb2b596a12458d265de2031ef3d78333"
    )


def test_build_prompt_shape():
    assert lv.build_prompt("A", ["x", "y"]) == (
        lv.RUBRIC + "\n\nANCHOR: A\n\nCANDIDATE 1: x\nCANDIDATE 2: y"
    )


def test_parse_verdicts_maps_same_and_rejects_garbage():
    assert lv.parse_verdicts(
        '```json\n{"1": "same", "2": " DIFFERENT ", "3": "Same"}\n```', 3
    ) == [True, False, True]
    for bad in ("garbage", "{invalid}", "[]", "null", '{"1": "same"'):
        assert lv.parse_verdicts(bad, 1) is None


@pytest.mark.parametrize("reply", [
    "{}", '{"1": "same"}', '{"1": "same", "3": "different"}', '{"1": "same", "2": "maybe"}',
    '{"1": "same", "2": true}', '{"1": "same", "2": null}',
])
def test_parse_verdicts_needs_every_candidate_answered(reply):
    assert lv.parse_verdicts(reply, 2) is None


def test_verify_anchors_incomplete_reply_is_failed_and_not_cached(cache):
    result = lv.verify_anchors({"a": "a", "b": "b", "c": "c"}, {"a": ["b", "c"]}, cache,
                               runner=lambda _: '{"1": "maybe"}')
    assert result == {"judged": 0, "failed": 1, "skipped": 0}
    assert not cache.judged("a")
    assert cache.edges() == set()
    assert cache._conn.execute("SELECT COUNT(*) FROM pairs").fetchone()[0] == 0


def test_run_haiku_uses_worker_env(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="result")

    monkeypatch.setattr(lv.subprocess, "run", fake_run)
    assert lv.run_haiku("prompt") == "result"
    args, kwargs = calls[0]
    assert args == ["claude", "-p", "--model", "haiku", "--setting-sources", "project",
                    "--strict-mcp-config", "--tools", ""]
    assert args[args.index("--tools") + 1] == "" and "--strict-mcp-config" in args
    assert kwargs["env"]["OBSIDIAN_BRAIN_WORKER"] == "1"
    assert kwargs["input"] == "prompt"
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 120
    assert "stdin" not in kwargs


def test_run_haiku_retries_once_on_timeout_then_none(monkeypatch):
    calls = []

    def timeout(args, **kwargs):
        calls.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(lv.subprocess, "run", timeout)
    assert lv.run_haiku("prompt") is None
    assert calls == [120, 240]


def test_run_haiku_timeout_then_success(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout="ok")

    monkeypatch.setattr(lv.subprocess, "run", fake_run)
    assert lv.run_haiku("prompt", (1, 2)) == "ok"
    assert calls == [1, 2]


@pytest.mark.parametrize("failure", [FileNotFoundError(), OSError(), None],
                         ids=["missing-binary", "os-error", "nonzero-exit"])
def test_run_haiku_stops_on_other_failures(monkeypatch, failure):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        if failure is not None:
            raise failure
        return SimpleNamespace(returncode=1, stdout="ignored")

    monkeypatch.setattr(lv.subprocess, "run", fake_run)
    assert lv.run_haiku("prompt") is None
    assert len(calls) == 1


def test_default_cache_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert lv.default_cache_path() == tmp_path / ".claude/obsidian-brain/lessons-verdicts.db"


def test_cache_either_side_confirm_is_sticky(cache):
    cache.record("b", ["a", "c"], [True, False])
    cache.record("a", ["b"], [False])
    assert cache.edges() == {("a", "b")}
    cache.record("c", ["b"], [True])
    assert cache.edges() == {("a", "b"), ("b", "c")}
    assert cache.judged("a") and cache.judged("b") and cache.judged("c")


def test_cache_rubric_change_hides_old_rows(cache, monkeypatch):
    cache.record("a", ["b"], [True])
    original = lv.RUBRIC_VERSION
    monkeypatch.setattr(lv, "RUBRIC_VERSION", "next")
    assert not cache.judged("a")
    assert cache.edges() == set()
    cache.record("a", ["b"], [False])
    assert cache.judged("a")
    assert cache.edges() == set()
    monkeypatch.setattr(lv, "RUBRIC_VERSION", original)
    assert cache.edges() == {("a", "b")}
    monkeypatch.setattr(lv, "MODEL", "other")
    assert not cache.judged("a")
    assert cache.edges() == set()


def test_cache_prune_drops_dead_keys(cache):
    cache.record("a", ["b", "dead"], [True, False])
    cache.record("dead", ["b"], [True])
    assert cache.prune({"a", "b"}) == 3
    assert cache.edges() == {("a", "b")}
    assert cache.judged("a") and not cache.judged("dead")
    assert cache.prune({"a", "b"}) == 0
    assert cache.prune(set()) == 2


def test_cache_file_mode_0600(tmp_path):
    path = tmp_path / "private" / "verdicts.db"
    cache = lv.VerdictCache(path)
    cache.record("a", ["b"], [True])
    cache.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    path.chmod(0o644)
    cache = lv.VerdictCache(path)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert cache.judged("a")
        assert cache.edges() == {("a", "b")}
    finally:
        cache.close()


def test_cache_sidecars_chmod_0600(tmp_path):
    # A crashed run leaves -wal/-shm behind with whatever mode they had; reopening must tighten them.
    src = tmp_path / "src"
    src.mkdir()
    old = os.umask(0o022)
    try:
        first = lv.VerdictCache(src / "verdicts.db")
        first.record("a", ["b"], [True])
        dest = tmp_path / "private"
        dest.mkdir(mode=0o700)
        for name in ("verdicts.db", "verdicts.db-wal", "verdicts.db-shm"):
            shutil.copy(src / name, dest / name)
            (dest / name).chmod(0o644)
        first.close()
    finally:
        os.umask(old)
    path = dest / "verdicts.db"
    wal, shm = (path.with_name(path.name + sfx) for sfx in ("-wal", "-shm"))
    assert wal.stat().st_size > 0
    cache = lv.VerdictCache(path)
    try:
        assert wal.exists() and shm.exists()
        assert stat.S_IMODE(wal.stat().st_mode) == 0o600
        assert stat.S_IMODE(shm.stat().st_mode) == 0o600
        assert cache.edges() == {("a", "b")}
    finally:
        cache.close()


def test_verify_anchors_skips_judged_and_counts_failures(cache, monkeypatch):
    texts = {key: key for key in ("old", "empty", "good", "bad", "invalid", "c")}
    cands = {key: ["c"] for key in ("old", "good", "bad", "invalid")}
    cands["empty"] = []
    cache.record("old", ["c"], [False])
    calls = []
    progress = []

    def runner(prompt):
        calls.append(prompt)
        if "ANCHOR: bad\n" in prompt:
            return None
        if "ANCHOR: invalid\n" in prompt:
            return "invalid JSON"
        return '{"1": "same"}'

    monkeypatch.setattr(lv, "run_haiku", runner)
    assert lv.verify_anchors(texts, cands, cache, progress=lambda *p: progress.append(p)) == {
        "judged": 1, "failed": 2, "skipped": 2,
    }
    assert len(calls) == 3
    assert progress == [(3, 3, 2)]
    assert cache.judged("good")
    assert not cache.judged("bad") and not cache.judged("invalid")


def test_verify_anchors_resumes_after_interrupt(tmp_path, monkeypatch):
    path = tmp_path / "verdicts.db"
    cache = lv.VerdictCache(path)
    texts = {key: key for key in ("a", "b", "c")}
    cands = {"a": ["c"], "b": ["c"]}
    record = cache.record

    def interrupt_after_commit(*args):
        record(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr(cache, "record", interrupt_after_commit)
    with pytest.raises(KeyboardInterrupt):
        lv.verify_anchors(texts, cands, cache, runner=lambda _: '{"1": "same"}')
    committed = {a for a in cands if cache.judged(a)}
    assert len(committed) == 1
    cache.close()
    cache = lv.VerdictCache(path)
    calls = []
    try:
        def runner(prompt):
            calls.append(prompt)
            return '{"1": "same"}'

        assert lv.verify_anchors(texts, cands, cache, runner=runner) == {
            "judged": 1, "failed": 0, "skipped": 1,
        }
        assert len(calls) == 1
        assert all(f"ANCHOR: {a}\n" not in calls[0] for a in committed)
    finally:
        cache.close()


def test_verify_anchors_progress_every_100_and_at_end(cache):
    texts = {str(i): str(i) for i in range(201)}
    cands = {str(i): ["200"] for i in range(200)}
    progress = []
    result = lv.verify_anchors(texts, cands, cache, runner=lambda _: '{"1": "same"}',
                               progress=lambda *p: progress.append(p))
    assert result == {"judged": 200, "failed": 0, "skipped": 0}
    assert progress == [(100, 200, 0), (200, 200, 0)]
    progress.clear()
    lv.verify_anchors(texts, cands, cache, runner=lambda _: pytest.fail("already judged"),
                      progress=lambda *p: progress.append(p))
    assert progress == [(0, 0, 0)]


def test_name_lessons_falls_back_to_centre_sentence():
    lessons = [{"id": "L1", "centre": "a", "members": ["a", "b"]}]
    assert lv.name_lessons(lessons, {"a": "Verify citations. Then dispatch.", "b": "B"},
                           runner=lambda _: None) == {"L1": "Verify citations"}


@pytest.mark.parametrize("reply", ["garbage", "[]", "{}", '{"1": null}',
                                    '{"1": "  "}', json.dumps({"1": "x" * 81})])
def test_name_lessons_invalid_names_use_truncated_fallback(reply):
    lessons = [{"id": "L", "centre": "a", "members": ["a"]}]
    assert lv.name_lessons(lessons, {"a": "x" * 100}, runner=lambda _: reply) == {"L": "x" * 80}


def test_name_lessons_batches_and_limits_examples(monkeypatch):
    lessons = [{"id": str(i), "centre": "a", "members": ["a", "b", "c", "d", "e", "f"]}
               for i in range(41)]
    texts = {key: key.upper() for key in "abcdef"}
    calls = []

    def runner(prompt):
        calls.append(prompt)
        return '```json\n{"1": "  Verify rules  "}\n```'

    monkeypatch.setattr(lv, "run_haiku", runner)
    names = lv.name_lessons(lessons, texts)
    assert len(calls) == 2
    assert calls[0].count("LESSON ") == 40
    assert calls[1].count("LESSON ") == 1
    assert "LESSON 1:\nA\n- B\n- C\n- D\n- E" in calls[1]
    assert "- F" not in calls[0] and "- A" not in calls[0]
    assert names["0"] == names["40"] == "Verify rules"
    assert names["1"] == "A"
    assert lv.name_lessons([], texts, runner=lambda _: pytest.fail("empty batch")) == {}
