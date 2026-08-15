"""Tests for the memory-index vault_doctor check module (#308).

Isolation strategy (mirrors test_vault_doctor_session_coverage.py):
  - HOME is redirected via monkeypatch so ~/.claude/projects lands under
    tmp_path. memory_index.scan() resolves the store root from Path.home(),
    which reads $HOME on POSIX.
  - A vault tree is still created because the dispatcher requires a valid
    --vault, even though this check never reads it.

The core fixture (``_seed_store``) always seeds a clean entry, an orphan and
a dangling pointer together. A check stuck ON flags the clean entry; a check
stuck OFF misses the orphan and the dangling pointer; both fail.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import vault_doctor_checks  # noqa: E402
import vault_doctor_checks.memory_index as mi  # noqa: E402

_SCRIPT = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"

_ROOT_SKIP = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="chmod-based unreadability does not apply to root",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store(env, project_dir: str = "-Users-me-dev-demo") -> Path:
    store = env["projects"] / project_dir / "memory"
    store.mkdir(parents=True, exist_ok=True)
    return store


def _seed_store(env, project_dir: str = "-Users-me-dev-demo") -> Path:
    """Seed a store holding a clean entry, an orphan, and a dangling pointer.

    All three at once, deliberately: a fixture with only orphans cannot fail
    a check that flags everything, and one with only clean entries cannot
    fail a check that flags nothing.
    """
    store = _store(env, project_dir)
    (store / "clean_entry.md").write_text("indexed and reachable\n")
    (store / "orphan_entry.md").write_text("nobody points at me\n")
    (store / "MEMORY.md").write_text(
        "# Memory index\n"
        "- [Clean](clean_entry.md) — the one that is wired up\n"
        "- [Gone](deleted_entry.md) — points at a file that no longer exists\n"
    )
    return store


def _scan(project: str | None = None):
    """Run scan() with the interface's unused vault arguments stubbed."""
    return mi.scan("/unused/vault", "claude-sessions", "claude-insights",
                   mi.DEFAULT_WINDOW_DAYS, project=project)


def _classes(issues) -> dict[str, list[str]]:
    """{signal_class: [note basename, ...]} — the assertion surface."""
    out: dict[str, list[str]] = {}
    for i in issues:
        out.setdefault(i.extra["signal_class"], []).append(Path(i.note_path).name)
    return {k: sorted(v) for k, v in out.items()}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    projects = tmp_path / ".claude" / "projects"
    projects.mkdir(parents=True)
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    (vault / "claude-insights").mkdir(parents=True)
    return {"tmp_path": tmp_path, "projects": projects, "vault": vault}


# ---------------------------------------------------------------------------
# 1. The three-way fixture: exactly the right rows, no more
# ---------------------------------------------------------------------------

class TestThreeWayFixture:

    def test_flags_exactly_the_orphan_and_the_dangling_pointer(self, env):
        _seed_store(env)
        issues = _scan()
        assert _classes(issues) == {
            "orphan-isolated": ["orphan_entry.md"],
            "index-dangling": ["deleted_entry.md"],
        }

    def test_the_clean_entry_is_never_flagged(self, env):
        """Stuck-ON control: the indexed entry must not appear in any row."""
        _seed_store(env)
        flagged = {Path(i.note_path).name for i in _scan()}
        assert "clean_entry.md" not in flagged

    def test_a_fully_indexed_store_is_clean(self, env):
        """Stuck-ON control at store level: zero rows when nothing has drifted."""
        store = _store(env)
        (store / "a.md").write_text("a\n")
        (store / "b.md").write_text("b\n")
        (store / "MEMORY.md").write_text("- [A](a.md) — x\n- [B](b.md) — y\n")
        assert _scan() == []

    def test_orphan_row_carries_an_actionable_proposal(self, env):
        _seed_store(env)
        orphan = next(i for i in _scan()
                      if i.extra["signal_class"] == "orphan-isolated")
        assert "orphan_entry.md" in orphan.proposed_source
        assert orphan.extra["unresolved"] is True
        assert orphan.confidence == 0.0


# ---------------------------------------------------------------------------
# 2. Reachability rules — generous inbound, conservative dangling
# ---------------------------------------------------------------------------

