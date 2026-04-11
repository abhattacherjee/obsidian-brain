"""vault_doctor check: detect and repair stale source_session backlinks.

Detection strategy:
  For each insight-type note with a `source_session` frontmatter field,
  read the note's file mtime as the "capture time." For each JSONL file
  under ~/.claude/projects/*<project>/*.jsonl, determine its activity
  window (first entry timestamp → file mtime). The correct source session
  is the JSONL whose window contains the note's capture time. Flag as
  stale whenever the note's current source_session does not match.
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import Issue, Result

NAME = "source-sessions"
DESCRIPTION = "Detect and repair stale source_session backlinks"
DEFAULT_WINDOW_DAYS = 7

# Insight-type folders we scan. Each is relative to vault root.
_INSIGHT_FOLDERS = [
    "claude-insights",
    "claude-decisions",
    "claude-error-fixes",
    "claude-retros",
]

# Regex helpers for minimal frontmatter parsing (stdlib only — no yaml dep)
_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_WIKI_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _parse_frontmatter(text: str) -> dict:
    """Parse a flat key: value YAML frontmatter block. Nested blocks ignored."""
    m = _FRONT_RE.match(text)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        if not line or line.startswith(" ") or line.startswith("-"):
            continue  # skip list items and nested keys
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _parse_iso_ts(ts: str) -> float | None:
    """Parse ISO 8601 timestamp to a POSIX float, or None on failure."""
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def _jsonl_window(jsonl_path: str) -> tuple[float, float] | None:
    """Return (first_entry_ts, mtime) for a JSONL session file, or None."""
    try:
        mtime = os.path.getmtime(jsonl_path)
    except OSError:
        return None
    first_ts: float | None = None
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_iso_ts(entry.get("timestamp", ""))
                if ts is not None:
                    first_ts = ts
                    break
    except OSError:
        return None
    if first_ts is None:
        # Fallback: loose 1-hour lower bound before mtime
        first_ts = mtime - 3600
    return (first_ts, mtime)


def _jsonl_dir_for_project(project: str) -> Path | None:
    """Find ~/.claude/projects/*<project>/ directory for this project name."""
    home = os.environ.get("HOME", os.path.expanduser("~"))
    pattern = os.path.join(home, ".claude", "projects", f"*{project}")
    matches = glob.glob(pattern)
    if not matches:
        return None
    # Pick the most recently modified directory if multiple match (path encoding variants)
    return Path(max(matches, key=os.path.getmtime))


def _list_session_notes(sessions_dir: Path, project: str) -> dict[str, dict]:
    """Map session_id → {path, basename, date} for a project's session notes."""
    out: dict[str, dict] = {}
    if not sessions_dir.is_dir():
        return out
    for entry in sessions_dir.iterdir():
        if not entry.name.endswith(".md"):
            continue
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        if fm.get("project") != project:
            continue
        sid = fm.get("session_id", "")
        if not sid:
            continue
        out[sid] = {
            "path": entry,
            "basename": entry.name[:-3],
            "date": fm.get("date", ""),
        }
    return out


def _find_matching_session(
    capture_time: float,
    jsonl_dir: Path | None,
    session_note_index: dict[str, dict],
) -> dict | None:
    """Return the session note dict (+ 'sid' key) whose JSONL window contains capture_time.

    Iteration is deterministic (sorted by filename). When multiple windows
    contain capture_time (boundary tie between a previous session's end and
    a new session's start), the session with the LATEST first_ts wins — the
    most recently started session is the one actively capturing insights.
    """
    if not jsonl_dir or not jsonl_dir.is_dir():
        return None
    candidates: list[tuple[float, str]] = []
    for jsonl in sorted(jsonl_dir.glob("*.jsonl")):
        sid = jsonl.stem
        if sid not in session_note_index:
            continue
        window = _jsonl_window(str(jsonl))
        if window is None:
            continue
        first_ts, last_ts = window
        if first_ts <= capture_time <= last_ts:
            candidates.append((first_ts, sid))
    if not candidates:
        return None
    # Latest-start wins on boundary ties
    _, best_sid = max(candidates)
    return {**session_note_index[best_sid], "sid": best_sid}


def scan(
    vault_path: str,
    sessions_folder: str,
    insights_folder: str,  # kept for signature uniformity; we iterate all _INSIGHT_FOLDERS
    days: int,
    project: str | None = None,
) -> list[Issue]:
    """Detect stale source_session backlinks modified within the last `days` days."""
    _ = insights_folder  # explicitly unused; we scan all _INSIGHT_FOLDERS
    vault = Path(vault_path)
    cutoff = time.time() - days * 86400
    sessions_dir = vault / sessions_folder

    issues: list[Issue] = []
    session_index_cache: dict[str, dict[str, dict]] = {}
    jsonl_dir_cache: dict[str, Path | None] = {}

    for folder in _INSIGHT_FOLDERS:
        folder_path = vault / folder
        if not folder_path.is_dir():
            continue
        for note in folder_path.iterdir():
            if not note.name.endswith(".md"):
                continue
            try:
                mtime = os.path.getmtime(note)
            except OSError:
                continue
            if mtime < cutoff:
                continue
            try:
                text = note.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm = _parse_frontmatter(text)
            if "source_session" not in fm:
                continue  # non-source-session note type (e.g., standups with source_notes[])
            note_project = fm.get("project", "")
            if not note_project:
                continue
            if project and note_project != project:
                continue

            if note_project not in session_index_cache:
                session_index_cache[note_project] = _list_session_notes(sessions_dir, note_project)
                jsonl_dir_cache[note_project] = _jsonl_dir_for_project(note_project)

            idx = session_index_cache[note_project]
            jsonl_dir = jsonl_dir_cache[note_project]

            current_sid = fm.get("source_session", "")
            m = _WIKI_RE.search(fm.get("source_session_note", ""))
            current_src_basename = m.group(1) if m else ""

            match = _find_matching_session(mtime, jsonl_dir, idx)
            if match is None:
                # No JSONL window contains this mtime. Flag as unresolved ONLY if
                # the current source doesn't resolve to any known session note — that
                # way we don't get false positives on notes whose current source is
                # correct but just doesn't have a matching JSONL window locally.
                if current_sid not in idx:
                    issues.append(
                        Issue(
                            check=NAME,
                            note_path=str(note),
                            project=note_project,
                            current_source=f"[[{current_src_basename}]]" if current_src_basename else "",
                            proposed_source="",
                            reason="no session window contains note mtime",
                            confidence=0.0,
                            extra={"unresolved": True},
                        )
                    )
                continue

            if match["sid"] == current_sid:
                continue  # correct

            issues.append(
                Issue(
                    check=NAME,
                    note_path=str(note),
                    project=note_project,
                    current_source=f"[[{current_src_basename}]]",
                    proposed_source=f"[[{match['basename']}]]",
                    reason=(
                        f"note mtime {datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec='seconds')}"
                        f" matches session {match['sid'][:8]} window, not current source {current_sid[:8]}"
                    ),
                    confidence=0.95,
                    extra={"proposed_sid": match["sid"]},
                )
            )

    return issues


def apply(issues, backup_root) -> list[Result]:
    raise NotImplementedError("implemented in Task 8")
