"""Tests for the audit-historic-repairs vault_doctor check module (#95)."""

import json
import os
import re
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import vault_doctor_checks  # noqa: E402 (must follow sys.path setup)
import vault_doctor_checks.audit_historic_repairs as ahr  # noqa: E402
from vault_doctor_checks import Issue  # noqa: E402


def _run_dirname(dt: datetime) -> str:
    """Mirror the dispatcher's backup-run dir naming (ISO with ':' → '-')."""
    return dt.strftime("%Y-%m-%dT%H-%M-%S") + "+00-00"


def _days_ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _insight_text(date: str, sid: str, backlink: str, project: str = "proj1") -> str:
    return (
        f"---\n"
        f"type: claude-insight\n"
        f"date: {date}\n"
        f"source_session: {sid}\n"
        f'source_session_note: "[[{backlink}]]"\n'
        f"project: {project}\n"
        f"---\n"
        f"# Insight body\n"
    )


@pytest.fixture
def audit_env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    insights = vault / "claude-insights"
    sessions = vault / "claude-sessions"
    insights.mkdir(parents=True)
    sessions.mkdir(parents=True)
    backup_root = tmp_path / "doctor-backup"
    backup_root.mkdir()
    monkeypatch.setenv("OBSIDIAN_BRAIN_DOCTOR_BACKUP_ROOT", str(backup_root))
    return {
        "vault": vault,
        "insights": insights,
        "sessions": sessions,
        "backups": backup_root,
    }


def _seed(env, basename, run_dt, orig_sid, orig_link, curr_sid, curr_link,
          project="proj1", write_current=True):
    """Seed one backed-up note + its current vault state."""
    date = basename[:10]
    run_dir = env["backups"] / _run_dirname(run_dt) / project / "claude-insights"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / basename).write_text(
        _insight_text(date, orig_sid, orig_link, project), encoding="utf-8")
    if write_current:
        (env["insights"] / basename).write_text(
            _insight_text(date, curr_sid, curr_link, project), encoding="utf-8")


def _scan(env, days=180, project=None):
    return ahr.scan(str(env["vault"]), "claude-sessions", "claude-insights",
                    days, project=project)


# ---------------------------------------------------------------- categories

def test_category_a_proposes_restore(audit_env):
    _seed(audit_env, "2026-04-09-foo-1111.md", _days_ago(30),
          "sid-orig", "2026-04-09-proj1-aaaa",   # original: same-day (correct)
          "sid-curr", "2026-04-10-proj1-bbbb")   # current: different-day (drift)
    issues = _scan(audit_env)
    assert len(issues) == 1
    i = issues[0]
    assert i.extra["category"] == "A"
    assert i.extra["signal_class"] == "historic-restore"
    assert i.extra["unresolved"] is False
    assert i.confidence == 0.9
    assert i.proposed_source == "[[2026-04-09-proj1-aaaa]]"
    assert i.extra["orig_sid"] == "sid-orig"


def test_category_b_keep(audit_env):
    _seed(audit_env, "2026-04-09-foo-2222.md", _days_ago(30),
          "sid-orig", "2026-04-08-proj1-aaaa",   # original: wrong-day
          "sid-curr", "2026-04-09-proj1-bbbb")   # current: same-day (legit fix)
    issues = _scan(audit_env)
    assert len(issues) == 1
    i = issues[0]
    assert i.extra["category"] == "B"
    assert i.extra["signal_class"] == "historic-keep"
    assert i.extra["unresolved"] is True
    assert i.confidence == 0.0
    assert i.proposed_source == ""


def test_category_c_ambiguous(audit_env):
    _seed(audit_env, "2026-04-09-foo-3333.md", _days_ago(30),
          "sid-orig", "2026-04-09-proj1-aaaa",   # both same-day,
          "sid-curr", "2026-04-09-proj1-bbbb")   # different sessions
    issues = _scan(audit_env)
    assert len(issues) == 1
    assert issues[0].extra["category"] == "C"
    assert issues[0].extra["unresolved"] is True