class TestReachabilityRules:

    def test_wikilink_in_the_index_counts_as_reachable(self, env):
        """#308's measured correction: markdown-links-only over-reported orphans."""
        store = _store(env)
        (store / "wiki_target.md").write_text("x\n")
        (store / "MEMORY.md").write_text("see [[wiki_target]] for detail\n")
        assert _scan() == []

    def test_wikilink_with_alias_and_heading_counts(self, env):
        store = _store(env)
        (store / "aliased.md").write_text("x\n")
        (store / "anchored.md").write_text("x\n")
        (store / "MEMORY.md").write_text(
            "[[aliased|Nice Title]] and [[anchored#Section]]\n"
        )
        assert _scan() == []

    def test_bare_filename_mention_counts_as_reachable(self, env):
        store = _store(env)
        (store / "bare_target.md").write_text("x\n")
        (store / "MEMORY.md").write_text("mentioned inline: bare_target.md\n")
        assert _scan() == []

    def test_wikilink_to_a_non_memory_file_is_not_dangling(self, env):
        """The live store's only 'dangling' hit was a wikilink to a VAULT note."""
        store = _store(env)
        (store / "a.md").write_text("x\n")
        (store / "MEMORY.md").write_text(
            "Cross-project: [[2026-05-16-some-vault-note]]\n"
            "- [A](a.md) — x\n"
        )
        assert _scan() == []

    def test_bare_mention_of_a_missing_file_is_not_dangling(self, env):
        """CLAUDE.md / README.md appear in prose constantly — never dangling."""
        store = _store(env)
        (store / "a.md").write_text("x\n")
        (store / "MEMORY.md").write_text(
            "conventions live in CLAUDE.md and README.md\n- [A](a.md) — x\n"
        )
        assert _scan() == []

    def test_markdown_link_to_a_missing_file_is_dangling(self, env):
        """Positive control for the rule above: the explicit form DOES flag."""
        store = _store(env)
        (store / "a.md").write_text("x\n")
        (store / "MEMORY.md").write_text(
            "- [A](a.md) — x\n- [Missing](CLAUDE.md) — x\n"
        )
        assert _classes(_scan()) == {"index-dangling": ["CLAUDE.md"]}

    def test_relative_path_in_a_markdown_link_resolves_by_basename(self, env):
        store = _store(env)
        (store / "a.md").write_text("x\n")
        (store / "MEMORY.md").write_text("- [A](./a.md) — x\n")
        assert _scan() == []

    def test_index_self_reference_is_not_dangling(self, env):
        store = _store(env)
        (store / "a.md").write_text("x\n")
        (store / "MEMORY.md").write_text(
            "this file is MEMORY.md, see [self](MEMORY.md)\n- [A](a.md) — x\n"
        )
        assert _scan() == []


# ---------------------------------------------------------------------------
# 3. Transitive reachability
# ---------------------------------------------------------------------------

class TestTransitiveReachability:

    def test_entry_linked_only_from_an_indexed_entry_is_reachable(self, env):
        store = _store(env)
        (store / "hub.md").write_text("see [[leaf]]\n")
        (store / "leaf.md").write_text("x\n")
        (store / "MEMORY.md").write_text("- [Hub](hub.md) — x\n")
        assert _scan() == []

    def test_entry_linked_only_from_an_orphan_is_unreachable(self, env):
        """Weaker orphan class: inbound link exists, but not from the index."""
        store = _store(env)
        (store / "lost_hub.md").write_text("see [[lost_leaf]]\n")
        (store / "lost_leaf.md").write_text("x\n")
        (store / "MEMORY.md").write_text("# empty index\n")
        assert _classes(_scan()) == {
            "orphan-isolated": ["lost_hub.md"],
            "orphan-unreachable": ["lost_leaf.md"],
        }

    def test_a_link_cycle_among_orphans_still_reports_both(self, env):
        """BFS must terminate and must not launder a cycle into reachability."""
        store = _store(env)
        (store / "ring_a.md").write_text("[[ring_b]]\n")
        (store / "ring_b.md").write_text("[[ring_a]]\n")
        (store / "MEMORY.md").write_text("# empty index\n")
        assert _classes(_scan()) == {
            "orphan-unreachable": ["ring_a.md", "ring_b.md"],
        }

    def test_self_link_does_not_make_an_orphan_look_linked(self, env):
        store = _store(env)
        (store / "narcissus.md").write_text("see narcissus.md\n")
        (store / "MEMORY.md").write_text("# empty index\n")
        assert _classes(_scan()) == {"orphan-isolated": ["narcissus.md"]}


# ---------------------------------------------------------------------------
# 4. Index-missing
# ---------------------------------------------------------------------------

