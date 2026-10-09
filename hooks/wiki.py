"""LLM wiki for /vault-ask answers (#383, #395).

Library + JSON CLI (``python3 hooks/wiki.py rule|lookup|stale|count|file|memgrep``).
Every vault write goes through ``write_vault_note`` (atomic, 0o600,
contained under the vault). Vault ownership precedes the legacy wiki lock
and covers page, index and log publication. Design:
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
import functools
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
# Memory files (#396) are named "<project-dir>/<file>.md" and fingerprinted
# under "memory:<name>". Only memory_sources.py knows where they live.
MEMORY_PREFIX = "memory:"
MEMORY_NAME_RE = re.compile(r"^[^/\\\x00]{1,255}/[^/\\\x00]{1,252}\.md$")
MEMGREP_MAX = 200
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


_QUOTED_KEY_RE = re.compile(r'^(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\']|\'\')*)\')\s*:(?:\s(.*))?$')


def _split_map_line(stripped: str):
    """``(key, value text)`` for one block-map line, or None.

    A key may hold a colon (``memory:proj/a.md: 1111``), so the line splits
    on the first ``": "``, or on a final ``:`` when there is no value. A
    quoted key ends at its closing quote. A line with neither falls back to
    the first ``:``, as before."""
    m = _QUOTED_KEY_RE.match(stripped)
    if m:
        if m.group(1) is not None:
            try:
                key = json.loads('"' + m.group(1) + '"')
            except ValueError:
                key = m.group(1)
        else:
            key = m.group(2).replace("''", "'")
        return key, m.group(3) or ""
    k, sep, v = stripped.partition(": ")
    if not sep:
        if stripped.endswith(":"):
            k, sep, v = stripped[:-1], ":", ""
        else:
            k, sep, v = stripped.partition(":")
    if not sep:
        return None
    return str(_scalar(k)), v


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
            kv = _split_map_line(stripped)
            if kv is not None:
                if current == "":
                    current = meta[block_key] = {}
                if isinstance(current, dict):
                    current[kv[0]] = _scalar(kv[1])
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
    mem = resolve_memory(memory_sources)
    rejected += mem["rejected"]
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
    qualifying += [MEMORY_PREFIX + m["name"] for m in mem["resolved"]]
    return {"count": len(qualifying), "qualifying": qualifying, "other": other,
            "rejected": rejected, "resolved": resolved, "memory_resolved": mem["resolved"]}


def _memory_files() -> list:
    """This host's memory files (the seam tests replace)."""
    return _memory_listing()[0]


def _memory_listing() -> tuple:
    """``(files, errors, host)`` for this host: the files from
    ``memory_sources``, the ``{"path", "error"}`` failures it hit while
    listing, and the host name. The sibling seam of ``_memory_files`` for
    callers that must tell "no such file" from "could not look"."""
    import memory_sources as ms

    host = ms.detect_host()
    errors: list = []
    return ms.memory_sources(host, errors=errors), errors, host


def resolve_memory(names) -> dict:
    """Map ``<project-dir>/<file>.md`` names to this host's memory files.

    A name of the wrong shape (not a string, no folder, ``..``) is rejected
    as ``not a memory file name``; a well-formed name with no such file as
    ``not a memory file on this host``. Repeats of one file count once.
    """
    names = list(names or [])
    if not names:
        return {"resolved": [], "rejected": []}
    import memory_sources as ms

    by_name = {ms.memory_name(f): f for f in _memory_files()}
    resolved, rejected, seen = [], [], set()
    for n in names:
        if (not isinstance(n, str) or not MEMORY_NAME_RE.fullmatch(n)
                or ".." in n.split("/")):
            rejected.append({"name": str(n), "reason": "not a memory file name"})
            continue
        f = by_name.get(n)
        if f is None:
            rejected.append({"name": n, "reason": "not a memory file on this host"})
            continue
        if f in seen:
            continue
        seen.add(f)
        resolved.append({"name": n, "path": str(f)})
    return {"resolved": resolved, "rejected": rejected}


def memgrep(pattern: str, files, skipped: list | None = None) -> list:
    """Memory files whose text holds ``pattern`` (case-insensitive fixed
    string, never a regex). A file that cannot be read is left out and, when
    ``skipped`` is a list, appended to it as ``{"path", "error"}``."""
    import memory_sources as ms

    needle = pattern.casefold()
    out = []
    for f in files:
        try:
            text = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            if skipped is not None:
                skipped.append({"path": str(f), "error": str(exc)})
            continue
        if needle in text.casefold():
            out.append({"name": ms.memory_name(f), "path": str(f)})
    return out


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


