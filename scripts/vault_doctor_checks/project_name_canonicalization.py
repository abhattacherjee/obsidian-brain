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

git results are cached per project_path to avoid one subprocess per note for
the common case where many session notes share the same repo. Failure results
("unavailable"/"git-error") are cached too: their disposition is WARN-safe
(they never seed the Phase-2 index and never leave a note alone), so the
worst case of a stale cache entry is a redundant WARN row — while an UNcached
hung path would cost the 5s git timeout once per note. WARN reasons served
from the cache carry a "(cached)" suffix.

**Tag rewriting**

Production hooks write the project tag as ``claude/project/{slugify(project)}``
where slugify (hooks/obsidian_utils.py) collapses non-alphanumeric runs to a
single ``-`` AND truncates to 40 chars — so a worktree slug like
``obsidian-brain--issue-81-duplicate-sid-collision`` appears on disk as the
truncated ``claude/project/obsidian-brain-issue-81-duplicate-sid-co``. scan()
therefore captures the OBSERVED ``claude/project/*`` tag lines from each
note's frontmatter and records the ones matching the old project (raw or
slugified form) in ``extra["old_tags"]``; apply() rewrites exactly those
lines with an anchored multiline regex (so a prefix-sharing sibling tag such
as ``claude/project/foo-extra`` is never mangled).

The REWRITE is deliberately line-format-only: it handles the block-list tag
form the hooks write (``- claude/project/<name>`` list items) and does not
attempt inline-array (``tags: [a, b]``) or scalar forms. The post-rewrite
LEFTOVER detection is deliberately WIDER: it token-scans the whole
frontmatter block for ``claude/project/*`` occurrences in ANY format and
reports every token that differs from the expected new tag in the applied
Result's error detail — so an unhandled tag format degrades to a visible
"applied with detail" instead of a silent no-op. Identical claude/project
tag LINES left behind by the rewrite (old form rewritten next to an
already-correct new tag) are deduped.

**Phase 2 — Insights (downstream, incl. _EXTRA_INSIGHT_FOLDERS)**

For each insight note with a ``source_session:`` UUID:
1. Look up the UUID in the Phase-1 session-note index (uses the CANONICAL value
   computed in Phase 1, NOT the current frontmatter — so stale session notes are
   corrected before their insights are resolved). Sessions excluded from the
   Phase-1 REPORT by ``--project`` are still INDEXED, so a filtered run never
   produces spurious not-in-index WARNs.
2. If the insight's project != canonical → same rewrite proposal.

**Edge cases**

+------------------------------------------+--------------------------------------+
| Scenario                                 | Behavior                             |
+------------------------------------------+--------------------------------------+
| Session note has no ``project_path:``    | WARN row; unresolved=True            |
| ``project_path`` no longer exists        | WARN row; unresolved=True            |
| ``project_path`` exists but not a git    | leave alone (cwd basename is canon)  |
|   repo (git says "not a git repository") |                                      |
| git exits nonzero for any OTHER reason   | WARN row ("git-error", stderr        |
|   (dubious ownership, corrupt .git, …)   |   snippet); session NOT seeded from  |
|                                          |   frontmatter — prevents backwards   |
|                                          |   Phase-2 proposals                  |
| OSError / timeout from git               | WARN row; unresolved=True            |
| ``project:`` field empty but path        | WARN row ("cannot rewrite in place") |
|   resolves                               |   — canonical still seeds Phase 2    |
| Insight whose source_session UUID doesn't| WARN row; unresolved=True            |
|   resolve to a session note              |                                      |
| Snapshot note (``type: claude-snapshot``)| skipped entirely (counted) — not the |
|   in the sessions folder                 |   session note; no project_path by   |
|                                          |   design, and "-snapshot" sorts      |
|                                          |   before ".md" so it would steal the |
|                                          |   first-wins index slot              |
+------------------------------------------+--------------------------------------+

**--project filter semantics**

A note matches the filter when the filter value equals EITHER its current
``project:`` frontmatter OR its derived canonical name — so
``--project obsidian-brain`` includes the worktree-slug notes that are about to
be canonicalized TO obsidian-brain (matching only the old name would filter out
exactly the notes being fixed). Notes that match neither (including
empty-project notes whose canonical could not be derived — unattributable) are
counted as project-filtered and suppressed from the report; Phase-1 index
seeding always happens BEFORE the filter.

**Ordering note**

Conceptually ``project-name-normalization`` (underscore → hyphen) should run
first so the session-note ``project:`` field is already hyphen-normalised before
this check derives the canonical name to compare against. This check does NOT
enforce that ordering at runtime; operators should run normalization first.

This check is OPT-IN: excluded from the default all-checks sweep because it is a
one-time backfill audit with permanent WARN rows (not actionable per-run drift).
Run explicitly:

    python3 scripts/vault_doctor.py --check project-name-canonicalization

DEFAULT_WINDOW_DAYS = 9999 scans ALL notes regardless of age; ``--days`` is
ignored (a stderr notice is emitted when a non-default value is passed).
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

# Anchored tag-line matcher for `- claude/project/<name>` frontmatter list
# items (optionally quoted). Anchoring start/end of line means a
# prefix-sharing sibling tag (claude/project/foo-extra vs claude/project/foo)
# can never be partially matched/mangled.
_TAG_LINE_RE = re.compile(
    r"^\s*-\s*[\"']?(claude/project/[^\s\"']+)[\"']?\s*$", re.MULTILINE
)

# Format-agnostic token matcher for the post-rewrite LEFTOVER check: catches
# claude/project tags in ANY frontmatter form (block list, inline array
# `tags: [a, b]`, scalar, flow map) so an unhandled format degrades to a
# visible "applied with detail" instead of reproducing the silent tag no-op.
# Deliberately WIDER than the line-anchored rewrite regex above.
_TAG_TOKEN_RE = re.compile(r"claude/project/[^\s,\]\"'}]+")


def _normalize(name: str) -> str:
    """Lowercase and replace spaces/underscores with hyphens (mirrors canonical_project_name)."""
    return name.lower().replace(" ", "-").replace("_", "-")


def _slugify(text: str, max_len: int = 40) -> str:
    """Vendored copy of hooks/obsidian_utils.slugify — MUST stay byte-compatible.

    Production session notes carry ``claude/project/{slugify(project)}`` tags
    (obsidian_session_log.py), so the on-disk tag for a long worktree slug is
    collapsed (``--`` → ``-``) AND truncated to 40 chars. We can't import the
    hooks tree here (different sys.path root), so the rules are vendored:
    lowercase, collapse non-alphanumeric runs to single ``-``, strip hyphens,
    truncate to max_len with trailing-hyphen rstrip, fallback "session".
    """
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text or "session"


def _frontmatter_block(content: str) -> str:
    """Return the frontmatter block (including both --- fences) or ''."""
    if not content.startswith("---"):
        return ""
    end = content.find("\n---", 3)
    if end == -1:
        return ""
    return content[: end + 4]


def _observed_old_tags(content: str, old_project: str, new_tag: str) -> list[str]:
    """Capture the observed claude/project tags belonging to old_project.

    Matches both the raw form (``claude/project/<old_project>``) and the
    production slugified form (``claude/project/{slugify(old_project)}`` —
    collapsed + 40-char truncated). Tags already equal to ``new_tag`` and any
    OTHER claude/project tags (e.g. prefix-sharing siblings) are excluded —
    apply() must never touch those.
    """
    fm_block = _frontmatter_block(content)
    if not fm_block:
        return []
    expected_old = {
        f"claude/project/{old_project}",
        f"claude/project/{_slugify(old_project)}",
    }
    out: list[str] = []
    for tag in _TAG_LINE_RE.findall(fm_block):
        if tag == new_tag:
            continue
        if tag in expected_old and tag not in out:
            out.append(tag)
    return out


def _derive_canonical(project_path: str) -> tuple[str | None, str, str]:
    """Derive the canonical project name from a project_path via git.

    Returns (canonical_name_or_None, reason, detail).

    reason values:
      "ok"           — name derived successfully
      "not-a-repo"   — git ran and said "not a git repository" (normal case)
      "git-error"    — git exited nonzero for any OTHER reason (dubious
                       ownership, corrupt .git, permission denied, …);
                       detail carries the first 200 chars of stderr
      "unavailable"  — git OSError or SubprocessError (incl. timeout)
      "not-found"    — project_path does not exist on disk
      "empty-output" — git returned nothing (should not happen)
      "resolve-failed" — relative common-dir path resolution failed

    Distinguishing "not-a-repo" from "git-error" matters: a non-repo dir is a
    NORMAL condition (cwd basename IS canonical → leave alone), but a
    corrupted/denied repo must NOT be silently treated as canonical — that
    would promote the note's stale slug into the Phase-2 index and generate
    backwards rewrite proposals for already-correct insights.
    """
    p = Path(project_path)
    if not p.exists():
        return (None, "not-found", "")

    try:
        result = subprocess.run(
            ["git", "-C", project_path, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        # SubprocessError covers TimeoutExpired — matches the exception set
        # of obsidian_utils._git_canonical_project_name_with_reason exactly.
        return (None, "unavailable", "")

    if result.returncode != 0:
        stderr_txt = (result.stderr or "").strip()
        if "not a git repository" in stderr_txt.lower():
            return (None, "not-a-repo", "")
        return (None, "git-error", stderr_txt[:200])

    common_dir = result.stdout.strip()
    if not common_dir:
        return (None, "empty-output", "")

    common_dir_path = Path(common_dir)
    if not common_dir_path.is_absolute():
        try:
            common_dir_path = (p / common_dir).resolve()
        except OSError:
            return (None, "resolve-failed", "")

    repo_name = common_dir_path.parent.name
    if not repo_name:
        return (None, "empty-output", "")

    return (_normalize(repo_name), "ok", "")


def _warn_issue(
    note_path: str,
    note_project: str,
    reason: str,
    phase: str,
    **extra_fields,
) -> Issue:
    """Build a WARN-unresolved Issue row (confidence 0.0, never applyable)."""
    extra = {
        "signal_class": "canonicalize-unresolved",
        "unresolved": True,
        "old_project": note_project,
        "new_project": "",
        "phase": phase,
    }
    extra.update(extra_fields)
    return Issue(
        check=NAME,
        note_path=note_path,
        project=note_project or "unknown",
        current_source=f"project: {note_project}",
        proposed_source="",
        reason=reason,
        confidence=0.0,
        extra=extra,
    )


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
    an Issue for any note whose ``project:`` field diverges. The session_id →
    canonical index is ALWAYS seeded before the ``--project`` filter is
    applied, so filtered runs keep Phase-2 lookups intact.

    Phase 2 processes all insight notes (``insights_folder`` plus the
    conventional ``_EXTRA_INSIGHT_FOLDERS``), looks up each note's
    ``source_session:`` UUID in the Phase-1 index, and emits an Issue for any
    insight whose project diverges from the session-derived canonical.

    The ``days`` parameter is intentionally ignored (this is a full-vault
    backfill); a stderr notice is emitted when a non-default value arrives.
    """
    vault = Path(vault_path)
    issues: list[Issue] = []

    if days != DEFAULT_WINDOW_DAYS:
        print(
            f"[{NAME}] notice: --days ignored by this check "
            f"(full-vault backfill; got days={days})",
            file=sys.stderr,
        )

    # git result cache: project_path -> (canonical_or_None, reason, detail).
    # Failure results (unavailable/git-error) are cached too: their
    # disposition is WARN-safe (never seeds the index, never leaves a note
    # alone), and re-probing a hung path would cost the 5s timeout per note.
    _git_cache: dict[str, tuple[str | None, str, str]] = {}

    # Phase-1 session index: session_id UUID -> canonical_name_or_None
    # (None when the note was unresolvable — Phase 2 will emit a WARN row)
    _session_canonical: dict[str, str | None] = {}

    # Coverage summary counters — together with the filtered/unreadable
    # buckets these PARTITION the scanned denominator exactly.
    p1_already_canonical = 0
    p1_proposed = 0
    p1_warn_no_project_path = 0
    p1_warn_not_found = 0
    p1_warn_unavailable = 0
    p1_warn_git_error = 0
    p1_warn_empty_project = 0
    p1_left_alone_not_a_repo = 0
    p1_project_filtered = 0
    p1_unreadable = 0
    p1_snapshots_skipped = 0

    p2_already_canonical = 0
    p2_proposed = 0
    p2_warn_unresolved_session = 0
    p2_warn_empty_project = 0
    p2_no_source_session = 0
    p2_project_filtered = 0
    p2_unreadable = 0

    # ---------------------------------------------------------------- Phase 1
    sessions_dir = vault / sessions_folder
    if not sessions_dir.is_dir():
        print(
            f"[{NAME}] WARNING: sessions folder {sessions_dir} not found — "
            f"Phase 1 skipped; Phase 2 source_session lookups will all be "
            f"unresolved",
            file=sys.stderr,
        )
    else:
        for md_file in sorted(sessions_dir.glob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                p1_unreadable += 1
                print(
                    f"[{NAME}] WARNING: could not read "
                    f"session note {md_file} ({exc}); skipping",
                    file=sys.stderr,
                )
                continue

            fm = _parse_frontmatter(content, source=str(md_file))

            # Snapshot notes live in the sessions folder and carry the
            # SESSION's session_id, but they are NOT the session note: they
            # have no project_path by design (every one would be a noise WARN
            # row), and — because "-snapshot" sorts before ".md" — they would
            # steal the first-wins index slot from the real session note,
            # degrading all of that session's insights to unresolved. Mirror
            # the source_sessions/session_coverage convention and exclude
            # them from Phase 1 entirely (counted in the summary partition).
            if fm.get("type") == "claude-snapshot":
                p1_snapshots_skipped += 1
                continue

            note_project = fm.get("project", "") or ""
            session_id = fm.get("session_id", "") or ""
            project_path_raw = (fm.get("project_path", "") or "").strip('"').strip("'")

            # -- derive outcome (always, BEFORE the project filter) ---------
            canonical: str | None = None
            detail = ""
            from_cache = False
            if not project_path_raw:
                reason = "no-project-path"
            elif project_path_raw in _git_cache:
                canonical, reason, detail = _git_cache[project_path_raw]
                from_cache = True
            else:
                canonical, reason, detail = _derive_canonical(project_path_raw)
                _git_cache[project_path_raw] = (canonical, reason, detail)

            # -- seed the Phase-2 index (always, BEFORE the filter) ---------
            # ok          → derived canonical (even when the note's own
            #               project field is empty — its insights are still
            #               resolvable)
            # not-a-repo  → the note's own project (cwd basename IS canonical
            #               for non-git projects)
            # anything else (incl. git-error) → None: NEVER promote the
            #               note's possibly-stale frontmatter to canonical on
            #               a git failure — that would generate BACKWARDS
            #               proposals for insights already carrying the true
            #               canonical name.
            if reason == "ok":
                canonical_for_index: str | None = canonical
            elif reason == "not-a-repo":
                canonical_for_index = note_project or None
            else:
                canonical_for_index = None
            if session_id:
                if session_id in _session_canonical:
                    print(
                        f"[{NAME}] duplicate session_id "
                        f"{session_id[:8]} — keeping first (sorted-order "
                        f"winner) for the Phase-2 index; later note: "
                        f"{md_file.name}",
                        file=sys.stderr,
                    )
                else:
                    _session_canonical[session_id] = canonical_for_index

            # -- project filter (AFTER canonical derivation + indexing) -----
            # Match against BOTH the old frontmatter name and the derived
            # canonical: `--project obsidian-brain` must include the
            # worktree-slug notes being canonicalized TO obsidian-brain.
            # Empty-project notes with no derivable canonical are
            # unattributable under a filter — deliberately suppressed
            # (counted as project-filtered) so filtered runs don't surface
            # unrelated WARN rows.
            if project is not None:
                match_names = set()
                if note_project:
                    match_names.add(note_project)
                if reason == "ok" and canonical:
                    match_names.add(canonical)
                if project not in match_names:
                    p1_project_filtered += 1
                    continue

            cached_suffix = " (cached)" if from_cache else ""

            # -- emit per outcome -------------------------------------------
            if reason == "no-project-path":
                p1_warn_no_project_path += 1
                issues.append(_warn_issue(
                    str(md_file), note_project,
                    "[WARN] missing project_path, cannot canonicalize",
                    "session",
                ))
            elif reason == "not-found":
                p1_warn_not_found += 1
                issues.append(_warn_issue(
                    str(md_file), note_project,
                    f"[WARN] project_path no longer exists ({project_path_raw!r}),"
                    f" cannot derive canonical{cached_suffix}",
                    "session",
                    project_path=project_path_raw,
                ))
            elif reason == "unavailable":
                p1_warn_unavailable += 1
                issues.append(_warn_issue(
                    str(md_file), note_project,
                    f"[WARN] git unavailable/timed out for {project_path_raw!r},"
                    f" cannot derive canonical{cached_suffix}",
                    "session",
                    project_path=project_path_raw,
                ))
            elif reason == "git-error":
                p1_warn_git_error += 1
                issues.append(_warn_issue(
                    str(md_file), note_project,
                    f"[WARN] git error for {project_path_raw!r}, cannot derive"
                    f" canonical (treated as unresolved, NOT as non-repo):"
                    f" {detail}{cached_suffix}",
                    "session",
                    project_path=project_path_raw,
                    git_stderr=detail,
                ))
            elif reason == "not-a-repo":
                # cwd basename IS canonical for non-git projects — leave alone
                p1_left_alone_not_a_repo += 1
            elif canonical is None:
                # empty-output / resolve-failed — git ran but gave nothing usable
                p1_warn_unavailable += 1
                issues.append(_warn_issue(
                    str(md_file), note_project,
                    f"[WARN] git returned no usable path for {project_path_raw!r}"
                    f" (reason={reason!r}), cannot derive canonical{cached_suffix}",
                    "session",
                    project_path=project_path_raw,
                ))
            elif note_project == canonical:
                p1_already_canonical += 1
            elif not note_project:
                # project: field empty/absent but the path resolves — apply()
                # has no old value to anchor a rewrite on, so this can never
                # be a resolvable 0.9 proposal. The canonical still seeded the
                # Phase-2 index above.
                p1_warn_empty_project += 1
                issues.append(_warn_issue(
                    str(md_file), note_project,
                    f"[WARN] project field empty; cannot rewrite in place"
                    f" (canonical would be {canonical!r})",
                    "session",
                    project_path=project_path_raw,
                ))
            else:
                # Resolvable rewrite proposal
                p1_proposed += 1
                new_tag = f"claude/project/{_slugify(canonical)}"
                issues.append(Issue(
                    check=NAME,
                    note_path=str(md_file),
                    project=note_project,
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
                        "old_tags": _observed_old_tags(content, note_project, new_tag),
                        "new_tag": new_tag,
                        "project_path": project_path_raw,
                        "phase": "session",
                    },
                ))

    # ---------------------------------------------------------------- Phase 2
    insight_folders = [insights_folder] + [
        f for f in _EXTRA_INSIGHT_FOLDERS if f != insights_folder
    ]
    if not (vault / insights_folder).is_dir():
        print(
            f"[{NAME}] WARNING: insights folder "
            f"{vault / insights_folder} not found — Phase 2 scans only the "
            f"auxiliary folders ({', '.join(_EXTRA_INSIGHT_FOLDERS)})",
            file=sys.stderr,
        )
    for folder_name in insight_folders:
        folder_path = vault / folder_name
        if not folder_path.is_dir():
            continue

        for md_file in sorted(folder_path.glob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                p2_unreadable += 1
                print(
                    f"[{NAME}] WARNING: could not read "
                    f"insight note {md_file} ({exc}); skipping",
                    file=sys.stderr,
                )
                continue

            fm = _parse_frontmatter(content, source=str(md_file))
            note_project = fm.get("project", "") or ""
            source_session = fm.get("source_session", "") or ""

            if not source_session:
                # No source_session — cannot derive canonical from session index
                p2_no_source_session += 1
                continue

            # Resolve canonical BEFORE the filter so the filter can match on
            # either the old name or the canonical (same semantics as Phase 1).
            in_index = source_session in _session_canonical
            canonical = _session_canonical.get(source_session)

            if project is not None:
                match_names = set()
                if note_project:
                    match_names.add(note_project)
                if canonical:
                    match_names.add(canonical)
                if project not in match_names:
                    p2_project_filtered += 1
                    continue

            if not in_index:
                p2_warn_unresolved_session += 1
                issues.append(_warn_issue(
                    str(md_file), note_project,
                    f"[WARN] source_session {source_session!r} not in session"
                    f" index (missing, outside sessions_folder, or unreadable),"
                    f" cannot derive canonical",
                    "insight",
                    source_session=source_session,
                ))
                continue

            if canonical is None:
                # Session note was itself unresolvable — propagate WARN
                p2_warn_unresolved_session += 1
                issues.append(_warn_issue(
                    str(md_file), note_project,
                    f"[WARN] source_session {source_session!r} session note could"
                    f" not be canonicalized, cannot derive canonical for insight",
                    "insight",
                    source_session=source_session,
                ))
                continue

            if note_project == canonical:
                p2_already_canonical += 1
                continue

            if not note_project:
                # Same as Phase 1: nothing to anchor an in-place rewrite on.
                p2_warn_empty_project += 1
                issues.append(_warn_issue(
                    str(md_file), note_project,
                    f"[WARN] project field empty; cannot rewrite in place"
                    f" (canonical would be {canonical!r})",
                    "insight",
                    source_session=source_session,
                ))
                continue

            # Resolvable rewrite proposal
            p2_proposed += 1
            new_tag = f"claude/project/{_slugify(canonical)}"
            issues.append(Issue(
                check=NAME,
                note_path=str(md_file),
                project=note_project,
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
                    "old_tags": _observed_old_tags(content, note_project, new_tag),
                    "new_tag": new_tag,
                    "source_session": source_session,
                    "phase": "insight",
                },
            ))

    # End-of-scan coverage summary. The buckets partition each phase's scanned
    # denominator exactly (unreadable notes count toward the denominator too).
    p1_total = (
        p1_already_canonical + p1_proposed + p1_warn_no_project_path
        + p1_warn_not_found + p1_warn_unavailable + p1_warn_git_error
        + p1_warn_empty_project + p1_left_alone_not_a_repo
        + p1_project_filtered + p1_unreadable + p1_snapshots_skipped
    )
    p2_total = (
        p2_already_canonical + p2_proposed + p2_warn_unresolved_session
        + p2_warn_empty_project + p2_no_source_session
        + p2_project_filtered + p2_unreadable
    )
    if p1_total > 0 or p2_total > 0:
        print(
            f"[{NAME}] phase1 (sessions): {p1_total} scanned:"
            f" {p1_already_canonical} already-canonical, {p1_proposed} proposed,"
            f" {p1_warn_no_project_path} warn-no-project-path,"
            f" {p1_warn_not_found} warn-path-not-found,"
            f" {p1_warn_unavailable} warn-git-unavailable,"
            f" {p1_warn_git_error} warn-git-error,"
            f" {p1_warn_empty_project} warn-empty-project,"
            f" {p1_left_alone_not_a_repo} left-alone-non-git,"
            f" {p1_project_filtered} project-filtered,"
            f" {p1_unreadable} unreadable,"
            f" {p1_snapshots_skipped} snapshots-skipped",
            file=sys.stderr,
        )
        print(
            f"[{NAME}] phase2 (insights): {p2_total} scanned:"
            f" {p2_already_canonical} already-canonical, {p2_proposed} proposed,"
            f" {p2_warn_unresolved_session} warn-unresolved-session,"
            f" {p2_warn_empty_project} warn-empty-project,"
            f" {p2_no_source_session} no-source-session,"
            f" {p2_project_filtered} project-filtered,"
            f" {p2_unreadable} unreadable",
            file=sys.stderr,
        )

    return issues


def apply(issues: list[Issue], backup_root: str) -> list[Result]:
    """Rewrite ``project:`` and the observed ``claude/project/*`` tag lines.

    Only ``signal_class == "canonicalize"`` (``unresolved=False``) rows are
    auto-applyable. ``canonicalize-unresolved`` rows are returned as
    ``"unresolved"`` without touching the file.

    Defense-in-depth: raises ``RuntimeError`` if a non-unresolved row has an
    unexpected ``signal_class`` — this is a programming error, not a per-issue
    error. A wrong-signal rewrite could corrupt a note; we must fail loudly.

    Tag rewriting uses an ANCHORED multiline regex per observed old tag line
    (``^(\\s*-\\s*)<escaped-old-tag>$``) — a prefix-sharing sibling tag like
    ``claude/project/foo-extra`` can never be partially rewritten. The rewrite
    is line-format-only (block-list tag items); identical claude/project tag
    lines left behind by the rewrite are deduped (first occurrence kept).
    The post-rewrite leftover check is deliberately WIDER than the rewrite:
    it token-scans the whole frontmatter block (any tag format — inline
    array, scalar, flow map) and, if any ``claude/project/*`` token differing
    from the expected new tag remains, the Result is still ``"applied"`` but
    its error field carries a "tag not rewritten" detail so the residue is
    visible (unhandled tag format, scan/apply drift, or a genuinely
    multi-project note).

    Skipped Results always carry a reason in the error field ("no
    frontmatter", "unterminated frontmatter", or "project: line not found for
    rewrite (old=…)" — the last indicates scan/apply disagreement).

    Notes are read STRICTLY as UTF-8 here (unlike scan, which tolerates
    replacement chars for read-only matching): a decode-then-rewrite with
    errors="replace" would permanently bake U+FFFD into the note. Undecodable
    notes return an error pointing at the encoding-corruption check.

    Backup path: ``<backup_root>/<check-name>/<source-folder>/<basename>``
    (containment-checked against ``backup_root`` before the copy).
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

        old_tags = issue.extra.get("old_tags", []) or []
        # new_tag is recomputed (not trusted from extra) so a hand-built Issue
        # can't desync the tag rewrite from the project rewrite.
        new_tag = f"claude/project/{_slugify(new_project)}"

        note_path = Path(issue.note_path)
        try:
            # STRICT decode — see docstring (never errors="replace" on a
            # read-modify-write path: U+FFFD would be baked in permanently).
            content = note_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            results.append(Result(
                check=NAME,
                note_path=issue.note_path,
                status="error",
                error=(
                    f"note is not valid UTF-8 ({exc}); refusing to rewrite —"
                    f" run --check encoding-corruption first"
                ),
            ))
            continue
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
                error="no frontmatter",
            ))
            continue

        end = content.find("\n---", 3)
        if end == -1:
            results.append(Result(
                check=NAME,
                note_path=issue.note_path,
                status="skipped",
                error="unterminated frontmatter",
            ))
            continue

        fm_block = content[: end + 4]
        body = content[end + 4:]

        # Replace `project: <old_project>` (with or without quotes). Lambda
        # replacement — never template-interpolate new_project into the
        # replacement string (a value containing backslashes/group refs
        # would corrupt the line).
        new_fm, n_proj = re.subn(
            rf"^(project:\s*)[\"']?{re.escape(old_project)}[\"']?\s*$",
            lambda m: m.group(1) + new_project,
            fm_block,
            flags=re.MULTILINE,
        )
        if n_proj == 0:
            # scan() saw this old value but apply() can't find it — the note
            # changed between scan and apply, or scan/apply disagree. Must be
            # distinguishable from the no-frontmatter skips.
            results.append(Result(
                check=NAME,
                note_path=issue.note_path,
                status="skipped",
                error=f"project: line not found for rewrite (old={old_project!r})",
            ))
            continue

        # Rewrite each observed old tag line (anchored — never substring;
        # an unanchored replace would mangle prefix-sharing sibling tags).
        # NOTE: the rewrite is deliberately line-format-only (block-list tag
        # items, the form the hooks write); other formats are caught by the
        # wider token-scan leftover check below.
        for old_tag in old_tags:
            tag_re = re.compile(
                rf"^(\s*-\s*[\"']?){re.escape(old_tag)}([\"']?)\s*$",
                re.MULTILINE,
            )
            new_fm = tag_re.sub(
                lambda m: m.group(1) + new_tag + m.group(2), new_fm
            )

        # Dedupe identical claude/project tag LINES: a note carrying both an
        # old form and the already-correct new tag ends the rewrite with two
        # identical list items — keep the first occurrence only.
        seen_tag_lines: set[str] = set()
        kept_lines: list[str] = []
        for line in new_fm.splitlines(keepends=True):
            stripped = line.rstrip("\n")
            if _TAG_LINE_RE.match(stripped):
                if stripped in seen_tag_lines:
                    continue
                seen_tag_lines.add(stripped)
            kept_lines.append(line)
        new_fm = "".join(kept_lines)

        # Post-rewrite visibility check — deliberately WIDER than the
        # rewrite: token-scan the whole frontmatter block so claude/project
        # tags in ANY format (inline array, scalar, flow map) that still
        # differ from the expected new tag are surfaced (NOT silently left).
        leftover = sorted(
            {t for t in _TAG_TOKEN_RE.findall(new_fm) if t != new_tag}
        )
        tag_detail = None
        if leftover:
            tag_detail = (
                f"tag not rewritten: found {leftover} (expected only {new_tag!r})"
            )

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
            error=tag_detail,
        ))

    return results
