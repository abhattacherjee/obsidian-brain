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


@pytest.mark.parametrize(
    "raiser",
    [
        subprocess.TimeoutExpired(cmd=["git"], timeout=5),
        OSError("git binary not found"),
    ],
    ids=["timeout", "oserror"],
)
def test_git_unavailable_emits_warn_real_seam(canon_vault, tmp_path, raiser):
    """When the REAL subprocess seam raises (TimeoutExpired/OSError), the
    failure must route through _derive_canonical's except clause to a WARN
    unresolved row — kills the except→"not-a-repo" mutation. (An earlier
    version mocked _derive_canonical itself, ABOVE the seam, so that
    mutation survived.)"""
    existing_dir = tmp_path / "some-dir"
    existing_dir.mkdir()

    note = canon_vault["sessions"] / "2026-04-13-proj-abcd.md"
    note.write_text(
        _session_note("some-project", project_path=str(existing_dir)),
        encoding="utf-8",
    )

    with patch.object(check.subprocess, "run", side_effect=raiser):
        issues = _scan(canon_vault)

    session_issues = [i for i in issues if i.note_path == str(note)]
    assert len(session_issues) == 1
    issue = session_issues[0]
    assert issue.extra["unresolved"] is True
    assert issue.extra["signal_class"] == "canonicalize-unresolved"
    assert "unavailable" in issue.reason.lower() or "timed out" in issue.reason.lower()


def test_git_error_distinguished_from_not_a_repo(canon_vault, tmp_path):
    """A nonzero git exit that is NOT "not a git repository" (dubious
    ownership, corrupted .git, permission denied) must become a WARN
    unresolved row carrying the stderr snippet — NOT a silent leave-alone —
    and must NOT seed the Phase-2 index from the note's (possibly stale)
    frontmatter, which would generate a backwards 0.9 proposal for insights
    already carrying the true canonical name."""
    repo_dir = tmp_path / "dubious-repo"
    repo_dir.mkdir()

    sid = "dubious-sid-001"
    note = canon_vault["sessions"] / "2026-04-13-dubious.md"
    note.write_text(
        _session_note(
            "stale-worktree--slug", project_path=str(repo_dir), session_id=sid
        ),
        encoding="utf-8",
    )
    # Insight already carrying the TRUE canonical name — must NOT receive a
    # backwards proposal toward the session's stale slug.
    insight = canon_vault["insights"] / "2026-04-13-true-canon-insight.md"
    insight.write_text(
        _insight_note("real-canon", source_session=sid), encoding="utf-8"
    )

    fake = MagicMock(
        returncode=128,
        stdout="",
        stderr="fatal: detected dubious ownership in repository at '/x'",
    )
    with patch.object(check.subprocess, "run", return_value=fake):
        issues = _scan(canon_vault)

    session_issues = [i for i in issues if i.note_path == str(note)]
    assert len(session_issues) == 1
    si = session_issues[0]
    assert si.extra["unresolved"] is True
    assert "git error" in si.reason
    assert "dubious ownership" in si.reason  # stderr snippet surfaced

    # The insight must be a WARN row, NOT a backwards canonicalize proposal.
    insight_issues = [i for i in issues if i.note_path == str(insight)]
    assert len(insight_issues) == 1
    ii = insight_issues[0]
    assert ii.extra["signal_class"] == "canonicalize-unresolved", (
        f"git-error session seeded the index from stale frontmatter — insight "
        f"got a backwards proposal: {ii.extra}"
    )
    assert not [i for i in issues if i.extra.get("signal_class") == "canonicalize"]


