"""vault_doctor check: backfill canonical project names from git worktree paths.

Follow-up to PR #97 (issue #93). New session notes and insights now use the
canonical main-repo basename (e.g., ``obsidian-brain``) instead of the worktree
slug (e.g., ``obsidian-brain--issue-81-duplicate-sid-collision``). This check
backfills existing vault notes that still carry a worktree-derived project name.

**Phase 1 — Session notes (the source of truth)**

For each session note with a ``project_path:`` field:
1. Run ``git -C <project_path> rev-parse --git-common-dir`` (subprocess; list
   form — never shell=True; path comes from untrusted frontmatter).
2. Derive canonical = ``basename(parent(common_dir))``, lowercased, spaces and
   underscores normalized to hyphens — identical to
   ``obsidian_utils.canonical_project_name()``'s normalization.
3. If ``frontmatter.project != canonical``, propose rewriting ``project:`` and
   the ``claude/project/<name>`` tag (frontmatter block only).

git results are cached per project_path to avoid one subprocess per note for the
common case where many session notes share the same repo.

**Phase 2 — Insights (downstream, incl. _EXTRA_INSIGHT_FOLDERS)**

For each insight note with a ``source_session:`` UUID:
1. Look up the UUID in the Phase-1 session-note index (uses the CANONICAL value
   computed in Phase 1, NOT the current frontmatter — so stale session notes are
   corrected before their insights are resolved).
2. If the insight's project != canonical → same rewrite proposal.

**Edge cases**

+------------------------------------------+--------------------------------------+
| Scenario                                 | Behavior                             |
+------------------------------------------+--------------------------------------+
| Session note has no ``project_path:``    | WARN row; unresolved=True            |
| ``project_path`` no longer exists        | WARN row; unresolved=True            |
| ``project_path`` exists but not a git    | leave alone (cwd basename is canon)  |
|   repo (clean git nonzero exit)          |                                      |
| OSError / timeout from git               | WARN row; unresolved=True            |
| Insight whose source_session UUID doesn't| WARN row; unresolved=True            |
|   resolve to a session note              |                                      |
+------------------------------------------+--------------------------------------+

**Ordering note**

Conceptually ``project-name-normalization`` (underscore → hyphen) should run
first so the session-note ``project:`` field is already hyphen-normalised before
this check derives the canonical name to compare against. This check does NOT
enforce that ordering at runtime; operators should run normalization first.

This check is OPT-IN: excluded from the default all-checks sweep because it is a
one-time backfill audit with permanent WARN rows (not actionable per-run drift).
Run explicitly:

    python3 scripts/vault_doctor.py --check project-name-canonicalization

DEFAULT_WINDOW_DAYS = 9999 scans ALL notes regardless of age.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import Issue, Result
from .source_sessions import _EXTRA_INSIGHT_FOLDERS, _parse_frontmatter

NAME = "project-name-canonicalization"
DESCRIPTION = (
    "Backfill canonical project names (main-repo basename) in session notes "
    "and insights that still carry worktree-slug project names (opt-in, run "
    "via --check project-name-canonicalization)"
)
DEFAULT_WINDOW_DAYS = 9999  # scan ALL notes — this is a one-time backfill audit
OPT_IN = True  # excluded from the default all-checks sweep

# Timeout (seconds) for each git subprocess call.
_GIT_TIMEOUT = 5

# Reuse the same normalization as canonical_project_name() in obsidian_utils.
# We intentionally replicate the two-step transform here to avoid the hooks/
# sys.path dependency that project_name_normalization.py uses.
def _normalize(name: str) -> str:
    """Lowercase and replace spaces/underscores with hyphens (mirrors canonical_project_name)."""
    return name.lower().replace(" ", "-").replace("_", "-")


def _derive_canonical(project_path: str) -> tuple[str | None, str]:
    """Derive the canonical project name from a project_path via git.

    Returns (canonical_name_or_None, reason).

    reason values:
      "ok"           — name derived successfully
      "not-a-repo"   — git ran cleanly but path is not in a git work-tree
      "unavailable"  — git OSError or TimeoutExpired
      "not-found"    — project_path does not exist on disk
      "empty-output" — git returned nothing (should not happen)
      "resolve-failed" — relative common-dir path resolution failed
    """
    p = Path(project_path)
    if not p.exists():
        return (None, "not-found")

    try:
        result = subprocess.run(
            ["git", "-C", project_path, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return (None, "unavailable")

    if result.returncode != 0:
        return (None, "not-a-repo")

    common_dir = result.stdout.strip()
    if not common_dir:
        return (None, "empty-output")

    common_dir_path = Path(common_dir)
    if not common_dir_path.is_absolute():
        try:
            common_dir_path = (p / common_dir).resolve()
        except OSError:
            return (None, "resolve-failed")

    repo_name = common_dir_path.parent.name
    if not repo_name:
        return (None, "empty-output")

    return (_normalize(repo_name), "ok")


def scan(
    vault_path: str,
    sessions_folder: str,
    insights_folder: str,
    days: int,
    project: str | None = None,
) -> list[Issue]:
    """Scan session notes and insights for non-canonical project names.

    Phase 1 processes all session notes in ``sessions_folder``, derives the
    canonical name from ``project_path:`` via git (cached per path), and emits
    an Issue for any note whose ``project:`` field diverges.

    Phase 2 processes all insight notes (``insights_folder`` plus the
    conventional ``_EXTRA_INSIGHT_FOLDERS``), looks up each note's
    ``source_session:`` UUID in the Phase-1 index, and emits an Issue for any
    insight whose project diverges from the session-derived canonical.

    The ``days`` parameter is accepted for API compatibility but intentionally
    ignored (DEFAULT_WINDOW_DAYS = 9999 already scans everything).
    """
    vault = Path(vault_path)
    issues: list[Issue] = []

    # git result cache: project_path -> (canonical_or_None, reason)
    _git_cache: dict[str, tuple[str | None, str]] = {}

    # Phase-1 session index: session_id UUID -> canonical_name_or_None
    # (None when the note was unresolvable — Phase 2 will emit a WARN row)
    _session_canonical: dict[str, str | None] = {}

    # Coverage summary counters (partitions the denominator)
    p1_already_canonical = 0
    p1_proposed = 0
    p1_warn_no_project_path = 0
    p1_warn_not_found = 0
    p1_warn_unavailable = 0
    p1_left_alone_not_a_repo = 0
    p1_project_filtered = 0

    p2_already_canonical = 0
    p2_proposed = 0
    p2_warn_unresolved_session = 0
    p2_project_filtered = 0

    # ---------------------------------------------------------------- Phase 1
    sessions_dir = vault / sessions_folder
    if sessions_dir.is_dir():
        for md_file in sorted(sessions_dir.glob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(
                    f"[project-name-canonicalization] WARNING: could not read "
                    f"session note {md_file} ({exc}); skipping",
                    file=sys.stderr,
                )
                continue

            fm = _parse_frontmatter(content, source=str(md_file))
            note_project = fm.get("project", "") or ""
            session_id = fm.get("session_id", "") or ""
            project_path_raw = fm.get("project_path", "") or ""

            # Strip surrounding quotes that the frontmatter parser may leave
            project_path_raw = project_path_raw.strip('"').strip("'")

            # Optional project filter
            if project and note_project and note_project != project:
                p1_project_filtered += 1
                continue

            if not project_path_raw:
                # No project_path — cannot derive canonical; emit WARN
                p1_warn_no_project_path += 1
                if session_id:
                    _session_canonical[session_id] = None
                issues.append(Issue(
                    check=NAME,
                    note_path=str(md_file),
                    project=note_project or "unknown",
                    current_source=f"project: {note_project}",
                    proposed_source="",
                    reason="[WARN] missing project_path, cannot canonicalize",
                    confidence=0.0,
                    extra={
                        "signal_class": "canonicalize-unresolved",
                        "unresolved": True,
                        "old_project": note_project,
                        "new_project": "",
                        "phase": "session",
                    },
                ))
                continue

            # Derive canonical (with cache)
            if project_path_raw not in _git_cache:
                _git_cache[project_path_raw] = _derive_canonical(project_path_raw)
            canonical, reason = _git_cache[project_path_raw]

            if reason == "not-found":
                p1_warn_not_found += 1
                if session_id:
                    _session_canonical[session_id] = None
                issues.append(Issue(
                    check=NAME,
                    note_path=str(md_file),
                    project=note_project or "unknown",
                    current_source=f"project: {note_project}",
                    proposed_source="",
                    reason=(
                        f"[WARN] project_path no longer exists ({project_path_raw!r}),"
                        f" cannot derive canonical"
                    ),
                    confidence=0.0,
                    extra={
                        "signal_class": "canonicalize-unresolved",
                        "unresolved": True,
                        "old_project": note_project,
                        "new_project": "",
                        "project_path": project_path_raw,
                        "phase": "session",
                    },
                ))
                continue

            if reason == "unavailable":
                p1_warn_unavailable += 1
                if session_id:
                    _session_canonical[session_id] = None
                issues.append(Issue(
                    check=NAME,
                    note_path=str(md_file),
                    project=note_project or "unknown",
                    current_source=f"project: {note_project}",
                    proposed_source="",
                    reason=(
                        f"[WARN] git unavailable/timed out for {project_path_raw!r},"
                        f" cannot derive canonical"
                    ),
                    confidence=0.0,
                    extra={
                        "signal_class": "canonicalize-unresolved",
                        "unresolved": True,
                        "old_project": note_project,
                        "new_project": "",
                        "project_path": project_path_raw,
                        "phase": "session",
                    },
                ))
                continue

            if reason == "not-a-repo":
                # cwd basename IS canonical for non-git projects — leave alone
                p1_left_alone_not_a_repo += 1
                # Record the current project name as canonical for Phase 2
                if session_id and note_project:
                    _session_canonical[session_id] = note_project
                continue

            # reason in ("ok", "empty-output", "resolve-failed") — ok is the
            # success path; the others imply canonical is None but we handle
            # them similarly to unavailable.
            if canonical is None:
                p1_warn_unavailable += 1
                if session_id:
                    _session_canonical[session_id] = None
                issues.append(Issue(
                    check=NAME,
                    note_path=str(md_file),
                    project=note_project or "unknown",
                    current_source=f"project: {note_project}",
                    proposed_source="",
                    reason=(
                        f"[WARN] git returned no usable path for {project_path_raw!r}"
                        f" (reason={reason!r}), cannot derive canonical"
                    ),
                    confidence=0.0,
                    extra={
                        "signal_class": "canonicalize-unresolved",
                        "unresolved": True,
                        "old_project": note_project,
                        "new_project": "",
                        "project_path": project_path_raw,
                        "phase": "session",
                    },
                ))
                continue

            # Successful canonical derivation
            if session_id:
                _session_canonical[session_id] = canonical

            if note_project == canonical:
                p1_already_canonical += 1
                continue

            # Propose rewrite
            p1_proposed += 1
            issues.append(Issue(
                check=NAME,
                note_path=str(md_file),
                project=note_project or "unknown",
                current_source=f"project: {note_project}",
                proposed_source=f"project: {canonical}",
                reason=(
                    f"session note project {note_project!r} differs from canonical"
                    f" main-repo name {canonical!r} (derived via git --git-common-dir"
                    f" for {project_path_raw!r})"
                ),
                confidence=0.9,
                extra={
                    "signal_class": "canonicalize",
                    "unresolved": False,
                    "old_project": note_project,
                    "new_project": canonical,
                    "project_path": project_path_raw,
                    "phase": "session",
                },
            ))

    # ---------------------------------------------------------------- Phase 2
    insight_folders = [insights_folder] + [
        f for f in _EXTRA_INSIGHT_FOLDERS if f != insights_folder
    ]
    for folder_name in insight_folders:
        folder_path = vault / folder_name
        if not folder_path.is_dir():
            continue

        for md_file in sorted(folder_path.glob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(
                    f"[project-name-canonicalization] WARNING: could not read "
                    f"insight note {md_file} ({exc}); skipping",
                    file=sys.stderr,
                )
                continue

            fm = _parse_frontmatter(content, source=str(md_file))
            note_project = fm.get("project", "") or ""
            source_session = fm.get("source_session", "") or ""

            if not source_session:
                # No source_session — cannot derive canonical from session index
                continue

            # Optional project filter
            if project and note_project and note_project != project:
                p2_project_filtered += 1
                continue

            if source_session not in _session_canonical:
                # Session UUID not in index (session note missing or outside
                # sessions_folder) — emit WARN unresolved
                p2_warn_unresolved_session += 1
                issues.append(Issue(
                    check=NAME,
                    note_path=str(md_file),
                    project=note_project or "unknown",
                    current_source=f"project: {note_project}",
                    proposed_source="",
                    reason=(
                        f"[WARN] source_session {source_session!r} not in session index,"
                        f" cannot derive canonical"
                    ),
                    confidence=0.0,
                    extra={
                        "signal_class": "canonicalize-unresolved",
                        "unresolved": True,
                        "old_project": note_project,
                        "new_project": "",
                        "source_session": source_session,
                        "phase": "insight",
                    },
                ))
                continue

            canonical = _session_canonical[source_session]
            if canonical is None:
                # Session note was itself unresolvable — propagate WARN
                p2_warn_unresolved_session += 1
                issues.append(Issue(
                    check=NAME,
                    note_path=str(md_file),
                    project=note_project or "unknown",
                    current_source=f"project: {note_project}",
                    proposed_source="",
                    reason=(
                        f"[WARN] source_session {source_session!r} session note could"
                        f" not be canonicalized, cannot derive canonical for insight"
                    ),
                    confidence=0.0,
                    extra={
                        "signal_class": "canonicalize-unresolved",
                        "unresolved": True,
                        "old_project": note_project,
                        "new_project": "",
                        "source_session": source_session,
                        "phase": "insight",
                    },
                ))
                continue

            if note_project == canonical:
                p2_already_canonical += 1
                continue

            # Propose rewrite
            p2_proposed += 1
            issues.append(Issue(
                check=NAME,
                note_path=str(md_file),
                project=note_project or "unknown",
                current_source=f"project: {note_project}",
                proposed_source=f"project: {canonical}",
                reason=(
                    f"insight project {note_project!r} differs from canonical"
                    f" {canonical!r} (derived from source_session {source_session!r}"
                    f" session note)"
                ),
                confidence=0.9,
                extra={
                    "signal_class": "canonicalize",
                    "unresolved": False,
                    "old_project": note_project,
                    "new_project": canonical,
                    "source_session": source_session,
                    "phase": "insight",
                },
            ))

    # End-of-scan coverage summary. Buckets partition the scanned denominator.
    p1_total = (
        p1_already_canonical + p1_proposed + p1_warn_no_project_path
        + p1_warn_not_found + p1_warn_unavailable + p1_left_alone_not_a_repo
        + p1_project_filtered
    )
    p2_total = (
        p2_already_canonical + p2_proposed + p2_warn_unresolved_session
        + p2_project_filtered
    )
    if p1_total > 0 or p2_total > 0:
        print(
            f"[project-name-canonicalization] phase1 (sessions): {p1_total} scanned:"
            f" {p1_already_canonical} already-canonical, {p1_proposed} proposed,"
            f" {p1_warn_no_project_path} warn-no-project-path,"
            f" {p1_warn_not_found} warn-path-not-found,"
            f" {p1_warn_unavailable} warn-git-unavailable,"
            f" {p1_left_alone_not_a_repo} left-alone-non-git,"
            f" {p1_project_filtered} project-filtered",
            file=sys.stderr,
        )
        print(
            f"[project-name-canonicalization] phase2 (insights): {p2_total} scanned:"
            f" {p2_already_canonical} already-canonical, {p2_proposed} proposed,"
            f" {p2_warn_unresolved_session} warn-unresolved-session,"
            f" {p2_project_filtered} project-filtered",
            file=sys.stderr,
        )

    return issues


def apply(issues: list[Issue], backup_root: str) -> list[Result]:
    """Rewrite ``project:`` and ``claude/project/<name>`` in frontmatter.

    Only ``signal_class == "canonicalize"`` (``unresolved=False``) rows are
    auto-applyable. ``canonicalize-unresolved`` rows are returned as
    ``"unresolved"`` without touching the file.

    Defense-in-depth: raises ``RuntimeError`` if a non-unresolved row has an
    unexpected ``signal_class`` — this is a programming error, not a per-issue
    error. A wrong-signal rewrite could corrupt a note; we must fail loudly.

    Backup path: ``<backup_root>/<check-name>/<source-folder>/<basename>``
    (mirrors the source-sessions and audit-historic-repairs conventions).
    Backup write uses ``shutil.copy2``; the backup path is containment-checked
    against ``backup_root`` before the copy.
    """
    results: list[Result] = []

    for issue in issues:
        if issue.extra.get("unresolved"):
            results.append(Result(
                check=NAME,
                note_path=issue.note_path,
                status="unresolved",
            ))
            continue

        # Defense-in-depth: only "canonicalize" rows are auto-applyable.
        sc = issue.extra.get("signal_class", "")
        if sc != "canonicalize":
            raise RuntimeError(
                f"apply() refuses signal_class={sc!r} for {issue.note_path}; "
                f"only signal_class='canonicalize' (unresolved=False) rows are "
                f"auto-applyable."
            )

        old_project = issue.extra.get("old_project", "")
        new_project = issue.extra.get("new_project", "")
        if not old_project or not new_project:
            results.append(Result(
                check=NAME,
                note_path=issue.note_path,
                status="error",
                error="missing old_project or new_project in issue extra",
            ))
            continue

        note_path = Path(issue.note_path)
        try:
            content = note_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            results.append(Result(
                check=NAME,
                note_path=issue.note_path,
                status="error",
                error=str(exc),
            ))
            continue

        # Frontmatter rewrite (block only, not body text)
        if not content.startswith("---"):
            results.append(Result(
                check=NAME,
                note_path=issue.note_path,
                status="skipped",
            ))
            continue

        end = content.find("\n---", 3)
        if end == -1:
            results.append(Result(
                check=NAME,
                note_path=issue.note_path,
                status="skipped",
            ))
            continue

        fm_block = content[: end + 4]
        body = content[end + 4:]

        # Replace `project: <old_project>` (with or without quotes)
        new_fm = re.sub(
            rf"^(project:\s*)[\"']?{re.escape(old_project)}[\"']?\s*$",
            rf"\g<1>{new_project}",
            fm_block,
            flags=re.MULTILINE,
        )
        # Replace `claude/project/<old_project>` tag in frontmatter
        new_fm = new_fm.replace(
            f"claude/project/{old_project}",
            f"claude/project/{new_project}",
        )

        if new_fm == fm_block:
            results.append(Result(
                check=NAME,
                note_path=issue.note_path,
                status="skipped",
            ))
            continue

        # Backup original (with containment check)
        source_folder = note_path.parent.name
        backup_dir = Path(backup_root) / NAME / source_folder
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / note_path.name
            resolved_root = Path(backup_root).resolve()
            resolved_backup = backup_path.resolve()
            if resolved_root not in resolved_backup.parents:
                raise ValueError(
                    f"backup path {backup_path} would escape backup_root {backup_root}"
                )
            shutil.copy2(note_path, backup_path)
        except (OSError, ValueError) as exc:
            results.append(Result(
                check=NAME,
                note_path=issue.note_path,
                status="error",
                error=f"backup failed: {exc}",
            ))
            continue

        # Atomic write
        new_content = new_fm + body
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(
                dir=str(note_path.parent),
                prefix=".vd-projcanon-",
                suffix=".tmp",
            )
            try:
                os.write(fd, new_content.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(tmp, 0o600)
            os.replace(tmp, str(note_path))
            tmp = None  # consumed by os.replace
        except OSError as exc:
            results.append(Result(
                check=NAME,
                note_path=issue.note_path,
                status="error",
                error=str(exc),
            ))
            continue
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        results.append(Result(
            check=NAME,
            note_path=issue.note_path,
            status="applied",
            backup_path=str(backup_path),
        ))

    return results
