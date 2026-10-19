"""capture_save.py -- turn one auto-capture candidate into a vault note (#425).

A candidate is what the capture worker returns: ``{type, title, body, topic}``.
``save_candidate`` runs the same dedup ``/compress`` Step 3.5 uses. A
high-confidence match on a curated note gets an ``## Update`` section; anything
else becomes a new note built from ``templates/<type>.md``. Every saved note is
tagged ``claude/auto`` with ``source: auto-capture`` and gets one ledger row.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import io
import re
import secrets
import time
from pathlib import Path
from typing import TypedDict

import capture_state
import compress_guard
import note_writer
import obsidian_utils
import vault_index
from obsidian_utils import indexed_folders

TYPES = {
    "insight": ("insight.md", "claude-insight"),
    "decision": ("decision.md", "claude-decision"),
    "error-fix": ("error-fix.md", "claude-error-fix"),
}
_CURATED_NOTE_TYPES = ("claude-insight", "claude-decision", "claude-error-fix")
_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
_REJECTED = "_rejected"


class TransientSaveError(OSError):
    """A write that may work on retry: the note was locked, changed under us, or the write failed."""


_TRANSIENT_MARKERS = ("another process is updating", "changed on disk", "write failed", "cannot create lock")


def _writer_failed(what: str, output: str) -> Exception:
    if any(m in output for m in _TRANSIENT_MARKERS):
        return TransientSaveError(f"{what} failed: {output}")
    return RuntimeError(f"{what} failed: {output}")


class Candidate(TypedDict):
    type: str
    title: str
    body: str
    topic: str


def _slug(text: str, limit: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].strip("-") or "note"


def _hash4() -> str:
    return hashlib.md5(f"{secrets.token_hex(8)}{time.time()}".encode()).hexdigest()[:4]


def _yaml_str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


SUMMARY_CHARS = 200  # the pane's one-line preview of the body
_HEADING_RE = re.compile(r"^(#{1,6})(?=\s)")


def fence_step(open_fence: tuple[str, int] | None, line: str) -> tuple[tuple[str, int] | None, bool]:
    """CommonMark fence state after ``line``: ``(open fence, line is a fence line)``.

    A fence opens on 3+ backticks or tildes and closes only on the same
    character, at least as long, with nothing after it (note_writer's rule),
    so a literal ```` ``` ```` inside a ```````` ```` ```````` block stays code.
    """
    m = note_writer._FENCE_RE.match(line.rstrip("\r\n"))
    if not m:
        return open_fence, False
    marker = m.group("fence")
    if open_fence is None:
        return (marker[0], len(marker)), True
    if marker[0] == open_fence[0] and len(marker) >= open_fence[1] and not m.group("info").strip():
        return None, True
    return open_fence, True


def end_marker(capture_id: str) -> str:
    """The line that closes an update section; Reject removes through it."""
    return f"<!-- /auto-capture {capture_id} -->"


def _demote_headings(body: str) -> str:
    """Body headings sit under the ``## Update`` heading: at least ``###``, one level down.

    Keeps the note's outline nested; ``end_marker`` is what bounds the
    section for Reject. Lines inside fenced code blocks (a shell
    ``# comment``) are left alone.
    """
    out, fence = [], None
    for line in body.split("\n"):
        fence, is_fence = fence_step(fence, line)
        if not is_fence and fence is None:
            line = _HEADING_RE.sub(lambda m: "#" * min(6, max(3, len(m.group(1)) + 1)), line)
        out.append(line)
    return "\n".join(out)


def _is_rejected(rel_path: str) -> bool:
    return _REJECTED in Path(rel_path).parts


def find_match(query: str, config: dict) -> str | None:
    """Vault-relative path of a curated note that ``query`` clearly duplicates."""
    vault = config["vault_path"]
    folders = indexed_folders(config)
    db = vault_index.ensure_index(vault, folders)
    query_vec = vault_index.compute_query_vector(db, query)
    results: list[dict] = []
    for note_type in (*_CURATED_NOTE_TYPES, "claude-session"):
        results += vault_index.search_vault(db, query, note_type=note_type, limit=3, include_vectors=True)
    results = [r for r in results if not _is_rejected(r.get("path", ""))]
    results.sort(key=lambda r: r["rank"])
    if not results or results[0].get("type") not in _CURATED_NOTE_TYPES:
        return None  # updates only ever go into curated notes, never session notes
    if not compress_guard.is_high_confidence_match(results, query_vec=query_vec or None):
        return None
    path = Path(results[0]["path"])
    if path.is_absolute():  # the index stores absolute paths; the ledger keeps vault-relative ones
        root = Path(vault).resolve()
        if not path.resolve().is_relative_to(root):
            return None
        return str(path.resolve().relative_to(root))
    return str(path)


def session_note_for(session_id: str, project: str, config: dict) -> str:
    """The ``source_session_note`` wikilink stem for ``session_id``, or ''."""
    vault, folder = config["vault_path"], config.get("sessions_folder", "claude-sessions")
    h = hashlib.sha256(session_id.encode()).hexdigest()[:4]
    name, _collisions = obsidian_utils._resolve_session_note_by_hash(Path(vault) / folder, h, cwd=None)
    if not name:
        name = obsidian_utils.make_filename(
            obsidian_utils._first_seen_date(session_id), obsidian_utils.slugify(project), session_id
        )[:-3]
    return obsidian_utils.resolve_source_session_note(name, session_id, vault, folder)


def _frontmatter(c: Candidate, *, session_id: str, project: str, session_note: str, today: str) -> str:
    template_name, _ = TYPES[c["type"]]
    template = (_TEMPLATES / template_name).read_text(encoding="utf-8")
    front = template.split("---", 2)[1]
    project_tag = _slug(project, 80)
    values = {
        "date": today,
        "created_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_session": session_id,
        "source_session_note": f"[[{session_note}]]" if session_note else "",
        "project": project_tag,
        "topic": _slug(str(c.get("topic") or "general"), 40),
    }
    for key, value in values.items():
        front = front.replace("{{" + key + "}}", value)
    if not session_note:
        front = re.sub(r'^source_session_note: ""\n', "", front, flags=re.M)
    front = front.replace(f"project: {project_tag}\n", f"project: {project_tag}\nsource: auto-capture\n", 1)
    front = front.rstrip("\n") + "\n  - claude/auto\n"
    return f"---{front}---\n"


def save_candidate(
    c: Candidate,
    *,
    session_id: str,
    project: str,
    session_note: str | None = None,
    today: str | None = None,
    config: dict | None = None,
) -> dict:
    """Write ``c`` as a new note or an update; append and return its ledger row."""
    if c.get("type") not in TYPES:
        raise ValueError(f"unknown candidate type: {c.get('type')!r}")
    config = config or obsidian_utils.load_config()
    vault, folder = config["vault_path"], config.get("insights_folder", "claude-insights")
    today = today or datetime.date.today().isoformat()
    scrub = obsidian_utils.scrub_secrets
    title = scrub(" ".join(str(c.get("title") or "").split()))[:120] or "Untitled capture"
    body = scrub(str(c.get("body") or "").strip())
    if not body:
        raise ValueError("candidate has an empty body")
    cid = secrets.token_hex(6)
    row = {"id": cid, "session_id": session_id, "project": project, "type": c["type"],
           "title": title, "status": "active", "summary": " ".join(body.split())[:SUMMARY_CHARS]}

    match = find_match(f"{title}\n{body[:300]}", config)
    sink = io.StringIO()
    if match:
        heading = f"## Update ({today}) — auto-capture {cid}"
        abs_path = str(Path(vault) / match)
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            rc = note_writer.run_append_update(vault, abs_path, f"{heading}\n\n**{title}**\n\n{_demote_headings(body)}\n\n{end_marker(cid)}\n",
                                               last_updated=today, add_tags_csv="claude/auto")
        if rc != 0:
            raise _writer_failed("append-update", sink.getvalue().strip())
        row.update(kind="updated", note_path=match, update_heading=heading)
    else:
        if session_note is None:
            session_note = session_note_for(session_id, project, config)
        content = _frontmatter(c, session_id=session_id, project=project,
                               session_note=session_note, today=today) + f"\n# {title}\n\n{body}\n"
        for _ in range(2):
            filename = f"{today}-{_slug(title)}-{_hash4()}.md"
            if not (Path(vault) / folder / filename).exists():
                break
        else:
            raise RuntimeError("could not find a free filename")
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            rc = note_writer.run_write(vault, folder, filename, content)
        if rc != 0:
            raise _writer_failed("write", sink.getvalue().strip())
        row.update(kind="new", note_path=f"{folder}/{filename}")
    capture_state.ledger_append(row)
    return row
