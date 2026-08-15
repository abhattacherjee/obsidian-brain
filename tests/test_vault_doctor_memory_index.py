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
import time
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
        (store / "anchor.md").write_text("x\n")
        (store / "lost_hub.md").write_text("see [[lost_leaf]]\n")
        (store / "lost_leaf.md").write_text("x\n")
        (store / "MEMORY.md").write_text("- [Anchor](anchor.md) — x\n")
        assert _classes(_scan()) == {
            "orphan-isolated": ["lost_hub.md"],
            "orphan-unreachable": ["lost_leaf.md"],
        }

    def test_a_link_cycle_among_orphans_still_reports_both(self, env):
        """BFS must terminate and must not launder a cycle into reachability."""
        store = _store(env)
        (store / "anchor.md").write_text("x\n")
        (store / "ring_a.md").write_text("[[ring_b]]\n")
        (store / "ring_b.md").write_text("[[ring_a]]\n")
        (store / "MEMORY.md").write_text("- [Anchor](anchor.md) — x\n")
        assert _classes(_scan()) == {
            "orphan-unreachable": ["ring_a.md", "ring_b.md"],
        }

    def test_self_link_does_not_make_an_orphan_look_linked(self, env):
        store = _store(env)
        (store / "anchor.md").write_text("x\n")
        (store / "narcissus.md").write_text("see narcissus.md\n")
        (store / "MEMORY.md").write_text("- [Anchor](anchor.md) — x\n")
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
        head = "- [A](a.md) — x\n".encode("utf-8")
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
            issues = _scan()
        finally:
            (store / "clean_entry.md").chmod(0o600)
        classes = _classes(issues)
        assert classes["entry-unreadable"] == ["clean_entry.md"]
        # The rest of the store is still scanned. The orphan is reported as
        # -unreachable, not -isolated: one entry could not be read, so its
        # outbound links are unknown and "nothing links this" is unprovable.
        assert classes["orphan-unreachable"] == ["orphan_entry.md"]
        assert "orphan-isolated" not in classes
        assert classes["index-dangling"] == ["deleted_entry.md"]
        orphan = next(i for i in issues
                      if Path(i.note_path).name == "orphan_entry.md")
        assert "provisional" in orphan.reason


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
        """Drift is undated — a 1-day window must not hide a 4-month orphan.

        The fixture is backdated 120 days on purpose. Seconds-old files
        would survive any mtime window, so `wide == narrow` would hold even
        if someone added a --days filter, and the test would prove nothing.
        """
        store = _seed_store(env)
        old = time.time() - 120 * 86400
        for p in store.glob("*.md"):
            os.utime(p, (old, old))
        expected = {
            "orphan-isolated": ["orphan_entry.md"],
            "index-dangling": ["deleted_entry.md"],
        }
        assert _classes(mi.scan("/unused", "s", "i", 9999)) == expected
        assert _classes(mi.scan("/unused", "s", "i", 1)) == expected

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


# ---------------------------------------------------------------------------
# 9. The store itself is unreadable (F1) — the guard the old glob() bypassed
# ---------------------------------------------------------------------------

class TestStoreUnreadable:

    @_ROOT_SKIP
    def test_unreadable_store_directory_raises(self, env):
        """Path.glob() swallows the scandir OSError; iterdir() raises it.

        With glob() the listing came back empty, the store looked clean and
        the run exited 0 while holding a real orphan.
        """
        store = _seed_store(env)
        store.chmod(0o000)
        try:
            with pytest.raises(mi.MemoryStoreUnreadable):
                _scan()
        finally:
            store.chmod(0o700)

    @_ROOT_SKIP
    def test_unreadable_store_directory_exits_2_through_the_cli(self, env):
        store = _seed_store(env)
        store.chmod(0o000)
        cmd = [
            sys.executable, str(_SCRIPT),
            "--vault", str(env["vault"]),
            "--sessions-folder", "claude-sessions",
            "--insights-folder", "claude-insights",
            "--check", "memory-index", "--json",
        ]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                env={**os.environ, "HOME": str(env["tmp_path"])},
            )
        finally:
            store.chmod(0o700)
        assert r.returncode == 2, r.stderr
        assert json.loads(r.stdout)["crashed_checks"] == ["memory-index"]
        assert "MemoryStoreUnreadable" in r.stderr


