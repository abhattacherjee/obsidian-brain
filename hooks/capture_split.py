"""capture_split.py -- move snowballed auto-capture updates to their own notes (#427).

Before #427 an auto-capture candidate became an ``## Update`` on any note the
whole-note search matched, so a few notes grew into catch-alls. This applies
the two rules ``capture_save.find_match`` now enforces to updates already
written. An active ``updated`` ledger row moves when:

- ``same-session``: its note was created by the same session, through
  auto-capture (ledger) or by hand (frontmatter ``source_session``), or
- ``low-cosine``: ``title + body[:300]`` scores below
  ``auto_capture_update_min_cosine`` against the note's original text, with
  the same scorer ``find_match`` uses (``capture_save.original_vector``).

A moved section becomes a new note in the insights folder and keeps its
capture id: its ledger row gets ``kind=new``, the new ``note_path``,
``split_from`` and ``split_heading`` (and ``update_heading=None``), so Reject
still works on it. Per move, the ledger row is written first, then the new
note; the source note is rewritten once, last, without the moves that worked.
A crash at any point leaves the section in the source, so nothing is lost,
and the next ``--apply`` repairs it: a leftover section whose split note
exists is removed, and one whose split note is missing is written to that
note first. The dry run writes nothing, the index included: it needs an
existing index and refuses without one.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

import capture_reject
import capture_save
import capture_state
import frontmatter
import note_writer
import obsidian_utils
import vault_index
from obsidian_utils import indexed_folders
from tfidf import _cosine_similarity

_DATE_RE = re.compile(r"\((\d{4}-\d{2}-\d{2})\)")
UNTITLED = "Untitled capture"  # what save_candidate writes for an empty title


class ConfigError(ValueError):
    """Nothing can run: the config has no usable vault, or a dry run has no index to score with."""


class SplitError(RuntimeError):
    """The run cannot score safely (the index is unreadable); nothing was written."""


def _norm(rel: str, root: Path) -> str | None:
    return capture_save._vault_rel(rel, root)


def _title(row: dict) -> str:
    """The row's title as save_candidate would have written it; a null or non-string title is ``UNTITLED``."""
    title = row.get("title")
    return " ".join(title.split()) if isinstance(title, str) and title.strip() else UNTITLED


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


def _topic(source_text: str) -> str:
    """The source note's topic, from its ``claude/topic/<x>`` tag (the templates have no ``topic:`` key)."""
    _open, fm_lines, _close, _body, err = frontmatter.split_frontmatter(
        frontmatter.split_lines_lf_crlf(source_text.lstrip("\ufeff")))
    if err:  # only a line that is exactly ``---`` closes it, so a "a---b" title cannot cut it short
        return "general"
    for tag in obsidian_utils._parse_note_tags("".join(fm_lines)):
        if tag.startswith("claude/topic/") and tag[len("claude/topic/"):]:
            return tag[len("claude/topic/"):]
    return "general"


class _Scorer:
    """Cosine of a section against its note's original text: find_match's scorer, on the index's term weights."""

    def __init__(self, config: dict, apply: bool):
        db = vault_index._default_db_path()
        if not os.path.isfile(db):
            if not apply:  # the dry run writes nothing, the index included
                raise ConfigError(f"index missing at {db}; run /vault-reindex first (the dry run never builds it)")
            db = vault_index.ensure_index(config["vault_path"], indexed_folders(config))
        try:  # fail before any write when the index cannot score
            conn = vault_index._connect(db)
            try:
                conn.execute("SELECT COUNT(*) FROM notes").fetchone()
                conn.execute("SELECT term, df FROM term_df LIMIT 1").fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise SplitError(f"index unreadable at {db}: {exc}; run /vault-reindex") from exc
        self.db = db
        self._notes: dict[str, dict] = {}

    def cosine(self, path: Path, title: str, body: str) -> float:
        key = str(path)
        if key not in self._notes:
            self._notes[key] = capture_save.original_vector(self.db, path)
        note_vec = self._notes[key]
        query_vec = vault_index.compute_query_vector(self.db, capture_save.query_text(title, body))
        if not note_vec or not query_vec:
            return 0.0  # find_match refuses an update it cannot score
        return _cosine_similarity(query_vec, note_vec)


