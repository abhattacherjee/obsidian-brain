"""LLM wiki for /vault-ask answers (#383, #395).

Library + JSON CLI (``python3 hooks/wiki.py rule|lookup|stale|count|file``).
Every vault write goes through ``write_vault_note`` (atomic, 0o600,
contained under the vault) while holding one wiki lock. Design:
``docs/plans/383-llm-wiki-design.md``.

CLI contract: subcommands that take input read one JSON object on stdin
(capped at ``STDIN_CAP_CHARS``) and print one JSON object on stdout. Exit 0
is success; exit 1 is a refusal with an ``ERROR: <reason>`` line on stderr;
exit 2 is a usage error or a crash.
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


def read_page(path) -> tuple:
    """Return ``(meta, body)``. Values are JSON-decoded when they parse,
    else kept as the raw string (a hand-edited page)."""
    from frontmatter import split_frontmatter, split_lines_lf_crlf

    text = Path(path).read_text(encoding="utf-8")
    _open, fm_lines, _close, body_lines, err = split_frontmatter(split_lines_lf_crlf(text))
    if err:
        raise WikiRefusal(f"{path}: {err}")
    meta: dict = {}
    tags: list = []
    in_tags = False
    for raw in fm_lines:
        stripped = raw.strip()
        if in_tags and stripped.startswith("- "):
            tags.append(stripped[2:].strip())
            continue
        in_tags = False
        key, sep, val = stripped.partition(":")
        if not sep:
            continue
        key, val = key.strip(), val.strip()
        if key == "tags" and not val:
            in_tags = True
            continue
        try:
            meta[key] = json.loads(val)
        except ValueError:
            meta[key] = val
    if tags:
        meta["tags"] = tags
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
    """Count distinct qualifying sources (the filing threshold input)."""
    res = resolve_sources(db_path, names, roots)
    rejected = list(res["rejected"])
    for m in memory_sources or []:
        rejected.append({"name": str(m), "reason": "memory sources arrive in #396"})
    qualifying, other = [], []
    for r in res["resolved"]:
        if r["count_type"] in COUNTING_TYPES:
            if r["key"] not in qualifying:
                qualifying.append(r["key"])
        else:
            other.append(r["name"])
    return {"count": len(qualifying), "qualifying": qualifying, "other": other,
            "rejected": rejected, "resolved": res["resolved"]}


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


_COMMANDS: dict = {
    "count": _cmd_count,
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
