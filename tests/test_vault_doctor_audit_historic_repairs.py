"""Tests for the audit-historic-repairs vault_doctor check module (#95)."""

import sys
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


def test_non_source_session_backup_skipped(audit_env):
    """Backups from other checks (e.g. session notes) have no source_session."""
    basename = "2026-04-09-proj1-9999.md"
    run_dir = audit_env["backups"] / _run_dirname(_days_ago(30)) / "snapshot-integrity"
    run_dir.mkdir(parents=True)
    text = "---\ntype: claude-session\ndate: 2026-04-09\nproject: proj1\n---\n# S\n"
    (run_dir / basename).write_text(text, encoding="utf-8")
    (audit_env["sessions"] / basename).write_text(text, encoding="utf-8")
    assert _scan(audit_env) == []


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