def test_genuine_not_a_repo_still_left_alone(canon_vault, tmp_path):
    """The clean "not a git repository" stderr keeps the leave-alone path
    (regression guard for the git-error split — only OTHER nonzero exits
    become WARN rows)."""
    plain = tmp_path / "plain-dir"
    plain.mkdir()

    note = canon_vault["sessions"] / "2026-04-13-plain.md"
    note.write_text(
        _session_note("plain-dir", project_path=str(plain)), encoding="utf-8"
    )

    fake = MagicMock(
        returncode=128,
        stdout="",
        stderr="fatal: not a git repository (or any of the parent directories): .git",
    )
    with patch.object(check.subprocess, "run", return_value=fake):
        issues = _scan(canon_vault)

    assert [i for i in issues if i.note_path == str(note)] == []


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

    # Mutation-killer (rewrite-whole-file mutation): the body contains BOTH a
    # literal `project: <old>` line at column 0 AND the literal old tag
    # string — a rewrite applied to the whole file (instead of the
    # frontmatter block only) would change both.
    body_text = (
        "Body: discussion of the worktree.\n"
        "project: the-project--some-branch\n"
        "and a tag mention: claude/project/the-project--some-branch here.\n"
        "  - claude/project/the-project--some-branch\n"
    )
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

    results = check.apply(note_issues, str(canon_vault["backups"]))
    assert results[0].status == "applied"
    new_content = note.read_text(encoding="utf-8")

    # Body must be preserved BYTE-IDENTICALLY (split on the fm close fence)
    fm_end = new_content.find("\n---", 3)
    new_fm = new_content[: fm_end + 4]
    new_body = new_content[fm_end + 4:]
    assert new_body == "\n\n" + body_text, (
        f"Body was modified — only the frontmatter block may change.\n"
        f"Expected body:\n{body_text!r}\nGot body:\n{new_body!r}"
    )
    # And the frontmatter project line + tag WERE rewritten
    assert "\nproject: the-project\n" in new_fm
    assert "  - claude/project/the-project\n" in new_fm


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


# ---------------------------------------------------------------------------
# Production-shape tag rewrite (truncated slugified tags + prefix siblings)
# ---------------------------------------------------------------------------

_PROD_OLD_PROJECT = "obsidian-brain--issue-81-duplicate-sid-collision"
# What the production hook actually writes: slugify() collapses `--` to `-`
# AND truncates to 40 chars (hooks/obsidian_utils.py).
_PROD_TRUNCATED_TAG = "claude/project/obsidian-brain-issue-81-duplicate-sid-co"


def test_vendored_slugify_matches_production_shape():
    """The vendored _slugify must reproduce the production on-disk tag for the
    historical worktree slug byte-for-byte (collapse + 40-char truncation)."""
    assert check._slugify(_PROD_OLD_PROJECT) == "obsidian-brain-issue-81-duplicate-sid-co"


@_REQUIRES_GIT
def test_production_truncated_tag_rewritten_and_sibling_survives(canon_vault, tmp_path):
    """MUTATION-KILLER (silent tag no-op): the production tag is the SLUGIFIED
    (collapsed + truncated) form, which a plain
    replace('claude/project/<old_project>') never matches. After apply, BOTH
    the project: field and the truncated tag must be rewritten; a
    prefix-sharing sibling tag must survive byte-identically (an unanchored
    replace would mangle it) and be surfaced in the Result's error detail."""
    repo = tmp_path / "obsidian-brain"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / _PROD_OLD_PROJECT
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue/81", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    sibling_tag = _PROD_TRUNCATED_TAG + "-sibling"
    note = canon_vault["sessions"] / "2026-04-13-prod-shape.md"
    note.write_text(
        "---\n"
        "type: claude-session\n"
        f"project: {_PROD_OLD_PROJECT}\n"
        f'project_path: "{str(worktree)}"\n'
        "session_id: prod-sid\n"
        "tags:\n"
        "  - claude/session\n"
        f"  - {_PROD_TRUNCATED_TAG}\n"
        f"  - {sibling_tag}\n"
        "---\n\n"
        "Body.\n",
        encoding="utf-8",
    )

    issues = _scan(canon_vault)
    proposals = [i for i in issues if i.note_path == str(note)]
    assert len(proposals) == 1
    issue = proposals[0]
    assert issue.extra["new_project"] == "obsidian-brain"
    # scan captured the OBSERVED (truncated) tag, not the raw old_project form
    assert issue.extra["old_tags"] == [_PROD_TRUNCATED_TAG]
    assert issue.extra["new_tag"] == "claude/project/obsidian-brain"

    results = check.apply(proposals, str(canon_vault["backups"]))
    assert len(results) == 1
    assert results[0].status == "applied"
    # Leftover sibling is surfaced, not silently ignored
    assert results[0].error is not None
    assert "tag not rewritten" in results[0].error
    assert sibling_tag in results[0].error

    new_content = note.read_text(encoding="utf-8")
    assert "project: obsidian-brain\n" in new_content
    assert "  - claude/project/obsidian-brain\n" in new_content
    # Truncated old tag gone as a whole line
    assert f"  - {_PROD_TRUNCATED_TAG}\n" not in new_content
    # Prefix-sharing sibling survives BYTE-IDENTICALLY
    assert f"  - {sibling_tag}\n" in new_content


