"""vault_doctor check: snapshot integrity (orphans, broken backlinks,
stale/missing session snapshot lists, session_id mismatch, status/summary
mismatch).

Module registry interface: NAME, DESCRIPTION, DEFAULT_WINDOW_DAYS, scan,
apply. Produces Issue objects with distinct ``check`` names so the
dispatcher can report per-check counts.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from . import Issue, Result

NAME = "snapshot-integrity"
DESCRIPTION = "Snapshot orphans, broken backlinks, stale lists, status/summary mismatches"
DEFAULT_WINDOW_DAYS = 3650  # all-history; additive + idempotent

_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_WIKI_RE = re.compile(r'\[\[([^\]]+)\]\]')


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _parse_fm(text: str) -> dict:
    m = _FRONT_RE.match(text)
    if not m:
        return {}
    out: dict = {"snapshots": []}
    in_list = None
    for line in m.group(1).splitlines():
        s = line.rstrip("\r")
        if s.startswith("  - ") and in_list:
            val = s[4:].strip().strip('"').strip("'")
            out.setdefault(in_list, []).append(val)
            continue
        in_list = None
        if not s or s.startswith(" "):
            continue
        if ":" not in s:
            continue
        key, _, val = s.partition(":")
        key = key.strip()
        val = val.strip()
        if not val:
            in_list = key
            out[key] = []
        else:
            out[key] = val.strip('"').strip("'")
    return out


def _project_matches(meta_project: str, filter_project: str | None) -> bool:
    if not filter_project:
        return True
    return meta_project.lower() == filter_project.lower()


def scan(vault_path: str, sessions_folder: str, insights_folder: str,
         days: int, project: str | None = None) -> list:
    """Return Issue objects for all six check kinds."""
    sess_dir = Path(vault_path) / sessions_folder
    if not sess_dir.is_dir():
        return []

    # Index all session/snapshot files once to avoid O(N^2) scans
    sessions_by_id: dict[str, dict] = {}
    snapshots: list[dict] = []
    for p in sess_dir.iterdir():
        if p.suffix != ".md":
            continue
        text = _read_text(str(p))
        if not text:
            continue
        fm = _parse_fm(text)
        if not fm:
            continue
        if not _project_matches(fm.get("project", ""), project):
            continue
        type_ = fm.get("type", "")
        if type_ == "claude-session":
            sid = fm.get("session_id", "")
            if sid:
                sessions_by_id[sid] = {
                    "path": str(p), "fm": fm, "stem": p.stem, "text": text,
                }
        elif type_ == "claude-snapshot":
            snapshots.append({"path": str(p), "fm": fm, "stem": p.stem, "text": text})

    issues: list[Issue] = []

    # 1. snapshot-orphan
    # 2. snapshot-broken-backlink
    # 5. snapshot-session-id-mismatch
    # 6. snapshot-summary-status-mismatch
    for snap in snapshots:
        fm = snap["fm"]
        sid = fm.get("session_id", "")
        project_name = fm.get("project", "")
        parent_session = sessions_by_id.get(sid)

        if not parent_session:
            issues.append(Issue(
                check="snapshot-orphan",
                note_path=snap["path"],
                project=project_name,
                current_source=f"session_id={sid}",
                proposed_source="",
                reason="no session note on disk matches this session_id",
                confidence=0.9,
                extra={"unresolved": True},
            ))
            continue

        # Backlink check
        raw_backlink = fm.get("source_session_note", "")
        m = _WIKI_RE.search(raw_backlink)
        backlink_stem = m.group(1) if m else ""
        if backlink_stem and backlink_stem != parent_session["stem"]:
            issues.append(Issue(
                check="snapshot-broken-backlink",
                note_path=snap["path"],
                project=project_name,
                current_source=f"[[{backlink_stem}]]",
                proposed_source=f"[[{parent_session['stem']}]]",
                reason="source_session_note wikilink does not match resolved parent",
                confidence=0.95,
            ))

        # session_id check
        parent_sid_field = parent_session["fm"].get("session_id", "")
        if parent_sid_field != sid:
            issues.append(Issue(
                check="snapshot-session-id-mismatch",
                note_path=snap["path"],
                project=project_name,
                current_source=f"snapshot={sid}, session={parent_sid_field}",
                proposed_source="",
                reason="snapshot session_id and target session session_id disagree",
                confidence=0.7,
                extra={"unresolved": True},
            ))

        # Status vs summary
        has_real_summary = bool(re.search(r"^## Summary\s*\n\S", snap["text"], re.MULTILINE))
        status = fm.get("status", "")
        if status == "auto-logged" and has_real_summary:
            issues.append(Issue(
                check="snapshot-summary-status-mismatch",
                note_path=snap["path"],
                project=project_name,
                current_source="status=auto-logged, ## Summary present",
                proposed_source="status=summarized",
                reason="summary body exists but status was not flipped",
                confidence=0.95,
            ))
        elif status == "summarized" and not has_real_summary:
            issues.append(Issue(
                check="snapshot-summary-status-mismatch",
                note_path=snap["path"],
                project=project_name,
                current_source="status=summarized, ## Summary missing or placeholder",
                proposed_source="status=auto-logged",
                reason="status claims summarized but ## Summary body is absent",
                confidence=0.8,
            ))

    # 3. session-snapshot-list-stale, 4. session-snapshot-list-missing
    snaps_by_session: dict[str, list[str]] = {}
    for snap in snapshots:
        sid = snap["fm"].get("session_id", "")
        if sid:
            snaps_by_session.setdefault(sid, []).append(f"[[{snap['stem']}]]")

    for sid, sess in sessions_by_id.items():
        on_disk = sorted(snaps_by_session.get(sid, []))
        in_frontmatter = sess["fm"].get("snapshots") or []

        # Missing list
        if on_disk and not in_frontmatter:
            issues.append(Issue(
                check="session-snapshot-list-missing",
                note_path=sess["path"],
                project=sess["fm"].get("project", ""),
                current_source="snapshots field absent",
                proposed_source="\n".join(on_disk),
                reason=f"{len(on_disk)} snapshot(s) on disk but session has no snapshots: list",
                confidence=0.98,
            ))
            continue

        # Stale entries (present in frontmatter but not on disk)
        stale = [s for s in in_frontmatter if s not in set(on_disk)]
        if stale:
            issues.append(Issue(
                check="session-snapshot-list-stale",
                note_path=sess["path"],
                project=sess["fm"].get("project", ""),
                current_source="\n".join(in_frontmatter),
                proposed_source="\n".join([s for s in in_frontmatter if s in set(on_disk)]),
                reason=f"{len(stale)} stale snapshot wikilink(s)",
                confidence=0.95,
                extra={"stale": stale},
            ))

    return issues


def _write_atomic(path: str, text: str, backup_root: str, check_name: str) -> None:
    """Write text to path atomically, backing up the original under backup_root."""
    import shutil, tempfile
    p = Path(path)
    if p.exists():
        bdir = Path(backup_root) / check_name
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, bdir / p.name)
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


def apply(issues, backup_root: str) -> list:
    results: list[Result] = []
    for issue in issues:
        if issue.extra.get("unresolved"):
            results.append(Result(
                check=issue.check, note_path=issue.note_path, status="unresolved",
                error="no auto-fix available; manual review required",
            ))
            continue
        try:
            if issue.check == "snapshot-broken-backlink":
                text = _read_text(issue.note_path) or ""
                new_text = re.sub(
                    r'(?m)^source_session_note:.*$',
                    f'source_session_note: "{issue.proposed_source}"',
                    text, count=1,
                )
                _write_atomic(issue.note_path, new_text, backup_root, issue.check)
            elif issue.check == "snapshot-summary-status-mismatch":
                new_status = "summarized" if "status=summarized" in issue.proposed_source else "auto-logged"
                text = _read_text(issue.note_path) or ""
                new_text = re.sub(r"(?m)^status:\s*\S+", f"status: {new_status}", text, count=1)
                _write_atomic(issue.note_path, new_text, backup_root, issue.check)
            elif issue.check == "session-snapshot-list-missing":
                text = _read_text(issue.note_path) or ""
                wikilinks = sorted(issue.proposed_source.splitlines())
                block = "snapshots:\n" + "\n".join(f'  - "{s}"' for s in wikilinks) + "\n"
                new_text = re.sub(r"(?m)^status:", block + "status:", text, count=1)
                _write_atomic(issue.note_path, new_text, backup_root, issue.check)
            elif issue.check == "session-snapshot-list-stale":
                text = _read_text(issue.note_path) or ""
                stale = issue.extra.get("stale", [])
                lines = text.splitlines(keepends=True)
                cleaned: list[str] = []
                for line in lines:
                    s = line.strip()
                    if any(s == f'- "{v}"' or s == f"- '{v}'" for v in stale):
                        continue
                    cleaned.append(line)
                _write_atomic(issue.note_path, "".join(cleaned), backup_root, issue.check)
            else:
                results.append(Result(
                    check=issue.check, note_path=issue.note_path, status="skipped",
                ))
                continue
            results.append(Result(
                check=issue.check, note_path=issue.note_path, status="applied",
                backup_path=os.path.join(backup_root, issue.check),
            ))
        except Exception as exc:  # noqa: BLE001
            results.append(Result(
                check=issue.check, note_path=issue.note_path, status="error",
                error=f"{type(exc).__name__}: {exc}",
            ))
            print(f"[vault_doctor] apply failed for {issue.note_path}: {exc}",
                  file=sys.stderr)
    return results
