"""Tests for hooks/memory_sources.py (#396): the one host-specific place."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import memory_sources as ms  # noqa: E402


def _store(home: Path, project: str) -> Path:
    mem = home / ".claude" / "projects" / project / "memory"
    mem.mkdir(parents=True)
    return mem


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_lists_memory_files_and_skips_the_index(home):
    a = _store(home, "a")
    b = _store(home, "b")
    (a / "x.md").write_text("x")
    (a / "MEMORY.md").write_text("- [x](x.md)")
    (b / "y.md").write_text("y")
    got = ms.memory_sources("claude-code")
    assert [ms.memory_name(p) for p in got] == ["a/x.md", "b/y.md"]
    assert all(p.is_absolute() for p in got)


def test_skips_symlinked_files_and_dirs(home, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.md").write_text("s")
    a = _store(home, "a")
    (a / "real.md").write_text("r")
    (a / "link.md").symlink_to(outside / "secret.md")
    proj = home / ".claude" / "projects" / "b"
    proj.mkdir(parents=True)
    (proj / "memory").symlink_to(outside)
    assert [ms.memory_name(p) for p in ms.memory_sources("claude-code")] == ["a/real.md"]


def test_skips_non_md_and_nested_files(home):
    a = _store(home, "a")
    (a / "notes.txt").write_text("t")
    (a / "sub").mkdir()
    (a / "sub" / "z.md").write_text("z")
    (a / "dir.md").mkdir()
    (a / "ok.md").write_text("o")
    assert [ms.memory_name(p) for p in ms.memory_sources("claude-code")] == ["a/ok.md"]


def test_other_hosts_and_missing_root_give_nothing(home):
    assert ms.memory_sources("claude-code") == []
    (_store(home, "a") / "x.md").write_text("x")
    assert ms.memory_sources("codex") == []
    assert ms.memory_sources("something-else") == []


def test_one_unreadable_project_does_not_hide_the_rest(home):
    a = _store(home, "a")
    b = _store(home, "b")
    (a / "x.md").write_text("x")
    (b / "y.md").write_text("y")
    a.chmod(0o000)
    try:
        assert [ms.memory_name(p) for p in ms.memory_sources("claude-code")] == ["b/y.md"]
    finally:
        a.chmod(0o700)


@pytest.mark.parametrize("value,expected", [("1", "codex"), ("", "claude-code"), ("  ", "claude-code")])
def test_detect_host(monkeypatch, value, expected):
    for name in ms._CODEX_HOST_MARKERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", value)
    assert ms.detect_host() == expected


def test_memory_name_is_project_and_file(home):
    a = _store(home, "-Users-x-proj")
    (a / "feedback_y.md").write_text("y")
    [p] = ms.memory_sources("claude-code")
    assert ms.memory_name(p) == "-Users-x-proj/feedback_y.md"