@_REQUIRES_GIT
def test_idempotent_after_apply(canon_vault, tmp_path):
    """After apply, a re-scan emits NO new rows for the fixed note — the
    truncated-tag case must not regenerate proposals once fully rewritten."""
    repo = tmp_path / "obsidian-brain"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / _PROD_OLD_PROJECT
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue/81b", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    sid = "idem-sid"
    note = canon_vault["sessions"] / "2026-04-13-idem.md"
    note.write_text(
        "---\n"
        "type: claude-session\n"
        f"project: {_PROD_OLD_PROJECT}\n"
        f'project_path: "{str(worktree)}"\n'
        f"session_id: {sid}\n"
        "tags:\n"
        f"  - {_PROD_TRUNCATED_TAG}\n"
        "---\n\n"
        "Body.\n",
        encoding="utf-8",
    )
    insight = canon_vault["insights"] / "2026-04-13-idem-insight.md"
    insight.write_text(
        _insight_note(_PROD_OLD_PROJECT, source_session=sid), encoding="utf-8"
    )

    issues = _scan(canon_vault)
    assert len([i for i in issues if i.extra.get("signal_class") == "canonicalize"]) == 2

    results = check.apply(issues, str(canon_vault["backups"]))
    assert all(r.status == "applied" for r in results)

    re_issues = _scan(canon_vault)
    assert re_issues == [], (
        f"Re-scan after apply must be clean; got: "
        f"{[(i.note_path, i.reason) for i in re_issues]}"
    )


# ---------------------------------------------------------------------------
# --project filter semantics (matches old OR canonical; index seeded first)
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_project_filter_matches_canonical_name(canon_vault, tmp_path):
    """`--project <canonical>` must INCLUDE worktree-slug notes that
    canonicalize TO that name (matching only the old name filters out exactly
    the notes being fixed). The old slug also still matches."""
    repo = tmp_path / "main-proj"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "main-proj--feat"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/f", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    note = canon_vault["sessions"] / "2026-04-13-filter.md"
    note.write_text(
        _session_note("main-proj--feat", project_path=str(worktree)),
        encoding="utf-8",
    )

    # Filter by CANONICAL name → the slug note must still surface
    by_canonical = _scan(canon_vault, project="main-proj")
    assert len([i for i in by_canonical if i.note_path == str(note)]) == 1

    # Filter by the OLD slug → also matches
    by_old = _scan(canon_vault, project="main-proj--feat")
    assert len([i for i in by_old if i.note_path == str(note)]) == 1

    # Unrelated filter → suppressed
    by_other = _scan(canon_vault, project="unrelated-project")
    assert [i for i in by_other if i.note_path == str(note)] == []


@_REQUIRES_GIT
def test_project_filter_keeps_phase2_index(canon_vault, tmp_path):
    """Sessions excluded from the Phase-1 REPORT by --project are still
    INDEXED — a filtered run must not produce spurious not-in-index WARNs and
    must still canonicalize matching insights via the session lookup."""
    repo = tmp_path / "main-proj"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "main-proj--feat"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/g", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    sid = "filter-idx-sid"
    session_note = canon_vault["sessions"] / "2026-04-13-filter-idx.md"
    session_note.write_text(
        _session_note("main-proj--feat", project_path=str(worktree), session_id=sid),
        encoding="utf-8",
    )
    # Insight under a DIFFERENT (stale) project name referencing that session
    insight = canon_vault["insights"] / "2026-04-13-filter-idx-insight.md"
    insight.write_text(
        _insight_note("insight-proj", source_session=sid), encoding="utf-8"
    )

    issues = _scan(canon_vault, project="insight-proj")

    # The session row is filtered out of the REPORT...
    assert [i for i in issues if i.note_path == str(session_note)] == []
    # ...but the insight still resolves through the index → a real proposal,
    # NOT a spurious "not in session index" WARN.
    insight_issues = [i for i in issues if i.note_path == str(insight)]
    assert len(insight_issues) == 1
    ii = insight_issues[0]
    assert ii.extra["signal_class"] == "canonicalize", (
        f"filtered session dropped from Phase-2 index: {ii.reason}"
    )
    assert ii.extra["new_project"] == "main-proj"


