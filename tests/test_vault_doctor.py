"""Tests for scripts/vault_doctor.py and the check registry."""

import importlib
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))


def test_registry_lists_source_sessions_check():
    """The registry auto-discovers the source_sessions check module."""
    import vault_doctor_checks
    importlib.reload(vault_doctor_checks)
    names = vault_doctor_checks.list_checks()
    assert "source-sessions" in names


def test_issue_and_result_dataclasses_importable():
    """Shared Issue and Result types are importable from the registry."""
    from vault_doctor_checks import Issue, Result
    issue = Issue(
        check="source-sessions",
        note_path="/tmp/foo.md",
        project="testproj",
        current_source="[[old]]",
        proposed_source="[[new]]",
        reason="mtime falls inside different session window",
        confidence=0.95,
    )
    assert issue.check == "source-sessions"
    assert issue.confidence == 0.95
    assert issue.extra == {}  # default empty dict

    result = Result(
        check="source-sessions",
        note_path="/tmp/foo.md",
        status="applied",
        backup_path="/tmp/backup/foo.md",
        error=None,
    )
    assert result.status == "applied"
    assert result.backup_path == "/tmp/backup/foo.md"


def test_cli_dry_run_reports_issues(tmp_path):
    """vault_doctor.py --check source-sessions reports without applying."""
    import subprocess, json, sys, os, time, calendar
    from pathlib import Path

    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    (vault / "claude-insights").mkdir(parents=True)
    claude_home = tmp_path / ".claude" / "projects" / "-x-proj1"
    claude_home.mkdir(parents=True)

    # Session B widened to include 2026-04-10 midday (12:00) so the new
    # date-based capture_time matches it. (issue #93)
    b_start = calendar.timegm(time.strptime("2026-04-10 10:00", "%Y-%m-%d %H:%M"))
    (claude_home / "sid-b.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": "2026-04-10T10:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    os.utime(claude_home / "sid-b.jsonl", (b_start + 4 * 3600, b_start + 4 * 3600))

    (vault / "claude-sessions" / "2026-04-10-proj1-bbbb.md").write_text(
        "---\ntype: claude-session\ndate: 2026-04-10\nsession_id: sid-b\nproject: proj1\nstatus: summarized\n---\n# s\n",
        encoding="utf-8",
    )
    # source_session matches sid-b (in the index) but source_session_note
    # erroneously points to session-A's note → uuid-basename-stale (Issue #106).
    insight = vault / "claude-insights" / "2026-04-10-stale.md"
    insight.write_text(
        '---\ntype: claude-insight\ndate: 2026-04-10\nsource_session: sid-b\nsource_session_note: "[[2026-04-09-proj1-aaaa]]"\nproject: proj1\n---\n# x\n',
        encoding="utf-8",
    )
    os.utime(insight, (b_start + 1800, b_start + 1800))

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["OBSIDIAN_BRAIN_VAULT"] = str(vault)
    env["OBSIDIAN_BRAIN_SESSIONS_FOLDER"] = "claude-sessions"
    env["OBSIDIAN_BRAIN_INSIGHTS_FOLDER"] = "claude-insights"

    script = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"
    result = subprocess.run(
        [sys.executable, str(script), "--check", "source-sessions", "--days", "10000",
         "--project", "proj1", "--json"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1, f"expected exit 1 (issues found), got {result.returncode}: {result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["total_issues"] >= 1
    assert any(i["check"] == "source-sessions" for i in payload["issues"])
    # File was NOT modified (dry-run default)
    assert "source_session: sid-b" in insight.read_text(encoding="utf-8")


def test_cli_apply_with_yes(tmp_path):
    """vault_doctor.py --apply --yes patches the file non-interactively."""
    import subprocess, json, sys, os, time, calendar
    from pathlib import Path

    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    (vault / "claude-insights").mkdir(parents=True)
    claude_home = tmp_path / ".claude" / "projects" / "-x-proj1"
    claude_home.mkdir(parents=True)

    # Session B widened to include 2026-04-10 midday (12:00) so the new
    # date-based capture_time matches it. (issue #93)
    b_start = calendar.timegm(time.strptime("2026-04-10 10:00", "%Y-%m-%d %H:%M"))
    (claude_home / "sid-b.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": "2026-04-10T10:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    os.utime(claude_home / "sid-b.jsonl", (b_start + 4 * 3600, b_start + 4 * 3600))

    (vault / "claude-sessions" / "2026-04-10-proj1-bbbb.md").write_text(
        "---\ntype: claude-session\ndate: 2026-04-10\nsession_id: sid-b\nproject: proj1\nstatus: summarized\n---\n# s\n",
        encoding="utf-8",
    )
    # source_session matches sid-b (in the index) but source_session_note
    # erroneously points to session-A's note → uuid-basename-stale (Issue #106).
    insight = vault / "claude-insights" / "2026-04-10-apply.md"
    insight.write_text(
        '---\ntype: claude-insight\ndate: 2026-04-10\nsource_session: sid-b\nsource_session_note: "[[2026-04-09-proj1-aaaa]]"\nproject: proj1\n---\n# x\n',
        encoding="utf-8",
    )
    os.utime(insight, (b_start + 1800, b_start + 1800))

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["OBSIDIAN_BRAIN_VAULT"] = str(vault)
    env["OBSIDIAN_BRAIN_SESSIONS_FOLDER"] = "claude-sessions"
    env["OBSIDIAN_BRAIN_INSIGHTS_FOLDER"] = "claude-insights"

    script = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"
    result = subprocess.run(
        [sys.executable, str(script), "--check", "source-sessions", "--days", "10000",
         "--project", "proj1", "--apply", "--yes"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1, f"expected exit 1 after successful apply, got {result.returncode}: {result.stderr}"
    patched = insight.read_text(encoding="utf-8")
    assert "source_session: sid-b" in patched
    assert 'source_session_note: "[[2026-04-10-proj1-bbbb]]"' in patched


def test_cli_unknown_check_errors(tmp_path):
    """Unknown --check name returns exit code 3."""
    import subprocess, sys, os
    from pathlib import Path

    env = os.environ.copy()
    env["OBSIDIAN_BRAIN_VAULT"] = str(tmp_path)

    script = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"
    result = subprocess.run(
        [sys.executable, str(script), "--check", "nonexistent-check", "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 3, f"expected exit 3, got {result.returncode}"


def test_registry_lists_spurious_wikilinks_check():
    """The registry auto-discovers the spurious_wikilinks check module."""
    import vault_doctor_checks
    importlib.reload(vault_doctor_checks)
    names = vault_doctor_checks.list_checks()
    assert "spurious-wikilinks" in names


def test_spurious_wikilinks_scan_detects_bash_conditionals(tmp_path):
    """scan() finds unescaped [[ in conversation lines."""
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    (vault / "claude-sessions" / "2026-04-12-proj-abcd.md").write_text(
        "---\ntype: claude-session\nproject: proj\n---\n"
        "# Session\n"
        '**User:** if [[ $BRANCH == feature/* ]]; then echo yes; fi\n'
        "**Assistant:** ok\n",
        encoding="utf-8",
    )
    from vault_doctor_checks import spurious_wikilinks
    issues = spurious_wikilinks.scan(str(vault), "claude-sessions", "claude-insights", 9999)
    assert len(issues) == 1
    assert issues[0].project == "proj"
    assert "1 line(s)" in issues[0].current_source


def test_spurious_wikilinks_scan_ignores_escaped(tmp_path):
    """scan() does not flag already-escaped \\[\\[."""
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    (vault / "claude-sessions" / "2026-04-12-proj-abcd.md").write_text(
        "---\ntype: claude-session\nproject: proj\n---\n"
        "# Session\n"
        r'**User:** if \[\[ $BRANCH == feature/* ]]; then echo yes; fi' + "\n",
        encoding="utf-8",
    )
    from vault_doctor_checks import spurious_wikilinks
    issues = spurious_wikilinks.scan(str(vault), "claude-sessions", "claude-insights", 9999)
    assert len(issues) == 0


def test_spurious_wikilinks_scan_ignores_non_conversation_lines(tmp_path):
    """scan() does not flag [[ in non-conversation lines (e.g., frontmatter wikilinks)."""
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    (vault / "claude-sessions" / "2026-04-12-proj-abcd.md").write_text(
        '---\ntype: claude-session\nproject: proj\nsource_session_note: "[[2026-04-12-proj-1234]]"\n---\n'
        "# Session\n"
        "Normal text with no bash.\n",
        encoding="utf-8",
    )
    from vault_doctor_checks import spurious_wikilinks
    issues = spurious_wikilinks.scan(str(vault), "claude-sessions", "claude-insights", 9999)
    assert len(issues) == 0


def test_spurious_wikilinks_apply_escapes_and_backs_up(tmp_path):
    """apply() escapes [[ and creates a backup."""
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    note = vault / "claude-sessions" / "2026-04-12-proj-abcd.md"
    note.write_text(
        "---\ntype: claude-session\nproject: proj\n---\n"
        "# Session\n"
        '**User:** if [[ $X == y ]]; then echo yes; fi\n'
        "**Assistant:** sure\n",
        encoding="utf-8",
    )

    from vault_doctor_checks import spurious_wikilinks
    issues = spurious_wikilinks.scan(str(vault), "claude-sessions", "claude-insights", 9999)
    assert len(issues) == 1

    backup_root = str(tmp_path / "backups")
    results = spurious_wikilinks.apply(issues, backup_root)
    assert len(results) == 1
    assert results[0].status == "applied"
    assert results[0].backup_path is not None

    patched = note.read_text(encoding="utf-8")
    assert "[[" not in patched.split("# Session")[1]
    assert r"\[\[" in patched

    # Backup contains original
    backup_content = Path(results[0].backup_path).read_text(encoding="utf-8")
    assert "[[" in backup_content


def test_registry_warns_on_interfaceless_module(tmp_path, capsys, monkeypatch):
    """A module that imports fine but lacks NAME/scan/apply must produce a
    one-line stderr warning instead of being silently skipped (a typo'd
    NAME/scan symbol would otherwise make a check vanish without trace)."""
    import vault_doctor_checks

    fake_pkg_dir = tmp_path / "fake_checks"
    fake_pkg_dir.mkdir()
    (fake_pkg_dir / "no_interface.py").write_text(
        "X = 1  # imports fine, exposes no check interface\n", encoding="utf-8"
    )

    original = vault_doctor_checks._CHECKS.copy()
    try:
        vault_doctor_checks._CHECKS.clear()
        monkeypatch.setattr(vault_doctor_checks, "__path__", [str(fake_pkg_dir)])
        vault_doctor_checks._discover()
        err = capsys.readouterr().err
        assert "no_interface" in err, f"warning must name the module: {err!r}"
        assert "does not expose the check interface" in err
        assert "skipped" in err
        assert vault_doctor_checks._CHECKS == {}
    finally:
        vault_doctor_checks._CHECKS.clear()
        vault_doctor_checks._CHECKS.update(original)
        sys.modules.pop("vault_doctor_checks.no_interface", None)


class TestCrashContainment:
    """Per-check crash containment in the dispatcher: one buggy check must
    not take down the whole run; the crash is surfaced loudly (stderr +
    crashed_checks + exit 2) instead of a traceback-and-die.

    Injection follows the registry-pollution pattern from
    test_vault_doctor_audit_historic_repairs.py (snapshot/restore _CHECKS in
    finally) and drives vault_doctor.main() in-process.
    """

    @staticmethod
    def _fake_issue(check_name, note_path="/tmp/fake-crash-note.md"):
        from vault_doctor_checks import Issue
        return Issue(
            check=check_name,
            note_path=note_path,
            project="crashproj",
            current_source="[[old]]",
            proposed_source="[[new]]",
            reason="synthetic issue from crash-containment fixture",
            confidence=0.9,
            extra={"unresolved": False},
        )

    @pytest.fixture
    def doctor_env(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        (vault / "claude-sessions").mkdir(parents=True)
        (vault / "claude-insights").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("OBSIDIAN_BRAIN_VAULT", str(vault))
        monkeypatch.setenv("OBSIDIAN_BRAIN_SESSIONS_FOLDER", "claude-sessions")
        monkeypatch.setenv("OBSIDIAN_BRAIN_INSIGHTS_FOLDER", "claude-insights")
        return vault

    def _patched_checks(self, extra_mods):
        """Context: pollute _CHECKS with extra modules; caller restores."""
        import vault_doctor_checks
        vault_doctor_checks._discover()
        original = vault_doctor_checks._CHECKS.copy()
        for mod in extra_mods:
            vault_doctor_checks._CHECKS[mod.NAME] = mod
        return original

    def test_scan_crash_contained_other_issues_still_reported(
        self, doctor_env, monkeypatch, capsys
    ):
        """A check whose scan() raises is recorded in crashed_checks; a later
        check's issues are still scanned and reported; exit 2."""
        import json
        import types
        import vault_doctor
        import vault_doctor_checks

        def _raise(*a, **kw):
            raise RuntimeError("boom-scan")

        crasher = types.SimpleNamespace(
            NAME="fake-crash", scan=_raise, apply=lambda *a, **kw: [])
        producer = types.SimpleNamespace(
            NAME="fake-issues",
            scan=lambda *a, **kw: [self._fake_issue("fake-issues")],
            apply=lambda *a, **kw: [])

        original = self._patched_checks([crasher, producer])
        try:
            monkeypatch.setattr(sys, "argv", ["vault_doctor", "--json"])
            rc = vault_doctor.main()
        finally:
            vault_doctor_checks._CHECKS.clear()
            vault_doctor_checks._CHECKS.update(original)

        out, err = capsys.readouterr()
        assert rc == 2, f"crashed check must force exit 2, got {rc}\n{err}"
        payload = json.loads(out)
        assert payload["crashed_checks"] == ["fake-crash"]
        assert any(i["check"] == "fake-issues" for i in payload["issues"]), (
            "issues from the check AFTER the crasher must still be reported"
        )
        assert "CHECK CRASHED: fake-crash: RuntimeError: boom-scan" in err
        assert "Traceback" in err  # full traceback printed for diagnosis

    def test_zero_issues_with_crash_no_plain_clean_exit_2(
        self, doctor_env, monkeypatch, capsys
    ):
        """0 issues + a crashed check must NOT print the plain clean line —
        the results are incomplete; exit 2."""
        import types
        import vault_doctor
        import vault_doctor_checks

        def _raise(*a, **kw):
            raise ValueError("boom-clean")

        crasher = types.SimpleNamespace(
            NAME="fake-crash-clean", scan=_raise, apply=lambda *a, **kw: [])

        original = self._patched_checks([crasher])
        try:
            monkeypatch.setattr(sys, "argv", ["vault_doctor"])
            rc = vault_doctor.main()
        finally:
            vault_doctor_checks._CHECKS.clear()
            vault_doctor_checks._CHECKS.update(original)

        _, err = capsys.readouterr()
        assert rc == 2, f"expected exit 2, got {rc}\n{err}"
        assert "vault_doctor: clean" not in err, (
            "a run with crashed checks must not claim a clean bill of health"
        )
        assert "0 issues, but 1 check(s) crashed" in err
        assert "results incomplete" in err
        # The human header also surfaces the crash.
        assert "crashed" in err

    def test_apply_crash_contained_exit_2_with_warning(
        self, doctor_env, monkeypatch, capsys
    ):
        """A check whose apply() raises mid-run is contained: stderr warns
        that some fixes may already be applied (pointing at the backup root),
        the check lands in crashed_checks, and the run exits 2."""
        import types
        import vault_doctor
        import vault_doctor_checks

        def _apply_raise(*a, **kw):
            raise RuntimeError("boom-apply")

        mod = types.SimpleNamespace(
            NAME="fake-apply-crash",
            scan=lambda *a, **kw: [self._fake_issue("fake-apply-crash")],
            apply=_apply_raise)

        original = self._patched_checks([mod])
        try:
            monkeypatch.setattr(
                sys, "argv", ["vault_doctor", "--apply", "--yes"])
            rc = vault_doctor.main()
        finally:
            vault_doctor_checks._CHECKS.clear()
            vault_doctor_checks._CHECKS.update(original)

        _, err = capsys.readouterr()
        assert rc == 2, f"apply crash must take the exit-2 path, got {rc}\n{err}"
        assert "APPLY CRASHED: fake-apply-crash: RuntimeError: boom-apply" in err
        assert "aborted mid-run" in err
        assert "may already be applied" in err
        assert "backup root" in err

    def test_apply_crash_in_earlier_check_does_not_block_later_check(
        self, doctor_env, monkeypatch, capsys
    ):
        """An apply() crash in check A must not prevent check B's apply from
        running. The break on crash exits only the per-project inner loop for
        check A; the outer per-check loop continues to check B.

        Coverage pin: this test passes immediately because the break is
        scoped to the inner for-project loop, not the outer for-mod loop.
        """
        import types
        import vault_doctor
        import vault_doctor_checks
        from vault_doctor_checks import Result

        applied_by = []

        def _apply_raise(*a, **kw):
            raise RuntimeError("boom-apply-early")

        def _apply_ok(issues, backup_root):
            for i in issues:
                applied_by.append(i.check)
            return [
                Result(
                    check=i.check,
                    note_path=i.note_path,
                    status="applied",
                    backup_path=None,
                    error=None,
                )
                for i in issues
            ]

        # crasher comes first (dict insertion order is preserved in Python 3.7+
        # and _CHECKS is a plain dict; _patched_checks inserts in list order).
        crasher = types.SimpleNamespace(
            NAME="fake-apply-crasher",
            scan=lambda *a, **kw: [self._fake_issue("fake-apply-crasher")],
            apply=_apply_raise,
        )
        succeeder = types.SimpleNamespace(
            NAME="fake-apply-succeeder",
            scan=lambda *a, **kw: [self._fake_issue("fake-apply-succeeder")],
            apply=_apply_ok,
        )

        original = self._patched_checks([crasher, succeeder])
        try:
            monkeypatch.setattr(
                sys, "argv", ["vault_doctor", "--apply", "--yes"])
            rc = vault_doctor.main()
        finally:
            vault_doctor_checks._CHECKS.clear()
            vault_doctor_checks._CHECKS.update(original)

        _, err = capsys.readouterr()
        assert rc == 2, f"apply crash forces exit 2, got {rc}\n{err}"
        assert "APPLY CRASHED: fake-apply-crasher" in err, (
            "crash in the first check must be reported"
        )
        assert "fake-apply-succeeder" in applied_by, (
            "later check's apply must still run after earlier check crashed"
        )


def test_cli_missing_vault_errors(tmp_path, monkeypatch):
    """Missing vault config → exit 3."""
    import subprocess, sys, os
    from pathlib import Path

    env = os.environ.copy()
    env.pop("OBSIDIAN_BRAIN_VAULT", None)
    # Point HOME at tmp_path so load_config() can't find a real config file.
    # Run cwd from tmp_path so the per-project session cache in /tmp doesn't
    # collide with a cache entry from the real obsidian-brain project.
    env["HOME"] = str(tmp_path)
    isolated_cwd = tmp_path / "isolated-project-xyz"
    isolated_cwd.mkdir()

    script = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"
    result = subprocess.run(
        [sys.executable, str(script), "--json"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(isolated_cwd),
    )
    assert result.returncode == 3, f"expected exit 3, got {result.returncode}: {result.stderr}"