class TestIndexMissing:

    def test_store_with_entries_and_no_index_reports_one_row(self, env):
        store = _store(env)
        for n in ("a.md", "b.md", "c.md"):
            (store / n).write_text("x\n")
        issues = _scan()
        assert _classes(issues) == {"index-missing": ["MEMORY.md"]}
        assert issues[0].extra["entry_count"] == 3

    def test_empty_store_is_silent(self, env):
        _store(env)
        assert _scan() == []

    def test_project_dir_without_a_memory_store_is_skipped(self, env):
        (env["projects"] / "-Users-me-dev-other").mkdir(parents=True)
        _seed_store(env)
        assert {i.project for i in _scan()} == {"-Users-me-dev-demo"}


# ---------------------------------------------------------------------------
# 5. Index size budget — exact-threshold fixtures for the >= boundary
# ---------------------------------------------------------------------------

class TestIndexSizeBudget:

    def _index_of_size(self, env, size: int) -> Path:
        store = _store(env)
        (store / "a.md").write_text("x\n")
        # Pad in BYTES, not characters — the pointer line's em dash is 3
        # UTF-8 bytes, and the check measures st_size.
        head = "- [A](a.md) - x\n".encode("utf-8")
        index = store / "MEMORY.md"
        index.write_bytes(head + b"#" * (size - len(head)))
        assert index.stat().st_size == size
        return index

    def test_one_byte_below_the_soft_limit_is_clean(self, env):
        self._index_of_size(env, mi.INDEX_SIZE_SOFT_LIMIT_BYTES - 1)
        assert _scan() == []

    def test_exactly_the_soft_limit_is_flagged(self, env):
        """`>=` boundary: a wide-gap fixture would pass with `>` too."""
        self._index_of_size(env, mi.INDEX_SIZE_SOFT_LIMIT_BYTES)
        assert _classes(_scan()) == {"index-oversize-soft": ["MEMORY.md"]}

    def test_one_byte_below_the_hard_limit_is_still_soft(self, env):
        self._index_of_size(env, mi.INDEX_SIZE_HARD_LIMIT_BYTES - 1)
        assert _classes(_scan()) == {"index-oversize-soft": ["MEMORY.md"]}

    def test_exactly_the_hard_limit_escalates(self, env):
        self._index_of_size(env, mi.INDEX_SIZE_HARD_LIMIT_BYTES)
        assert _classes(_scan()) == {"index-oversize-hard": ["MEMORY.md"]}

    def test_size_row_carries_the_counts_and_limits(self, env):
        self._index_of_size(env, mi.INDEX_SIZE_HARD_LIMIT_BYTES)
        row = _scan()[0]
        assert row.extra["index_size_bytes"] == mi.INDEX_SIZE_HARD_LIMIT_BYTES
        assert row.extra["soft_limit_bytes"] == mi.INDEX_SIZE_SOFT_LIMIT_BYTES
        assert row.extra["hard_limit_bytes"] == mi.INDEX_SIZE_HARD_LIMIT_BYTES
        assert row.extra["entry_count"] == 1
        assert row.extra["indexed_count"] == 1


# ---------------------------------------------------------------------------
# 6. Unreadable stores and entries
# ---------------------------------------------------------------------------

class TestUnreadable:

    @_ROOT_SKIP
    def test_unreadable_index_raises(self, env):
        store = _seed_store(env)
        (store / "MEMORY.md").chmod(0o000)
        try:
            with pytest.raises(mi.MemoryStoreUnreadable):
                _scan()
        finally:
            (store / "MEMORY.md").chmod(0o600)

    @_ROOT_SKIP
    def test_unreadable_entry_is_a_row_not_a_crash(self, env):
        store = _seed_store(env)
        (store / "clean_entry.md").chmod(0o000)
        try:
            classes = _classes(_scan())
        finally:
            (store / "clean_entry.md").chmod(0o600)
        assert classes["entry-unreadable"] == ["clean_entry.md"]
        # The rest of the store is still scanned.
        assert classes["orphan-isolated"] == ["orphan_entry.md"]
        assert classes["index-dangling"] == ["deleted_entry.md"]


# ---------------------------------------------------------------------------
# 7. Interface contract
# ---------------------------------------------------------------------------