def test_empty_project_unattributable_suppressed_under_filter(canon_vault, tmp_path):
    """Deliberate decision: an empty-project note whose canonical cannot be
    derived is unattributable under --project — suppressed (counted as
    project-filtered) instead of surfacing an unrelated WARN row."""
    note = canon_vault["sessions"] / "2026-04-13-empty-unattributable.md"
    note.write_text(
        "---\n"
        "type: claude-session\n"
        "project: \n"
        f'project_path: "{tmp_path / "gone-dir"}"\n'
        "session_id: empty-sid\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )

    # Unfiltered: the not-found WARN row appears
    unfiltered = _scan(canon_vault)
    assert len([i for i in unfiltered if i.note_path == str(note)]) == 1

    # Filtered: suppressed (unattributable)
    filtered = _scan(canon_vault, project="some-project")
    assert [i for i in filtered if i.note_path == str(note)] == []


# ---------------------------------------------------------------------------
# Empty project field with resolvable path → WARN (never a 0.9 proposal)
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_empty_project_with_resolvable_path_is_warn(canon_vault, tmp_path):
    """A session note whose project: is empty but whose project_path resolves
    must be a WARN-unresolved row (apply() has no old value to anchor a
    rewrite) — NOT a resolvable 0.9 proposal. The derived canonical still
    seeds the Phase-2 index so the session's insights remain fixable."""
    repo = tmp_path / "resolvable-proj"
    repo.mkdir()
    _init_git_repo(repo)

    sid = "empty-proj-sid"
    note = canon_vault["sessions"] / "2026-04-13-empty-project.md"
    note.write_text(
        "---\n"
        "type: claude-session\n"
        "project: \n"
        f'project_path: "{str(repo)}"\n'
        f"session_id: {sid}\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    insight = canon_vault["insights"] / "2026-04-13-empty-project-insight.md"
    insight.write_text(
        _insight_note("stale-name", source_session=sid), encoding="utf-8"
    )

    issues = _scan(canon_vault)

    session_issues = [i for i in issues if i.note_path == str(note)]
    assert len(session_issues) == 1
    si = session_issues[0]
    assert si.extra["unresolved"] is True
    assert si.extra["signal_class"] == "canonicalize-unresolved"
    assert "project field empty" in si.reason
    assert "resolvable-proj" in si.reason  # canonical surfaced for the operator

    # The insight still canonicalizes through the index
    insight_issues = [i for i in issues if i.note_path == str(insight)]
    assert len(insight_issues) == 1
    assert insight_issues[0].extra["signal_class"] == "canonicalize"
    assert insight_issues[0].extra["new_project"] == "resolvable-proj"


# ---------------------------------------------------------------------------
# apply() skip reasons (error field populated)
# ---------------------------------------------------------------------------

def _canonicalize_issue(note_path: str, old: str = "old-proj", new: str = "new-proj") -> Issue:
    return Issue(
        check=check.NAME,
        note_path=note_path,
        project=old,
        current_source=f"project: {old}",
        proposed_source=f"project: {new}",
        reason="test",
        confidence=0.9,
        extra={
            "signal_class": "canonicalize",
            "unresolved": False,
            "old_project": old,
            "new_project": new,
            "old_tags": [],
            "new_tag": f"claude/project/{new}",
            "phase": "session",
        },
    )


def test_apply_skip_reasons_have_error_detail(canon_vault):
    """Skipped Results must carry a distinguishable reason in the error field:
    no frontmatter / unterminated frontmatter / project line not found (the
    last indicates scan-apply disagreement)."""
    no_fm = canon_vault["sessions"] / "no-fm.md"
    no_fm.write_text("just text\nproject: old-proj\n", encoding="utf-8")

    unterminated = canon_vault["sessions"] / "unterminated.md"
    unterminated.write_text("---\nproject: old-proj\nno closing fence\n", encoding="utf-8")

    drifted = canon_vault["sessions"] / "drifted.md"
    drifted.write_text(
        "---\nproject: somebody-else\n---\nBody.\n", encoding="utf-8"
    )

    issues = [
        _canonicalize_issue(str(no_fm)),
        _canonicalize_issue(str(unterminated)),
        _canonicalize_issue(str(drifted)),
    ]
    results = check.apply(issues, str(canon_vault["backups"]))
    by_path = {r.note_path: r for r in results}

    assert by_path[str(no_fm)].status == "skipped"
    assert by_path[str(no_fm)].error == "no frontmatter"

    assert by_path[str(unterminated)].status == "skipped"
    assert by_path[str(unterminated)].error == "unterminated frontmatter"

    assert by_path[str(drifted)].status == "skipped"
    assert "project: line not found for rewrite" in by_path[str(drifted)].error
    assert "old-proj" in by_path[str(drifted)].error


# ---------------------------------------------------------------------------
# apply() refuses non-UTF-8 notes (no U+FFFD bake-in)
# ---------------------------------------------------------------------------

def test_apply_rejects_non_utf8_note(canon_vault):
    """apply() reads strictly: an undecodable note must produce an error
    Result pointing at the encoding-corruption check, never a rewrite that
    bakes U+FFFD replacement chars into the file."""
    bad = canon_vault["sessions"] / "bad-encoding.md"
    bad.write_bytes(b"---\nproject: old-proj\n---\nBody \xff\xfe broken\n")
    original_bytes = bad.read_bytes()

    results = check.apply([_canonicalize_issue(str(bad))], str(canon_vault["backups"]))
    assert len(results) == 1
    assert results[0].status == "error"
    assert "not valid UTF-8" in results[0].error
    assert "encoding-corruption" in results[0].error
    # File untouched
    assert bad.read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# Caching semantics (fix: transient failures not cached; cached WARNs marked)
# ---------------------------------------------------------------------------

def test_cached_warn_reason_carries_suffix(canon_vault, tmp_path):
    """Two notes sharing a nonexistent project_path: the second WARN row is
    served from the cache and must say so."""
    gone = tmp_path / "gone-worktree"  # never created
    for name in ("2026-04-13-a-first.md", "2026-04-13-b-second.md"):
        (canon_vault["sessions"] / name).write_text(
            _session_note("some-proj", project_path=str(gone), session_id=f"sid-{name}"),
            encoding="utf-8",
        )

    issues = _scan(canon_vault)
    warn_rows = sorted(
        (i for i in issues if i.extra.get("phase") == "session"),
        key=lambda i: i.note_path,
    )
    assert len(warn_rows) == 2
    assert "(cached)" not in warn_rows[0].reason
    assert "(cached)" in warn_rows[1].reason


def test_unavailable_results_cached(canon_vault, tmp_path):
    """Failure results (timeout/OSError → "unavailable") ARE cached: their
    disposition is WARN-safe (never seeds the index, never leaves a note
    alone), and re-probing a hung path would cost the 5s git timeout once
    per note. The cache-served second WARN carries the "(cached)" suffix."""
    existing = tmp_path / "shared-dir"
    existing.mkdir()
    for name in ("2026-04-13-x.md", "2026-04-13-y.md"):
        (canon_vault["sessions"] / name).write_text(
            _session_note("some-proj", project_path=str(existing), session_id=f"sid-{name}"),
            encoding="utf-8",
        )

    calls = {"n": 0}

    def _raise(*args, **kwargs):
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=5)

    with patch.object(check.subprocess, "run", side_effect=_raise):
        issues = _scan(canon_vault)

    warn_rows = sorted(
        (i for i in issues if i.extra.get("phase") == "session"),
        key=lambda i: i.note_path,
    )
    assert len(warn_rows) == 2
    # ONE subprocess attempt total — the second note is served from cache
    assert calls["n"] == 1, f"failure result was not cached (calls={calls['n']})"
    assert "(cached)" not in warn_rows[0].reason
    assert "(cached)" in warn_rows[1].reason


# ---------------------------------------------------------------------------
# Duplicate session_id in the Phase-1 index (first wins + stderr warn)
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_duplicate_session_id_first_wins(canon_vault, tmp_path, capsys):
    """Two session notes claiming the same session_id: sorted-order first
    wins the Phase-2 index slot; a stderr warning surfaces the collision
    (mirrors the source-sessions convention)."""
    repo_a = tmp_path / "first-canon"
    repo_a.mkdir()
    _init_git_repo(repo_a)
    repo_b = tmp_path / "second-canon"
    repo_b.mkdir()
    _init_git_repo(repo_b)

    sid = "dup-sid-001"
    (canon_vault["sessions"] / "2026-04-13-a-dup.md").write_text(
        _session_note("stale-a", project_path=str(repo_a), session_id=sid),
        encoding="utf-8",
    )
    (canon_vault["sessions"] / "2026-04-13-b-dup.md").write_text(
        _session_note("stale-b", project_path=str(repo_b), session_id=sid),
        encoding="utf-8",
    )
    insight = canon_vault["insights"] / "2026-04-13-dup-insight.md"
    insight.write_text(_insight_note("stale-i", source_session=sid), encoding="utf-8")

    capsys.readouterr()
    issues = _scan(canon_vault)
    captured = capsys.readouterr()

    assert "duplicate session_id" in captured.err
    insight_issues = [i for i in issues if i.note_path == str(insight)]
    assert len(insight_issues) == 1
    # First (sorted) note's canonical wins
    assert insight_issues[0].extra["new_project"] == "first-canon"


# ---------------------------------------------------------------------------
# Normalization through the real git path (kills identity-_normalize mutation)
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_underscore_repo_dir_normalized(canon_vault, tmp_path):
    """A repo directory named My_Project must canonicalize to 'my-project'
    (lowercase, underscores → hyphens) — matches canonical_project_name()."""
    repo = tmp_path / "My_Project"
    repo.mkdir()
    _init_git_repo(repo)

    note = canon_vault["sessions"] / "2026-04-13-underscore.md"
    note.write_text(
        _session_note("stale-name", project_path=str(repo)), encoding="utf-8"
    )

    issues = _scan(canon_vault)
    proposals = [i for i in issues if i.note_path == str(note)]
    assert len(proposals) == 1
    assert proposals[0].extra["new_project"] == "my-project"


# ---------------------------------------------------------------------------
# Operator notices (--days ignored; missing folders)
# ---------------------------------------------------------------------------

def test_days_override_notice(canon_vault, capsys):
    """A non-default --days emits a stderr notice that it is ignored."""
    capsys.readouterr()
    check.scan(str(canon_vault["vault"]), "claude-sessions", "claude-insights", 30)
    captured = capsys.readouterr()
    assert "--days ignored" in captured.err

    # Default value → no notice
    capsys.readouterr()
    check.scan(
        str(canon_vault["vault"]), "claude-sessions", "claude-insights",
        check.DEFAULT_WINDOW_DAYS,
    )
    captured = capsys.readouterr()
    assert "--days ignored" not in captured.err


def test_missing_sessions_folder_warns(tmp_path, capsys):
    """A vault without the sessions folder gets a loud Phase-1-skipped warning."""
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)
    (vault / "claude-insights" / "2026-04-13-i.md").write_text(
        _insight_note("p", source_session="sid-zzz"), encoding="utf-8"
    )

    capsys.readouterr()
    issues = check.scan(str(vault), "claude-sessions", "claude-insights", 9999)
    captured = capsys.readouterr()

    assert "Phase 1 skipped" in captured.err
    # The insight surfaces as a not-in-index WARN
    assert len(issues) == 1
    assert issues[0].extra["signal_class"] == "canonicalize-unresolved"