# ---------------------------------------------------------------------------
# 10. A store with no entry files still has an index worth checking (F2)
# ---------------------------------------------------------------------------

class TestEntrylessStore:

    def test_no_entries_but_a_drifted_index_still_reports(self, env):
        """`if not entries: return []` used to drop both of these rows."""
        store = _store(env)
        index = store / "MEMORY.md"
        head = (
            "- [Gone](deleted_one.md) — x\n"
            "- [AlsoGone](deleted_two.md) — x\n"
        ).encode("utf-8")
        index.write_bytes(head + b"#" * (30_042 - len(head)))
        assert _classes(_scan()) == {
            "index-dangling": ["deleted_one.md", "deleted_two.md"],
            "index-oversize-hard": ["MEMORY.md"],
        }

    def test_no_entries_and_no_index_is_still_silent(self, env):
        """Every project dir carries an empty memory/ — it must stay quiet."""
        _store(env)
        assert _scan() == []

    def test_no_entries_and_a_clean_index_is_silent(self, env):
        store = _store(env)
        (store / "MEMORY.md").write_text("# Memory index\n")
        assert _scan() == []


# ---------------------------------------------------------------------------
# 11. Encoding (F3)
# ---------------------------------------------------------------------------

class TestEncoding:

    def test_utf16_index_raises_instead_of_orphaning_the_whole_store(self, env):
        """errors='replace' turned a UTF-16 index into a storeful of orphans."""
        store = _store(env)
        (store / "a.md").write_text("x\n")
        (store / "b.md").write_text("x\n")
        (store / "MEMORY.md").write_bytes(
            "- [A](a.md) — x\n- [B](b.md) — y\n".encode("utf-16")
        )
        with pytest.raises(mi.MemoryStoreUnreadable):
            _scan()

    def test_latin1_index_raises_rather_than_inventing_two_wrong_rows(self, env):
        """Replacement here produced a false orphan AND a false dangling row."""
        store = _store(env)
        (store / "café.md").write_text("x\n")
        (store / "MEMORY.md").write_bytes("- [C](café.md) - x\n".encode("latin-1"))
        with pytest.raises(mi.MemoryStoreUnreadable):
            _scan()

    def test_undecodable_entry_is_reported_and_the_store_still_scans(self, env):
        store = _store(env)
        (store / "bad.md").write_bytes(b"links to [[a]] but byte \xff is not utf-8\n")
        (store / "a.md").write_text("x\n")
        (store / "MEMORY.md").write_text("- [Bad](bad.md) — x\n")
        issues = _scan()
        classes = _classes(issues)
        assert classes["entry-undecodable"] == ["bad.md"]
        row = next(i for i in issues
                   if i.extra["signal_class"] == "entry-undecodable")
        assert row.extra["byte_offset"] == 24
        assert "24" in row.current_source
        # The rest of the store is still scanned: a.md is reachable through
        # the replaced-byte read, so it is not reported.
        assert "orphan-isolated" not in classes


# ---------------------------------------------------------------------------
# 12. One bad store must not empty the whole report (F4)
# ---------------------------------------------------------------------------