class TestCheckInterface:

    def test_registered_under_its_name(self):
        assert vault_doctor_checks.get_check("memory-index") is mi

    def test_opt_in_excludes_it_from_the_default_sweep(self):
        assert mi.OPT_IN is True
        assert mi not in vault_doctor_checks.all_checks()

    def test_days_is_ignored(self, env):
        """Drift is undated — a 1-day window must not hide a 4-month orphan."""
        _seed_store(env)
        wide = mi.scan("/unused", "s", "i", 9999)
        narrow = mi.scan("/unused", "s", "i", 1)
        assert _classes(wide) == _classes(narrow)

    def test_project_filter_is_a_substring_match_on_the_store_dir(self, env):
        _seed_store(env, "-Users-me-dev-demo")
        _seed_store(env, "-Users-me-dev-other")
        assert {i.project for i in _scan(project="demo")} == {"-Users-me-dev-demo"}
        assert len({i.project for i in _scan()}) == 2

    def test_project_filter_is_case_insensitive(self, env):
        _seed_store(env, "-Users-me-dev-Demo")
        assert _scan(project="DEMO")

    def test_missing_projects_root_is_clean_not_a_crash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _scan() == []

    def test_apply_writes_nothing_and_reports_unresolved(self, env):
        store = _seed_store(env)
        before = {p.name: p.read_bytes() for p in store.glob("*.md")}
        issues = _scan()
        results = mi.apply(issues, str(env["tmp_path"] / "backup"))
        assert results and all(r.status == "unresolved" for r in results)
        assert all(r.error for r in results)
        assert {p.name: p.read_bytes() for p in store.glob("*.md")} == before


# ---------------------------------------------------------------------------
# 8. CLI e2e — the JSON shape is asserted from a real run, not assumed
# ---------------------------------------------------------------------------

class TestCLIE2E:

    def _run(self, env, *args: str) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable, str(_SCRIPT),
            "--vault", str(env["vault"]),
            "--sessions-folder", "claude-sessions",
            "--insights-folder", "claude-insights",
            "--check", "memory-index",
            "--json",
            *args,
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True,
            env={**os.environ, "HOME": str(env["tmp_path"])},
        )

    def test_clean_store_exits_0(self, env):
        store = _store(env)
        (store / "a.md").write_text("x\n")
        (store / "MEMORY.md").write_text("- [A](a.md) — x\n")
        r = self._run(env)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["total_issues"] == 0

    def test_drift_exits_1_with_the_expected_json_shape(self, env):
        _seed_store(env)
        r = self._run(env)
        assert r.returncode == 1, r.stderr

        payload = json.loads(r.stdout)
        assert payload["total_issues"] == 2
        by_class = {row["signal_class"]: row for row in payload["issues"]}
        assert set(by_class) == {"orphan-isolated", "index-dangling"}

        row = by_class["orphan-isolated"]
        assert row["check"] == "memory-index"
        assert row["note_path"].endswith("/memory/orphan_entry.md")
        assert row["project"] == "-Users-me-dev-demo"
        assert row["unresolved"] is True
        assert row["confidence"] == 0.0
        # Filter metadata is conditional and must be absent without the flag.
        assert "min_confidence" not in payload
        assert "crashed_checks" not in payload

    @_ROOT_SKIP
    def test_unreadable_store_exits_2_and_names_the_crashed_check(self, env):
        store = _seed_store(env)
        (store / "MEMORY.md").chmod(0o000)
        try:
            r = self._run(env)
        finally:
            (store / "MEMORY.md").chmod(0o600)
        assert r.returncode == 2, r.stderr
        assert json.loads(r.stdout)["crashed_checks"] == ["memory-index"]
        assert "MemoryStoreUnreadable" in r.stderr

    def test_min_confidence_hides_every_row(self, env):
        """Report-only rows are confidence 0.0 — documented consequence."""
        _seed_store(env)
        r = self._run(env, "--min-confidence", "0.5")
        assert r.returncode == 0, r.stderr
        payload = json.loads(r.stdout)
        assert payload["total_issues"] == 0
        assert payload["dropped_by_confidence"] == 2

    def test_not_run_in_the_default_sweep(self, env):
        """OPT_IN: a bare sweep over a drifted store must report nothing."""
        _seed_store(env)
        cmd = [
            sys.executable, str(_SCRIPT),
            "--vault", str(env["vault"]),
            "--sessions-folder", "claude-sessions",
            "--insights-folder", "claude-insights",
            "--json",
        ]
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            env={**os.environ, "HOME": str(env["tmp_path"])},
        )
        rows = json.loads(r.stdout)["issues"]
        assert [x for x in rows if x["check"] == "memory-index"] == []
