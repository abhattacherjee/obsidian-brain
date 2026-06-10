"""Tests for vault_doctor_checks/project_name_canonicalization.py (issue #99).

Covers:
  - Main-repo session: already-canonical → no issue
  - Worktree session: project_path is a git worktree; derives main-repo canonical
  - Insight canonicalized via session lookup (using CANONICAL value, not current fm)
  - Missing project_path → WARN unresolved
  - Deleted worktree path → WARN unresolved
  - Non-git project dir → leave alone
  - OSError/timeout from git → WARN unresolved
  - Tag rewrite asserted byte-level
  - Dry-run vs apply (apply changes only proposed notes; bodies preserved)
  - Unresolved rows not applyable
  - git-call caching (subprocess count for 2 notes sharing a path)
  - Summary partition (stderr output)
  - Fail-first discipline: defense-in-depth guard rejects wrong signal_class
  - Phase-2 uses CANONICAL value, not stale session-note frontmatter
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

# Register skip mark for tests that shell out to git.
_GIT_AVAILABLE = shutil.which("git") is not None
_REQUIRES_GIT = pytest.mark.skipif(
    not _GIT_AVAILABLE, reason="git binary not available on PATH"
)

import vault_doctor_checks.project_name_canonicalization as check  # noqa: E402
from vault_doctor_checks import Issue  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session_note(
    project: str,
    project_path: str = "",
    session_id: str = "sid-abc123",
    extra_frontmatter: str = "",
) -> str:
    pp_line = f'project_path: "{project_path}"\n' if project_path else ""
    return (
        "---\n"
        "type: claude-session\n"
        f"date: 2026-04-13\n"
        f"project: {project}\n"
        f"session_id: {session_id}\n"
        f"{pp_line}"
        "tags:\n"
        f"  - claude/session\n"
        f"  - claude/project/{project}\n"
        f"{extra_frontmatter}"
        "---\n\n"
        "# Session body\n"
        f"Some text mentioning project {project}.\n"
    )


def _insight_note(
    project: str,
    source_session: str = "sid-abc123",
) -> str:
    return (
        "---\n"
        "type: claude-insight\n"
        f"date: 2026-04-13\n"
        f"project: {project}\n"
        f"source_session: {source_session}\n"
        "tags:\n"
        f"  - claude/insight\n"
        f"  - claude/project/{project}\n"
        "---\n\n"
        "# Insight body\n"
        f"Some insight from project {project}.\n"
    )


def _init_git_repo(path: Path) -> None:
    """Create a minimal committed git repo at path."""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    (path / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


@pytest.fixture
def canon_vault(tmp_path):
    """Minimal vault with sessions and insights folders."""
    sessions = tmp_path / "vault" / "claude-sessions"
    insights = tmp_path / "vault" / "claude-insights"
    sessions.mkdir(parents=True)
    insights.mkdir(parents=True)
    return {
        "vault": tmp_path / "vault",
        "sessions": sessions,
        "insights": insights,
        "backups": tmp_path / "backups",
    }


def _scan(env, project=None):
    return check.scan(
        str(env["vault"]),
        "claude-sessions",
        "claude-insights",
        9999,
        project=project,
    )


# ---------------------------------------------------------------------------
# Phase 1 — Session notes
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_main_repo_session_already_canonical(canon_vault, tmp_path):
    """A session note whose project: already matches the canonical name → no issue."""
    repo = tmp_path / "my-project"
    repo.mkdir()
    _init_git_repo(repo)

    note = canon_vault["sessions"] / "2026-04-13-my-project-abcd.md"
    note.write_text(_session_note("my-project", project_path=str(repo)), encoding="utf-8")

    issues = _scan(canon_vault)
    # No proposed rewrite — already canonical
    session_issues = [i for i in issues if i.note_path == str(note)]
    assert session_issues == [], (
        f"Expected no issues for already-canonical session note, got: {session_issues}"
    )


@_REQUIRES_GIT
def test_worktree_session_derives_main_repo_canonical(canon_vault, tmp_path):
    """Worktree session note: project= worktree-slug → proposed canonical = main-repo name."""
    repo = tmp_path / "my-project"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "my-project--feature-x"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/x", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    note = canon_vault["sessions"] / "2026-04-13-my-project--feature-x-abcd.md"
    note.write_text(
        _session_note("my-project--feature-x", project_path=str(worktree)),
        encoding="utf-8",
    )

    issues = _scan(canon_vault)
    session_issues = [i for i in issues if i.note_path == str(note)]

    assert len(session_issues) == 1
    issue = session_issues[0]
    assert issue.extra["signal_class"] == "canonicalize"
    assert issue.extra["unresolved"] is False
    assert issue.extra["old_project"] == "my-project--feature-x"
    assert issue.extra["new_project"] == "my-project"
    assert issue.confidence == 0.9
    assert "project: my-project" in issue.proposed_source


def test_missing_project_path_emits_warn(canon_vault):
    """Session note with no project_path → WARN unresolved row."""
    note = canon_vault["sessions"] / "2026-04-13-proj-abcd.md"
    note.write_text(
        _session_note("some-project", project_path=""),
        encoding="utf-8",
    )

    issues = _scan(canon_vault)
    session_issues = [i for i in issues if i.note_path == str(note)]

    assert len(session_issues) == 1
    issue = session_issues[0]
    assert issue.extra["unresolved"] is True
    assert issue.extra["signal_class"] == "canonicalize-unresolved"
    assert issue.confidence == 0.0
    assert "missing project_path" in issue.reason


def test_deleted_project_path_emits_warn(canon_vault, tmp_path):
    """Session note with a project_path that no longer exists → WARN unresolved."""
    deleted_path = tmp_path / "deleted-worktree"  # never created

    note = canon_vault["sessions"] / "2026-04-13-proj-abcd.md"
    note.write_text(
        _session_note("some-project", project_path=str(deleted_path)),
        encoding="utf-8",
    )

    issues = _scan(canon_vault)
    session_issues = [i for i in issues if i.note_path == str(note)]

    assert len(session_issues) == 1
    issue = session_issues[0]
    assert issue.extra["unresolved"] is True
    assert "no longer exists" in issue.reason


def test_non_git_project_dir_left_alone(canon_vault, tmp_path):
    """project_path exists but isn't a git repo → leave alone (no issue)."""
    non_git = tmp_path / "plain-folder"
    non_git.mkdir()

    note = canon_vault["sessions"] / "2026-04-13-plain-folder-abcd.md"
    note.write_text(
        _session_note("plain-folder", project_path=str(non_git)),
        encoding="utf-8",
    )

    issues = _scan(canon_vault)
    session_issues = [i for i in issues if i.note_path == str(note)]
    assert session_issues == [], (
        f"Non-git project dir should produce no issue, got: {session_issues}"
    )