class TestPartialStoreFailure:

    def _bad_index(self, env, project_dir: str) -> Path:
        store = _store(env, project_dir)
        (store / "a.md").write_text("x\n")
        (store / "MEMORY.md").write_bytes("- [A](a.md)\n".encode("utf-16"))
        return store

    def test_other_stores_survive_and_the_bad_one_gets_a_row(self, env):
        _seed_store(env, "-Users-me-dev-aaa")
        self._bad_index(env, "-Users-me-dev-bbb")
        _seed_store(env, "-Users-me-dev-ccc")
        issues = _scan()
        classes = _classes(issues)
        assert classes["store-unreadable"] == ["memory"]
        assert classes["orphan-isolated"] == ["orphan_entry.md"] * 2
        assert classes["index-dangling"] == ["deleted_entry.md"] * 2
        bad = next(i for i in issues
                   if i.extra["signal_class"] == "store-unreadable")
        assert bad.project == "-Users-me-dev-bbb"
        assert "-Users-me-dev-bbb" in bad.note_path
        assert bad.extra["unresolved"] is True
        assert bad.confidence == 0.0

    def test_every_store_unreadable_still_raises(self, env):
        self._bad_index(env, "-Users-me-dev-aaa")
        self._bad_index(env, "-Users-me-dev-bbb")
        with pytest.raises(mi.MemoryStoreUnreadable):
            _scan()

    def test_partial_failure_exits_1_through_the_cli(self, env):
        _seed_store(env, "-Users-me-dev-aaa")
        self._bad_index(env, "-Users-me-dev-bbb")
        cmd = [
            sys.executable, str(_SCRIPT),
            "--vault", str(env["vault"]),
            "--sessions-folder", "claude-sessions",
            "--insights-folder", "claude-insights",
            "--check", "memory-index", "--json",
        ]
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            env={**os.environ, "HOME": str(env["tmp_path"])},
        )
        assert r.returncode == 1, r.stderr
        payload = json.loads(r.stdout)
        assert "crashed_checks" not in payload
        assert {row["signal_class"] for row in payload["issues"]} == {
            "orphan-isolated", "index-dangling", "store-unreadable",
        }


# ---------------------------------------------------------------------------
# 13. Regex cost (F5)
# ---------------------------------------------------------------------------

class TestRegexCost:

    def test_link_targets_stays_fast_on_a_large_entry(self):
        """The unbounded runs this replaced were quadratic.

        Measured through _link_targets before the bound: 10 KB 0.13 s,
        20 KB 0.52 s, 40 KB 2.1 s, 80 KB 8.4 s — 4x per doubling. The
        bounded regexes do this input in ~0.00 s, so the 2 s ceiling has
        roughly 36x headroom and cannot flake; it only trips if an
        unbounded run comes back.
        """
        start = time.perf_counter()
        mi._link_targets("a" * 80_000)
        assert time.perf_counter() - start < 2.0

    def test_link_targets_stays_fast_on_a_run_of_open_link_brackets(self):
        """Same guard for _MD_LINK_RE, which was quadratic too.

        `[^)\\s]+` used to consume the rest of the input from every `](`,
        so this 90 KB input took 4.3 s; bounded it takes 0.03 s.
        """
        start = time.perf_counter()
        mi._link_targets("](a" * 30_000)
        assert time.perf_counter() - start < 2.0

    def test_bare_mention_inside_a_path_still_counts(self, env):
        """_BARE_MD_RE is deliberately unanchored — see the comment on it."""
        store = _store(env)
        (store / "bare_target.md").write_text("x\n")
        (store / "MEMORY.md").write_text(
            "superseded by docs/archive/bare_target.md\n"
        )
        assert _scan() == []


# ---------------------------------------------------------------------------
# 14. Case-only mismatch (F6)
# ---------------------------------------------------------------------------

class TestCaseMismatch:

    def test_case_only_mismatch_is_its_own_class_not_a_double_false_positive(self, env):
        """On macOS the link resolves; on Linux it breaks. Neither is drift."""
        store = _store(env)
        (store / "mynote.md").write_text("x\n")
        (store / "MEMORY.md").write_text("- [C](MyNote.md) — x\n")
        issues = _scan()
        assert _classes(issues) == {"index-case-mismatch": ["MyNote.md"]}
        row = issues[0]
        assert "MyNote.md" in row.reason and "mynote.md" in row.reason
        assert "case-sensitive" in row.reason
        assert row.extra["entry_names"] == ["mynote.md"]

    def test_a_genuinely_missing_target_is_still_dangling(self, env):
        """Negative control: case folding must not swallow real dangling rows."""
        store = _store(env)
        (store / "mynote.md").write_text("x\n")
        (store / "MEMORY.md").write_text(
            "- [C](mynote.md) — x\n- [G](othernote.md) — x\n"
        )
        assert _classes(_scan()) == {"index-dangling": ["othernote.md"]}

    def test_a_case_matched_entry_can_reach_others(self, env):
        """The pointer works on the machine that wrote it, so the BFS uses it."""
        store = _store(env)
        (store / "hub.md").write_text("see [[leaf]]\n")
        (store / "leaf.md").write_text("x\n")
        (store / "MEMORY.md").write_text("- [H](Hub.md) — x\n")
        assert _classes(_scan()) == {"index-case-mismatch": ["Hub.md"]}


