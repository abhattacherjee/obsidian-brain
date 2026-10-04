"""LLM wiki for /vault-ask answers (#383, #395).

Library + JSON CLI (``python3 hooks/wiki.py rule|lookup|stale|count|file``).
Every vault write goes through ``write_vault_note`` (atomic, 0o600,
contained under the vault) while holding one wiki lock. The one exception:
``rebuild_wiki_index`` deletes stale ``index-*.md`` files directly (only
those typed ``claude-wiki-index``). Design:
``docs/plans/383-llm-wiki-design.md``.

CLI contract: subcommands that take input read one JSON object on stdin
(capped at ``STDIN_CAP_CHARS``) and print one JSON object on stdout. Exit 0
is success; ``file`` also exits 0 when the page was saved but the index or
log update failed, and then adds a ``WARNING: <warning>`` line on stderr.
Exit 1 is a refusal with an ``ERROR: <reason>`` line on stderr; exit 2 is a
usage error (no or unknown subcommand) or a crash.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STDIN_CAP_CHARS = 1_000_000
THRESHOLD = 3
INDEX_SPLIT = 500
QUESTION_MAX = 500
TOPICS_MAX = 8
COUNTING_TYPES = frozenset({
    "claude-insight", "claude-error-fix", "claude-decision",
    "claude-retro", "claude-session", "claude-memory",
})
CALLER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
CONFIDENCE = ("high", "medium", "low")
INDEX_TYPE = "claude-wiki-index"
PAGE_TYPE = "claude-wiki"

WRITING_RULE = """Write this wiki page "80% of the way to ASD-STE100" (Simplified Technical English):
- Short sentences: at most 20 words in procedures, 25 in descriptions.
- One topic per sentence. Use the active voice.
- One meaning per word; use the same term for the same thing throughout. No filler words.
- Keep technical terms, names, paths, flags, numbers and quoted text exact. Never swap a precise term for a simpler word if that loses information.
- Never reword citations or [[wikilinks]].
- A Mermaid diagram is allowed when the answer needs one; no other formats."""


class WikiRefusal(Exception):
    """A refused operation: the CLI prints ``ERROR: <msg>`` and exits 1."""


# ---------------------------------------------------------------------------
# Page format
# ---------------------------------------------------------------------------


def slugify(question: str) -> str:
    """Lowercase, non-alphanumeric runs to '-', at most 60 chars."""
    s = re.sub(r"[^a-z0-9]+", "-", str(question).lower()).strip("-")
    if len(s) > 60:
        cut = s.rfind("-", 0, 61)
        s = s[:cut] if cut > 0 else s[:60]
    return s.strip("-") or "untitled"


_FALSE = {"", "false", "no", "off", "0"}


def is_reviewed(value) -> bool:
    """True unless the value clearly means "not reviewed".

    Lenient on purpose: reading a hand-written flag as "not reviewed" would
    let a refresh overwrite the user's edits, so unknown values count as
    reviewed.
    """
    if value is None or value is False:
        return False
    if value is True:
        return True
    return str(value).strip().strip("\"'").strip().lower() not in _FALSE


def render_page(meta: dict, body: str) -> str:
    """Frontmatter (one JSON value per line, ``tags`` as a block list) + body."""
    lines = ["---"]
    for key, value in meta.items():
        if key == "tags":
            lines.append("tags:")
            lines += [f"  - {t}" for t in value]
        else:
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body.rstrip("\n") + "\n"


def _scalar(text: str):
    """One block item or map value: JSON when it parses, else the text with
    one pair of matching outer quotes removed. A number stays text: a
    fingerprint such as ``12e4567890123456`` must not turn into a float."""
    s = text.strip()
    try:
        value = json.loads(s)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value
    except ValueError:
        pass
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def read_page(path) -> tuple:
    """Return ``(meta, body)``. Values are JSON-decoded when they parse,
    else kept as the raw string (a hand-edited page).

    A key with an empty value followed by indented lines takes them as a
    block: ``- item`` lines make a list and ``k: v`` lines make a dict.
    Obsidian's Properties panel saves lists and maps in this form.
    """
    from frontmatter import split_frontmatter, split_lines_lf_crlf

    text = Path(path).read_text(encoding="utf-8")
    _open, fm_lines, _close, body_lines, err = split_frontmatter(split_lines_lf_crlf(text))
    if err:
        raise WikiRefusal(f"{path}: {err}")
    meta: dict = {}
    block_key = None  # the empty-valued key whose indented block we are reading
    for raw in fm_lines:
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not stripped:
            continue
        if block_key is not None and line[:1] in (" ", "\t"):
            current = meta[block_key]
            if stripped == "-" or stripped.startswith("- "):
                if current == "":
                    current = meta[block_key] = []
                if isinstance(current, list):
                    current.append(_scalar(stripped[1:]))
                continue
            k, sep, v = stripped.partition(":")
            if sep:
                if current == "":
                    current = meta[block_key] = {}
                if isinstance(current, dict):
                    current[str(_scalar(k))] = _scalar(v)
            continue
        block_key = None
        key, sep, val = stripped.partition(":")
        if not sep:
            continue
        key, val = key.strip(), val.strip()
        if not val:
            meta[key] = ""
            block_key = key
            continue
        try:
            meta[key] = json.loads(val)
        except ValueError:
            meta[key] = val
    return meta, "".join(body_lines)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _norm_name(name) -> str:
    """``x``, ``x.md``, ``[[x]]`` and ``[[x|alias]]`` all become ``x``."""
    n = str(name).strip()
    if n.startswith("[[") and n.endswith("]]"):
        n = n[2:-2].split("|", 1)[0].strip()
    return n[:-3] if n.endswith(".md") else n


def resolve_sources(db_path: str, names, roots) -> dict:
    """Map cited note names to indexed notes under ``roots``.

    A name matching no note is rejected as ``unresolved``; one matching
    several is rejected as ``ambiguous`` (never a silent first match). A
    snapshot's ``key`` is its parent session, so the two count once.
    """
    import vault_index

    root_paths = [Path(r) for r in roots]
    conn = vault_index._connect(db_path)
    try:
        resolved, rejected, seen = [], [], set()
        for raw in names:
            name = _norm_name(raw)
            if not name or name in seen:
                continue
            seen.add(name)
            suffix = "/" + name + ".md"
            rows = [
                r for r in conn.execute(
                    "SELECT path, type, project, source_note FROM notes "
                    "WHERE substr(path, -?) = ?",
                    (len(suffix), suffix),
                ).fetchall()
                if any(vault_index._is_under(Path(r["path"]), root) for root in root_paths)
            ]
            if not rows:
                rejected.append({"name": name, "reason": "unresolved"})
                continue
            if len(rows) > 1:
                rejected.append({
                    "name": name,
                    "reason": "ambiguous: " + ", ".join(sorted(r["path"] for r in rows)),
                })
                continue
            row = rows[0]
            is_snapshot = row["type"] == "claude-snapshot"
            resolved.append({
                "name": name,
                "path": row["path"],
                "type": row["type"],
                "project": row["project"] or "",
                "key": (row["source_note"] or name) if is_snapshot else name,
                "count_type": "claude-session" if is_snapshot else row["type"],
            })
        return {"resolved": resolved, "rejected": rejected}
    finally:
        conn.close()


def count_sources(db_path: str, names, memory_sources, roots) -> dict:
    """Count distinct qualifying sources (the filing threshold input).

    Notes are told apart by resolved file path, not by spelling: ``i1``,
    ``claude-insights/i1`` and ``<vault>/claude-insights/i1`` are one note.
    A snapshot is identified by its parent session's path. The lists keep
    the caller's spelling of the first occurrence. ``resolved`` drops later
    spellings of the same file but keeps a snapshot and its parent, so the
    page fingerprints both.
    """
    res = resolve_sources(db_path, names, roots)
    rejected = list(res["rejected"])
    for m in memory_sources or []:
        rejected.append({"name": str(m), "reason": "memory sources arrive in #396"})
    parents = [r["key"] for r in res["resolved"] if r["type"] == "claude-snapshot"]
    parent_path = ({p["name"]: p["path"] for p in resolve_sources(db_path, parents, roots)["resolved"]}
                   if parents else {})
    qualifying, other, resolved, paths, seen = [], [], [], set(), set()
    for r in res["resolved"]:
        if r["path"] in paths:
            continue  # another spelling of a note already listed
        paths.add(r["path"])
        resolved.append(r)
        is_snapshot = r["type"] == "claude-snapshot"
        ident = parent_path.get(r["key"], "key:" + r["key"]) if is_snapshot else r["path"]
        if ident in seen:
            continue
        seen.add(ident)
        if r["count_type"] in COUNTING_TYPES:
            qualifying.append(r["key"] if is_snapshot else r["name"])
        else:
            other.append(r["name"])
    return {"count": len(qualifying), "qualifying": qualifying, "other": other,
            "rejected": rejected, "resolved": resolved}


# ---------------------------------------------------------------------------
# Lookup and staleness
# ---------------------------------------------------------------------------


def fingerprint(path) -> str:
    """First 16 hex chars of the SHA-256 of the file's bytes (not its mtime)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def _unquote(value) -> str:
    """Undo the JSON escaping ``render_page`` gave a title.

    The indexer stores the raw frontmatter value with ``.strip('"')``, so
    ``"He said \\"hi\\""`` arrives as ``He said \\"hi\\`` (outer quotes
    gone, and a trailing escaped quote loses its ``"``). Re-wrap and decode;
    a value that does not decode (a hand-edited title) is kept as it is.
    """
    s = str(value or "")
    if "\\" not in s and not (len(s) >= 2 and s[0] == s[-1] == '"'):
        return s
    inner = s[1:-1] if len(s) >= 2 and s[0] == s[-1] == '"' else s
    for candidate in (inner, inner + '"'):
        try:
            decoded = json.loads('"' + candidate + '"')
        except ValueError:
            continue
        return " ".join(decoded.split())
    return s