def test_git_unavailable_emits_warn(canon_vault, tmp_path):
    """When git subprocess raises OSError, emit WARN unresolved."""
    existing_dir = tmp_path / "some-dir"
    existing_dir.mkdir()

    note = canon_vault["sessions"] / "2026-04-13-proj-abcd.md"
    note.write_text(
        _session_note("some-project", project_path=str(existing_dir)),
        encoding="utf-8",
    )

    # Patch _derive_canonical to simulate OSError (git unavailable)
    with patch.object(check, "_derive_canonical", return_value=(None, "unavailable")):
        issues = _scan(canon_vault)

    session_issues = [i for i in issues if i.note_path == str(note)]
    assert len(session_issues) == 1
    issue = session_issues[0]
    assert issue.extra["unresolved"] is True
    assert "unavailable" in issue.reason.lower() or "timed out" in issue.reason.lower()


# ---------------------------------------------------------------------------
# Phase 2 — Insights via session lookup
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_insight_canonicalized_via_session_lookup(canon_vault, tmp_path):
    """Insight's project is canonicalized using the session-derived canonical."""
    repo = tmp_path / "canonical-project"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "canonical-project--feature-y"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/y", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    # Session note with worktree-derived project name
    sid = "session-uuid-001"
    session_note = canon_vault["sessions"] / "2026-04-13-session-001.md"
    session_note.write_text(
        _session_note(
            "canonical-project--feature-y",
            project_path=str(worktree),
            session_id=sid,
        ),
        encoding="utf-8",
    )

    # Insight that references the session and also has the worktree slug
    insight_note = canon_vault["insights"] / "2026-04-13-insight-001.md"
    insight_note.write_text(
        _insight_note("canonical-project--feature-y", source_session=sid),
        encoding="utf-8",
    )

    issues = _scan(canon_vault)

    session_issues = [i for i in issues if i.note_path == str(session_note)]
    insight_issues = [i for i in issues if i.note_path == str(insight_note)]

    # Session note should have a proposed rewrite
    assert len(session_issues) == 1
    assert session_issues[0].extra["new_project"] == "canonical-project"

    # Insight should also have a proposed rewrite, using the same canonical
    assert len(insight_issues) == 1
    ii = insight_issues[0]
    assert ii.extra["signal_class"] == "canonicalize"
    assert ii.extra["unresolved"] is False
    assert ii.extra["old_project"] == "canonical-project--feature-y"
    assert ii.extra["new_project"] == "canonical-project"
    assert ii.confidence == 0.9