# ---------------------------------------------------------------------------
# 15. Non-child link targets (F8)
# ---------------------------------------------------------------------------

class TestNonChildLinkTargets:

    def test_urls_parents_and_subdirs_are_not_dangling(self, env):
        """A top-level entry is required here — without one the entry-less
        early return used to hide the phantom rows."""
        store = _store(env)
        (store / "a.md").write_text("x\n")
        (store / "sub").mkdir()
        (store / "sub" / "deep.md").write_text("x\n")
        (store / "MEMORY.md").write_text(
            "- [A](a.md) — x\n"
            "- [Spec](https://github.com/o/r/blob/main/README.md) — x\n"
            "- [Up](../outside.md) — x\n"
            "- [S](sub/deep.md) — x\n"
        )
        assert _scan() == []

    def test_a_subdirectory_named_like_an_entry_is_not_an_entry(self, env):
        """`p.is_file()`: without it the directory reads as an unreadable entry."""
        store = _store(env)
        (store / "a.md").write_text("x\n")
        (store / "notes.md").mkdir()
        (store / "MEMORY.md").write_text("- [A](a.md) — x\n")
        assert _scan() == []

    def test_index_pointer_at_a_broken_symlink_is_dangling(self, env):
        """A deleted file is a dangling pointer, not a permissions problem."""
        store = _store(env)
        (store / "a.md").write_text("x\n")
        (store / "ghost.md").symlink_to(store / "vanished.md")
        (store / "MEMORY.md").write_text(
            "- [A](a.md) — x\n- [G](ghost.md) — x\n"
        )
        assert _classes(_scan()) == {"index-dangling": ["ghost.md"]}


# ---------------------------------------------------------------------------
# 16. Subdirectory wikilinks (F11)
# ---------------------------------------------------------------------------

class TestWikilinkNormalisation:

    def test_wikilink_with_a_subdirectory_reaches_the_entry(self, env):
        """Markdown links were basename-normalised; wikilinks were not."""
        store = _store(env)
        (store / "a.md").write_text("x\n")
        (store / "MEMORY.md").write_text("archived: [[archive/a]]\n")
        assert _scan() == []

    def test_wikilink_with_a_subdirectory_and_an_alias_reaches_the_entry(self, env):
        store = _store(env)
        (store / "a.md").write_text("x\n")
        (store / "MEMORY.md").write_text("[[archive/a|Old A]]\n")
        assert _scan() == []


# ---------------------------------------------------------------------------
# 17. An unreadable entry must not invent orphans downstream of it (F12)
# ---------------------------------------------------------------------------

class TestUnreadableEntryDownstream:

    @_ROOT_SKIP
    def test_leaf_of_an_unreadable_hub_is_not_called_isolated(self, env):
        """hub.md links leaf.md — the check simply could not read the link."""
        store = _store(env)
        (store / "hub.md").write_text("see [[leaf]]\n")
        (store / "leaf.md").write_text("x\n")
        (store / "MEMORY.md").write_text("- [Hub](hub.md) — x\n")
        (store / "hub.md").chmod(0o000)
        try:
            issues = _scan()
        finally:
            (store / "hub.md").chmod(0o600)
        classes = _classes(issues)
        assert classes["entry-unreadable"] == ["hub.md"]
        assert classes["orphan-unreachable"] == ["leaf.md"]
        assert "orphan-isolated" not in classes
        leaf = next(i for i in issues if Path(i.note_path).name == "leaf.md")
        assert "1 entry file(s)" in leaf.reason
        assert "provisional" in leaf.reason

    def test_an_undecodable_entry_also_downgrades_the_verdict(self, env):
        store = _store(env)
        (store / "hub.md").write_bytes(b"see [[leaf]] \xff\n")
        (store / "leaf.md").write_text("x\n")
        (store / "loner.md").write_text("x\n")
        (store / "MEMORY.md").write_text("- [Hub](hub.md) — x\n")
        classes = _classes(_scan())
        assert classes["entry-undecodable"] == ["hub.md"]
        assert "orphan-isolated" not in classes
        assert classes["orphan-unreachable"] == ["loner.md"]