def stale(db_path: str, page_path, roots, *, memory_listing=None) -> dict:
    """Why a page is stale: ``unverifiable:``/``changed:``/``missing:`` per
    source, ``newer:`` per newer counting note in the question's top 5 hits.
    All reasons are returned. An unreadable vault source counts as missing,
    and a cited source with no usable fingerprint (field missing, not a
    mapping, or no non-empty string for it) as unverifiable -- never as
    fresh. A page gets ``unverifiable: sources`` when ``sources`` is not a
    list, or when neither ``sources`` nor ``memory_sources`` is a non-empty
    list (memory files count toward the threshold, so a page may cite only
    memory). Memory sources follow ``_memory_reasons``. ``memory_listing``
    is a ``_memory_listing()`` result to use instead of listing again (a
    caller checking many pages lists once). ``memory_paths`` maps each cited
    memory file found here to its path, so a refresh can re-read it."""
    meta, _body = read_page(page_path)
    raw_fps = meta.get("sources_fingerprint")
    fps = {}
    mem_fps = {}
    if isinstance(raw_fps, dict):
        for k, v in raw_fps.items():
            if not (isinstance(v, str) and v):
                continue
            if str(k).startswith(MEMORY_PREFIX):
                mem_fps[str(k)] = v
            else:
                fps[_norm_name(k)] = v
    sources = meta.get("sources")
    mem_names = meta.get("memory_sources")
    reasons = []
    if not isinstance(sources, list) or not (sources or (isinstance(mem_names, list) and mem_names)):
        # Unparseable, or no source of either kind: the page cannot be checked.
        reasons.append("unverifiable: sources")
    if not isinstance(sources, list):
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
    mem_names = list(dict.fromkeys(mem_names if isinstance(mem_names, list) else []))
    for n in mem_names:
        if MEMORY_PREFIX + str(n) not in mem_fps:
            reasons.append(f"unverifiable: {MEMORY_PREFIX}{n}")
    memory_paths: dict = {}
    if mem_fps or mem_names:
        reasons += _memory_reasons(mem_fps, mem_names, memory_paths, memory_listing)
    cited = {_norm_name(x) for x in sources} | set(fps)
    updated = str(meta.get("updated") or "")
    question = str(meta.get("question") or "")
    for base in _newer_notes(db_path, question, updated, cited, roots):
        reasons.append(f"newer: {base}")
    return {"stale": bool(reasons), "reasons": reasons, "memory_paths": memory_paths}


def _memory_reasons(mem_fps: dict, mem_names: list, memory_paths: dict,
                    listing=None) -> list:
    """Reasons for the page's ``memory:`` fingerprints, and fill
    ``memory_paths`` with ``{name: path}`` for each cited file found.

    ``unverifiable:`` when this host has no memory files or the listing
    could not look (root, project folder or the file itself failed), or the
    listed file cannot be read. ``missing:`` only when a listing that
    worked does not have the file (or it was deleted since). ``listing`` is
    a ``_memory_listing()`` result to reuse; None lists now."""
    import memory_sources as ms

    files, errors, host = listing if listing is not None else _memory_listing()
    root_failed, bad_projects, bad_names = ms.failed_scopes(errors)
    by_name = {ms.memory_name(f): f for f in files}
    cited = [n for n in mem_names if isinstance(n, str)] + [k[len(MEMORY_PREFIX):] for k in mem_fps]
    memory_paths.update({n: str(by_name[n]) for n in dict.fromkeys(cited) if n in by_name})
    out = []
    for key, old in mem_fps.items():
        name = key[len(MEMORY_PREFIX):]
        if host != "claude-code" or root_failed:
            out.append(f"unverifiable: {key}")
            continue
        f = by_name.get(name)
        if f is None:
            if name.split("/", 1)[0] in bad_projects or name in bad_names:
                out.append(f"unverifiable: {key}")
            else:
                out.append(f"missing: {key}")
            continue
        try:
            now = fingerprint(f)
        except FileNotFoundError:
            out.append(f"missing: {key}")  # deleted after the listing
            continue
        except OSError:
            out.append(f"unverifiable: {key}")
            continue
        if now != old:
            out.append(f"changed: {key}")
    return out