@_REQUIRES_GIT
def test_phase2_uses_canonical_not_stale_session_frontmatter(canon_vault, tmp_path):
    """FAIL-FIRST: Phase 2 must use the CANONICAL value, not the session note's
    current (stale) frontmatter project. Seed a session note whose frontmatter
    says 'worktree-slug' but whose project_path resolves to 'canonical-name'.
    The insight must get 'canonical-name', not 'worktree-slug'."""
    repo = tmp_path / "canonical-name"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "canonical-name--worktree-branch"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/z", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    sid = "session-uuid-stale-test"

    # Session note: frontmatter still says worktree-slug (stale)
    session_note = canon_vault["sessions"] / "2026-04-13-stale-session.md"
    session_note.write_text(
        _session_note(
            "canonical-name--worktree-branch",  # stale frontmatter value
            project_path=str(worktree),
            session_id=sid,
        ),
        encoding="utf-8",
    )

    # Insight also has worktree slug — should be rewritten to canonical-name,
    # NOT to worktree-slug (which would be wrong — it would just copy the stale fm).
    insight_note = canon_vault["insights"] / "2026-04-13-stale-insight.md"
    insight_note.write_text(
        _insight_note("canonical-name--worktree-branch", source_session=sid),
        encoding="utf-8",
    )

    issues = _scan(canon_vault)
    insight_issues = [i for i in issues if i.note_path == str(insight_note)]

    assert len(insight_issues) == 1, (
        f"Expected 1 insight issue, got {len(insight_issues)}: {insight_issues}"
    )
    ii = insight_issues[0]
    # Must use the git-derived canonical, NOT the stale session-note frontmatter
    assert ii.extra["new_project"] == "canonical-name", (
        f"Phase 2 used stale frontmatter instead of canonical; new_project={ii.extra['new_project']!r}"
    )


def test_insight_with_unresolved_session_emits_warn(canon_vault):
    """Insight whose source_session UUID is not in the session index → WARN."""
    insight_note = canon_vault["insights"] / "2026-04-13-orphaned-insight.md"
    insight_note.write_text(
        _insight_note("some-project", source_session="unknown-session-uuid"),
        encoding="utf-8",
    )

    issues = _scan(canon_vault)
    insight_issues = [i for i in issues if i.note_path == str(insight_note)]

    assert len(insight_issues) == 1
    ii = insight_issues[0]
    assert ii.extra["unresolved"] is True
    assert "not in session index" in ii.reason