# ---------------------------------------------------------------------------
# 18. TOCTOU: an entry deleted between the listing and the read (F7)
# ---------------------------------------------------------------------------

class TestEntryVanishesMidScan:

    def test_a_vanished_entry_produces_no_rows_at_all(self, env, monkeypatch):
        """Reporting it would blame permissions and orphan a file that is gone."""
        store = _store(env)
        (store / "keeper.md").write_text("x\n")
        (store / "vanisher.md").write_text("x\n")
        (store / "MEMORY.md").write_text("- [K](keeper.md) — x\n")

        real_bytes = Path.read_bytes
        real_text = Path.read_text

        def _vanish(path: Path) -> None:
            if path.name == "vanisher.md" and path.exists():
                path.unlink()

        def racy_bytes(self, *a, **kw):
            _vanish(self)
            return real_bytes(self, *a, **kw)

        def racy_text(self, *a, **kw):
            _vanish(self)
            return real_text(self, *a, **kw)

        monkeypatch.setattr(Path, "read_bytes", racy_bytes)
        monkeypatch.setattr(Path, "read_text", racy_text)
        assert _scan() == []


# ---------------------------------------------------------------------------
# 19. Store walk and console output (F9, F14)
# ---------------------------------------------------------------------------

class TestStoreWalk:

    def test_a_memory_path_that_is_a_regular_file_is_named_on_stderr(self, env, capsys):
        proj = env["projects"] / "-Users-me-dev-bogus"
        proj.mkdir(parents=True)
        (proj / "memory").write_text("this should have been a directory\n")
        assert _scan() == []
        err = capsys.readouterr().err
        assert "is not a directory" in err
        assert "scanned 0 memory store(s)" in err

    def test_a_memory_symlink_to_a_real_directory_is_scanned(self, env, tmp_path):
        """Plausible setup: the store lives elsewhere and memory/ points at it."""
        real = tmp_path / "elsewhere"
        real.mkdir()
        (real / "anchor.md").write_text("x\n")
        (real / "orphan_entry.md").write_text("x\n")
        (real / "MEMORY.md").write_text("- [Anchor](anchor.md) — x\n")
        proj = env["projects"] / "-Users-me-dev-linked"
        proj.mkdir(parents=True)
        (proj / "memory").symlink_to(real, target_is_directory=True)
        assert _classes(_scan()) == {"orphan-isolated": ["orphan_entry.md"]}

    def test_stderr_summary_counts_stores_issues_and_the_project_filter(self, env, capsys):
        """The summary line is this check's only console output."""
        _seed_store(env, "-Users-me-dev-demo")
        _seed_store(env, "-Users-me-dev-other")
        _scan(project="demo")
        err = capsys.readouterr().err
        assert "scanned 1 memory store(s)" in err
        assert "(1 filtered out by --project demo)" in err
        assert "2 issue(s)" in err

    def test_project_filter_ignores_surrounding_whitespace(self, env):
        _seed_store(env, "-Users-me-dev-demo")
        assert {i.project for i in _scan(project="  demo  ")} == {
            "-Users-me-dev-demo"
        }


# ---------------------------------------------------------------------------
# 20. CLI e2e for --apply and --project (F15)
# ---------------------------------------------------------------------------

class TestCLIApplyAndProject:

    def _run(self, env, *args: str) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable, str(_SCRIPT),
            "--vault", str(env["vault"]),
            "--sessions-folder", "claude-sessions",
            "--insights-folder", "claude-insights",
            "--check", "memory-index", "--json",
            *args,
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True,
            env={**os.environ, "HOME": str(env["tmp_path"])},
        )

    def test_apply_yes_changes_nothing_in_the_store(self, env):
        """Report-only: --apply must leave every memory file byte-identical."""
        store = _seed_store(env)
        before = {p.name: p.read_bytes() for p in sorted(store.iterdir())}
        r = self._run(env, "--apply", "--yes")
        assert r.returncode == 1, r.stderr
        assert {p.name: p.read_bytes() for p in sorted(store.iterdir())} == before

    def test_project_filter_through_the_cli(self, env):
        _seed_store(env, "-Users-me-dev-demo")
        _seed_store(env, "-Users-me-dev-other")
        r = self._run(env, "--project", "demo")
        assert r.returncode == 1, r.stderr
        payload = json.loads(r.stdout)
        assert {row["project"] for row in payload["issues"]} == {
            "-Users-me-dev-demo"
        }
        assert payload["total_issues"] == 2