def test_category_d_both_wrong(audit_env):
    _seed(audit_env, "2026-04-09-foo-4444.md", _days_ago(30),
          "sid-orig", "2026-04-07-proj1-aaaa",   # neither matches
          "sid-curr", "2026-04-08-proj1-bbbb")   # the filename date
    issues = _scan(audit_env)
    assert len(issues) == 1
    assert issues[0].extra["category"] == "D"
    assert issues[0].extra["unresolved"] is True


def test_unparsable_backlink_is_category_d(audit_env):
    _seed(audit_env, "2026-04-09-foo-5555.md", _days_ago(30),
          "sid-orig", "no-date-prefix-note",
          "sid-curr", "2026-04-09-proj1-bbbb")
    issues = _scan(audit_env)
    assert len(issues) == 1
    assert issues[0].extra["category"] == "D"


# ------------------------------------------------------------------ skipping

def test_no_drift_skipped(audit_env):
    _seed(audit_env, "2026-04-09-foo-6666.md", _days_ago(30),
          "sid-same", "2026-04-09-proj1-aaaa",
          "sid-same", "2026-04-09-proj1-aaaa")
    assert _scan(audit_env) == []


def test_aged_run_skipped(audit_env):
    _seed(audit_env, "2026-04-09-foo-7777.md", _days_ago(200),
          "sid-orig", "2026-04-09-proj1-aaaa",
          "sid-curr", "2026-04-10-proj1-bbbb")
    assert _scan(audit_env, days=180) == []
    # ...but a wider window picks it up
    assert len(_scan(audit_env, days=365)) == 1


def test_missing_current_note_skipped(audit_env):
    _seed(audit_env, "2026-04-09-foo-8888.md", _days_ago(30),
          "sid-orig", "2026-04-09-proj1-aaaa",
          "sid-curr", "2026-04-10-proj1-bbbb", write_current=False)
    assert _scan(audit_env) == []


def test_non_source_session_backup_skipped(audit_env, capsys):
    """T1(a): backup and current both lack source fields → no issues, no corrupt warning."""
    basename = "2026-04-09-proj1-9999.md"
    run_dir = audit_env["backups"] / _run_dirname(_days_ago(30)) / "snapshot-integrity"
    run_dir.mkdir(parents=True)
    text = "---\ntype: claude-session\ndate: 2026-04-09\nproject: proj1\n---\n# S\n"
    (run_dir / basename).write_text(text, encoding="utf-8")
    (audit_env["sessions"] / basename).write_text(text, encoding="utf-8")
    issues = _scan(audit_env)
    assert issues == []
    _, err = capsys.readouterr()
    assert "may be corrupted" not in err


def test_corrupt_backup_warns(audit_env, capsys):
    """T1(b): backup with NO frontmatter; current WITH source fields → no issues, stderr warns."""
    basename = "2026-04-09-proj1-corrupt.md"
    run_dir = audit_env["backups"] / _run_dirname(_days_ago(30)) / "snapshot-integrity"
    run_dir.mkdir(parents=True)
    # Backup has no frontmatter at all
    (run_dir / basename).write_text("# just body\nsome content here\n", encoding="utf-8")
    # Current note has both source fields
    (audit_env["sessions"] / basename).write_text(
        _insight_text("2026-04-09", "sid-curr", "2026-04-09-proj1-xxxx"),
        encoding="utf-8")
    issues = _scan(audit_env)
    assert issues == []
    _, err = capsys.readouterr()
    assert "may be corrupted" in err


def test_own_output_dir_excluded(audit_env):
    """Restore backups written by this check's own apply() are not re-audited."""
    basename = "2026-04-09-foo-aaaa.md"
    run_dir = (audit_env["backups"] / _run_dirname(_days_ago(5))
               / "audit-historic-repairs" / "claude-insights")
    run_dir.mkdir(parents=True)
    (run_dir / basename).write_text(
        _insight_text("2026-04-09", "sid-x", "2026-04-09-proj1-xxxx"),
        encoding="utf-8")
    (audit_env["insights"] / basename).write_text(
        _insight_text("2026-04-09", "sid-y", "2026-04-10-proj1-yyyy"),
        encoding="utf-8")
    assert _scan(audit_env) == []


def test_project_filter(audit_env):
    _seed(audit_env, "2026-04-09-foo-bbbb.md", _days_ago(30),
          "sid-orig", "2026-04-09-proj1-aaaa",
          "sid-curr", "2026-04-10-proj1-bbbb", project="proj1")
    assert len(_scan(audit_env, project="proj1")) == 1
    assert _scan(audit_env, project="other") == []


