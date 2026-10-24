"""capture_select.py -- promote at most 10 auto notes per session by merit (#455).

Every new auto note is saved ``status: provisional`` (out of the default
retrieval pool). After each capture slice, ``run_selection`` re-ranks the
session's new notes from the ledger and promotes the best ``CAP`` of them:

- merit = 0.7 * score/10 + 0.3 * novelty (+ 0.02 for error-fix and decision);
  novelty = 1 - the max TF-IDF cosine to earlier curated notes of the same
  project from other sessions;
- a note the worker marked ``duplicate_of`` or ``superseded_by`` is left
  provisional, and a pick within ``DUP_COS`` of an earlier pick is skipped;
- picks are greedy: merit minus ``LAMBDA`` times the max cosine to the notes
  already picked (maximal marginal relevance).

A note that was really used (a ``search`` or ``tool-open`` access after it
was created) or updated by hand is promoted for good, outside the cap.
``promote_used`` does that for finished sessions too: it runs at the end of
every capture batch and promotes any provisional note used since it was
captured, in any project and session. ``demote_closed`` demotes a promoted note that is chiefly the state of an
issue that closed after the note was captured.

Statuses: provisional, promoted (insight and error-fix), active (decision,
keeping its lifecycle). A note with any other status, or none, is never
touched.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
from pathlib import Path

import capture_save
import capture_slice
import capture_state
import note_writer
import obsidian_utils
import vault_index
from obsidian_utils import indexed_folders
from tfidf import _cosine_similarity

CAP = 10
LAMBDA = 0.5
DUP_COS = 0.6  # the lexical backstop for reworded duplicates the worker missed
TYPE_BONUS = {"error-fix": 0.02, "decision": 0.02}
NOVELTY_MISSING = 0.5  # no vector to compare: neither novel nor stale
DEFAULT_SCORE = 5
PROVISIONAL = capture_save.PROVISIONAL
STICKY_CONTEXTS = ("search", "tool-open")  # "related" and "recall" rows fire on their own
TOOL_OPEN = "tool-open"
ISSUE_CLOSED = "issue-closed"
ISSUE_WINDOW_SECONDS = 60 * 86400
ISSUE_CHECK_EVERY_SECONDS = 6 * 3600
GH_TIMEOUT_SECONDS = 10
_FM_FIELDS = ("status", "promoted_by", "demoted_by", "created_at", "capture_merit")
_CURATED = ("claude-insight", "claude-decision", "claude-error-fix")


def promoted_status(note_type: str) -> str:
    """The promoted tier's status: decisions keep ``active`` (their lifecycle); the rest say ``promoted``."""
    return "active" if note_type == "decision" else "promoted"


