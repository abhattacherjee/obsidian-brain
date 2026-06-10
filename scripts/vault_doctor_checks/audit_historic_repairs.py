"""vault_doctor check: audit historic source-sessions repairs and reverse corruptions.

One-shot audit tool (issue #95, companion to #93). Earlier `/vault-doctor fix`
runs applied a buggy mtime-as-capture-time algorithm to `source_session`
backlinks. Even after the algorithm fix (#93/#106), the vault still contains
backlinks rewritten by the buggy versions. This check walks the doctor backup
roots under ``~/.claude/obsidian-brain-doctor-backup/``, diffs each backed-up
note's ``source_session`` / ``source_session_note`` against the note's current
state, and classifies every historic repair:

  A — restore:   original backlink matched the note's filename date; current
                 one does not → mtime-drift corruption; auto-proposes restore.
  B — keep:      original was wrong-day; current matches → legit fix; no action.
  C — ambiguous: both same-day but different sessions → human review.
  D — both-wrong: neither matches the filename date → unresolved warning.

Only category A is auto-applyable. ``apply()`` restores the ORIGINAL
``source_session`` + ``source_session_note`` from the oldest backup (the true
pre-doctor state) and writes its own backups under
``<backup_root>/audit-historic-repairs/`` so the restore is itself reversible.

This check is OPT-IN: it does not run in the default all-checks sweep
(`vault_doctor` with no ``--check``) because B/C/D classifications are
permanent report rows, not actionable drift. Run it explicitly:

    python3 scripts/vault_doctor.py --check audit-historic-repairs

The backup root can be overridden via ``OBSIDIAN_BRAIN_DOCTOR_BACKUP_ROOT``
(used by tests). ``--days`` bounds the age of backup RUNS considered
(default 180 — stale backups aren't load-bearing).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import Issue, Result

# Reuse the package-shared frontmatter helpers so parsing/rewriting semantics
# stay byte-identical with the source-sessions check that wrote these repairs.
from .source_sessions import (
    _EXTRA_INSIGHT_FOLDERS,
    _FILENAME_DATE_RE,
    _parse_frontmatter,
    _rewrite_frontmatter,
)

NAME = "audit-historic-repairs"
DESCRIPTION = (
    "Classify historic source-sessions repairs from doctor backups and "
    "reverse clear mtime-bug corruptions (opt-in; run via --check)"
)
DEFAULT_WINDOW_DAYS = 180  # backup runs older than this are skipped
OPT_IN = True  # excluded from the default all-checks sweep

_CATEGORY_REASONS = {
    "A": "original backlink matches note date; current does not — mtime-drift corruption, restore original",
    "B": "current backlink matches note date; original did not — likely a legitimate repair (verify manually if the session spanned midnight)",
    "C": "original and current backlinks are both same-day as the note — ambiguous, needs JSONL-window or human review",
    "D": "neither original nor current backlink matches the note date — unresolved, needs human review",
}

_CATEGORY_SIGNALS = {
    "A": "historic-restore",
    "B": "historic-keep",
    "C": "historic-ambiguous",
    "D": "historic-both-wrong",
}


def _backup_root() -> Path:
    override = os.environ.get("OBSIDIAN_BRAIN_DOCTOR_BACKUP_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "obsidian-brain-doctor-backup"


def _parse_run_ts(dirname: str) -> float | None:
    """Parse a backup-run dir name like ``2026-04-11T19-28-58+00-00`` to epoch.

    Run dirs are written by the dispatcher as ``_iso_now().replace(":", "-")``,
    so the first 19 chars are ``YYYY-MM-DDTHH-MM-SS`` in UTC. Anything that
    doesn't parse is not a run dir and returns None.
    """
    try:
        dt = datetime.strptime(dirname[:19], "%Y-%m-%dT%H-%M-%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).timestamp()


def _note_date(basename: str) -> str | None:
    """Extract the YYYY-MM-DD prefix from a note basename, if present."""
    m = _FILENAME_DATE_RE.match(basename)
    return m.group(1) if m else None


def _backlink_basename(fm: dict) -> str:
    """Extract the bare basename from a ``source_session_note`` value.

    Values look like ``[[2026-04-09-obsidian-brain-2381]]`` (quotes already
    stripped by the frontmatter parser).
    """
    raw = fm.get("source_session_note", "")
    return raw.strip().lstrip("[").rstrip("]").strip()


def _candidate_folders(sessions_folder: str, insights_folder: str) -> list[str]:
    return [insights_folder, *_EXTRA_INSIGHT_FOLDERS, sessions_folder]


def _find_current_note(
    vault: Path, basename: str, sessions_folder: str, insights_folder: str
) -> Path | None:
    for folder in _candidate_folders(sessions_folder, insights_folder):
        candidate = vault / folder / basename
        if candidate.is_file():
            return candidate
    return None


def _classify(file_date: str, orig_date: str | None, curr_date: str | None) -> str:
    """Classify a historic repair by date agreement (see module docstring)."""
    if orig_date is None or curr_date is None:
        return "D"
    orig_ok = orig_date == file_date
    curr_ok = curr_date == file_date
    if orig_ok and not curr_ok:
        return "A"
    if not orig_ok and curr_ok:
        return "B"
    if orig_ok and curr_ok:
        return "C"
    return "D"


def scan(
    vault_path: str,
    sessions_folder: str,
    insights_folder: str,
    days: int,
    project: str | None = None,
) -> list[Issue]:
    """Diff backed-up notes against current state and classify each repair."""
    vault = Path(vault_path)
    root = _backup_root()
    if not root.is_dir():
        # Loud no-op: a missing backup root means NOTHING was audited — a
        # silent [] return would read as a clean bill of health.
        print(
            f"[audit-historic-repairs] backup root {root} not found — no "
            f"doctor backups to audit (no-op, not a clean bill of health)",
            file=sys.stderr,
        )
        return []

    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - days * 86400

    # Resolve each backup to its current note FIRST, then key `oldest` on the
    # RESOLVED target path — not the bare basename. A bare-basename key would
    # collapse cross-folder basename collisions (e.g. an insight and a
    # decision both named 2026-04-10-foo.md) into one entry, silently
    # dropping one note from the audit and potentially pairing a backup with
    # the WRONG note (which apply() would then "restore" into an uncorrupted
    # note). Modern backup runs nest each note under its source folder
    # (<run>/<project>/<folder>/<basename>), so the backup's parent dir name
    # is used as a folder hint when it matches a known note folder.
    known_folders = set(_candidate_folders(sessions_folder, insights_folder))

    # resolved target path (str) -> (run_ts, backup_path); the OLDEST run per
    # target wins — it is the true pre-doctor original. Intermediate backups
    # are vault-doctor iterations on vault-doctor.
    oldest: dict[str, tuple[float, Path]] = {}
    # Distinct backups whose current note cannot be found (deleted/renamed
    # since). Tracked separately so the coverage summary still partitions the
    # full audited population.
    missing: set[str] = set()
    skipped_old_runs = 0  # parseable run dirs excluded by the --days cutoff
    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir():
            continue
        run_ts = _parse_run_ts(run_dir.name)
        if run_ts is None:
            continue
        if run_ts < cutoff:
            skipped_old_runs += 1
            continue
        for md in run_dir.rglob("*.md"):
            # Skip this check's own restore backups — re-running the audit
            # must not treat its own output as a historic repair.
            if NAME in md.relative_to(run_dir).parts:
                continue
            folder_hint = md.parent.name if md.parent.name in known_folders else None
            if folder_hint:
                candidate = vault / folder_hint / md.name
                target = candidate if candidate.is_file() else None
            else:
                # Legacy-layout backups (<run>/<project>/<basename>) carry no
                # folder information; fall back to the ordered folder search.
                # A cross-folder basename collision can still mis-resolve
                # here — no folder info exists to disambiguate — but
                # reachability is near-zero in practice.
                target = _find_current_note(
                    vault, md.name, sessions_folder, insights_folder
                )
            if target is None:
                missing.add(f"{folder_hint or '?'}/{md.name}")
                continue
            key = str(target)
            existing = oldest.get(key)
            if existing is None or run_ts < existing[0]:
                oldest[key] = (run_ts, md)

    # The 'oldest backup wins' premise breaks if older runs were age-filtered
    # out — surface that to the operator.
    if skipped_old_runs > 0:
        print(
            f"[audit-historic-repairs] WARNING: {skipped_old_runs} backup run(s) older than"
            f" --days={days} were excluded; 'oldest backup' may not be the true"
            f" pre-doctor original. Re-run with a larger --days.",
            file=sys.stderr,
        )

    issues: list[Issue] = []
    # Counters for the end-of-scan coverage summary (every audited backup
    # lands in exactly one bucket).
    non_source = 0
    no_drift = 0
    unreadable = 0
    project_filtered = 0

    for target in sorted(oldest):
        _, backup_path = oldest[target]
        current_path = Path(target)
        basename = current_path.name

        try:
            backup_text = backup_path.read_text(encoding="utf-8", errors="replace")
            current_text = current_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            # Don't silently drop unreadable notes. Emitted UNCONDITIONALLY —
            # even with --project set — because an unreadable note cannot be
            # attributed to a project, and the project filter must not
            # suppress audit-infrastructure failures.
            unreadable += 1
            issues.append(Issue(
                check=NAME,
                note_path=str(current_path),
                project="unknown",
                current_source="(unreadable)",
                proposed_source="",
                reason=(
                    f"could not read note or backup for audit (note could not"
                    f" be attributed to a project): {exc}"
                ),
                confidence=0.0,
                extra={
                    "category": "D",
                    "signal_class": "historic-unreadable",
                    "unresolved": True,
                    "orig_sid": "",
                    "orig_basename": "",
                    "backup_path": str(backup_path),
                },
            ))
            continue

        backup_fm = _parse_frontmatter(backup_text, source=str(backup_path))
        current_fm = _parse_frontmatter(current_text, source=str(current_path))

        orig_sid = backup_fm.get("source_session", "")
        curr_sid = current_fm.get("source_session", "")
        orig_note = _backlink_basename(backup_fm)
        curr_note = _backlink_basename(current_fm)

        # Not a source-sessions note (e.g. a session note backed up by the
        # snapshot checks) — nothing to audit.
        if not orig_sid and not orig_note:
            # A backup without source fields whose CURRENT note has them is
            # suspicious — the backup may be truncated/corrupted; warn.
            if curr_sid or curr_note:
                print(
                    f"[audit-historic-repairs] WARNING: backup {backup_path} has no"
                    f" parsable source_session frontmatter but current note does —"
                    f" backup may be corrupted; skipping.",
                    file=sys.stderr,
                )
            non_source += 1
            continue

        # No drift: the backlink was never rewritten (backup is from another
        # check, or a restore already ran).
        if orig_sid == curr_sid and orig_note == curr_note:
            no_drift += 1
            continue

        note_project = current_fm.get("project", "unknown") or "unknown"
        if project and note_project != project:
            project_filtered += 1
            continue

        file_date = _note_date(basename)
        if file_date is None:
            category = "D"
        else:
            category = _classify(file_date, _note_date(orig_note), _note_date(curr_note))

        # Category A without a complete source_session pair must not be
        # auto-applyable — mark as unresolved so apply() never attempts it.
        unresolved = category != "A"
        confidence = 0.9 if category == "A" else 0.0
        reason = f"category {category}: {_CATEGORY_REASONS[category]}"
        if category == "A" and (not orig_sid or not orig_note):
            unresolved = True
            confidence = 0.0
            reason = (
                "category A: original backlink matches note date but backup lacks"
                " a complete source_session pair — manual restore needed"
            )

        issues.append(Issue(
            check=NAME,
            note_path=str(current_path),
            project=note_project,
            current_source=f"[[{curr_note}]]" if curr_note else "(missing)",
            proposed_source=f"[[{orig_note}]]" if category == "A" else "",
            reason=reason,
            confidence=confidence,
            extra={
                "category": category,
                "signal_class": _CATEGORY_SIGNALS[category],
                "unresolved": unresolved,
                "orig_sid": orig_sid,
                "orig_basename": orig_note,
                "backup_path": str(backup_path),
            },
        ))

    # End-of-scan coverage summary. The audited denominator is the resolved
    # targets plus the distinct backups whose note no longer exists; the six
    # buckets partition it exactly. Every unreadable note also appended an
    # issue above, so subtract it from the classified count to avoid
    # double-counting. Printed UNCONDITIONALLY — an empty partition
    # ("audited 0 backed-up note(s)") must be visible, not silent.
    audited = len(oldest) + len(missing)
    classified = len(issues) - unreadable
    print(
        f"[audit-historic-repairs] audited {audited} backed-up note(s):"
        f" {classified} classified, {len(missing)} missing-current,"
        f" {non_source} non-source-session, {no_drift} no-drift,"
        f" {unreadable} unreadable, {project_filtered} project-filtered",
        file=sys.stderr,
    )

    return issues


def apply(issues: list[Issue], backup_root: str) -> list[Result]:
    """Restore category-A notes to their original source_session backlink.

    Note: the OBSIDIAN_BRAIN_DOCTOR_BACKUP_ROOT override only affects which
    backups scan() reads; apply() always writes its restore-backups under the
    dispatcher-provided backup_root.
    """
    results: list[Result] = []

    for issue in issues:
        if issue.extra.get("unresolved"):
            results.append(Result(
                check=NAME, note_path=issue.note_path, status="unresolved",
            ))
            continue

        # Defense-in-depth (mirrors source_sessions.apply): only category A
        # is auto-applyable. Anything else reaching this point is a
        # programming error — fail loudly rather than writing the wrong fix.
        category = issue.extra.get("category", "")
        if category != "A":
            raise RuntimeError(
                f"apply() refuses category={category!r} for {issue.note_path}; "
                f"only category A (historic-restore) is auto-applyable."
            )

        orig_sid = issue.extra.get("orig_sid", "")
        orig_basename = issue.extra.get("orig_basename", "")
        if not orig_sid or not orig_basename:
            results.append(Result(
                check=NAME, note_path=issue.note_path, status="error",
                error="missing orig_sid or orig_basename in issue extra",
            ))
            continue

        note_path = Path(issue.note_path)
        try:
            content = note_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            results.append(Result(
                check=NAME, note_path=issue.note_path, status="error", error=str(exc),
            ))
            continue

        # Backup the current state under a check-named subdir so re-running
        # this tool is itself reversible.
        source_folder = note_path.parent.name
        backup_dir = Path(backup_root) / NAME / source_folder
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / note_path.name
            # Defense-in-depth (mirrors source_sessions.apply): the backup
            # write must never escape backup_root via a hostile folder name.
            resolved_root = Path(backup_root).resolve()
            resolved_backup = backup_path.resolve()
            if resolved_root not in resolved_backup.parents:
                raise ValueError(
                    f"backup path {backup_path} would escape backup_root {backup_root}"
                )
            shutil.copy2(note_path, backup_path)
        except (OSError, ValueError) as exc:
            results.append(Result(
                check=NAME, note_path=issue.note_path, status="error",
                error=f"backup failed: {exc}",
            ))
            continue

        try:
            new_content = _rewrite_frontmatter(content, orig_sid, orig_basename)
        except ValueError as exc:
            results.append(Result(
                check=NAME, note_path=issue.note_path, status="error", error=str(exc),
            ))
            continue

        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(
                dir=str(note_path.parent), prefix=".vd-audithist-", suffix=".tmp",
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
                check=NAME, note_path=issue.note_path, status="error", error=str(exc),
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