# ---------------------------------------------------------------------------
# 21. The extras must survive the trip through --json (F16)
# ---------------------------------------------------------------------------

class TestJSONExtras:
    """In-process Issue.extra assertions elsewhere pass on a payload no
    consumer receives — _issue_row drops every key it does not whitelist.
    skills/vault-doctor/SKILL.md drives this check through --json."""

    def _run(self, env, *args: str) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable, str(_SCRIPT),
            "--vault", str(env["vault"]),
            "--sessions-folder", "claude-sessions",
            "--insights-folder", "claude-insights",
            "--check", "memory-index", "--json",
            *args,
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True,
            env={**os.environ, "HOME": str(env["tmp_path"])},
        )

    def test_the_oversize_row_carries_its_counts_and_limits_in_json(self, env):
        store = _store(env)
        (store / "a.md").write_text("x\n")
        head = "- [A](a.md) — x\n".encode("utf-8")
        (store / "MEMORY.md").write_bytes(
            head + b"#" * (mi.INDEX_SIZE_HARD_LIMIT_BYTES - len(head))
        )
        r = self._run(env)
        assert r.returncode == 1, r.stderr
        row = json.loads(r.stdout)["issues"][0]
        assert row["index_size_bytes"] == mi.INDEX_SIZE_HARD_LIMIT_BYTES
        assert row["soft_limit_bytes"] == mi.INDEX_SIZE_SOFT_LIMIT_BYTES
        assert row["hard_limit_bytes"] == mi.INDEX_SIZE_HARD_LIMIT_BYTES
        assert row["entry_count"] == 1
        assert row["indexed_count"] == 1

    def test_index_missing_carries_its_entry_count_in_json(self, env):
        store = _store(env)
        for n in ("a.md", "b.md", "c.md"):
            (store / n).write_text("x\n")
        row = json.loads(self._run(env).stdout)["issues"][0]
        assert row["signal_class"] == "index-missing"
        assert row["entry_count"] == 3

    def test_dangling_carries_the_index_path_in_json(self, env):
        _seed_store(env)
        rows = {r["signal_class"]: r for r in json.loads(self._run(env).stdout)["issues"]}
        assert rows["index-dangling"]["index_path"].endswith("/memory/MEMORY.md")

    def test_store_unreadable_carries_the_underlying_error_in_json(self, env):
        """F4's row is useless without it: the class alone says nothing."""
        _seed_store(env, "-Users-me-dev-aaa")
        bad = _store(env, "-Users-me-dev-bbb")
        (bad / "a.md").write_text("x\n")
        (bad / "MEMORY.md").write_bytes("- [A](a.md)\n".encode("utf-16"))
        rows = {r["signal_class"]: r for r in json.loads(self._run(env).stdout)["issues"]}
        assert "codec can't decode" in rows["store-unreadable"]["read_error"]
        assert "MEMORY.md" in rows["store-unreadable"]["read_error"]

    def test_undecodable_entry_carries_its_byte_offset_in_json(self, env):
        store = _store(env)
        (store / "bad.md").write_bytes(b"x \xff\n")
        (store / "MEMORY.md").write_text("- [Bad](bad.md) — x\n")
        rows = {r["signal_class"]: r for r in json.loads(self._run(env).stdout)["issues"]}
        assert rows["entry-undecodable"]["byte_offset"] == 2

    def test_case_mismatch_carries_both_spellings_in_json(self, env):
        store = _store(env)
        (store / "mynote.md").write_text("x\n")
        (store / "MEMORY.md").write_text("- [C](MyNote.md) — x\n")
        row = json.loads(self._run(env).stdout)["issues"][0]
        assert row["signal_class"] == "index-case-mismatch"
        assert row["entry_names"] == ["mynote.md"]

    def test_other_checks_row_shape_is_unchanged(self, env):
        """The new keys are conditional: a check that sets none gets the
        prior schema exactly."""
        note = env["vault"] / "claude-sessions" / "2026-08-15-demo.md"
        note.write_text(
            "---\nproject: demo\nsource_session: /nope/missing.jsonl\n"
            "date: 2026-08-15\n---\n\nbody\n"
        )
        cmd = [
            sys.executable, str(_SCRIPT),
            "--vault", str(env["vault"]),
            "--sessions-folder", "claude-sessions",
            "--insights-folder", "claude-insights",
            "--check", "source-sessions", "--json",
        ]
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            env={**os.environ, "HOME": str(env["tmp_path"])},
        )
        new_keys = {
            "entry_count", "indexed_count", "index_size_bytes",
            "soft_limit_bytes", "hard_limit_bytes", "index_path",
            "entry_names", "byte_offset", "read_error",
        }
        for row in json.loads(r.stdout)["issues"]:
            assert new_keys.isdisjoint(row), row