def test_oldest_backup_wins(audit_env):
    """With multiple runs touching the same note, the oldest is the original."""
    basename = "2026-04-09-foo-cccc.md"
    # Oldest run: true original (same-day → category A vs current)
    _seed(audit_env, basename, _days_ago(60),
          "sid-true-orig", "2026-04-09-proj1-aaaa",
          "sid-curr", "2026-04-11-proj1-cccc")
    # Newer run: intermediate doctor iteration (wrong-day)
    run_dir = (audit_env["backups"] / _run_dirname(_days_ago(10))
               / "proj1" / "claude-insights")
    run_dir.mkdir(parents=True)
    (run_dir / basename).write_text(
        _insight_text("2026-04-09", "sid-intermediate", "2026-04-10-proj1-bbbb"),
        encoding="utf-8")
    issues = _scan(audit_env)
    assert len(issues) == 1
    assert issues[0].extra["orig_sid"] == "sid-true-orig"
    assert issues[0].extra["category"] == "A"


# --------------------------------------------------------------------- apply

def test_apply_restores_category_a(audit_env, tmp_path):
    _seed(audit_env, "2026-04-09-foo-dddd.md", _days_ago(30),
          "sid-orig", "2026-04-09-proj1-aaaa",
          "sid-curr", "2026-04-10-proj1-bbbb")
    issues = _scan(audit_env)
    assert len(issues) == 1 and issues[0].extra["category"] == "A"

    apply_root = tmp_path / "apply-backup"
    results = ahr.apply(issues, str(apply_root))
    assert len(results) == 1
    assert results[0].status == "applied"

    restored = (audit_env["insights"] / "2026-04-09-foo-dddd.md").read_text(
        encoding="utf-8")
    assert "source_session: sid-orig" in restored
    assert 'source_session_note: "[[2026-04-09-proj1-aaaa]]"' in restored
    assert "# Insight body" in restored  # body preserved

    # Own backup written under a check-named nested dir (reversible restore)
    backup = apply_root / ahr.NAME / "claude-insights" / "2026-04-09-foo-dddd.md"
    assert backup.is_file()
    assert "source_session: sid-curr" in backup.read_text(encoding="utf-8")

    # Re-scan after restore: no drift remains
    assert _scan(audit_env) == []


def test_apply_skips_unresolved(audit_env, tmp_path):
    _seed(audit_env, "2026-04-09-foo-eeee.md", _days_ago(30),
          "sid-orig", "2026-04-09-proj1-aaaa",
          "sid-curr", "2026-04-09-proj1-bbbb")  # category C
    issues = _scan(audit_env)
    before = (audit_env["insights"] / "2026-04-09-foo-eeee.md").read_text(
        encoding="utf-8")
    results = ahr.apply(issues, str(tmp_path / "apply-backup"))
    assert results[0].status == "unresolved"
    after = (audit_env["insights"] / "2026-04-09-foo-eeee.md").read_text(
        encoding="utf-8")
    assert before == after


def test_apply_refuses_non_a_resolvable(tmp_path):
    """Defense-in-depth: a non-A issue marked resolvable is a programming error."""
    bogus = Issue(
        check=ahr.NAME, note_path=str(tmp_path / "x.md"), project="p",
        current_source="[[c]]", proposed_source="[[o]]",
        reason="bogus", confidence=0.9,
        extra={"category": "C", "unresolved": False,
               "orig_sid": "s", "orig_basename": "o"},
    )
    with pytest.raises(RuntimeError, match="refuses category"):
        ahr.apply([bogus], str(tmp_path / "apply-backup"))


# ------------------------------------------------------------------ registry

def test_opt_in_excluded_from_default_sweep():
    names = [m.NAME for m in vault_doctor_checks.all_checks()]
    assert ahr.NAME not in names


def test_opt_in_reachable_via_get_check():
    mod = vault_doctor_checks.get_check(ahr.NAME)
    assert mod is ahr


# ------------------------------------- drift variants, CLI e2e, apply errors

