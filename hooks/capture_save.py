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


def summary_of(body: str) -> str:
    """The pane's preview: the body's text on one line, headings left out."""
    text = " ".join(line for line in body.splitlines() if not _HEADING_RE.match(line.lstrip()))
    return " ".join(text.split())[:SUMMARY_CHARS]


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


def section_end(lines: list[str], start: int, marker: str) -> int:
    """Index just past the section that opens at ``start``.

    Through the capture's own end marker when it has one; otherwise (an
    update written before the marker existed) up to the next ``## `` heading
    outside a fenced code block.
    """
    if marker:
        for i in range(start + 1, len(lines)):
            if lines[i].strip() == marker:
                return i + 1
    fence = None
    for i in range(start + 1, len(lines)):
        fence, is_fence = fence_step(fence, lines[i])
        if not is_fence and fence is None and lines[i].startswith("## "):
            return i
    return len(lines)


AUTO_HEADING_RE = re.compile(r"^## Update \([^)]*\) — auto-capture (?P<id>[0-9a-f]{12})\Z")


def strip_auto_updates(text: str) -> str:
    """``text`` without its auto-capture ``## Update`` sections; hand-written ones stay."""
    lines = text.replace("\r\n", "\n").split("\n")
    out, i, fence = [], 0, None
    while i < len(lines):
        fence, is_fence = fence_step(fence, lines[i])
        m = None if is_fence or fence is not None else AUTO_HEADING_RE.match(lines[i])
        if m:  # a heading quoted inside a code fence is text, not a section
            i = section_end(lines, i, end_marker(m.group("id")))
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _strip_frontmatter(text: str) -> str:
    text = text.lstrip("\ufeff")  # a BOM must not hide the frontmatter from the strip
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        if end != -1:
            return text[end + 5:]
    return text


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


UPDATE_MIN_COSINE = 0.35  # #427: >= 0.35 against the original text refined the same fact in a live replay


def update_min_cosine(config: dict) -> float:
    """``auto_capture_update_min_cosine`` when it is a number in [0, 1], else the default."""
    value = config.get("auto_capture_update_min_cosine", UPDATE_MIN_COSINE)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        return UPDATE_MIN_COSINE  # NaN fails the range check too
    return value


def _vault_rel(path: str, root: Path) -> str | None:
    """``path`` (absolute, as the index stores it, or vault-relative, as the ledger does) relative to ``root``."""
    p = Path(path)
    resolved = (p if p.is_absolute() else root / p).resolve()
    if not resolved.is_relative_to(root):
        return None
    return str(resolved.relative_to(root))


def original_text(note: Path) -> str:
    """The note as written before auto-capture grew it: no frontmatter, no auto-capture updates."""
    text = note.read_text(encoding="utf-8").replace("\r\n", "\n")
    return strip_auto_updates(_strip_frontmatter(text))


def written_by(note: Path, session_id: str | None) -> bool:
    """True when ``note``'s frontmatter ``source_session`` is ``session_id`` (a note the session wrote by hand).

    Read from the file, the source of truth; an unreadable note or one with
    no frontmatter ``source_session`` is not the session's.
    """
    return bool(session_id) and obsidian_utils._peek_frontmatter_field(note, "source_session") == session_id


def query_text(title: str, body: str) -> str:
    """What a candidate (or an update section) is scored by."""
    return f"{title}\n{body[:300]}"


def original_vector(db: str, note: Path) -> dict:
    """TF-IDF vector of ``note``'s title and original text; the one scorer find_match and split-updates share.

    The title is the index's (frontmatter ``title`` or first H1). Raises
    OSError/UnicodeDecodeError when the note cannot be read.
    """
    text = original_text(note)
    title = (vault_index._parse_note(str(note)) or {}).get("title") or ""
    return vault_index.compute_query_vector(db, f"{title}\n{text}")