# ---------------------------------------------------------------------------
# 22. An unreadable project directory (F17)
# ---------------------------------------------------------------------------

class TestUnreadableProjectDir:

    @_ROOT_SKIP
    def test_an_unreadable_project_dir_is_counted_not_skipped(self, env):
        """Probed with os.stat: on Python 3.13, Path.is_dir() raises
        PermissionError here and crashes the whole check."""
        _seed_store(env, "-Users-me-dev-good")
        bad = _store(env, "-Users-me-dev-bad")
        (bad / "a.md").write_text("x\n")
        bad.parent.chmod(0o000)
        try:
            issues = _scan()
        finally:
            bad.parent.chmod(0o700)
        classes = _classes(issues)
        assert classes["store-unreadable"] == ["memory"]
        # The readable store's findings survive alongside it.
        assert classes["orphan-isolated"] == ["orphan_entry.md"]
        row = next(i for i in issues
                   if i.extra["signal_class"] == "store-unreadable")
        assert row.project == "-Users-me-dev-bad"

    @_ROOT_SKIP
    def test_the_store_count_includes_the_unreadable_project_dir(self, env, capsys):
        bad = _store(env, "-Users-me-dev-bad")
        (bad / "a.md").write_text("x\n")
        bad.parent.chmod(0o000)
        try:
            with pytest.raises(mi.MemoryStoreUnreadable):
                _scan()
        finally:
            bad.parent.chmod(0o700)


# ---------------------------------------------------------------------------
# 23. Traversal order (F18)
# ---------------------------------------------------------------------------

class TestTraversalOrder:

    def test_a_long_chain_is_fully_reachable(self, env):
        """BFS or DFS, the reachable set is the same — this pins that it
        terminates and reaches the far end of a deep chain."""
        store = _store(env)
        names = [f"n{i}.md" for i in range(50)]
        for i, name in enumerate(names):
            nxt = f"[[n{i + 1}]]\n" if i + 1 < len(names) else "end\n"
            (store / name).write_text(nxt)
        (store / "MEMORY.md").write_text("- [Head](n0.md) — x\n")
        assert _scan() == []


# ---------------------------------------------------------------------------
# 24. A whitespace-only --project must not act as "match everything" (F19)
# ---------------------------------------------------------------------------

class TestBlankProjectFilter:

    def test_a_whitespace_only_project_filter_is_ignored(self, env, capsys):
        """`"  ".strip()` is "", and "" is a substring of every name."""
        _seed_store(env, "-Users-me-dev-demo")
        _seed_store(env, "-Users-me-dev-other")
        assert len({i.project for i in _scan(project="  ")}) == 2
        assert "filtered out" not in capsys.readouterr().err

    def test_a_real_project_filter_still_filters(self, env):
        """Negative control: the guard must not disable filtering outright."""
        _seed_store(env, "-Users-me-dev-demo")
        _seed_store(env, "-Users-me-dev-other")
        assert {i.project for i in _scan(project="demo")} == {"-Users-me-dev-demo"}


# ---------------------------------------------------------------------------
# 25. The index vanishes between the exists() check and the read (F21)
# ---------------------------------------------------------------------------