# ---------------------------------------------------------------------------
# Tag rewrite (byte-level)
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_apply_rewrites_tag_byte_level(canon_vault, tmp_path):
    """apply() rewrites BOTH project: field and claude/project/<name> tag exactly."""
    repo = tmp_path / "real-project"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "real-project--feature-tag"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/tag", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    note = canon_vault["sessions"] / "2026-04-13-tag-test.md"
    original_content = (
        "---\n"
        "type: claude-session\n"
        "project: real-project--feature-tag\n"
        f'project_path: "{str(worktree)}"\n'
        "session_id: tag-test-sid\n"
        "tags:\n"
        "  - claude/session\n"
        "  - claude/project/real-project--feature-tag\n"
        "---\n\n"
        "Body text mentioning real-project--feature-tag in body (must NOT be changed).\n"
    )
    note.write_bytes(original_content.encode("utf-8"))

    issues = _scan(canon_vault)
    session_issues = [i for i in issues if i.note_path == str(note)]
    assert len(session_issues) == 1, f"Expected 1 issue, got {session_issues}"

    backup_root = str(canon_vault["backups"])
    results = check.apply(session_issues, backup_root)

    assert len(results) == 1
    assert results[0].status == "applied"
    assert results[0].backup_path is not None

    new_content = note.read_bytes().decode("utf-8")
    # project: line rewritten
    assert "project: real-project\n" in new_content
    # tag rewritten
    assert "claude/project/real-project\n" in new_content
    # old value gone from frontmatter
    assert "project: real-project--feature-tag\n" not in new_content
    assert "claude/project/real-project--feature-tag\n" not in new_content
    # body preserved
    assert "Body text mentioning real-project--feature-tag in body" in new_content


@_REQUIRES_GIT
def test_apply_preserves_body(canon_vault, tmp_path):
    """apply() must not touch note body — only frontmatter block is rewritten."""
    repo = tmp_path / "the-project"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "the-project--some-branch"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/body", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    body_text = "Body: project: the-project--some-branch is mentioned here.\n"
    note = canon_vault["sessions"] / "2026-04-13-body-test.md"
    note.write_text(
        "---\n"
        "type: claude-session\n"
        "project: the-project--some-branch\n"
        f'project_path: "{str(worktree)}"\n'
        "session_id: body-sid\n"
        "tags:\n"
        "  - claude/project/the-project--some-branch\n"
        "---\n\n" + body_text,
        encoding="utf-8",
    )

    issues = _scan(canon_vault)
    note_issues = [i for i in issues if i.note_path == str(note)]
    assert len(note_issues) == 1

    check.apply(note_issues, str(canon_vault["backups"]))
    new_content = note.read_text(encoding="utf-8")

    # Body must be preserved verbatim
    assert body_text in new_content, (
        "Body was modified — only frontmatter should change"
    )


# ---------------------------------------------------------------------------
# Dry-run vs apply
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_dry_run_does_not_modify_file(canon_vault, tmp_path):
    """scan() is read-only: files unchanged after scan."""
    repo = tmp_path / "dry-project"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "dry-project--branch"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/dry", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    note = canon_vault["sessions"] / "2026-04-13-dry.md"
    original = _session_note("dry-project--branch", project_path=str(worktree))
    note.write_text(original, encoding="utf-8")

    _scan(canon_vault)
    assert note.read_text(encoding="utf-8") == original, "scan() must not modify files"


@_REQUIRES_GIT
def test_apply_only_touches_proposed_notes(canon_vault, tmp_path):
    """apply() only modifies notes with signal_class='canonicalize'; WARN rows untouched."""
    repo = tmp_path / "apply-project"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "apply-project--apply-branch"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/apply", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    # One applyable note (worktree path exists)
    proposed_note = canon_vault["sessions"] / "2026-04-13-proposed.md"
    proposed_note.write_text(
        _session_note("apply-project--apply-branch", project_path=str(worktree)),
        encoding="utf-8",
    )

    # One WARN note (no project_path)
    warn_note = canon_vault["sessions"] / "2026-04-13-warn.md"
    warn_original = _session_note("apply-project", project_path="")
    warn_note.write_text(warn_original, encoding="utf-8")

    issues = _scan(canon_vault)
    results = check.apply(issues, str(canon_vault["backups"]))

    # Proposed note should be applied
    applied = [r for r in results if r.status == "applied"]
    unresolved = [r for r in results if r.status == "unresolved"]
    assert len(applied) == 1
    assert len(unresolved) == 1

    # WARN note untouched
    assert warn_note.read_text(encoding="utf-8") == warn_original