def test_sid_only_drift_detected(audit_env):
    """T2: same source_session_note backlink (same-day) but different sids → 1 issue, cat C."""
    _seed(audit_env, "2026-04-09-foo-t2t2.md", _days_ago(30),
          "sid-orig", "2026-04-09-proj1-aaaa",  # same-day backlink
          "sid-curr", "2026-04-09-proj1-aaaa")   # same backlink, different sid
    issues = _scan(audit_env)
    assert len(issues) == 1
    assert issues[0].extra["category"] == "C"


def test_backlink_only_drift_detected(audit_env):
    """T3: same sid both sides; orig backlink same-day, curr different-day → cat A, not unresolved."""
    _seed(audit_env, "2026-04-09-foo-t3t3.md", _days_ago(30),
          "sid-same", "2026-04-09-proj1-aaaa",  # orig: same-day backlink
          "sid-same", "2026-04-10-proj1-bbbb")   # curr: different-day (drift)
    issues = _scan(audit_env)
    assert len(issues) == 1
    i = issues[0]
    assert i.extra["category"] == "A"
    assert i.extra["unresolved"] is False


def test_category_a_without_orig_sid_is_unresolved(audit_env):
    """T4: backup has source_session_note (same-day) but NO source_session → cat A, unresolved (no restore data)."""
    basename = "2026-04-09-foo-t4t4.md"
    date = "2026-04-09"
    run_dir = audit_env["backups"] / _run_dirname(_days_ago(30)) / "proj1" / "claude-insights"
    run_dir.mkdir(parents=True, exist_ok=True)
    # Backup: has source_session_note (same-day) but NO source_session field
    backup_text = (
        f"---\n"
        f"type: claude-insight\n"
        f"date: {date}\n"
        f'source_session_note: "[[{date}-proj1-aaaa]]"\n'
        f"project: proj1\n"
        f"---\n"
        f"# body\n"
    )
    (run_dir / basename).write_text(backup_text, encoding="utf-8")
    # Current note has both fields, but backlink is different-day (drift)
    (audit_env["insights"] / basename).write_text(
        _insight_text(date, "sid-curr", "2026-04-10-proj1-bbbb"),
        encoding="utf-8")
    issues = _scan(audit_env)
    assert len(issues) == 1
    i = issues[0]
    assert i.extra["category"] == "A"
    assert i.extra["unresolved"] is True
    assert i.confidence == 0.0


