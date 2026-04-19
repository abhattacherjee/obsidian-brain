"""vault_doctor check: legacy snapshot migration.

Runs FOUR ordered checks that bring pre-spec snapshots into the new
first-class format:

  1. snapshot-legacy-filename       — add -HHMMSS derived from mtime
  2. snapshot-missing-status         — add status: auto-logged or summarized
  3. snapshot-missing-backlink       — write source_session_note wikilink
  4. session-missing-snapshots-list  — backfill snapshots: list on sessions

All are idempotent. The filename rename also rewrites vault-wide
[[<old-stem>]] wikilinks so decisions/insights that pointed at legacy
filenames still resolve after migration.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from . import Issue, Result

NAME = "snapshot-migration"
DESCRIPTION = "Migrate pre-spec snapshots to first-class format"
DEFAULT_WINDOW_DAYS = 3650

_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _read_text(p):
    """Read UTF-8 text, normalising BOM and CRLF so regex anchors match."""
    try:
        text = Path(p).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if text.startswith("\ufeff"):
        text = text[1:]
    if "\r\n" in text:
        text = text.replace("\r\n", "\n")
    return text


def _parse_fm(text):
    m = _FRONT_RE.match(text or "")
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if not line or line.startswith(" ") or line.startswith("-"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _legacy_filename(p: Path) -> bool:
    """True if filename matches <date>-<proj>-<sid4>-snapshot.md (no HHMMSS)."""
    return bool(re.match(r".*-snapshot\.md$", p.name)) and not re.match(
        r".*-snapshot-\d{6}\.md$", p.name
    )


def _hhmmss_from_mtime(p: Path) -> str:
    ts = p.stat().st_mtime
    return datetime.datetime.fromtimestamp(ts).strftime("%H%M%S")


def _short_session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:4] if session_id else "e3b0"


def _slugify(text: str) -> str:
    """Minimal inline copy; avoids import cycles with hooks/ module."""
    return re.sub(r"[^a-zA-Z0-9]+", "-", text.strip()).strip("-").lower() or "project"


def scan(vault_path, sessions_folder, insights_folder, days, project=None):
    sess_dir = Path(vault_path) / sessions_folder
    if not sess_dir.is_dir():
        return []

    issues = []

    all_md = [p for p in sess_dir.iterdir() if p.suffix == ".md"]
    sessions_by_id: dict[str, Path] = {}
    snapshots: list[tuple[Path, dict, str]] = []

    # Stash the resolved vault root on each legacy-filename issue so
    # ``apply()`` can rewrite wikilinks from the correct root even when
    # ``sessions_folder`` is nested (e.g. ``notes/claude-sessions``).
    vault_root_str = str(Path(vault_path).resolve())

    for p in all_md:
        text = _read_text(p) or ""
        fm = _parse_fm(text)
        if not fm:
            continue
        if project and fm.get("project", "").lower() != project.lower():
            continue
        type_ = fm.get("type", "")
        if type_ == "claude-session":
            sid = fm.get("session_id", "")
            if sid:
                sessions_by_id[sid] = p
        elif type_ == "claude-snapshot":
            snapshots.append((p, fm, text))

    used_filenames = {p.name for p in all_md}

    # 1. snapshot-legacy-filename
    for p, fm, text in snapshots:
        if not _legacy_filename(p):
            continue
        hh = _hhmmss_from_mtime(p)
        new_name = p.name.replace("-snapshot.md", f"-snapshot-{hh}.md")
        unresolved = new_name in used_filenames
        issues.append(Issue(
            check="snapshot-legacy-filename",
            note_path=str(p),
            project=fm.get("project", ""),
            current_source=p.name,
            proposed_source=new_name,
            reason=(f"rename adds HHMMSS={hh} from file mtime"
                    + (" — COLLISION: target filename already exists" if unresolved else "")),
            confidence=0.95 if not unresolved else 0.0,
            extra={
                "unresolved": unresolved,
                "new_name": new_name,
                "vault_path": vault_root_str,
            },
        ))

    # 2. snapshot-missing-status
    for p, fm, text in snapshots:
        if "status" in fm and fm["status"]:
            continue
        has_summary = bool(re.search(r"^## Summary\s*\n\S", text, re.MULTILINE))
        new_status = "summarized" if has_summary else "auto-logged"
        issues.append(Issue(
            check="snapshot-missing-status",
            note_path=str(p),
            project=fm.get("project", ""),
            current_source="(no status field)",
            proposed_source=f"status: {new_status}",
            reason="legacy snapshot lacks status field",
            confidence=0.98,
        ))

    # 3. snapshot-missing-backlink
    for p, fm, text in snapshots:
        if "source_session_note" in fm and fm["source_session_note"]:
            continue
        sid = fm.get("session_id", "")
        date = fm.get("date", "")
        proj = fm.get("project", "")
        if not sid or not date or not proj:
            issues.append(Issue(
                check="snapshot-missing-backlink",
                note_path=str(p),
                project=proj,
                current_source="(no source_session_note)",
                proposed_source="",
                reason="cannot compute parent stem (missing session_id/date/project)",
                confidence=0.0,
                extra={"unresolved": True},
            ))
            continue
        parent_stem = f"{date}-{_slugify(proj)}-{_short_session_hash(sid)}"
        parent_exists = (sess_dir / f"{parent_stem}.md").exists()
        issues.append(Issue(
            check="snapshot-missing-backlink",
            note_path=str(p),
            project=proj,
            current_source="(no source_session_note)",
            proposed_source=f'source_session_note: "[[{parent_stem}]]"',
            reason=("parent session found" if parent_exists else
                    "parent session not found — will warn only"),
            confidence=0.95 if parent_exists else 0.3,
            extra={"unresolved": not parent_exists, "parent_stem": parent_stem},
        ))

    # 4. session-missing-snapshots-list
    snaps_by_sid: dict[str, list[str]] = {}
    for p, fm, _ in snapshots:
        sid = fm.get("session_id", "")
        if sid:
            snaps_by_sid.setdefault(sid, []).append(p.stem)
    for sid, stems in snaps_by_sid.items():
        if sid not in sessions_by_id:
            continue
        sess_path = sessions_by_id[sid]
        sess_text = _read_text(sess_path) or ""
        # Scope ``snapshots:`` anchor detection to the frontmatter block only
        fm_parts = sess_text.split("---\n", 2)
        if len(fm_parts) >= 3 and re.search(r"(?m)^snapshots:", fm_parts[1]):
            continue
        wikilinks = sorted(f"[[{stem}]]" for stem in stems)
        issues.append(Issue(
            check="session-missing-snapshots-list",
            note_path=str(sess_path),
            project=_parse_fm(sess_text).get("project", ""),
            current_source="(no snapshots field)",
            proposed_source="\n".join(wikilinks),
            reason=f"{len(wikilinks)} snapshot(s) on disk but session has no back-reference",
            confidence=0.98,
        ))

    return issues


def _backup_file(path: str, backup_root: str, check_name: str) -> str:
    bdir = Path(backup_root) / check_name
    bdir.mkdir(parents=True, exist_ok=True)
    dest = bdir / Path(path).name
    shutil.copy2(path, dest)
    return str(dest)


def _atomic_write(path: str, text: str) -> None:
    p = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".ob-doctor-", suffix=".md.tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, str(p))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _rewrite_wikilinks_in_vault(
    vault_path: str,
    old_stem: str,
    new_stem: str,
    exclude_dirs: list[str] | None = None,
) -> int:
    """Find and replace [[<old_stem>]] with [[<new_stem>]] across the vault.

    Returns the number of files modified. Uses atomic writes per file.
    Raises ``RuntimeError`` if any read or write failed so the caller can
    roll back the rename and surface the error to the user.

    ``exclude_dirs`` (e.g. the per-apply ``backup_root``) are skipped so
    the rewrite cannot poison files OUTSIDE the live vault content. Real
    callers and tests both place backups inside the vault tree, and
    rewriting them would leave the backup pointing at the post-migration
    stem — defeating the rollback guarantee.
    """
    count = 0
    failed: list[str] = []
    pattern = re.compile(r"\[\[" + re.escape(old_stem) + r"\]\]")
    excluded_resolved: list[Path] = []
    for d in exclude_dirs or []:
        try:
            excluded_resolved.append(Path(d).resolve())
        except OSError:
            continue
    for p in Path(vault_path).rglob("*.md"):
        try:
            p_resolved = p.resolve()
        except OSError:
            continue
        if any(
            excl == p_resolved or excl in p_resolved.parents
            for excl in excluded_resolved
        ):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            failed.append(f"{p}: {exc}")
            continue
        if pattern.search(text):
            new_text = pattern.sub(f"[[{new_stem}]]", text)
            try:
                _atomic_write(str(p), new_text)
                count += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{p}: {exc}")
    if failed:
        raise RuntimeError(
            f"wikilink rewrite failed for {len(failed)} file(s): {failed[:3]}"
        )
    return count


def apply(issues, backup_root):
    results = []
    order = {
        "snapshot-legacy-filename": 0,
        "snapshot-missing-status": 1,
        "snapshot-missing-backlink": 2,
        "session-missing-snapshots-list": 3,
    }
    issues_sorted = sorted(issues, key=lambda i: order.get(i.check, 9))
    # Track stem renames applied during this batch so subsequent issues
    # that reference the OLD path or OLD stem get the NEW value. Two
    # consumers:
    #   - path redirect: ``issue.note_path == old_path`` → ``new_path``
    #     (so missing-status/backlink targeting the renamed file succeed)
    #   - stem rewrite:  ``[[old_stem]]`` → ``[[new_stem]]`` inside
    #     session-missing-snapshots-list's ``proposed_source`` (so the
    #     session's snapshots list doesn't point at the pre-rename name)
    renamed_paths: dict[str, str] = {}
    renamed_stems: dict[str, str] = {}
    for issue in issues_sorted:
        # Redirect any path that was renamed earlier in this apply pass.
        if issue.note_path in renamed_paths:
            issue.note_path = renamed_paths[issue.note_path]
        if issue.extra.get("unresolved"):
            results.append(Result(
                check=issue.check, note_path=issue.note_path, status="unresolved",
                error=issue.reason,
            ))
            continue
        try:
            backup: str | None = None
            if issue.check == "snapshot-legacy-filename":
                src = Path(issue.note_path)
                dst = src.parent / issue.extra["new_name"]
                # Scan() stashed the resolved vault root; fall back to
                # ``src.parents[1]`` for backwards compatibility with Issue
                # objects constructed by older code paths.
                vault_root = issue.extra.get("vault_path") or str(src.parents[1])
                backup = _backup_file(str(src), backup_root, issue.check)
                # Rename FIRST so a wikilink-rewrite failure can roll back
                # the filesystem move without leaving other files pointing
                # at a non-existent dst.
                os.rename(str(src), str(dst))
                try:
                    # Exclude backup_root so the just-created backup copy
                    # (which lives under <backup_root>/snapshot-legacy-filename/)
                    # does NOT have its [[old-stem]] reference rewritten —
                    # backups must reflect the pre-migration state to remain
                    # useful for rollback.
                    _rewrite_wikilinks_in_vault(
                        vault_root, src.stem, dst.stem,
                        exclude_dirs=[backup_root],
                    )
                except Exception:
                    # Roll back the rename so the vault stays consistent
                    try:
                        os.rename(str(dst), str(src))
                    except OSError:
                        pass
                    raise
                renamed_paths[str(src)] = str(dst)
                renamed_stems[src.stem] = dst.stem
            elif issue.check == "snapshot-missing-status":
                text = _read_text(issue.note_path) or ""
                parts = text.split("---\n", 2)
                if len(parts) < 3:
                    raise RuntimeError("could not locate frontmatter")
                # Defensive re-check: if status: already exists in the
                # frontmatter (stale Issue replay / race with another
                # writer), treat as an idempotent no-op. Run BEFORE the
                # backup so a stale-Issue replay does not create a useless
                # backup file (and avoids failing the apply for what
                # should be a no-op when backup I/O errors).
                if re.search(r"(?m)^status:", parts[1]):
                    results.append(Result(
                        check=issue.check, note_path=issue.note_path, status="skipped",
                        error="status field already present (stale Issue?)",
                    ))
                    continue
                parts[1] = parts[1] + issue.proposed_source + "\n"
                new_text = "---\n".join(parts)
                backup = _backup_file(issue.note_path, backup_root, issue.check)
                _atomic_write(issue.note_path, new_text)
            elif issue.check == "snapshot-missing-backlink":
                text = _read_text(issue.note_path) or ""
                parts = text.split("---\n", 2)
                if len(parts) < 3:
                    raise RuntimeError("could not locate frontmatter")
                # Idempotency BEFORE backup — see snapshot-missing-status above.
                if re.search(r"(?m)^source_session_note:", parts[1]):
                    results.append(Result(
                        check=issue.check, note_path=issue.note_path, status="skipped",
                        error="source_session_note already present (stale Issue?)",
                    ))
                    continue
                parts[1] = parts[1] + issue.proposed_source + "\n"
                new_text = "---\n".join(parts)
                backup = _backup_file(issue.note_path, backup_root, issue.check)
                _atomic_write(issue.note_path, new_text)
            elif issue.check == "session-missing-snapshots-list":
                text = _read_text(issue.note_path) or ""
                parts = text.split("---\n", 2)
                if len(parts) < 3:
                    raise RuntimeError("could not locate frontmatter")
                fm = parts[1]
                # Defensive re-check: if snapshots: already exists in the
                # frontmatter (stale Issue replay / race with another
                # writer), treat as an idempotent no-op. Parity with the
                # snapshot_integrity module — prevents duplicate
                # snapshots: blocks when ^status: anchor injects above an
                # existing list. Run BEFORE backup so stale-Issue replay
                # does not produce a useless backup file.
                if re.search(r"(?m)^snapshots:", fm):
                    results.append(Result(
                        check=issue.check, note_path=issue.note_path, status="skipped",
                        error="snapshots field already present in frontmatter (stale Issue?)",
                    ))
                    continue
                # Apply stem-rename translation so the session list points
                # at the POST-rename filenames.
                proposed = issue.proposed_source
                for old_stem, new_stem in renamed_stems.items():
                    proposed = proposed.replace(f"[[{old_stem}]]", f"[[{new_stem}]]")
                wikilinks = sorted(proposed.splitlines())
                block = "snapshots:\n" + "\n".join(f'  - "{s}"' for s in wikilinks) + "\n"
                # Constrain the ``status:`` anchor lookup to the
                # frontmatter block only — body content containing a
                # ``status:`` line (code blocks, ``## Status`` headings)
                # must NOT be matched.
                new_fm, n = re.subn(
                    r"(?m)^status:", block + "status:", fm, count=1,
                )
                if n == 0:
                    new_fm = fm + block
                new_text = parts[0] + "---\n" + new_fm + "---\n" + parts[2]
                backup = _backup_file(issue.note_path, backup_root, issue.check)
                _atomic_write(issue.note_path, new_text)
            else:
                results.append(Result(check=issue.check, note_path=issue.note_path, status="skipped"))
                continue
            results.append(Result(
                check=issue.check, note_path=issue.note_path, status="applied",
                backup_path=backup,
            ))
        except Exception as exc:  # noqa: BLE001
            results.append(Result(
                check=issue.check, note_path=issue.note_path, status="error",
                error=f"{type(exc).__name__}: {exc}",
            ))
            print(
                f"[vault_doctor] apply failed for {issue.note_path}:\n"
                f"{traceback.format_exc()}",
                file=sys.stderr,
            )
    return results