# ---------------------------------------------------------------------------
# Unresolved rows not applyable
# ---------------------------------------------------------------------------

def test_apply_returns_unresolved_for_warn_rows(canon_vault):
    """apply() must return status='unresolved' for WARN rows, not attempt a write."""
    issue = Issue(
        check=check.NAME,
        note_path=str(canon_vault["sessions"] / "fake.md"),
        project="unknown",
        current_source="project: unknown",
        proposed_source="",
        reason="[WARN] missing project_path",
        confidence=0.0,
        extra={
            "signal_class": "canonicalize-unresolved",
            "unresolved": True,
            "old_project": "unknown",
            "new_project": "",
            "phase": "session",
        },
    )
    results = check.apply([issue], str(canon_vault["backups"]))
    assert len(results) == 1
    assert results[0].status == "unresolved"


def test_apply_defense_in_depth_wrong_signal_class(canon_vault, tmp_path):
    """FAIL-FIRST: apply() must raise RuntimeError for non-canonicalize signal_class rows."""
    issue = Issue(
        check=check.NAME,
        note_path=str(canon_vault["sessions"] / "fake.md"),
        project="some-project",
        current_source="project: some-project",
        proposed_source="project: canonical",
        reason="test",
        confidence=0.9,
        extra={
            "signal_class": "some-other-class",  # wrong — not "canonicalize"
            "unresolved": False,
            "old_project": "some-project",
            "new_project": "canonical",
            "phase": "session",
        },
    )
    with pytest.raises(RuntimeError, match="signal_class="):
        check.apply([issue], str(canon_vault["backups"]))


# ---------------------------------------------------------------------------
# git-call caching
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_git_caching_one_call_per_unique_path(canon_vault, tmp_path):
    """Two session notes sharing the same project_path make only one git call."""
    repo = tmp_path / "cache-test-project"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "cache-test-project--cached"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/cache", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    # Two session notes pointing at the same worktree path
    for name in ("note-a.md", "note-b.md"):
        (canon_vault["sessions"] / name).write_text(
            _session_note(
                "cache-test-project--cached",
                project_path=str(worktree),
                session_id=f"sid-{name}",
            ),
            encoding="utf-8",
        )

    call_count = 0
    _orig_derive = check._derive_canonical

    def _counted(path):
        nonlocal call_count
        call_count += 1
        return _orig_derive(path)

    with patch.object(check, "_derive_canonical", side_effect=_counted):
        issues = _scan(canon_vault)

    # Both notes should have proposed rewrites
    proposed = [i for i in issues if i.extra.get("signal_class") == "canonicalize"]
    assert len(proposed) == 2
    # Only ONE git call despite two notes (cache hit on second)
    assert call_count == 1, (
        f"Expected 1 git call for 2 notes with same path, got {call_count}"
    )


# ---------------------------------------------------------------------------
# Summary partition (stderr)
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_stderr_summary_partition(canon_vault, tmp_path, capsys):
    """The Phase-1 and Phase-2 summary lines are emitted to stderr."""
    repo = tmp_path / "summary-project"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "summary-project--branch"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/summary", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    sid = "summary-sid-001"
    session_note = canon_vault["sessions"] / "2026-04-13-summary.md"
    session_note.write_text(
        _session_note("summary-project--branch", project_path=str(worktree), session_id=sid),
        encoding="utf-8",
    )
    insight_note = canon_vault["insights"] / "2026-04-13-summary-insight.md"
    insight_note.write_text(
        _insight_note("summary-project--branch", source_session=sid),
        encoding="utf-8",
    )

    capsys.readouterr()
    _scan(canon_vault)
    captured = capsys.readouterr()

    assert "[project-name-canonicalization] phase1 (sessions)" in captured.err
    assert "[project-name-canonicalization] phase2 (insights)" in captured.err
    assert "proposed" in captured.err


# ---------------------------------------------------------------------------
# OPT_IN and NAME
# ---------------------------------------------------------------------------

def test_module_opt_in_and_name():
    """Module must be OPT_IN=True and NAME must match the expected slug."""
    assert check.NAME == "project-name-canonicalization"
    assert check.OPT_IN is True
    assert check.DEFAULT_WINDOW_DAYS == 9999