def test_missing_insights_folder_warns(tmp_path, capsys):
    """A vault without the primary insights folder gets one stderr line."""
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)

    capsys.readouterr()
    check.scan(str(vault), "claude-sessions", "claude-insights", 9999)
    captured = capsys.readouterr()
    assert "insights folder" in captured.err


# ---------------------------------------------------------------------------
# Summary partition is total (incl. new buckets)
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_summary_partition_is_total(canon_vault, tmp_path, capsys):
    """The phase-1 summary buckets must sum to the scanned denominator —
    including the empty-project and git-error buckets added in review."""
    import re as _re

    repo = tmp_path / "part-proj"
    repo.mkdir()
    _init_git_repo(repo)
    non_git = tmp_path / "plain"
    non_git.mkdir()

    # 1 already-canonical, 1 warn-no-project-path, 1 left-alone, 1 warn-not-found,
    # 1 warn-empty-project
    (canon_vault["sessions"] / "a-canon.md").write_text(
        _session_note("part-proj", project_path=str(repo), session_id="s1"),
        encoding="utf-8",
    )
    (canon_vault["sessions"] / "b-nopath.md").write_text(
        _session_note("p2", project_path="", session_id="s2"), encoding="utf-8"
    )
    (canon_vault["sessions"] / "c-nongit.md").write_text(
        _session_note("plain", project_path=str(non_git), session_id="s3"),
        encoding="utf-8",
    )
    (canon_vault["sessions"] / "d-gone.md").write_text(
        _session_note("p4", project_path=str(tmp_path / "gone"), session_id="s4"),
        encoding="utf-8",
    )
    (canon_vault["sessions"] / "e-empty.md").write_text(
        "---\ntype: claude-session\nproject: \n"
        f'project_path: "{str(repo)}"\nsession_id: s5\n---\nBody.\n',
        encoding="utf-8",
    )

    capsys.readouterr()
    check.scan(str(canon_vault["vault"]), "claude-sessions", "claude-insights", 9999)
    captured = capsys.readouterr()

    m = _re.search(r"phase1 \(sessions\): (\d+) scanned:(.*)", captured.err)
    assert m, f"no phase1 summary in stderr: {captured.err!r}"
    total = int(m.group(1))
    buckets = [int(x) for x in _re.findall(r" (\d+) [a-z-]+[,]?", m.group(2))]
    assert total == 5
    assert sum(buckets) == total, (
        f"phase1 buckets {buckets} do not partition total {total}: {m.group(0)}"
    )


