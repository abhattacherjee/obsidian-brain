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
from pathlib import Path

from . import Issue, Result

NAME = "snapshot-migration"
DESCRIPTION = "Migrate pre-spec snapshots to first-class format"
DEFAULT_WINDOW_DAYS = 3650

_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _read_text(p):
    try:
        return Path(p).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


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
            extra={"unresolved": unresolved, "new_name": new_name},
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
        if re.search(r"(?m)^snapshots:", sess_text):
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


def _rewrite_wikilinks_in_vault(vault_path: str, old_stem: str, new_stem: str) -> int:
    """Find and replace [[<old_stem>]] with [[<new_stem>]] across the vault.

    Returns the number of files modified. Uses atomic writes per file.
    """
    count = 0
    pattern = re.compile(r"\[\[" + re.escape(old_stem) + r"\]\]")
    for p in Path(vault_path).rglob("*.md"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if pattern.search(text):
            new_text = pattern.sub(f"[[{new_stem}]]", text)
            try:
                _atomic_write(str(p), new_text)
                count += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[vault_doctor] wikilink rewrite failed for {p}: {exc}",
                      file=sys.stderr)
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
    # Track renames applied during this batch so later issues targeting the
    # old path can be redirected to the new path. Without this, snapshot-
    # missing-status/backlink fail with FileNotFoundError when batched with
    # a rename of the same snapshot.
    renamed: dict[str, str] = {}
    for issue in issues_sorted:
        # Redirect any path that was renamed earlier in this apply pass.
        if issue.note_path in renamed:
            issue.note_path = renamed[issue.note_path]
        if issue.extra.get("unresolved"):
            results.append(Result(
                check=issue.check, note_path=issue.note_path, status="unresolved",
                error=issue.reason,
            ))
            continue
        try:
            if issue.check == "snapshot-legacy-filename":
                src = Path(issue.note_path)
                dst = src.parent / issue.extra["new_name"]
                _backup_file(str(src), backup_root, issue.check)
                _rewrite_wikilinks_in_vault(str(src.parents[1]), src.stem, dst.stem)
                os.rename(str(src), str(dst))
                renamed[str(src)] = str(dst)
            elif issue.check == "snapshot-missing-status":
                _backup_file(issue.note_path, backup_root, issue.check)
                text = _read_text(issue.note_path) or ""
                parts = text.split("---\n", 2)
                if len(parts) >= 3:
                    parts[1] = parts[1] + issue.proposed_source + "\n"
                    new_text = "---\n".join(parts)
                    _atomic_write(issue.note_path, new_text)
                else:
                    raise RuntimeError("could not locate frontmatter")
            elif issue.check == "snapshot-missing-backlink":
                _backup_file(issue.note_path, backup_root, issue.check)
                text = _read_text(issue.note_path) or ""
                parts = text.split("---\n", 2)
                if len(parts) >= 3:
                    parts[1] = parts[1] + issue.proposed_source + "\n"
                    new_text = "---\n".join(parts)
                    _atomic_write(issue.note_path, new_text)
                else:
                    raise RuntimeError("could not locate frontmatter")
            elif issue.check == "session-missing-snapshots-list":
                _backup_file(issue.note_path, backup_root, issue.check)
                text = _read_text(issue.note_path) or ""
                wikilinks = sorted(issue.proposed_source.splitlines())
                block = "snapshots:\n" + "\n".join(f'  - "{s}"' for s in wikilinks) + "\n"
                new_text, n = re.subn(r"(?m)^status:", block + "status:", text, count=1)
                if n == 0:
                    parts = text.split("---\n", 2)
                    if len(parts) < 3:
                        raise RuntimeError("could not locate frontmatter")
                    parts[1] = parts[1] + block
                    new_text = "---\n".join(parts)
                _atomic_write(issue.note_path, new_text)
            else:
                results.append(Result(check=issue.check, note_path=issue.note_path, status="skipped"))
                continue
            results.append(Result(
                check=issue.check, note_path=issue.note_path, status="applied",
                backup_path=str(Path(backup_root) / issue.check),
            ))
        except Exception as exc:  # noqa: BLE001
            results.append(Result(
                check=issue.check, note_path=issue.note_path, status="error",
                error=f"{type(exc).__name__}: {exc}",
            ))
    return results
