"""Tests for hooks/memory_sources.py (#396): the one host-specific place."""

from __future__ import annotations

import os
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


# --- review fixes (#399): symlinked projects, dedupe, visible errors ----------

_ROOT_SKIP = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                                reason="root ignores file permissions")


def test_symlinked_project_dir_is_skipped(home, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "memory").mkdir()
    (outside / "memory" / "s.md").write_text("secret")
    (_store(home, "p1") / "a.md").write_text("a")
    (home / ".claude" / "projects" / "evil").symlink_to(outside)
    assert [ms.memory_name(p) for p in ms.memory_sources("claude-code")] == ["p1/a.md"]


def test_alias_project_lists_the_real_files_once(home):
    (_store(home, "p1") / "a.md").write_text("a")
    root = home / ".claude" / "projects"
    (root / "alias").symlink_to(root / "p1")
    assert [ms.memory_name(p) for p in ms.memory_sources("claude-code")] == ["p1/a.md"]


def test_output_is_deduped_by_resolved_path(home, monkeypatch):
    # The symlink skip already stops an alias; this proves the dedupe on its
    # own by letting every symlink through.
    (_store(home, "p1") / "a.md").write_text("a")
    root = home / ".claude" / "projects"
    (root / "alias").symlink_to(root / "p1")
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    got = ms.memory_sources("claude-code")
    assert got == [(root / "p1" / "memory" / "a.md").resolve()]


@_ROOT_SKIP
def test_unreadable_memory_dir_is_an_error_and_the_rest_still_list(home):
    a = _store(home, "a")
    b = _store(home, "b")
    (a / "x.md").write_text("x")
    (b / "y.md").write_text("y")
    a.chmod(0o000)
    errors: list = []
    try:
        got = ms.memory_sources("claude-code", errors=errors)
    finally:
        a.chmod(0o700)
    assert [ms.memory_name(p) for p in got] == ["b/y.md"]
    assert [e["path"] for e in errors] == [str(a)] and errors[0]["error"]


@_ROOT_SKIP
def test_unreadable_project_dir_is_an_error(home):
    (_store(home, "a") / "x.md").write_text("x")
    (_store(home, "b") / "y.md").write_text("y")
    proj = home / ".claude" / "projects" / "a"
    proj.chmod(0o000)
    errors: list = []
    try:
        got = ms.memory_sources("claude-code", errors=errors)
    finally:
        proj.chmod(0o700)
    assert [ms.memory_name(p) for p in got] == ["b/y.md"]
    assert [e["path"] for e in errors] == [str(proj / "memory")]


@_ROOT_SKIP
def test_unlistable_projects_root_is_an_error(home):
    (_store(home, "a") / "x.md").write_text("x")
    root = home / ".claude" / "projects"
    root.chmod(0o000)
    errors: list = []
    try:
        assert ms.memory_sources("claude-code", errors=errors) == []
    finally:
        root.chmod(0o700)
    assert [e["path"] for e in errors] == [str(root)]
    assert ms.failed_scopes(errors) == (True, set(), set())


def test_missing_root_is_not_an_error(home):
    errors: list = []
    assert ms.memory_sources("claude-code", errors=errors) == [] and errors == []


def test_unresolvable_file_is_an_error(home, monkeypatch):
    a = _store(home, "a")
    (a / "x.md").write_text("x")
    (a / "y.md").write_text("y")
    real_resolve = Path.resolve

    def resolve(self, strict=False):
        if self.name == "x.md":
            raise OSError("boom")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    errors: list = []
    got = ms.memory_sources("claude-code", errors=errors)
    assert [ms.memory_name(p) for p in got] == ["a/y.md"]
    assert errors == [{"path": str(a / "x.md"), "error": "boom"}]
    assert ms.failed_scopes(errors) == (False, set(), {"a/x.md"})


def test_failed_scopes_classifies_project_errors(home):
    root = ms._projects_root()
    errs = [{"path": str(root / "p" / "memory"), "error": "x"},
            {"path": "/somewhere/else", "error": "y"}]
    assert ms.failed_scopes(errs) == (False, {"p"}, set())


def test_symlinked_project_pointing_inside_the_root_is_skipped(home):
    # Containment and dedupe both pass this one: the target sits under the
    # projects root and is not listed any other way. Only the skip stops it.
    p1 = _store(home, "p1")
    (p1 / "a.md").write_text("a")
    hidden = p1 / "sub" / "memory"
    hidden.mkdir(parents=True)
    (hidden / "s.md").write_text("nested")
    (home / ".claude" / "projects" / "evil").symlink_to(p1 / "sub")
    assert [ms.memory_name(p) for p in ms.memory_sources("claude-code")] == ["p1/a.md"]