def _score(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
        return DEFAULT_SCORE
    return value


def _novelty(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        return NOVELTY_MISSING  # NaN fails the range check too
    return float(value)


def merit(score, novelty, note_type) -> float:
    """0.7 * score/10 + 0.3 * novelty, + 0.02 for error-fix and decision; bad inputs fall back."""
    return round(0.7 * _score(score) / 10 + 0.3 * _novelty(novelty) + TYPE_BONUS.get(note_type, 0.0), 4)


def select(items: list[dict], cap: int = CAP, lam: float = LAMBDA, dup_cos: float = DUP_COS) -> list[str]:
    """Ids of up to ``cap`` items, picked greedily by merit minus ``lam`` x max cosine to the picks.

    Each item is ``{id, merit, vector, ts}``; a missing vector has cosine 0.
    An item at cosine >= ``dup_cos`` to any pick is never picked. Ties break by
    earlier ``ts``, then id, so the result does not depend on input order.
    """
    pool = sorted(items, key=lambda it: (-it["merit"], it.get("ts") or 0.0, it["id"]))
    picked: list[dict] = []
    while pool and len(picked) < cap:
        best = best_val = None
        for it in pool:
            sim = max((_cosine_similarity(it.get("vector") or {}, p.get("vector") or {}) for p in picked),
                      default=0.0)
            if sim >= dup_cos:
                continue
            val = it["merit"] - lam * sim
            if best_val is None or val > best_val:
                best, best_val = it, val
        if best is None:
            break
        picked.append(best)
        pool.remove(best)
    return [it["id"] for it in picked]


def _epoch(iso: str | None, fallback: float) -> float:
    """``created_at`` (tz-aware ISO) as epoch seconds; a naive or bad value gives ``fallback``."""
    try:
        dt = datetime.datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return fallback
    return dt.timestamp() if dt.tzinfo else fallback


def hand_updated(path: Path) -> bool:
    """True when the note has a ``## Update (`` heading, outside code fences, that auto-capture did not write.

    That is the heading ``/compress`` appends; auto-capture's own match
    ``capture_save.AUTO_HEADING_RE``.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    fence = None
    for line in capture_save._strip_frontmatter(text.replace("\r\n", "\n")).split("\n"):
        fence, is_fence = capture_save.fence_step(fence, line)
        if is_fence or fence is not None:
            continue
        if line.startswith("## Update (") and not capture_save.AUTO_HEADING_RE.match(line):
            return True
    return False


def is_sticky(abs_path: str, created: float, db: str) -> str | None:
    """``"access"``, ``"hand-update"`` or None (Rulings 4 and 5).

    An access is a ``search`` or ``tool-open`` row after ``created`` (epoch s).
    """
    if os.path.isfile(db):
        conn = vault_index._connect(db)
        try:
            hit = conn.execute(
                f"SELECT 1 FROM access_log WHERE note_path = ? AND timestamp > ? "
                f"AND context_type IN ({','.join('?' * len(STICKY_CONTEXTS))}) LIMIT 1",
                (abs_path, created, *STICKY_CONTEXTS)).fetchone()
        finally:
            conn.close()
        if hit:
            return "access"
    return "hand-update" if hand_updated(Path(abs_path)) else None


def _index_rows(db: str, paths: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not paths or not os.path.isfile(db):
        return out
    conn = vault_index._connect(db)
    try:
        for i in range(0, len(paths), 900):
            chunk = paths[i:i + 900]
            for row in conn.execute(
                    f"SELECT path, mtime, project, date, tfidf_vector FROM notes "
                    f"WHERE path IN ({','.join('?' * len(chunk))})", chunk):
                out[row["path"]] = dict(row)
    finally:
        conn.close()
    return out


def _fresh_index_rows(db: str, paths: list[str]) -> dict[str, dict]:
    """Index rows for ``paths``, re-indexing first any note whose row is missing or stale."""
    rows = _index_rows(db, paths)
    stale = []
    for p in paths:
        try:
            mtime = os.stat(p).st_mtime
        except OSError:
            continue
        if p not in rows or not vault_index.mtime_unchanged(mtime, rows[p]["mtime"]):
            stale.append(p)
    for p in stale:
        vault_index.index_note(db, p)
    return {**rows, **_index_rows(db, stale)} if stale else rows


def _vector(raw) -> dict | None:
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _others(db: str, project: str | None, session_id: str) -> list[tuple[str, dict]]:
    """``(date, vector)`` of the project's curated notes from other sessions."""
    if not project or not os.path.isfile(db):
        return []
    conn = vault_index._connect(db)
    try:
        rows = conn.execute(
            f"SELECT date, tfidf_vector FROM notes WHERE project = ? AND type IN ({','.join('?' * len(_CURATED))}) "
            f"AND tfidf_vector IS NOT NULL AND (source_session IS NULL OR source_session != ?)",
            (project, *_CURATED, session_id)).fetchall()
    finally:
        conn.close()
    return [(str(r["date"] or ""), v) for r in rows if (v := _vector(r["tfidf_vector"]))]


def novelty_of(vector: dict | None, date: str | None, others: list[tuple[str, dict]]) -> float | None:
    """1 - max cosine to the other sessions' notes dated on or before ``date``; None without a vector."""
    if not vector:
        return None
    day = str(date or "")
    best = max((_cosine_similarity(vector, v) for d, v in others if not day or d <= day), default=0.0)
    return round(1 - best, 3)


def _classify(row: dict, fm: dict) -> str:
    """``candidate``, ``sticky``, ``excluded`` or ``foreign`` (never touched)."""
    status = fm.get("status")
    if status not in (PROVISIONAL, promoted_status(row.get("type"))):
        return "foreign"
    if fm.get("demoted_by") == ISSUE_CLOSED:
        return "excluded"
    if fm.get("promoted_by") in ("access", "hand-update"):
        return "sticky"
    if row.get("duplicate_of") or row.get("superseded_by"):
        return "excluded"
    return "candidate"


def _log(project: str, sid: str, outcome: str, cid: str, m, rank, why: str) -> None:
    capture_state.append_capture_log(project, sid, outcome, f"cid={cid};merit={m};rank={rank};why={why}")


def plan_session(session_id: str, config: dict, db: str, *, rows: list[dict] | None = None) -> list[dict]:
    """The selection outcome for each of the session's new notes, without writing anything.

    Each entry: ``{row, path, fm, kind, merit, rank, want, why, fields}``;
    ``fields`` is what the note's frontmatter should hold (``want`` is its
    status, or None to leave the note alone). Ledger novelty is cached as a
    side effect, since it is computed only once per note.
    """
    vault = Path(config["vault_path"])
    if rows is None:
        rows = capture_state.ledger_list(session=session_id, limit=10**9)
    rows = [r for r in rows if r.get("kind") == "new" and r.get("status") == "active"
            and isinstance(r.get("note_path"), str) and not capture_save._is_rejected(r["note_path"])]
    entries = []
    for row in sorted(rows, key=lambda r: (r.get("ts") or 0.0, r.get("id") or "")):
        path = vault / row["note_path"]
        if not path.resolve().is_relative_to(vault.resolve()) or not path.is_file():
            continue  # deleted, moved or outside the vault
        fm = obsidian_utils._peek_frontmatter_fields(path, _FM_FIELDS)
        entries.append({"row": row, "path": path, "fm": fm, "kind": _classify(row, fm),
                        "merit": None, "rank": None, "want": None, "why": None})
    live = [e for e in entries if e["kind"] != "foreign"]
    index = _fresh_index_rows(db, [str(e["path"]) for e in live])
    others = None
    for e in live:
        row, irow = e["row"], index.get(str(e["path"])) or {}
        e["vector"] = _vector(irow.get("tfidf_vector"))
        nov = row.get("novelty")
        if not isinstance(nov, (int, float)) or isinstance(nov, bool):
            if others is None:
                others = _others(db, irow.get("project"), session_id)
            nov = novelty_of(e["vector"], irow.get("date"), others)
            if nov is not None:  # cached only when computed from a vector
                capture_state.ledger_update(row["id"], novelty=nov)
                row["novelty"] = nov
        e["novelty"] = _novelty(nov)
        e["merit"] = merit(row.get("score"), nov, row.get("type"))
        if e["kind"] == "candidate":
            created = _epoch(e["fm"].get("created_at"), float(row.get("ts") or 0.0))
            e["sticky_by"] = is_sticky(str(e["path"]), created, db)
            if e["sticky_by"]:
                e["kind"] = "sticky"
    cands = [e for e in live if e["kind"] == "candidate"]
    picks = select([{"id": e["row"]["id"], "merit": e["merit"], "vector": e["vector"],
                     "ts": e["row"].get("ts")} for e in cands])
    pick_rank = {cid: i + 1 for i, cid in enumerate(picks)}
    rest = sorted((e for e in cands if e["row"]["id"] not in pick_rank),
                  key=lambda e: (-e["merit"], e["row"].get("ts") or 0.0, e["row"]["id"]))
    for i, e in enumerate(rest):
        pick_rank.setdefault(e["row"]["id"], len(picks) + i + 1)
    for e in live:
        cid, top = e["row"]["id"], promoted_status(e["row"].get("type"))
        was = e["fm"].get("status")
        fields = {"capture_score": _score(e["row"].get("score")), "capture_novelty": e["novelty"],
                  "capture_merit": e["merit"]}
        if e["kind"] == "sticky":
            how = e["fm"].get("promoted_by") or e["sticky_by"]
            e["want"], e["why"] = top, f"sticky-{how}"
            fields.update(status=top, promoted_by=how, demoted_by=None)
        elif e["kind"] == "excluded":
            e["want"] = PROVISIONAL
            e["why"] = (ISSUE_CLOSED if e["fm"].get("demoted_by") == ISSUE_CLOSED
                        else "duplicate" if e["row"].get("duplicate_of") else "superseded")
            fields.update(status=PROVISIONAL)
        else:
            e["rank"] = pick_rank[cid]
            picked = cid in picks
            e["want"] = top if picked else PROVISIONAL
            e["why"] = "pick" if picked else ("displaced" if was == top else "below-cap")
            fields.update(status=e["want"], capture_rank=e["rank"])
            if picked:
                fields["demoted_by"] = None  # a backlog demotion is only a recoverable flag
        e["fields"] = fields
    return live


def _apply(e: dict, config: dict, db: str, project: str, sid: str) -> str | None:
    """Write one entry when its status changes or its parts were never recorded; returns the log outcome."""
    was, want, fm = e["fm"].get("status"), e["want"], e["fm"]
    if want is None:
        return None
    unrecorded = not fm.get("capture_merit")  # first selection: record the parts once
    newly_sticky = e["kind"] == "sticky" and not fm.get("promoted_by")
    if was == want and not (unrecorded or newly_sticky):
        return None  # a rank change alone does not rewrite the file (Ruling 7)
    pre = vault_index.file_stat_key(str(e["path"]))
    note_writer.set_frontmatter_fields(config["vault_path"], str(e["path"]), e["fields"])
    vault_index.update_note_status(db, str(e["path"]), pre)
    if was == want:
        return None
    outcome = "promoted" if want != PROVISIONAL else "demoted"
    _log(project, sid, outcome, e["row"]["id"], e["merit"], e["rank"], e["why"])
    return outcome


def run_selection(session_id: str, project: str, config: dict, *, db: str | None = None) -> dict:
    """Re-select the session's notes and write the changes; returns ``{promoted, demoted, picked}``.

    Demotions are written before promotions. If any demotion write fails,
    this run makes no promotion at all (it logs ``select-failed`` with
    ``detail=demotion-write`` and adds ``deferred: True`` to the counts), so a
    failure never leaves more than ``CAP`` picks promoted; the next slice
    retries. Any other note that cannot be written (locked, changed under us)
    keeps its status and is retried next slice.
    """
    db = db or vault_index.ensure_index(config["vault_path"], indexed_folders(config))
    entries = plan_session(session_id, config, db)
    counts = {"promoted": 0, "demoted": 0, "picked": sum(e["why"] == "pick" for e in entries)}
    order = sorted(entries, key=lambda e: e["want"] != PROVISIONAL)  # demotions first
    demotion_failed = None
    for e in order:
        is_demotion = e["want"] == PROVISIONAL and e["fm"].get("status") != PROVISIONAL
        if demotion_failed and e["want"] != PROVISIONAL:
            break  # only promotions remain: leave them for the next slice
        try:
            outcome = _apply(e, config, db, project, session_id)
        except (OSError, ValueError) as exc:
            capture_state.append_capture_log(project, session_id, "select-write-failed",
                                             f"cid={e['row']['id']};{type(exc).__name__}: {exc}"[:160])
            if is_demotion and demotion_failed is None:
                demotion_failed = e["row"]["id"]
            continue
        if outcome:
            counts[outcome] += 1
    if demotion_failed:
        capture_state.append_capture_log(project, session_id, "select-failed",
                                         f"detail=demotion-write;cid={demotion_failed}")
        counts["deferred"] = True
    return counts


def promote_used(config: dict, *, now: float | None = None, db: str | None = None) -> dict:
    """Promote for good every provisional note that was used after it was captured, in any project and session.

    Selection checks use per session, and only while the session is still
    being captured; a note used after its session ended would stay hidden.
    One join finds provisional notes with a ``search`` or ``tool-open``
    access row; the note's own ``created_at`` and status are then confirmed
    from its frontmatter. The note becomes ``promoted_status(type)`` with
    ``promoted_by: access``, outside the cap. Notes in ``_rejected``, outside
    the vault, or flagged ``issue-closed`` are skipped. A second run writes
    nothing. Returns ``{"checked", "promoted"}``.
    """
    now = time.time() if now is None else now
    db = db or vault_index._default_db_path()
    out = {"checked": 0, "promoted": 0}
    if not os.path.isfile(db):
        return out
    vault = Path(config["vault_path"])
    root = vault.resolve()
    conn = vault_index._connect(db)
    try:
        hits = conn.execute(
            f"SELECT n.path AS path, n.type AS type, n.project AS project, n.source_session AS sid, "
            f"MAX(a.timestamp) AS used FROM notes n JOIN access_log a ON a.note_path = n.path "
            f"WHERE n.status = ? AND a.timestamp <= ? AND a.context_type IN ({','.join('?' * len(STICKY_CONTEXTS))}) "
            f"GROUP BY n.path", (PROVISIONAL, now, *STICKY_CONTEXTS)).fetchall()
    finally:
        conn.close()
    if not hits:
        return out
    ledger = {r["note_path"]: r for r in capture_state.ledger_list(limit=10**9)
              if r.get("kind") == "new" and isinstance(r.get("note_path"), str)}
    for hit in hits:
        path = Path(hit["path"])
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            continue
        if not resolved.is_relative_to(root) or capture_save._is_rejected(str(resolved.relative_to(root))):
            continue
        if not path.is_file():
            continue
        out["checked"] += 1
        fm = obsidian_utils._peek_frontmatter_fields(path, _FM_FIELDS)
        if fm.get("status") != PROVISIONAL or fm.get("demoted_by") == ISSUE_CLOSED:
            continue
        row = ledger.get(str(resolved.relative_to(root))) or {}
        created = _epoch(fm.get("created_at"), float(row.get("ts") or 0.0))
        if not hit["used"] > created:
            continue
        top = "active" if hit["type"] == "claude-decision" else "promoted"
        pre = vault_index.file_stat_key(str(path))
        try:
            note_writer.set_frontmatter_fields(config["vault_path"], str(path),
                                               {"status": top, "promoted_by": "access", "demoted_by": None})
        except (OSError, ValueError) as exc:
            capture_state.append_capture_log(hit["project"] or "", hit["sid"], "select-write-failed",
                                             f"path={path.name};{type(exc).__name__}: {exc}"[:160])
            continue
        vault_index.update_note_status(db, str(path), pre)
        _log(hit["project"] or "", hit["sid"], "promoted", row.get("id") or path.name,
             fm.get("capture_merit"), None, "sticky-access")
        out["promoted"] += 1
    return out


def _transcript_epoch(row: dict, fallback: float) -> float:
    ts = row.get("timestamp")
    if isinstance(ts, str):
        try:
            dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo:
                return dt.timestamp()
        except ValueError:
            pass
    return fallback


def _read_paths(rows: list[dict]):
    """``(row, file_path)`` for every ``Read`` tool call in transcript ``rows``."""
    for row in rows:
        msg = row.get("message") if isinstance(row, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if (isinstance(item, dict) and item.get("type") == "tool_use" and item.get("name") == "Read"
                    and isinstance(item.get("input"), dict) and isinstance(item["input"].get("file_path"), str)):
                yield row, item["input"]["file_path"]


def log_tool_opens(rows: list[dict], config: dict, session_id: str, db: str, *, now: float | None = None) -> int:
    """Log a ``tool-open`` access for each ``Read`` of a curated vault note in ``rows`` (Ruling 4).

    The row's own timestamp is used, so a retried slice logs nothing twice.
    Reads of notes this session auto-captured are skipped. Returns rows added.
    """
    if not os.path.isfile(db):
        return 0
    vault = Path(config["vault_path"])
    root = vault.resolve()
    own = {str((vault / p).resolve()) for p in capture_state.notes_created_by(session_id)}
    now = time.time() if now is None else now
    hits = []
    for row, raw in _read_paths(rows):
        try:
            resolved = Path(raw).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if not resolved.is_relative_to(root) or str(resolved) in own:
            continue
        hits.append((str(vault / resolved.relative_to(root)), _transcript_epoch(row, now)))
    if not hits:
        return 0
    conn = vault_index._connect(db)
    added = 0
    try:
        for path, ts in hits:
            note = conn.execute("SELECT project FROM notes WHERE path = ? AND type IN (?, ?, ?)",
                                (path, *_CURATED)).fetchone()
            if note is None or conn.execute(
                    "SELECT 1 FROM access_log WHERE note_path = ? AND context_type = ? AND timestamp = ?",
                    (path, TOOL_OPEN, ts)).fetchone():
                continue
            conn.execute("INSERT INTO access_log (note_path, timestamp, context_type, project) VALUES (?, ?, ?, ?)",
                         (path, ts, TOOL_OPEN, note["project"]))
            added += 1
        conn.commit()
    finally:
        conn.close()
    return added


def log_slice_opens(transcript_path: Path, start: int, end: int, config: dict, session_id: str,
                    db: str | None = None) -> int:
    """``log_tool_opens`` over the transcript bytes ``[start, end)`` of one slice."""
    if end <= start:
        return 0
    rows, _ = capture_slice.read_slice(Path(transcript_path), start, max_bytes=end - start)
    db = db or vault_index._default_db_path()
    return log_tool_opens(rows, config, session_id, db)


def _issue_state_path(project: str) -> Path:
    return capture_state.capture_dir() / f"issue-check-{capture_save._slug(project, 80)}.json"


def _gh_issue(repo: str, number: int, run) -> dict | None:
    try:
        result = run(["gh", "issue", "view", str(number), "--json", "state,closedAt"], cwd=repo,
                     capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    try:
        data = json.loads(result.stdout or "")
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _closed_after(issue: dict | None, created: float) -> bool:
    if not issue or issue.get("state") != "CLOSED" or not isinstance(issue.get("closedAt"), str):
        return False
    try:
        closed = datetime.datetime.fromisoformat(issue["closedAt"].replace("Z", "+00:00"))
    except ValueError:
        return False
    return closed.tzinfo is not None and closed.timestamp() > created


def demote_closed(project: str, config: dict, *, now: float | None = None, run=None, db: str | None = None) -> dict:
    """Demote promoted, non-sticky notes of ``project`` whose ``about_issue`` closed after capture (Ruling 8).

    Checks notes from the last 60 days, at most once per 6 h per project.
    Any ``gh`` failure (missing, timeout, bad JSON, not a repo) changes nothing.
    """
    now = time.time() if now is None else now
    run = run or subprocess.run
    state = _issue_state_path(project)
    try:
        last = float(json.loads(state.read_text(encoding="utf-8")).get("checked_at", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        last = 0.0
    if 0 <= now - last < ISSUE_CHECK_EVERY_SECONDS:
        return {"skipped": "rate-limited", "demoted": 0}
    capture_state._atomic_write(state, json.dumps({"checked_at": now}))
    vault = Path(config["vault_path"])
    root = vault.resolve()
    todo = []
    for row in capture_state.ledger_list(limit=10**9, since=now - ISSUE_WINDOW_SECONDS):
        issue, repo = row.get("about_issue"), row.get("repo")
        if (row.get("project") != project or row.get("kind") != "new" or row.get("status") != "active"
                or not isinstance(issue, int) or isinstance(issue, bool) or not isinstance(repo, str)
                or not isinstance(row.get("note_path"), str)):
            continue
        path = vault / row["note_path"]
        if not path.resolve().is_relative_to(root) or not path.is_file():
            continue  # outside the vault (a ledger path with .. or a symlink) or gone
        fm = obsidian_utils._peek_frontmatter_fields(path, _FM_FIELDS)
        if (fm.get("status") != promoted_status(row.get("type")) or fm.get("promoted_by")
                or fm.get("demoted_by")):
            continue
        todo.append((row, path, fm))
    if not todo:
        return {"checked": 0, "demoted": 0}
    db = db or vault_index._default_db_path()
    issues: dict[tuple[str, int], dict | None] = {}
    demoted = 0
    for row, path, fm in todo:
        key = (row["repo"], row["about_issue"])
        if key not in issues:
            issues[key] = _gh_issue(*key, run)
        created = _epoch(fm.get("created_at"), float(row.get("ts") or now))
        if not _closed_after(issues[key], created):
            continue
        if is_sticky(str(path), created, db):
            continue  # used since the last selection: kept
        pre = vault_index.file_stat_key(str(path))
        try:
            note_writer.set_frontmatter_fields(config["vault_path"], str(path),
                                               {"status": PROVISIONAL, "demoted_by": ISSUE_CLOSED})
        except (OSError, ValueError) as exc:
            capture_state.append_capture_log(project, row.get("session_id"), "select-write-failed",
                                             f"cid={row['id']};{type(exc).__name__}: {exc}"[:160])
            continue
        vault_index.update_note_status(db, str(path), pre)
        _log(project, row.get("session_id"), "demoted", row["id"], fm.get("capture_merit"), None,
             f"{ISSUE_CLOSED}:{row['about_issue']}")
        demoted += 1
    return {"checked": len(issues), "demoted": demoted}
