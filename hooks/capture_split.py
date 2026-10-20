"""capture_split.py -- move snowballed auto-capture updates to their own notes (#427).

Before #427 an auto-capture candidate became an ``## Update`` on any note the
whole-note search matched, so a few notes grew into catch-alls. This applies
the two rules ``capture_save.find_match`` now enforces to updates already
written. An active ``updated`` ledger row moves when:

- ``same-session``: its note was auto-created by the same session, or
- ``low-cosine``: ``title + body[:300]`` scores below
  ``auto_capture_update_min_cosine`` against the note's original text.

A moved section becomes a new note in the insights folder and keeps its
capture id: its ledger row gets ``kind=new``, the new ``note_path``,
``split_from`` and ``split_heading``, so Reject still works on it. Per note,
new notes and ledger rows are written before the source note is rewritten,
so a crash leaves a duplicate, never a loss; the next ``--apply`` removes
the section left behind (a "repair"). The dry run changes no note, ledger
row or backup.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import os
import re
from collections import Counter
from pathlib import Path

import capture_reject
import capture_save
import capture_state
import note_writer
import obsidian_utils
import vault_index
from obsidian_utils import indexed_folders
from tfidf import _cosine_similarity

_DATE_RE = re.compile(r"\((\d{4}-\d{2}-\d{2})\)")


class ConfigError(ValueError):
    """The config has no usable vault."""


def _norm(rel: str, root: Path) -> str | None:
    return capture_save._vault_rel(rel, root)


def _section(lines: list[str], heading: str, cid: str, title: str) -> tuple[int, int, str] | None:
    """``(start, end, body)`` of the update section ``heading`` opens, or None when it is gone.

    ``body`` is what the capture wrote: no heading, bold title line or end marker.
    """
    try:
        start = lines.index(heading)
    except ValueError:
        return None
    marker = capture_save.end_marker(cid)
    end = capture_save.section_end(lines, start, marker)
    body = lines[start + 1:end]
    if body and body[-1].strip() == marker:
        body = body[:-1]
    while body and not body[0].strip():
        body = body[1:]
    if body and body[0] == f"**{title}**":
        body = body[1:]
    return start, end, "\n".join(body).strip("\n")


def _day(heading: str, ts) -> str:
    m = _DATE_RE.search(heading)
    if m:
        try:
            return datetime.date.fromisoformat(m.group(1)).isoformat()
        except ValueError:
            pass
    try:
        return datetime.date.fromtimestamp(float(ts)).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.date.today().isoformat()


def _created_at(ts) -> str | None:
    try:
        return datetime.datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class _Scorer:
    """Cosine of a section against its note's original text, using the index's term weights."""

    def __init__(self, config: dict):
        db = vault_index._default_db_path()
        # Reuse the index as it is: the dry run must not write anything, the index included.
        self.db = db if os.path.isfile(db) else vault_index.ensure_index(config["vault_path"], indexed_folders(config))
        self._notes: dict[str, dict] = {}

    def note_vector(self, path: Path, text: str) -> dict:
        key = str(path)
        if key not in self._notes:
            parsed = vault_index._parse_note(key) or {}
            original = capture_save.strip_auto_updates(capture_save._strip_frontmatter(text))
            self._notes[key] = vault_index.compute_query_vector(self.db, f"{parsed.get('title') or ''}\n{original}")
        return self._notes[key]

    def cosine(self, path: Path, text: str, title: str, body: str) -> float:
        note_vec = self.note_vector(path, text)
        query_vec = vault_index.compute_query_vector(self.db, f"{title}\n{body[:300]}")
        if not note_vec or not query_vec:
            return 0.0  # find_match refuses an update it cannot score
        return _cosine_similarity(query_vec, note_vec)


def _plan_note(rel: str, text: str, rows: list[dict], repairs: list[dict], created: set[tuple[str, str]],
               scorer: _Scorer, path: Path, threshold: float, vault: Path, root: Path) -> dict:
    """What would change in one note: moves, repairs, skips, and the updates that stay."""
    lines = text.split("\n")
    out = {"note": rel, "moves": [], "repairs": [], "skipped": [], "kept": Counter()}
    for row in rows:
        found = _section(lines, row["update_heading"], row["id"], row.get("title", ""))
        if found is None:
            out["skipped"].append({"id": row["id"], "note": rel, "reason": "section-missing"})
            continue
        _, _, body = found
        if (row.get("session_id"), rel) in created:
            reason, cos = "same-session", None
        else:
            cos = scorer.cosine(path, text, row.get("title", ""), body)
            reason = "low-cosine" if cos < threshold else None
        if reason is None:
            out["kept"][row.get("session_id")] += 1
            continue
        out["moves"].append({"id": row["id"], "reason": reason, "cosine": cos, "title": row.get("title", ""),
                             "row": row, "body": body})
    for row in repairs:
        if row["split_heading"] not in lines:
            continue
        new_rel = _norm(row.get("note_path", ""), root)
        if new_rel is None or not (vault / new_rel).is_file():
            out["skipped"].append({"id": row["id"], "note": rel, "reason": "split-note-missing"})
            continue  # removing the section now would lose it
        out["repairs"].append({"id": row["id"], "heading": row["split_heading"]})
    return out


def _write_split_note(move: dict, config: dict, source_text: str, sessions: dict) -> str:
    """Write one moved section as a new note; return its vault-relative path."""
    row = move["row"]
    vault, folder = config["vault_path"], config.get("insights_folder", "claude-insights")
    ctype = row.get("type") if row.get("type") in capture_save.TYPES else "insight"
    sid, project = row.get("session_id") or "", row.get("project") or ""
    if sid not in sessions:
        sessions[sid] = capture_save.session_note_for(sid, project, config) if sid else ""
    day = _day(row["update_heading"], row.get("ts"))
    topic = obsidian_utils.parse_frontmatter_field(source_text, "topic") or "general"
    cand = {"type": ctype, "title": move["title"], "body": move["body"], "topic": topic}
    content = capture_save._frontmatter(cand, session_id=sid, project=project, session_note=sessions[sid],
                                        today=day, created_at=_created_at(row.get("ts")))
    content += f"\n# {move['title']}\n\n{move['body']}\n"
    filename = capture_save.new_filename(vault, folder, day, move["title"])
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        rc = note_writer.run_write(vault, folder, filename, content)
    if rc != 0:
        raise RuntimeError(f"write failed: {sink.getvalue().strip()}")
    return f"{folder}/{filename}"


def _backup(path: Path, rel: str, state: dict) -> None:
    if state.get("dir") is None:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        d = capture_state.capture_dir() / f"split-backup-{stamp}"
        d.mkdir(mode=0o700)
        state["dir"] = d
    with open(path, encoding="utf-8", newline="") as fh:
        capture_state._atomic_write(state["dir"] / rel.replace("/", "__"), fh.read())


def _apply_note(path: Path, rel: str, plan_args: tuple, config: dict, backup: dict, sessions: dict) -> dict:
    """Re-plan one note under its lock and apply it; the source is rewritten once, last."""
    out = {"note": rel, "moves": [], "repairs": [], "skipped": [], "kept": Counter(), "error": None}
    lock, err = note_writer._acquire_lock(path)
    if err:
        out["error"] = err
        return out
    try:
        st = os.stat(path)
        with open(path, encoding="utf-8", newline="") as fh:
            raw = fh.read()
        crlf = "\r\n" in raw
        text = raw.replace("\r\n", "\n") if crlf else raw
        out = _plan_note(rel, text, *plan_args)
        out["error"] = None
        if not out["moves"] and not out["repairs"]:
            return out
        _backup(path, rel, backup)
        headings = [r["heading"] for r in out["repairs"]]
        for move in out["moves"]:
            new_rel = _write_split_note(move, config, text, sessions)
            row = move["row"]
            capture_state.ledger_update(row["id"], kind="new", note_path=new_rel, update_heading=None,
                                        split_from=rel, split_heading=row["update_heading"])
            move["new_note"] = new_rel
            headings.append(row["update_heading"])
        kept = text
        for heading in headings:
            cid = capture_save.AUTO_HEADING_RE.match(heading)
            kept, _ = capture_reject._remove_section(kept, heading, capture_save.end_marker(cid.group("id")) if cid else "")
        if crlf:
            kept = kept.replace("\n", "\r\n")
        err = note_writer._atomic_rewrite(path, kept, (st.st_mtime_ns, st.st_size))
        if err:
            out["error"] = err
        return out
    except Exception as exc:  # noqa: BLE001 -- one bad note is reported; the run goes on
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        note_writer._release_lock(lock)


def split_updates(config: dict, apply: bool = False) -> dict:
    """Plan (and with ``apply``, carry out) the split; returns the report. Raises ConfigError."""
    vault_str = config.get("vault_path")
    if not vault_str or not Path(vault_str).is_dir():
        raise ConfigError(f"vault_path is not a directory: {vault_str!r}")
    vault = Path(vault_str)
    root = vault.resolve()
    threshold = capture_save.update_min_cosine(config)
    rows = capture_state.ledger_list(limit=10**9)
    rows.reverse()  # oldest first, so sections move in the order they were written

    created: set[tuple[str, str]] = set()
    for r in rows:
        if r.get("kind") == "new" and isinstance(r.get("note_path"), str):
            rel = _norm(r["note_path"], root)
            if rel:
                created.add((r.get("session_id"), rel))

    skipped: list[dict] = []
    by_note: dict[str, list[dict]] = {}
    repairs: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("status") != "active":
            continue
        if r.get("kind") == "updated" and isinstance(r.get("update_heading"), str):
            rel = _norm(str(r.get("note_path") or ""), root)
            if rel is None:
                skipped.append({"id": r["id"], "note": r.get("note_path"), "reason": "outside-vault"})
            elif not (vault / rel).is_file():
                skipped.append({"id": r["id"], "note": rel, "reason": "note-missing"})
            else:
                by_note.setdefault(rel, []).append(r)
        elif r.get("kind") == "new" and r.get("split_from") and isinstance(r.get("split_heading"), str):
            rel = _norm(str(r["split_from"]), root)
            if rel and (vault / rel).is_file():
                repairs.setdefault(rel, []).append(r)

    scorer = _Scorer(config)
    backup: dict = {"dir": None}
    sessions: dict = {}
    notes = []
    for rel in sorted(set(by_note) | set(repairs)):
        path = vault / rel
        plan_args = (by_note.get(rel, []), repairs.get(rel, []), created, scorer, path, threshold, vault, root)
        if apply:
            out = _apply_note(path, rel, plan_args, config, backup, sessions)
        else:
            try:
                with open(path, encoding="utf-8", newline="") as fh:
                    text = fh.read().replace("\r\n", "\n")
                out = _plan_note(rel, text, *plan_args)
                out["error"] = None
            except (OSError, UnicodeDecodeError) as exc:
                out = {"note": rel, "moves": [], "repairs": [], "skipped": [], "kept": Counter(),
                       "error": f"{type(exc).__name__}: {exc}"}
        skipped += out.pop("skipped")
        notes.append(out)

    moves = [m for n in notes for m in n["moves"]]
    reasons = Counter(m["reason"] for m in moves)
    totals = {
        "notes": sum(1 for n in notes if n["moves"] or n["repairs"]),
        "moves": len(moves),
        "same-session": reasons["same-session"],
        "low-cosine": reasons["low-cosine"],
        "repairs": sum(len(n["repairs"]) for n in notes),
        "skipped": len(skipped),
        "failed": sum(1 for n in notes if n["error"]),
        "max_kept_from_one_session": max((c for n in notes for c in n["kept"].values()), default=0),
    }
    for n in notes:
        n.pop("kept")
        for m in n["moves"]:
            m.pop("row")
            m.pop("body")
    report_notes = [n for n in notes if n["moves"] or n["repairs"] or n["error"]]
    return {"apply": apply, "threshold": threshold, "notes": report_notes, "skipped": skipped, "totals": totals,
            "backup": str(backup["dir"]) if backup["dir"] else None}


def format_report(report: dict) -> str:
    """One line per note, then the skips and the totals."""
    out = []
    for n in report["notes"]:
        by = Counter(m["reason"] for m in n["moves"])
        line = f"{n['note']}: move {len(n['moves'])} (same-session {by['same-session']}, low-cosine {by['low-cosine']})"
        if n["repairs"]:
            line += f", repair {len(n['repairs'])}"
        if n["error"]:
            line += f" FAILED: {n['error']}"
        out.append(line)
    for s in report["skipped"]:
        out.append(f"skipped {s['id']}: {s['reason']} ({s['note']})")
    t = report["totals"]
    verb = "moved" if report["apply"] else "would move"
    out.append(f"{verb} {t['moves']} sections from {t['notes']} note{'s' if t['notes'] != 1 else ''} "
               f"(same-session {t['same-session']}, low-cosine {t['low-cosine']}), repairs {t['repairs']}, "
               f"skipped {t['skipped']}, failed {t['failed']}, threshold {report['threshold']}; "
               f"most updates kept on one note from one session: {t['max_kept_from_one_session']}")
    if report.get("backup"):
        out.append(f"backup: {report['backup']}")
    return "\n".join(out)