def _plan_note(rel: str, text: str, rows: list[dict], repairs: list[dict], created: set[tuple[str, str]],
               scorer: _Scorer, path: Path, threshold: float, vault: Path, root: Path) -> dict:
    """What would change in one note: moves, repairs, skips, and the updates that stay."""
    lines = text.split("\n")
    out = {"note": rel, "moves": [], "repairs": [], "skipped": [], "kept": Counter(), "errors": []}
    author = obsidian_utils._peek_frontmatter_field(path, "source_session") if rows else None  # wrote it by hand
    for row in rows:
        title = _title(row)
        found = _section(lines, row["update_heading"], row["id"], title)
        if found is None:
            out["skipped"].append({"id": row["id"], "note": rel, "reason": "section-missing"})
            continue
        _, _, body = found
        if (row.get("session_id"), rel) in created or (author and row.get("session_id") == author):
            reason, cos = "same-session", None
        else:
            cos = scorer.cosine(path, title, body)
            reason = "low-cosine" if cos < threshold else None
        if reason is None:
            out["kept"][row.get("session_id")] += 1
            continue
        out["moves"].append({"id": row["id"], "reason": reason, "cosine": cos, "title": title,
                             "row": row, "body": body})
    for row in repairs:
        title = _title(row)
        found = _section(lines, row["split_heading"], row["id"], title)
        if found is None:
            continue  # already gone: the split finished
        new_rel = _norm(str(row.get("note_path") or ""), root)
        if new_rel is None and row.get("status") != "rejected":
            out["skipped"].append({"id": row["id"], "note": rel, "reason": "split-note-outside-vault"})
            continue
        out["repairs"].append({"id": row["id"], "heading": row["split_heading"], "new_note": new_rel,
                               "exists": bool(new_rel) and (vault / new_rel).is_file(), "title": title, "row": row,
                               "body": found[2], "rejected": row.get("status") == "rejected"})
    return out


def _note_content(row: dict, title: str, body: str, heading: str, config: dict, source_text: str,
                  sessions: dict) -> str:
    """The new note for one moved section."""
    ctype = row.get("type") if row.get("type") in capture_save.TYPES else "insight"
    sid, project = str(row.get("session_id") or ""), str(row.get("project") or "")
    if sid not in sessions:
        sessions[sid] = capture_save.session_note_for(sid, project, config) if sid else ""
    cand = {"type": ctype, "title": title, "body": body, "topic": _topic(source_text)}
    content = capture_save._frontmatter(cand, session_id=sid, project=project, session_note=sessions[sid],
                                        today=_day(heading, row.get("ts")), created_at=_created_at(row.get("ts")))
    return content + f"\n# {title}\n\n{body}\n"


def _write_split_note(vault: str, rel: str, content: str) -> None:
    folder, _, filename = rel.rpartition("/")
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        rc = note_writer.run_write(vault, folder, filename, content)
    if rc != 0:
        raise RuntimeError(f"write failed: {sink.getvalue().strip()}")


def _backup(path: Path, rel: str, state: dict) -> None:
    if state.get("dir") is None:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        d = capture_state.capture_dir() / f"split-backup-{stamp}"
        d.mkdir(mode=0o700)
        state["dir"] = d
    with open(path, encoding="utf-8", newline="") as fh:
        capture_state._atomic_write(state["dir"] / rel.replace("/", "__"), fh.read())