def lookup(db_path: str, question: str, limit: int = 3) -> list:
    """Top wiki pages for ``question`` via ``vault_index.search_vault``
    (BM25 candidates, then reranked; it also logs an access for each hit).
    Each candidate carries ``path``, ``question`` (unescaped title),
    ``updated`` and the initial FTS ``rank``."""
    import vault_index

    if not str(question or "").strip():
        return []
    hits = vault_index.search_vault(db_path, question, note_type=PAGE_TYPE, limit=limit)
    return [{"path": h.get("path"), "question": _unquote(h.get("title")),
             "updated": h.get("date") or "", "rank": h.get("rank", h.get("score"))}
            for h in hits[:limit]]


def stale(db_path: str, page_path, roots) -> dict:
    """Why a page is stale: ``unverifiable:``/``changed:``/``missing:`` per
    source, ``newer:`` per newer counting note in the question's top 5 hits.
    All reasons are returned. An unreadable source counts as missing, and a
    cited source with no usable fingerprint (field missing, not a mapping, or
    no non-empty string for it) as unverifiable -- never as fresh. A page
    whose ``sources`` is not a non-empty list gets ``unverifiable: sources``."""
    meta, _body = read_page(page_path)
    raw_fps = meta.get("sources_fingerprint")
    fps = {}
    if isinstance(raw_fps, dict):
        fps = {_norm_name(k): v for k, v in raw_fps.items() if isinstance(v, str) and v}
    sources = meta.get("sources")
    reasons = []
    if not isinstance(sources, list) or not sources:
        # Absent, empty or unparseable: the page cannot be checked.
        reasons.append("unverifiable: sources")
        sources = []
    for name in dict.fromkeys(_norm_name(x) for x in sources):
        if name and name not in fps:
            reasons.append(f"unverifiable: {name}")
    res = resolve_sources(db_path, list(fps), roots)
    found = {r["name"]: r for r in res["resolved"]}
    for name, old in fps.items():
        r = found.get(name)
        if r is None:
            reasons.append(f"missing: {name}")
            continue
        try:
            now = fingerprint(r["path"])
        except OSError:
            reasons.append(f"missing: {r['name']}")
            continue
        if now != old:
            reasons.append(f"changed: {r['name']}")
    cited = {_norm_name(x) for x in sources} | set(fps)
    updated = str(meta.get("updated") or "")
    question = str(meta.get("question") or "")
    for base in _newer_notes(db_path, question, updated, cited, roots):
        reasons.append(f"newer: {base}")
    return {"stale": bool(reasons), "reasons": reasons}