# ---------------------------------------------------------------------------
# Dispatcher end-to-end (opt-in registration + JSON shape)
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_dispatcher_e2e_opt_in(canon_vault, tmp_path):
    """End-to-end through scripts/vault_doctor.py: --check
    project-name-canonicalization emits a canonicalize row and exits 1; the
    default sweep (no --check) excludes the opt-in check entirely."""
    import json as _json

    repo = tmp_path / "e2e-proj"
    repo.mkdir()
    _init_git_repo(repo)
    worktree = tmp_path / "e2e-proj--feature"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/e2e", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (canon_vault["sessions"] / "2026-04-13-e2e.md").write_text(
        _session_note("e2e-proj--feature", project_path=str(worktree)),
        encoding="utf-8",
    )

    dispatcher = _SCRIPTS_DIR / "vault_doctor.py"
    base_cmd = [
        sys.executable, str(dispatcher),
        "--vault", str(canon_vault["vault"]),
        "--sessions-folder", "claude-sessions",
        "--insights-folder", "claude-insights",
        "--json",
    ]

    # Named check → exit 1 + a canonicalize row
    proc = subprocess.run(
        base_cmd + ["--check", "project-name-canonicalization"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    data = _json.loads(proc.stdout)
    rows = [i for i in data["issues"] if i["check"] == check.NAME]
    assert any(r.get("signal_class") == "canonicalize" for r in rows), rows

    # Default sweep → opt-in check excluded (zero rows for it)
    proc2 = subprocess.run(base_cmd, capture_output=True, text=True, timeout=120)
    data2 = _json.loads(proc2.stdout)
    assert all(i["check"] != check.NAME for i in data2["issues"]), (
        f"opt-in check ran in the default sweep: "
        f"{[i for i in data2['issues'] if i['check'] == check.NAME]}"
    )


# ---------------------------------------------------------------------------
# Snapshot notes excluded from Phase 1 + the Phase-2 index
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_snapshot_does_not_steal_index_slot(canon_vault, tmp_path):
    """Live-vault finding: snapshot notes share the session's session_id, lack
    project_path by design, and sort BEFORE the real session note
    ("-snapshot" < ".md") — under first-wins they would seed None and degrade
    every insight of that session to WARN. They must be skipped entirely
    (mirrors source_sessions/_list_all_session_notes and session_coverage)."""
    repo = tmp_path / "snap-proj"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "snap-proj--feat"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/snap", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    sid = "snap-sid-001"
    # Snapshot: sorts BEFORE the session note, same sid, no project_path
    snapshot = canon_vault["sessions"] / "2026-04-13-snap-proj--feat-ab12-snapshot-101010.md"
    snapshot.write_text(
        "---\n"
        "type: claude-snapshot\n"
        "project: snap-proj--feat\n"
        f"session_id: {sid}\n"
        "tags:\n"
        "  - claude/snapshot\n"
        "  - claude/project/snap-proj-feat\n"
        "---\n\nSnapshot body.\n",
        encoding="utf-8",
    )
    session_note = canon_vault["sessions"] / "2026-04-13-snap-proj--feat-ab12.md"
    session_note.write_text(
        _session_note("snap-proj--feat", project_path=str(worktree), session_id=sid),
        encoding="utf-8",
    )
    insight = canon_vault["insights"] / "2026-04-13-snap-insight.md"
    insight.write_text(
        _insight_note("snap-proj--feat", source_session=sid), encoding="utf-8"
    )

    issues = _scan(canon_vault)

    # No row for the snapshot (skipped, not a WARN missing-project-path)
    assert [i for i in issues if i.note_path == str(snapshot)] == []
    # The REAL session note won the index slot → insight gets a proposal,
    # not an unresolved WARN.
    insight_issues = [i for i in issues if i.note_path == str(insight)]
    assert len(insight_issues) == 1
    assert insight_issues[0].extra["signal_class"] == "canonicalize", (
        f"snapshot stole the index slot: {insight_issues[0].reason}"
    )
    assert insight_issues[0].extra["new_project"] == "snap-proj"


# ---------------------------------------------------------------------------
# N1: leftover detection is format-agnostic (wider than the rewrite)
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_inline_array_tag_surfaced_as_leftover(canon_vault, tmp_path):
    """An inline-array tag form (`tags: [a, b]`) is outside the line-format
    rewrite's reach — the OLD tag stays in place. The token-scan leftover
    check must surface it in the applied Result's error detail instead of
    reproducing the silent no-op (applied, error=None, re-scan clean)."""
    repo = tmp_path / "inline-proj"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "inline-proj--feat"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/inline", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    note = canon_vault["sessions"] / "2026-04-13-inline-array.md"
    note.write_text(
        "---\n"
        "type: claude-session\n"
        "project: inline-proj--feat\n"
        f'project_path: "{str(worktree)}"\n'
        "session_id: inline-sid\n"
        "tags: [claude/session, claude/project/inline-proj--feat]\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )

    issues = _scan(canon_vault)
    proposals = [i for i in issues if i.note_path == str(note)]
    assert len(proposals) == 1
    # The line-format capture finds nothing in the inline array
    assert proposals[0].extra["old_tags"] == []

    results = check.apply(proposals, str(canon_vault["backups"]))
    assert len(results) == 1
    assert results[0].status == "applied"
    # The unhandled inline tag MUST be surfaced, not silently left
    assert results[0].error is not None, (
        "inline-array old tag was silently left behind (applied, error=None)"
    )
    assert "tag not rewritten" in results[0].error
    assert "claude/project/inline-proj--feat" in results[0].error

    # The project: line itself was rewritten; the inline tag untouched
    new_content = note.read_text(encoding="utf-8")
    assert "project: inline-proj\n" in new_content
    assert "tags: [claude/session, claude/project/inline-proj--feat]" in new_content


# ---------------------------------------------------------------------------
# S-N1: identical claude/project tag lines deduped after rewrite
# ---------------------------------------------------------------------------

@_REQUIRES_GIT
def test_duplicate_tag_lines_deduped_after_rewrite(canon_vault, tmp_path):
    """A note carrying both the old tag form AND the already-correct new tag
    would end the rewrite with two identical list items — apply() must keep
    only the first occurrence."""
    repo = tmp_path / "dedupe-proj"
    repo.mkdir()
    _init_git_repo(repo)

    worktree = tmp_path / "dedupe-proj--feat"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/dedupe", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    note = canon_vault["sessions"] / "2026-04-13-dedupe.md"
    note.write_text(
        "---\n"
        "type: claude-session\n"
        "project: dedupe-proj--feat\n"
        f'project_path: "{str(worktree)}"\n'
        "session_id: dedupe-sid\n"
        "tags:\n"
        "  - claude/session\n"
        "  - claude/project/dedupe-proj--feat\n"
        "  - claude/project/dedupe-proj\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )

    issues = _scan(canon_vault)
    proposals = [i for i in issues if i.note_path == str(note)]
    assert len(proposals) == 1

    results = check.apply(proposals, str(canon_vault["backups"]))
    assert results[0].status == "applied"
    # No leftover detail — everything resolved to the single new tag
    assert results[0].error is None, results[0].error

    new_content = note.read_text(encoding="utf-8")
    assert new_content.count("  - claude/project/dedupe-proj\n") == 1, (
        f"duplicate tag lines not deduped:\n{new_content}"
    )
    assert "claude/project/dedupe-proj--feat" not in new_content


# ---------------------------------------------------------------------------
# S-N3: vendored _slugify parity with hooks/obsidian_utils.slugify
# ---------------------------------------------------------------------------

def test_vendored_slugify_parity_with_hooks():
    """Drift guard: the vendored _slugify must stay byte-compatible with the
    production hooks slugify for adversarial inputs (40-char boundary
    mid-word, boundary on a hyphen, unicode, leading/trailing separators,
    double-hyphen collapse, empty)."""
    hooks_dir = str(Path(__file__).parent.parent / "hooks")
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)
    import obsidian_utils

    adversarial = [
        _PROD_OLD_PROJECT,                            # the real-world case
        "a" * 39 + "-" + "b" * 10,                    # char 40 is the hyphen → rstrip
        "a" * 45,                                     # truncation mid-word
        "x" * 38 + "--" + "y" * 10,                   # double-hyphen collapse at boundary
        "Café_Über--Projekt",                         # unicode → collapsed to '-'
        "--leading-and-trailing--",                   # strip('-') both ends
        "___",                                        # collapses to nothing → fallback
        "",                                           # empty → fallback
        "  spaced  out  name  ",                      # whitespace runs
        "UPPER_case.With.Dots",                       # mixed separators
    ]
    for text in adversarial:
        assert check._slugify(text) == obsidian_utils.slugify(text), (
            f"vendored _slugify drifted from hooks slugify for {text!r}: "
            f"{check._slugify(text)!r} != {obsidian_utils.slugify(text)!r}"
        )