class TestIndexVanishesMidScan:

    def test_a_vanished_index_reports_index_missing_not_a_crash(self, env, monkeypatch):
        store = _store(env)
        (store / "a.md").write_text("x\n")
        (store / "MEMORY.md").write_text("- [A](a.md) — x\n")

        real_text = Path.read_text

        def racy_text(self, *a, **kw):
            if self.name == "MEMORY.md" and self.exists():
                self.unlink()
            return real_text(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", racy_text)
        issues = _scan()
        assert _classes(issues) == {"index-missing": ["MEMORY.md"]}
        assert issues[0].extra["entry_count"] == 1


# ---------------------------------------------------------------------------
# 26. An index that names nothing at all (F23)
# ---------------------------------------------------------------------------

class TestIndexNamesNothing:

    def test_an_index_with_text_but_no_links_summarises_AND_lists(self, env):
        """The summary row must not replace the per-entry rows.

        The index decoded cleanly as UTF-8 and names no file, so every entry
        really is unreachable — the orphan rows are correct, and they are
        the actionable content of this whole check.
        """
        store = _store(env)
        for n in ("a.md", "b.md", "c.md"):
            (store / n).write_text("x\n")
        (store / "MEMORY.md").write_text("# Memory index\n\nnotes go here\n")
        issues = _scan()
        assert _classes(issues) == {
            "index-names-nothing": ["MEMORY.md"],
            "orphan-isolated": ["a.md", "b.md", "c.md"],
        }
        summary = next(i for i in issues
                       if i.extra["signal_class"] == "index-names-nothing")
        assert summary.extra["entry_count"] == 3
        orphan = next(i for i in issues if Path(i.note_path).name == "a.md")
        assert "a.md" in orphan.proposed_source

    def test_an_empty_index_is_still_ordinary_drift(self, env):
        """Negative control: no text at all means the pointer lines were
        never written, which is exactly what this check exists to report."""
        store = _store(env)
        (store / "a.md").write_text("x\n")
        (store / "MEMORY.md").write_text("")
        assert _classes(_scan()) == {"orphan-isolated": ["a.md"]}

    def test_the_backstop_sits_alongside_every_other_row(self, env):
        store = _store(env)
        (store / "a.md").write_text("x\n")
        (store / "MEMORY.md").write_bytes(
            b"#" * mi.INDEX_SIZE_HARD_LIMIT_BYTES
        )
        assert _classes(_scan()) == {
            "index-names-nothing": ["MEMORY.md"],
            "index-oversize-hard": ["MEMORY.md"],
            "orphan-isolated": ["a.md"],
        }


# ---------------------------------------------------------------------------
# 27. Replacement really is replacement (F23)
# ---------------------------------------------------------------------------

class TestUndecodableReplacementSemantics:

    def test_a_bad_byte_inside_a_link_breaks_that_link(self, env):
        """errors='replace', not 'ignore': ignoring the byte would splice
        `no` and `te` into a working `[[note]]` and hide the damage. The
        entry-undecodable row's reason claims exactly this behaviour."""
        store = _store(env)
        (store / "hub.md").write_bytes(b"see [[no\xffte]]\n")
        (store / "note.md").write_text("x\n")
        (store / "MEMORY.md").write_text("- [Hub](hub.md) — x\n")
        classes = _classes(_scan())
        assert classes["entry-undecodable"] == ["hub.md"]
        assert classes["orphan-unreachable"] == ["note.md"]


# ---------------------------------------------------------------------------
# 28. One bad entry stays one row, whatever it raises (F20)
# ---------------------------------------------------------------------------

class TestEntryReadCatchIsSymmetric:

    def test_a_valueerror_from_an_entry_read_is_a_row_not_a_crash(self, env, monkeypatch):
        """The index read catches (OSError, ValueError); the entry read must
        too, or tightening the index read further would crash the check."""
        store = _store(env)
        (store / "a.md").write_text("x\n")
        (store / "orphan.md").write_text("x\n")
        (store / "MEMORY.md").write_text("- [A](a.md) — x\n")

        real_bytes = Path.read_bytes

        def picky_bytes(self, *args, **kwargs):
            if self.name == "a.md":
                raise ValueError("decoder rejected this file")
            return real_bytes(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_bytes", picky_bytes)
        classes = _classes(_scan())
        assert classes["entry-unreadable"] == ["a.md"]
        assert classes["orphan-unreachable"] == ["orphan.md"]