def _apply_note(path: Path, rel: str, plan_args: tuple, config: dict, backup: dict, sessions: dict) -> dict:
    """Re-plan one note under its lock and apply it; the source is rewritten once, last.

    A move or repair that fails is reported alone; the source is still
    rewritten without the ones that worked. Only moves whose note was written
    stay in ``moves``.
    """
    out = {"note": rel, "moves": [], "repairs": [], "skipped": [], "kept": Counter(), "errors": [], "error": None}
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
        out = {**_plan_note(rel, text, *plan_args), "error": None}
        if not out["moves"] and not out["repairs"]:
            return out
        _backup(path, rel, backup)
        vault, folder = config["vault_path"], config.get("insights_folder", "claude-insights")
        remove, done_repairs, done_moves = [], [], []
        for rep in out["repairs"]:
            try:
                if not rep["exists"] and not rep["rejected"]:  # named by the ledger, never written (or deleted)
                    _write_split_note(vault, rep["new_note"], _note_content(
                        rep["row"], rep["title"], rep["body"], rep["heading"], config, text, sessions))
                remove.append(rep["heading"])
                done_repairs.append(rep)
            except Exception as exc:  # noqa: BLE001 -- this repair alone failed; the section stays
                out["errors"].append({"id": rep["id"], "error": f"{type(exc).__name__}: {exc}"})
        for move in out["moves"]:
            row, heading = move["row"], move["row"]["update_heading"]
            try:
                content = _note_content(row, move["title"], move["body"], heading, config, text, sessions)
                new_rel = f"{folder}/{capture_save.new_filename(vault, folder, _day(heading, row.get('ts')), move['title'])}"
                # The ledger first: a crash after it leaves a row that names the note, which the next run writes.
                capture_state.ledger_update(row["id"], kind="new", note_path=new_rel, update_heading=None,
                                            split_from=rel, split_heading=heading)
                _write_split_note(vault, new_rel, content)
                move["new_note"] = new_rel
                remove.append(heading)
                done_moves.append(move)
            except Exception as exc:  # noqa: BLE001 -- this move alone failed; the section stays in the source
                out["errors"].append({"id": row["id"], "error": f"{type(exc).__name__}: {exc}"})
        out["moves"], out["repairs"] = done_moves, done_repairs
        if remove:
            kept = text
            for heading in remove:
                cid = capture_save.AUTO_HEADING_RE.match(heading)
                kept, _ = capture_reject._remove_section(kept, heading,
                                                         capture_save.end_marker(cid.group("id")) if cid else "")
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
    """Plan (and with ``apply``, carry out) the split; returns the report.

    Raises ConfigError (no vault, or a dry run without an index) or
    SplitError (unreadable index) before anything is written.
    """
    vault_str = config.get("vault_path")
    if not vault_str or not Path(vault_str).is_dir():
        raise ConfigError(f"vault_path is not a directory: {vault_str!r}")
    vault = Path(vault_str)
    root = vault.resolve()
    threshold = capture_save.update_min_cosine(config)
    scorer = _Scorer(config, apply)
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
        if (r.get("status") == "rejected" and r.get("kind") == "new" and r.get("split_from")
                and isinstance(r.get("split_heading"), str)):
            rel = _norm(str(r["split_from"]), root)  # its leftover section goes too; no note is written for it
            if rel and (vault / rel).is_file():
                repairs.setdefault(rel, []).append(r)
            continue
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
                out = {**_plan_note(rel, text, *plan_args), "error": None}
            except Exception as exc:  # noqa: BLE001 -- reported per note, as --apply does
                out = {"note": rel, "moves": [], "repairs": [], "skipped": [], "kept": Counter(), "errors": [],
                       "error": f"{type(exc).__name__}: {exc}"}
        skipped += out.pop("skipped")
        notes.append(out)

    moves = [m for n in notes for m in n["moves"]]  # under --apply, only the moves whose note was written
    reasons = Counter(m["reason"] for m in moves)
    totals = {
        "notes": sum(1 for n in notes if n["moves"] or n["repairs"]),
        "moves": len(moves),
        "same-session": reasons["same-session"],
        "low-cosine": reasons["low-cosine"],
        "repairs": sum(len(n["repairs"]) for n in notes),
        "skipped": len(skipped),
        "failed": sum(1 for n in notes if n["error"] or n["errors"]),
        "max_kept_from_one_session": max((c for n in notes for c in n["kept"].values()), default=0),
    }
    for n in notes:
        n.pop("kept")
        for item in (*n["moves"], *n["repairs"]):
            item.pop("row", None)
            item.pop("body", None)
    report_notes = [n for n in notes if n["moves"] or n["repairs"] or n["error"] or n["errors"]]
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
        out += [f"  failed {e['id']}: {e['error']} (the next --apply retries it)" for e in n["errors"]]
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