def test_cli_end_to_end_scan_apply_rescan(tmp_path):
    """T5: subprocess test for full scan → apply → rescan lifecycle."""
    # --- Build vault ---
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    (vault / "claude-insights").mkdir(parents=True)

    backup_root = tmp_path / ".claude" / "obsidian-brain-doctor-backup"
    backup_root.mkdir(parents=True)

    # Seed a category-A fixture in a run INSIDE the backup_root
    basename = "2026-04-09-proj1-t5t5.md"
    date = "2026-04-09"
    run_dir = backup_root / _run_dirname(_days_ago(30)) / "proj1" / "claude-insights"
    run_dir.mkdir(parents=True)
    (run_dir / basename).write_text(
        _insight_text(date, "sid-orig-t5", "2026-04-09-proj1-t5orig"),
        encoding="utf-8")
    # Current note: drifted (different-day backlink)
    (vault / "claude-insights" / basename).write_text(
        _insight_text(date, "sid-curr-t5", "2026-04-10-proj1-t5curr"),
        encoding="utf-8")

    script = Path(__file__).parent.parent / "scripts" / "vault_doctor.py"
    env = {
        "HOME": str(tmp_path),
        "OBSIDIAN_BRAIN_DOCTOR_BACKUP_ROOT": str(backup_root),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }

    # Step 1: scan with --check → exit 1; 1 issue with signal_class historic-restore; no "extra" top-level key
    r = subprocess.run(
        [sys.executable, str(script),
         "--check", "audit-historic-repairs",
         "--json",
         "--vault", str(vault),
         "--sessions-folder", "claude-sessions",
         "--insights-folder", "claude-insights"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 1, f"step1 exit: expected 1, got {r.returncode}:\n{r.stderr}"
    payload = json.loads(r.stdout)
    assert payload["total_issues"] == 1
    row = payload["issues"][0]
    assert row["signal_class"] == "historic-restore"
    assert "extra" not in row

    # Step 2: full sweep (no --check) → OPT_IN exclusion, no audit-historic-repairs rows
    r2 = subprocess.run(
        [sys.executable, str(script),
         "--json",
         "--vault", str(vault),
         "--sessions-folder", "claude-sessions",
         "--insights-folder", "claude-insights"],
        capture_output=True, text=True, env=env,
    )
    assert r2.returncode in (0, 1), r2.stderr
    p2 = json.loads(r2.stdout)
    for row2 in p2.get("issues", []):
        assert row2.get("check") != "audit-historic-repairs", \
            "OPT_IN check must not appear in default sweep"

    # Step 3: apply with --yes → exit 1; note content restored
    r3 = subprocess.run(
        [sys.executable, str(script),
         "--check", "audit-historic-repairs",
         "--apply", "--yes",
         "--vault", str(vault),
         "--sessions-folder", "claude-sessions",
         "--insights-folder", "claude-insights"],
        capture_output=True, text=True, env=env,
    )
    assert r3.returncode in (0, 1), f"step3 exit: got {r3.returncode}:\n{r3.stderr}"
    restored = (vault / "claude-insights" / basename).read_text(encoding="utf-8")
    assert "source_session: sid-orig-t5" in restored
    assert "2026-04-09-proj1-t5orig" in restored

    # Step 4: rescan → clean (exit 0); own restore backups under audit-historic-repairs/ not re-audited
    r4 = subprocess.run(
        [sys.executable, str(script),
         "--check", "audit-historic-repairs",
         "--json",
         "--vault", str(vault),
         "--sessions-folder", "claude-sessions",
         "--insights-folder", "claude-insights"],
        capture_output=True, text=True, env=env,
    )
    assert r4.returncode == 0, f"step4 exit: expected 0 (clean), got {r4.returncode}:\n{r4.stderr}"


def test_apply_errors_on_frontmatterless_note(tmp_path):
    """T6: category-A Issue whose note_path has no frontmatter → error result, file unchanged."""
    note = tmp_path / "2026-04-09-proj1-t6t6.md"
    note.write_text("# just body\nno frontmatter here\n", encoding="utf-8")
    original = note.read_text(encoding="utf-8")

    issue = Issue(
        check=ahr.NAME,
        note_path=str(note),
        project="proj1",
        current_source="[[2026-04-10-proj1-curr]]",
        proposed_source="[[2026-04-09-proj1-orig]]",
        reason="category A: ...",
        confidence=0.9,
        extra={
            "category": "A",
            "signal_class": "historic-restore",
            "unresolved": False,
            "orig_sid": "sid-orig-t6",
            "orig_basename": "2026-04-09-proj1-orig",
            "backup_path": str(tmp_path / "backup.md"),
        },
    )
    apply_root = tmp_path / "apply-backup"
    results = ahr.apply([issue], str(apply_root))
    assert len(results) == 1
    r = results[0]
    assert r.status == "error"
    assert "frontmatter" in r.error.lower()
    assert note.read_text(encoding="utf-8") == original  # file unchanged
    # backup file should exist (written before the rewrite attempt)
    backup = apply_root / ahr.NAME / note.parent.name / note.name
    assert backup.is_file()


def test_dateless_note_basename_is_category_d(audit_env):
    """T7: backed-up note with no date prefix and drift → 1 issue, category D."""
    basename = "nodate-note.md"
    run_dir = audit_env["backups"] / _run_dirname(_days_ago(30)) / "proj1" / "claude-insights"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / basename).write_text(
        "---\ntype: claude-insight\ndate: 2026-04-09\n"
        "source_session: sid-orig\n"
        'source_session_note: "[[2026-04-09-proj1-orig]]"\n'
        "project: proj1\n---\n# body\n",
        encoding="utf-8")
    (audit_env["insights"] / basename).write_text(
        "---\ntype: claude-insight\ndate: 2026-04-09\n"
        "source_session: sid-curr\n"
        'source_session_note: "[[2026-04-10-proj1-curr]]"\n'
        "project: proj1\n---\n# body\n",
        encoding="utf-8")
    issues = _scan(audit_env)
    assert len(issues) == 1
    assert issues[0].extra["category"] == "D"


def test_missing_current_backlink_renders_missing(audit_env):
    """T8: current note has source_session but NO source_session_note → current_source == '(missing)', cat D."""
    basename = "2026-04-09-foo-t8t8.md"
    date = "2026-04-09"
    run_dir = audit_env["backups"] / _run_dirname(_days_ago(30)) / "proj1" / "claude-insights"
    run_dir.mkdir(parents=True, exist_ok=True)
    # Backup has both fields
    (run_dir / basename).write_text(
        _insight_text(date, "sid-orig", "2026-04-09-proj1-orig"),
        encoding="utf-8")
    # Current note has source_session but NO source_session_note
    (audit_env["insights"] / basename).write_text(
        f"---\ntype: claude-insight\ndate: {date}\n"
        f"source_session: sid-curr\n"
        f"project: proj1\n---\n# body\n",
        encoding="utf-8")
    issues = _scan(audit_env)
    assert len(issues) == 1
    i = issues[0]
    assert i.current_source == "(missing)"
    assert i.extra["category"] == "D"


def test_excluded_old_run_warns(audit_env, capsys):
    """T9: only aged-out run (200 days old), scan days=180 → no issues, warns about older runs."""
    _seed(audit_env, "2026-04-09-foo-t9t9.md", _days_ago(200),
          "sid-orig", "2026-04-09-proj1-aaaa",
          "sid-curr", "2026-04-10-proj1-bbbb")
    issues = _scan(audit_env, days=180)
    assert issues == []
    _, err = capsys.readouterr()
    assert "older than --days=180" in err


def test_scan_emits_coverage_summary(audit_env, capsys):
    """T10: any seeded fixture → stderr contains 'audited' and 'classified'."""
    _seed(audit_env, "2026-04-09-foo-t10.md", _days_ago(30),
          "sid-orig", "2026-04-09-proj1-aaaa",
          "sid-curr", "2026-04-10-proj1-bbbb")
    _scan(audit_env)
    _, err = capsys.readouterr()
    assert "audited" in err
    assert "classified" in err


def test_opt_in_non_bool_raises():
    """T11: OPT_IN with a non-bool value raises TypeError from all_checks()."""
    fake_mod = types.SimpleNamespace(
        NAME="fake-optin",
        scan=lambda *a, **kw: [],
        apply=lambda *a, **kw: [],
        OPT_IN="true",  # string, not bool
    )
    vault_doctor_checks._discover()  # ensure _CHECKS is populated
    original = vault_doctor_checks._CHECKS.copy()
    try:
        vault_doctor_checks._CHECKS["fake-optin"] = fake_mod
        with pytest.raises(TypeError, match="OPT_IN must be bool"):
            vault_doctor_checks.all_checks()
    finally:
        vault_doctor_checks._CHECKS.clear()
        vault_doctor_checks._CHECKS.update(original)


# ------------------------------------------- unreadable notes + coverage summary

@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_note_emits_unresolved_issue(audit_env):
    """Unreadable current note → unresolved historic-unreadable issue (project=None)."""
    basename = "2026-04-09-foo-r2a.md"
    _seed(audit_env, basename, _days_ago(30),
          "sid-orig", "2026-04-09-proj1-aaaa",
          "sid-curr", "2026-04-10-proj1-bbbb")
    current = audit_env["insights"] / basename
    current.chmod(0o000)
    try:
        issues = _scan(audit_env)
    finally:
        current.chmod(0o600)  # restore so tmp_path cleanup works everywhere
    assert len(issues) == 1
    i = issues[0]
    assert i.extra["signal_class"] == "historic-unreadable"
    assert i.extra["unresolved"] is True
    assert i.project == "unknown"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_note_emitted_under_project_filter(audit_env):
    """With --project set, the unreadable issue is STILL emitted — the filter must not hide audit failures."""
    basename = "2026-04-09-foo-r2b.md"
    _seed(audit_env, basename, _days_ago(30),
          "sid-orig", "2026-04-09-proj1-aaaa",
          "sid-curr", "2026-04-10-proj1-bbbb")
    current = audit_env["insights"] / basename
    current.chmod(0o000)
    try:
        issues = _scan(audit_env, project="proj1")
    finally:
        current.chmod(0o600)
    assert len(issues) == 1
    assert issues[0].extra["signal_class"] == "historic-unreadable"
    assert issues[0].extra["unresolved"] is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_coverage_summary_partitions(audit_env, capsys):
    """Summary buckets partition the audited count; classified excludes unreadable."""
    # 1 classified (category A)
    _seed(audit_env, "2026-04-09-foo-r2c1.md", _days_ago(30),
          "sid-orig", "2026-04-09-proj1-aaaa",
          "sid-curr", "2026-04-10-proj1-bbbb")
    # 1 unreadable
    _seed(audit_env, "2026-04-09-foo-r2c2.md", _days_ago(30),
          "sid-orig", "2026-04-09-proj1-cccc",
          "sid-curr", "2026-04-10-proj1-dddd")
    unreadable_note = audit_env["insights"] / "2026-04-09-foo-r2c2.md"
    unreadable_note.chmod(0o000)
    # 1 no-drift
    _seed(audit_env, "2026-04-09-foo-r2c3.md", _days_ago(30),
          "sid-same", "2026-04-09-proj1-eeee",
          "sid-same", "2026-04-09-proj1-eeee")
    try:
        issues = _scan(audit_env)
    finally:
        unreadable_note.chmod(0o600)
    # issues: 1 classified + 1 unreadable
    assert len(issues) == 2

    _, err = capsys.readouterr()
    m = re.search(
        r"audited (\d+) backed-up note\(s\): (\d+) classified, (\d+) missing-current,"
        r" (\d+) non-source-session, (\d+) no-drift, (\d+) unreadable,"
        r" (\d+) project-filtered",
        err,
    )
    assert m, f"summary line not found in stderr:\n{err}"
    audited, classified, missing, non_src, no_drift, unreadable, proj_filt = map(
        int, m.groups())
    assert audited == 3
    assert classified == 1
    assert unreadable == 1
    assert no_drift == 1
    assert classified + missing + non_src + no_drift + unreadable + proj_filt == audited


# --------------------------------------------- cross-folder basename collisions

def test_cross_folder_collision_pairs_correctly(audit_env):
    """Same basename in two note folders → two independent audit entries,
    each backup paired with the note in ITS OWN folder (never the other's)."""
    basename = "2026-04-09-foo-coll.md"
    date = "2026-04-09"
    decisions = audit_env["vault"] / "claude-decisions"
    decisions.mkdir()

    # Current notes: same basename in claude-insights AND claude-decisions,
    # both drifted (different-day backlinks).
    (audit_env["insights"] / basename).write_text(
        _insight_text(date, "sid-curr-ins", "2026-04-10-proj1-ins-curr"),
        encoding="utf-8")
    (decisions / basename).write_text(
        _insight_text(date, "sid-curr-dec", "2026-04-11-proj1-dec-curr"),
        encoding="utf-8")

    # One backup run with a folder-qualified backup under EACH folder dir,
    # both same-day originals (category A) with distinct orig sids.
    run_root = audit_env["backups"] / _run_dirname(_days_ago(30)) / "proj1"
    for folder, orig_sid, orig_link in (
        ("claude-insights", "sid-orig-ins", "2026-04-09-proj1-ins-orig"),
        ("claude-decisions", "sid-orig-dec", "2026-04-09-proj1-dec-orig"),
    ):
        bdir = run_root / folder
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / basename).write_text(
            _insight_text(date, orig_sid, orig_link), encoding="utf-8")

    issues = _scan(audit_env)
    assert len(issues) == 2, [i.note_path for i in issues]

    expected_orig_sid = {
        "claude-insights": "sid-orig-ins",
        "claude-decisions": "sid-orig-dec",
    }
    seen_folders = set()
    for i in issues:
        note_folder = Path(i.note_path).parent.name
        backup_folder = Path(i.extra["backup_path"]).parent.name
        assert note_folder == backup_folder, \
            f"backup from {backup_folder} paired with note in {note_folder}"
        assert i.extra["orig_sid"] == expected_orig_sid[note_folder]
        assert i.extra["category"] == "A"
        seen_folders.add(note_folder)
    assert seen_folders == {"claude-insights", "claude-decisions"}