# ---------------------------------------------------------------------------
# apply(): backup path includes source folder (no cross-folder collision)
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_apply_backup_includes_source_folder(canon_vault, tmp_path):
    """Backups are written under <backup_root>/<check-name>/<folder>/<basename>."""
    repo = tmp_path / "backup-project"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "backup-project--backup-branch"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/backup", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    note = canon_vault["sessions"] / "2026-04-13-backup-test.md"
    note.write_text(
        _session_note("backup-project--backup-branch", project_path=str(worktree)),
        encoding="utf-8",
    )

    issues = _scan(canon_vault)
    assert len([i for i in issues if i.extra.get("signal_class") == "canonicalize"]) == 1

    backup_root = str(canon_vault["backups"])
    results = check.apply(issues, backup_root)

    applied = [r for r in results if r.status == "applied"]
    assert len(applied) == 1
    bp = applied[0].backup_path
    assert bp is not None
    assert os.path.isfile(bp)
    # Backup path must be under backup_root / check_name / folder / basename
    assert check.NAME in bp
    assert "claude-sessions" in bp
    assert note.name in bp


# ---------------------------------------------------------------------------
# apply(): missing extra fields
# ---------------------------------------------------------------------------

def test_apply_error_missing_extra_fields(canon_vault):
    """apply() returns error when Issue.extra lacks old_project/new_project."""
    issue = Issue(
        check=check.NAME,
        note_path=str(canon_vault["sessions"] / "fake.md"),
        project="test",
        current_source="project: test",
        proposed_source="project: canonical",
        reason="test",
        confidence=0.9,
        extra={
            "signal_class": "canonicalize",
            "unresolved": False,
            # missing old_project / new_project
        },
    )
    results = check.apply([issue], str(canon_vault["backups"]))
    assert len(results) == 1
    assert results[0].status == "error"
    assert "missing" in results[0].error


# ---------------------------------------------------------------------------
# apply(): note deleted between scan and apply
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_apply_error_note_deleted_between_scan_and_apply(canon_vault, tmp_path):
    """apply() returns error when the note is deleted after scan."""
    repo = tmp_path / "deleted-note-project"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "deleted-note-project--del"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/del", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    note = canon_vault["sessions"] / "2026-04-13-deleted.md"
    note.write_text(
        _session_note("deleted-note-project--del", project_path=str(worktree)),
        encoding="utf-8",
    )

    issues = _scan(canon_vault)
    note.unlink()  # delete before apply

    results = check.apply(issues, str(canon_vault["backups"]))
    assert len(results) == 1
    assert results[0].status == "error"


# ---------------------------------------------------------------------------
# Extra insight folders (_EXTRA_INSIGHT_FOLDERS)
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_extra_insight_folders_are_scanned(canon_vault, tmp_path):
    """Insights in _EXTRA_INSIGHT_FOLDERS (decisions, error-fixes, retros) are scanned."""
    from vault_doctor_checks.source_sessions import _EXTRA_INSIGHT_FOLDERS

    repo = tmp_path / "extra-folder-project"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "extra-folder-project--extra"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/extra", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    sid = "extra-folder-sid"
    session_note = canon_vault["sessions"] / "2026-04-13-extra-folder.md"
    session_note.write_text(
        _session_note("extra-folder-project--extra", project_path=str(worktree), session_id=sid),
        encoding="utf-8",
    )

    # Place a note in one of the extra folders (e.g., claude-decisions)
    extra_folder = canon_vault["vault"] / _EXTRA_INSIGHT_FOLDERS[0]
    extra_folder.mkdir(parents=True, exist_ok=True)
    decision_note = extra_folder / "2026-04-13-decision.md"
    decision_note.write_text(
        _insight_note("extra-folder-project--extra", source_session=sid),
        encoding="utf-8",
    )

    issues = _scan(canon_vault)
    decision_issues = [i for i in issues if i.note_path == str(decision_note)]

    assert len(decision_issues) == 1
    assert decision_issues[0].extra["new_project"] == "extra-folder-project"