# Question-frame verbs that _STOPWORDS keeps but nearly every "how does X
# work?" question contains; matching on them alone flags unrelated notes.
# Contraction endings (it's, we'll) are cut before splitting, so a bare
# "re" or "C" still counts as a topic.
_QUESTION_FILLER = frozenset({"work", "works", "working", "worked"})
_CONTRACTION_RE = re.compile(r"['\u2019](?:s|t|d|ll|re|ve|m)\b", re.IGNORECASE)


def _newer_notes(db_path: str, question: str, updated: str, cited: set, roots) -> list:
    """Counting notes among the question's top 5 OR-matched hits that are
    dated after ``updated`` and not already cited. Stopwords are dropped
    from the question first; with no words left the check is skipped.

    Queries FTS directly, restricted to counting types: search_vault's AND
    query matches the page itself first and then never falls back to OR, so
    a newer note sharing only some of the words would be missed.
    """
    import vault_index

    text = _CONTRACTION_RE.sub("", (question or "").replace("-", " "))
    words = [w for w in re.findall(r"[a-zA-Z0-9_/]+", text)
             if w.lower() not in vault_index._STOPWORDS
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


def _vault_owned(function):
    """Keep the final wiki publication under shared OS vault ownership."""
    @functools.wraps(function)
    def owned(ctx, *args, **kwargs):
        from note_transactions import context_for_vault, ownership_lock, LockBusy
        try:
            with ownership_lock(context_for_vault(ctx["vault"])):
                return function(ctx, *args, **kwargs)
        except LockBusy as exc:
            raise WikiRefusal(str(exc)) from exc
    return owned


def _revision(ctx, path):
    from note_transactions import context_for_vault, read_revision
    return read_revision(context_for_vault(ctx["vault"]), path)


def _write(ctx: dict, rel_folder: str, filename: str, content: str,
           expected_revision="unspecified") -> None:
    """Write one wiki file. Refused unless the folder, symlinks followed,
    is inside the resolved wiki root (a symlinked ``queries/`` could
    otherwise put a page elsewhere in the vault)."""
    from obsidian_utils import write_vault_note

    final = Path(ctx["vault"]) / rel_folder / filename
    real = final.resolve()
    if not real.is_relative_to(_wiki_root(ctx).resolve()):
        raise WikiRefusal(f"{real} is outside the wiki folder {_wiki_root(ctx).resolve()}; "
                          "refusing to write it")
    err = write_vault_note(ctx["vault"], rel_folder, filename, content,
                           expected_revision=expected_revision)
    if err:
        raise WikiRefusal(err)


def _index_header(title: str) -> str:
    # Literal type (see file_page): the writer scan needs to see it.
    return '---\ntype: "claude-wiki-index"\n---\n# ' + title + '\n'


def render_wiki_index(ctx: dict) -> dict:
    """The index files as ``{file name: text}``, built from the notes table
    (never from page files). ``index.md`` alone up to INDEX_SPLIT pages;
    above that, ``index.md`` lists one ``index-<project>.md`` per project.
    Writes nothing."""
    import vault_index

    queries = _wiki_root(ctx) / "queries"
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

    if len(rows) <= INDEX_SPLIT:
        return {"index.md": _index_header("Wiki index") + "\n" + "\n".join(line(r) for r in rows) + "\n"}
    groups: dict = {}
    for r in rows:
        projects = [t[len("claude/project/"):] for t in (r["tags"] or "").split(",")
                    if t.startswith("claude/project/")] or ["unassigned"]
        for proj in projects:
            groups.setdefault(slugify(proj), []).append(r)
    top = [f"- [[index-{p}]] — {len(rs)} page(s)" for p, rs in sorted(groups.items())]
    out = {"index.md": _index_header("Wiki index") + "\n" + "\n".join(top) + "\n"}
    for proj, rs in sorted(groups.items()):
        out[f"index-{proj}.md"] = (_index_header(f"Wiki index: {proj}") + "\n"
                                   + "\n".join(line(r) for r in rs) + "\n")
    return out


def rebuild_wiki_index(ctx: dict) -> list:
    """Write the files from ``render_wiki_index`` and delete any other
    ``index-*.md`` typed ``claude-wiki-index``. Returns the written paths."""
    from note_transactions import context_for_vault
    context_for_vault(ctx["vault"])
    root = _wiki_root(ctx)
    # Record before rendering: a manual edit during rendering must survive.
    revisions = {p.name: _revision(ctx, p) for p in root.glob("index*.md")}
    files = render_wiki_index(ctx)
    return _publish_wiki_index(ctx, root, revisions, files)


@_vault_owned
def _publish_wiki_index(ctx, root, revisions, files):
    written = []
    for name, text in files.items():
        _write(ctx, ctx["wiki_folder"], name, text, revisions.get(name))
        written.append(str(root / name))
    for old in root.glob("index-*.md"):
        if old.name in files:
            continue
        # Only delete our own index files: a user note may share the name.
        try:
            owned_index = read_page(old)[0].get("type") == INDEX_TYPE
        except (OSError, ValueError, WikiRefusal):
            continue
        if owned_index:
            if not old.resolve().is_relative_to(root.resolve()):
                raise WikiRefusal(f"{old} is outside the wiki folder; refusing to delete it")
            from note_transactions import context_for_vault, delete_note
            expected = revisions.get(old.name)
            operation = "wiki-index-delete-" + hashlib.sha256(
                (str(old) + "\0" + str(expected)).encode("utf-8")).hexdigest()
            result = delete_note(context_for_vault(ctx["vault"]), old, expected, operation)
            if result.status not in {"applied", "unchanged"}:
                raise WikiRefusal("index deletion " + result.status + ": " + "; ".join(result.warnings))
    return written


@_vault_owned
def append_log(ctx: dict, op: str, caller: str, question: str, basename: str, today,
               filing_id="") -> None:
    root = _wiki_root(ctx)
    name = f"log-{today.year:04d}.md"
    path = root / name
    # Preserve a corrupt log and report its failure after saving the page.
    from note_transactions import context_for_vault, record_read
    original = path.read_bytes().decode("utf-8") if path.exists() else None
    revision = record_read(context_for_vault(ctx["vault"]), path, original)
    text = original if original is not None else _index_header("Wiki log")
    marker = f"<!-- obsidian-brain:wiki-filing:{filing_id} -->" if filing_id else ""
    if marker and marker in text:
        return
    verb = "Updated" if op == "update" else "Created"
    text = text.rstrip("\n") + (f"\n\n## [{today.isoformat()}] {op} | {caller or '-'} | {question}\n"
                                f"- {verb}: [[{basename}]]\n" + (marker + "\n" if marker else ""))
    _write(ctx, ctx["wiki_folder"], name, text, revision)


def file_page(ctx: dict, payload: dict, today) -> dict:
    """Prepare sources outside ownership, then publish with late source checks."""
    import vault_index
    from obsidian_utils import scrub_secrets

    from note_transactions import context_for_vault
    context_for_vault(ctx["vault"])
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
    filing_id = hashlib.sha256(json.dumps({
        **p, "question": question, "body": body, "date": today.isoformat(),
        "resolved": [str(Path(r["path"]).resolve().relative_to(Path(ctx["vault"]).resolve()))
                     for r in resolved],
    }, sort_keys=True).encode("utf-8")).hexdigest()
    action = "update" if p["update"] else ("file-auto" if p["filed_by"] == "auto" else "file")

    source_revisions = {str(Path(item["path"]).resolve()):
                        hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest()
                        for item in resolved + counted["memory_resolved"]}
    out = _publish_file_page(ctx, p, today, root, queries, counted, resolved, projects,
                             filing_id, action, question, body, source_revisions)
    # Publication has succeeded. Slow index refresh needs no writer ownership;
    # each derived note then performs its own revision-checked transaction.
    stage = "index"
    try:
        vault_index.ensure_index(ctx["vault"], ctx["folders"], db_path=ctx["db"])
        rebuild_wiki_index(ctx)
        stage = "log"
        append_log(ctx, action, p["caller"], question, Path(out["path"]).stem, today, filing_id)
    except Exception as exc:
        then = ("the next filing rebuilds the index" if stage == "index"
                else "this filing is missing from the log")
        out["warning"] = f"page saved, but the {stage} update failed: {exc}; {then}"
    return out


@_vault_owned
def _publish_file_page(ctx, p, today, root, queries, counted, resolved, projects,
                       filing_id, action, question, body, source_revisions):
    root.mkdir(parents=True, exist_ok=True)
    for path, expected in source_revisions.items():
        try:
            actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError as exc:
            raise WikiRefusal("source changed before wiki publication") from exc
        if actual != expected:
            raise WikiRefusal("source changed before wiki publication")
    created = today.isoformat()
    retry = False
    revision = None
    if p["update"]:
        target = Path(p["update"]).resolve()
        if not target.is_relative_to(queries.resolve()):
            raise WikiRefusal(f"update must name a page under {queries}")
        if not target.is_file():
            raise WikiRefusal(f"update page not found: {target}")
        if any(Path(r["path"]).resolve() == target for r in resolved):
            raise WikiRefusal(f"a page cannot cite itself; drop [[{target.stem}]] from sources")
        revision = _revision(ctx, target)
        old, _ = read_page(target)
        if old.get("type") != PAGE_TYPE:
            raise WikiRefusal(f"{target} is not a wiki page")
        if is_reviewed(old.get("reviewed")) and not p["override_reviewed"]:
            raise WikiRefusal(f"{target.name} is marked reviewed; refusing to overwrite it")
        created = str(old.get("created") or created)
        retry = old.get("filing_id") == filing_id
        rel_folder = str(target.parent.relative_to(Path(ctx["vault"]).resolve()))
        filename = target.name
    else:
        rel_folder = f"{ctx['wiki_folder']}/queries/{today.year:04d}"
        stem = f"{today.month:02d}-{today.day:02d}-{slugify(question)}"
        filename, n = f"{stem}.md", 1
        while (Path(ctx["vault"]) / rel_folder / filename).exists():
            existing = Path(ctx["vault"]) / rel_folder / filename
            try:
                old, _ = read_page(existing)
            except (OSError, ValueError, WikiRefusal):
                old = {}
            if old.get("filing_id") == filing_id:
                retry = True
                break
            n += 1
            filename = f"{stem}-{n}.md"

    meta = {
        # Literal type, not PAGE_TYPE: tests/test_type_scores.py finds
        # writers by scanning for `"type": "claude-..."`.
        "type": "claude-wiki", "title": question, "question": question,
        "date": today.isoformat(), "created": created, "updated": today.isoformat(),
        "projects": projects,
        "sources": [f"[[{r['name']}]]" for r in resolved],
        "memory_sources": [m["name"] for m in counted["memory_resolved"]],
        "sources_fingerprint": {
            **{r["name"]: fingerprint(r["path"]) for r in resolved},
            **{MEMORY_PREFIX + m["name"]: fingerprint(m["path"])
               for m in counted["memory_resolved"]}},
        "confidence": p["confidence"], "filed_by": p["filed_by"],
        "filing_id": filing_id,
    }
    from runtime_context import current_runtime_context
    actor = current_runtime_context()
    if actor is not None:
        meta["author_host"] = actor.host
    if p["filed_by"] == "auto":
        meta["caller"] = p["caller"]
    meta["tags"] = (["claude/wiki", f"claude/wiki/confidence-{p['confidence']}"]
                    + [f"claude/project/{x}" for x in projects]
                    + [f"claude/topic/{t}" for t in p["topics"]])
    if not retry:
        _write(ctx, rel_folder, filename, render_page(meta, body), revision)
    page = Path(ctx["vault"]) / rel_folder / filename
    out = {"path": str(page), "action": action, "count": counted["count"]}

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


def _context(context=None) -> dict:
    """Vault, wiki folder, indexed folders and DB from a fresh config read."""
    from obsidian_utils import indexed_folders, load_config
    import vault_index
    if context is not None:
        from runtime_context import using_runtime_context
        with using_runtime_context(context):
            return _context()

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
    out.pop("memory_resolved")
    return out


def _cmd_memgrep() -> dict:
    payload = _read_stdin()
    pattern = payload.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > MEMGREP_MAX:
        raise WikiRefusal(f"pattern must be a non-blank string of at most {MEMGREP_MAX} characters")
    files, skipped, host = _memory_listing()
    matches = memgrep(pattern.strip(), files, skipped=skipped)
    # skipped: listing failures first, then files that could not be read.
    return {"host": host, "matches": matches, "skipped": skipped}


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
    "memgrep": _cmd_memgrep,
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