def find_match(query: str, config: dict, session_id: str | None = None) -> str | None:
    """Vault-relative path of a curated note that ``query`` clearly duplicates.

    #427: a note this session created (auto-captured, or by hand per its
    frontmatter ``source_session``) is never a match, and the top note
    is scored by its original text, so updates cannot snowball onto a hub.
    Anything that cannot be checked fails toward a new note.
    """
    vault = config["vault_path"]
    root = Path(vault).resolve()
    folders = indexed_folders(config)
    db = vault_index.ensure_index(vault, folders)
    query_vec = vault_index.compute_query_vector(db, query)
    if not query_vec:
        return None  # nothing to score the cosine gate with
    own = {_vault_rel(p, root) for p in capture_state.notes_created_by(session_id)} if session_id else set()
    results: list[dict] = []
    for note_type in (*_CURATED_NOTE_TYPES, "claude-session"):
        results += vault_index.search_vault(db, query, note_type=note_type, limit=3, include_vectors=True,
                                          log_access=False)
    results = [r for r in results if not _is_rejected(r.get("path", ""))]
    for r in results:
        r["rel_path"] = _vault_rel(r.get("path", ""), root)
    results = [r for r in results if r["rel_path"] is None
               or (r["rel_path"] not in own and not written_by(root / r["rel_path"], session_id))]
    results.sort(key=lambda r: r["rank"])
    if not results or results[0].get("type") not in _CURATED_NOTE_TYPES:
        return None  # updates only ever go into curated notes, never session notes
    top = results[0]
    if top["rel_path"] is None:
        return None  # outside the vault
    try:
        note_vec = original_vector(db, root / top["rel_path"])
    except (OSError, UnicodeDecodeError):
        return None
    if not note_vec:
        return None
    top["tfidf_vector"] = note_vec
    t = update_min_cosine(config)
    if not compress_guard.is_high_confidence_match(
            results, query_vec=query_vec, min_cosine=t,
            min_cosine_rescue=max(compress_guard.MIN_COSINE_RESCUE, t)):
        return None
    return top["rel_path"]


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


PROVISIONAL = "provisional"  # #455: every new auto note starts out of the default retrieval pool


def _frontmatter(c: Candidate, *, session_id: str, project: str, session_note: str, today: str,
                 created_at: str | None = None, status: str | None = PROVISIONAL) -> str:
    """The new note's frontmatter. ``status`` replaces the template's (decisions say
    ``active``) or is added after ``source:``; None keeps the template as it is."""
    template_name, _ = TYPES[c["type"]]
    template = (_TEMPLATES / template_name).read_text(encoding="utf-8")
    front = template.split("---", 2)[1]
    project_tag = _slug(project, 80)
    values = {
        "date": today,
        "created_at": created_at or datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
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
    if status is not None:
        front, n = re.subn(r"^status: .*\n", f"status: {status}\n", front, count=1, flags=re.M)
        if not n:
            front = front.replace("source: auto-capture\n", f"source: auto-capture\nstatus: {status}\n", 1)
    front = front.rstrip("\n") + "\n  - claude/auto\n"
    return f"---{front}---\n"


def new_filename(vault: str, folder: str, day: str, title: str) -> str:
    """``<day>-<slug>-<hash4>.md`` that does not exist yet in ``folder``."""
    for _ in range(2):
        filename = f"{day}-{_slug(title)}-{_hash4()}.md"
        if not (Path(vault) / folder / filename).exists():
            return filename
    raise RuntimeError("could not find a free filename")


def save_candidate(
    c: Candidate,
    *,
    session_id: str,
    project: str,
    session_note: str | None = None,
    today: str | None = None,
    config: dict | None = None,
    repo: str | None = None,
) -> dict:
    """Write ``c`` as a new note or an update; append and return its ledger row.

    #455: the row also keeps the worker's ``score`` and ``about_issue`` and
    the session's ``repo`` (its cwd), which selection and the closed-issue
    check read later. A new note is written ``status: provisional``.
    """
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
           "title": title, "status": "active", "summary": summary_of(body),
           "score": c.get("score"), "about_issue": c.get("about_issue"), "repo": repo}

    try:
        match = find_match(query_text(title, body), config, session_id=session_id)
    except Exception as exc:  # noqa: BLE001 -- a dedup that cannot run fails toward a new note, never a lost one
        capture_state.append_capture_log(project, session_id, "match-failed", f"{type(exc).__name__}: {exc}"[:160])
        match = None
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
        filename = new_filename(vault, folder, today, title)
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            rc = note_writer.run_write(vault, folder, filename, content)
        if rc != 0:
            raise _writer_failed("write", sink.getvalue().strip())
        row.update(kind="new", note_path=f"{folder}/{filename}")
    capture_state.ledger_append(row)
    return row