# Question-frame verbs that _STOPWORDS keeps but nearly every "how does X
# work?" question contains. Matching on them alone flags unrelated notes.
_QUESTION_FILLER = frozenset({"work", "works", "working", "worked"})


def _newer_notes(db_path: str, question: str, updated: str, cited: set, roots) -> list:
    """Counting notes among the question's top 5 OR-matched hits that are
    dated after ``updated`` and not already cited. Stopwords are dropped
    from the question first; with no words left the check is skipped.

    Queries FTS directly, restricted to counting types: search_vault's AND
    query matches the page itself first and then never falls back to OR, so
    a newer note sharing only some of the words would be missed.
    """
    import vault_index

    words = [w for w in re.findall(r"[a-zA-Z0-9_/]+", (question or "").replace("-", " "))
             if len(w) > 1 and w.lower() not in vault_index._STOPWORDS
             and w.lower() not in _QUESTION_FILLER]
    if not words:
        return []  # nothing topical to match on: skip the newer check
    fts = vault_index._sanitize_fts_query_or(" ".join(words))
    marks = ",".join("?" * len(COUNTING_TYPES))
    root_paths = [Path(r) for r in roots]
    conn = vault_index._connect(db_path)
    try:
        rows = conn.execute(
            "SELECT n.path, n.date FROM notes_fts f JOIN notes n ON n.rowid = f.rowid "
            f"WHERE notes_fts MATCH ? AND n.type IN ({marks}) "
            "ORDER BY bm25(notes_fts, 10.0, 1.0, 5.0) LIMIT 5",
            (fts, *sorted(COUNTING_TYPES)),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        base = Path(r["path"]).stem
        if (str(r["date"] or "") > updated and base not in cited
                and any(vault_index._is_under(Path(r["path"]), root) for root in root_paths)):
            out.append(base)
    return out


# ---------------------------------------------------------------------------
# Filing: page, index, log
# ---------------------------------------------------------------------------


def _validate_payload(p: dict) -> dict:
    question = " ".join(str(p.get("question") or "").split())
    if not question or len(question) > QUESTION_MAX:
        raise WikiRefusal(f"question must be 1-{QUESTION_MAX} characters")
    body = p.get("body")
    if not isinstance(body, str) or not body.strip():
        raise WikiRefusal("body must be a non-empty string")
    sources = _list_field(p, "sources")
    memory = _list_field(p, "memory_sources")
    topics = _list_field(p, "topics")
    if len(topics) > TOPICS_MAX or not all(isinstance(t, str) and TOPIC_RE.fullmatch(t) for t in topics):
        raise WikiRefusal(f"each topic must match {TOPIC_RE.pattern} (at most {TOPICS_MAX})")
    if p.get("confidence") not in CONFIDENCE:
        raise WikiRefusal(f"confidence must be one of {', '.join(CONFIDENCE)}")
    filed_by = p.get("filed_by")
    if filed_by not in ("user", "auto"):
        raise WikiRefusal("filed_by must be 'user' or 'auto'")
    update = p.get("update")
    if update is not None and not isinstance(update, str):
        raise WikiRefusal("update must be a string path")
    caller = p.get("caller") or ""
    if filed_by == "auto":
        if not (isinstance(caller, str) and CALLER_RE.fullmatch(caller)):
            raise WikiRefusal(f"an auto filing needs a caller matching {CALLER_RE.pattern}")
    elif caller:
        raise WikiRefusal("caller is only allowed when filed_by is 'auto'")
    return {"question": question, "body": body, "sources": sources, "memory": memory,
            "topics": topics, "confidence": p["confidence"], "filed_by": filed_by,
            "caller": caller, "update": update or "",
            "override_reviewed": p.get("override_reviewed") is True}


def _wiki_root(ctx: dict) -> Path:
    """``<vault>/<wiki_folder>``; refused unless it resolves (symlinks
    followed) to a folder strictly inside the vault."""
    root = Path(ctx["vault"]) / ctx["wiki_folder"]
    vault = Path(ctx["vault"]).resolve()
    real = root.resolve()
    if real == vault or not real.is_relative_to(vault):
        raise WikiRefusal(f"wiki_folder {ctx['wiki_folder']!r} must resolve to a folder "
                          f"inside the vault {vault}")
    return root


def _write(ctx: dict, rel_folder: str, filename: str, content: str) -> None:
    """Write one wiki file. Refused unless the folder, symlinks followed,
    is inside the resolved wiki root (a symlinked ``queries/`` could
    otherwise put a page elsewhere in the vault)."""
    from obsidian_utils import write_vault_note

    final = Path(ctx["vault"]) / rel_folder / filename
    real = final.parent.resolve() / final.name
    if not real.is_relative_to(_wiki_root(ctx).resolve()):
        raise WikiRefusal(f"{real} is outside the wiki folder {_wiki_root(ctx).resolve()}; "
                          "refusing to write it")
    err = write_vault_note(ctx["vault"], rel_folder, filename, content)
    if err:
        raise WikiRefusal(err)


def _index_header(title: str) -> str:
    # Literal type (see file_page): the writer scan needs to see it.
    return '---\ntype: "claude-wiki-index"\n---\n# ' + title + '\n'


def rebuild_wiki_index(ctx: dict) -> list:
    """Rebuild ``index.md`` (and ``index-<project>.md`` above INDEX_SPLIT
    pages) from the notes table, never from page files."""
    import vault_index

    root = _wiki_root(ctx)
    queries = root / "queries"
    conn = vault_index._connect(ctx["db"])
    try:
        rows = [r for r in conn.execute(
            "SELECT path, title, date, tags FROM notes WHERE type = ? ORDER BY date DESC, path",
            (PAGE_TYPE,)).fetchall()
            if vault_index._is_under(Path(r["path"]), queries)]
    finally:
        conn.close()

    def line(r) -> str:
        tags = (r["tags"] or "").split(",")
        conf = next((t.rsplit("-", 1)[1] for t in tags if t.startswith("claude/wiki/confidence-")), "?")
        return f"- [[{Path(r['path']).stem}]] — {_unquote(r['title'])} (updated {r['date']}, {conf})"

    written, keep = [], set()
    if len(rows) <= INDEX_SPLIT:
        body = _index_header("Wiki index") + "\n" + "\n".join(line(r) for r in rows) + "\n"
        _write(ctx, ctx["wiki_folder"], "index.md", body)
        written.append(str(root / "index.md"))
    else:
        groups: dict = {}
        for r in rows:
            projects = [t[len("claude/project/"):] for t in (r["tags"] or "").split(",")
                        if t.startswith("claude/project/")] or ["unassigned"]
            for proj in projects:
                groups.setdefault(slugify(proj), []).append(r)
        top = [f"- [[index-{p}]] — {len(rs)} page(s)" for p, rs in sorted(groups.items())]
        _write(ctx, ctx["wiki_folder"], "index.md", _index_header("Wiki index") + "\n" + "\n".join(top) + "\n")
        written.append(str(root / "index.md"))
        for proj, rs in sorted(groups.items()):
            name = f"index-{proj}.md"
            keep.add(name)
            _write(ctx, ctx["wiki_folder"], name,
                   _index_header(f"Wiki index: {proj}") + "\n" + "\n".join(line(r) for r in rs) + "\n")
            written.append(str(root / name))
    for old in root.glob("index-*.md"):
        if old.name in keep:
            continue
        # Only delete our own index files: a user note may share the name.
        try:
            if read_page(old)[0].get("type") == INDEX_TYPE:
                old.unlink()
        except (OSError, ValueError, WikiRefusal):
            continue
    return written


def append_log(ctx: dict, op: str, caller: str, question: str, basename: str, today) -> None:
    root = _wiki_root(ctx)
    name = f"log-{today.year:04d}.md"
    path = root / name
    # errors="replace": a corrupt byte in the log must never block filing.
    text = (path.read_text(encoding="utf-8", errors="replace") if path.exists()
            else _index_header("Wiki log"))
    verb = "Updated" if op == "update" else "Created"
    text = text.rstrip("\n") + (f"\n\n## [{today.isoformat()}] {op} | {caller or '-'} | {question}\n"
                                f"- {verb}: [[{basename}]]\n")
    _write(ctx, ctx["wiki_folder"], name, text)


def file_page(ctx: dict, payload: dict, today) -> dict:
    """Validate and count, then, under one lock, pick the target, write the
    page, and update the index and log.

    The target checks (update containment, type, self-citation, reviewed
    flag) and the new page's name choice run inside the lock, so two filers
    cannot pick the same name and a page marked reviewed after validation is
    still refused. Every write is checked against the resolved wiki root.
    Once the page is written, an index or log failure does not raise: the
    result gets a ``warning`` naming the stage instead.
    """
    import vault_index
    from note_writer import _acquire_lock, _release_lock
    from obsidian_utils import scrub_secrets

    p = _validate_payload(payload)
    root = _wiki_root(ctx)
    vault_index.ensure_index(ctx["vault"], ctx["folders"], db_path=ctx["db"])
    counted = count_sources(ctx["db"], p["sources"], p["memory"], _roots(ctx))
    if counted["rejected"]:
        raise WikiRefusal("sources refused: " + "; ".join(
            f"{r['name']} ({r['reason']})" for r in counted["rejected"]))
    if counted["count"] < THRESHOLD:
        raise WikiRefusal(f"only {counted['count']} qualifying sources (need {THRESHOLD})")

    # Scrub first: the question also feeds the file name, index and log.
    question = scrub_secrets(p["question"])
    body = scrub_secrets(p["body"])
    queries = root / "queries"
    resolved = counted["resolved"]
    projects = sorted({r["project"] for r in resolved if r["project"]})
    action = "update" if p["update"] else ("file-auto" if p["filed_by"] == "auto" else "file")

    root.mkdir(parents=True, exist_ok=True)
    lock, err = _acquire_lock(root / ".wiki")
    if err:
        raise WikiRefusal(err)
    try:
        created = today.isoformat()
        if p["update"]:
            target = Path(p["update"]).resolve()
            if not target.is_relative_to(queries.resolve()):
                raise WikiRefusal(f"update must name a page under {queries}")
            if not target.is_file():
                raise WikiRefusal(f"update page not found: {target}")
            if any(Path(r["path"]).resolve() == target for r in resolved):
                raise WikiRefusal(f"a page cannot cite itself; drop [[{target.stem}]] from sources")
            old, _ = read_page(target)
            if old.get("type") != PAGE_TYPE:
                raise WikiRefusal(f"{target} is not a wiki page")
            if is_reviewed(old.get("reviewed")) and not p["override_reviewed"]:
                raise WikiRefusal(f"{target.name} is marked reviewed; refusing to overwrite it")
            created = str(old.get("created") or created)
            rel_folder = str(target.parent.relative_to(Path(ctx["vault"]).resolve()))
            filename = target.name
        else:
            rel_folder = f"{ctx['wiki_folder']}/queries/{today.year:04d}"
            stem = f"{today.month:02d}-{today.day:02d}-{slugify(question)}"
            filename, n = f"{stem}.md", 1
            while (Path(ctx["vault"]) / rel_folder / filename).exists():
                n += 1
                filename = f"{stem}-{n}.md"

        meta = {
            # Literal type, not PAGE_TYPE: tests/test_type_scores.py finds
            # writers by scanning for `"type": "claude-..."`.
            "type": "claude-wiki", "title": question, "question": question,
            "date": today.isoformat(), "created": created, "updated": today.isoformat(),
            "projects": projects,
            "sources": [f"[[{r['name']}]]" for r in resolved],
            "memory_sources": [],
            "sources_fingerprint": {r["name"]: fingerprint(r["path"]) for r in resolved},
            "confidence": p["confidence"], "filed_by": p["filed_by"],
        }
        if p["filed_by"] == "auto":
            meta["caller"] = p["caller"]
        meta["tags"] = (["claude/wiki", f"claude/wiki/confidence-{p['confidence']}"]
                        + [f"claude/project/{x}" for x in projects]
                        + [f"claude/topic/{t}" for t in p["topics"]])
        _write(ctx, rel_folder, filename, render_page(meta, body))
        page = Path(ctx["vault"]) / rel_folder / filename
        out = {"path": str(page), "action": action, "count": counted["count"]}

        # The page is saved: from here a failure is a warning, not a refusal.
        stage = "index"
        try:
            vault_index.ensure_index(ctx["vault"], ctx["folders"], db_path=ctx["db"])
            rebuild_wiki_index(ctx)
            stage = "log"
            append_log(ctx, action, p["caller"], question, page.stem, today)
        except Exception as exc:
            then = ("the next filing rebuilds the index" if stage == "index"
                    else "this filing is missing from the log")
            out["warning"] = f"page saved, but the {stage} update failed: {exc}; {then}"
    finally:
        _release_lock(lock)
    return out


def _roots(ctx: dict) -> list:
    return [str(Path(ctx["vault"]) / f) for f in ctx["folders"]]


def _list_field(obj: dict, key: str) -> list:
    val = obj.get(key, [])
    if not isinstance(val, list):
        raise WikiRefusal(f"{key} must be a list")
    return val


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_stdin() -> dict:
    data = sys.stdin.read(STDIN_CAP_CHARS + 1)
    if len(data) > STDIN_CAP_CHARS:
        raise WikiRefusal(f"input too large (over {STDIN_CAP_CHARS} characters)")
    try:
        obj = json.loads(data or "{}")
    except ValueError as exc:
        raise WikiRefusal(f"input is not JSON: {exc}")
    if not isinstance(obj, dict):
        raise WikiRefusal("input must be a JSON object")
    return obj


def _context() -> dict:
    """Vault, wiki folder, indexed folders and DB from a fresh config read."""
    from obsidian_utils import indexed_folders, load_config
    import vault_index

    cfg = load_config(fresh=True)
    vault = cfg.get("vault_path") or ""
    if not vault:
        raise WikiRefusal("vault_path not configured; run /obsidian-setup")
    try:
        folders = indexed_folders(cfg, strict=True)
    except ValueError as exc:
        raise WikiRefusal(str(exc))
    wiki_folder = cfg.get("wiki_folder") or ""
    if not wiki_folder:
        raise WikiRefusal("wiki_folder is empty (the wiki is turned off)")
    return {
        "vault": vault,
        "wiki_folder": os.path.normpath(wiki_folder),
        "folders": folders,
        "db": vault_index._default_db_path(),
    }


def _cmd_count() -> dict:
    import vault_index

    payload = _read_stdin()
    sources = _list_field(payload, "sources")
    memory = _list_field(payload, "memory_sources")
    ctx = _context()
    vault_index.ensure_index(ctx["vault"], ctx["folders"], db_path=ctx["db"])
    out = count_sources(ctx["db"], sources, memory, _roots(ctx))
    out.pop("resolved")
    return out


def _cmd_lookup() -> dict:
    import vault_index

    payload = _read_stdin()
    ctx = _context()
    vault_index.ensure_index(ctx["vault"], ctx["folders"], db_path=ctx["db"])
    return {"candidates": lookup(ctx["db"], str(payload.get("question") or ""))}


def _cmd_stale() -> dict:
    import vault_index

    payload = _read_stdin()
    ctx = _context()
    page = Path(str(payload.get("page") or "")).resolve()
    if not page.is_relative_to(_wiki_root(ctx).resolve()) or not page.is_file():
        raise WikiRefusal(f"page must be an existing file under {_wiki_root(ctx)}")
    vault_index.ensure_index(ctx["vault"], ctx["folders"], db_path=ctx["db"])
    return stale(ctx["db"], page, _roots(ctx))


def _cmd_file() -> dict:
    payload = _read_stdin()
    ctx = _context()
    # Local date: page date/updated, file name and log line match the user's day.
    out = file_page(ctx, payload, _dt.date.today())
    if out.get("warning"):
        print(f"WARNING: {out['warning']}", file=sys.stderr)
    return out


_COMMANDS: dict = {
    "count": _cmd_count,
    "lookup": _cmd_lookup,
    "stale": _cmd_stale,
    "file": _cmd_file,
}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmds = {"rule": lambda: {"rule": WRITING_RULE}}
    cmds.update(_COMMANDS)
    if not argv or argv[0] not in cmds:
        print(f"ERROR: usage: wiki.py {{{'|'.join(sorted(cmds))}}}", file=sys.stderr)
        return 2
    try:
        out = cmds[argv[0]]()
    except WikiRefusal as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # a crash, not a refusal
        print(f"ERROR: internal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
