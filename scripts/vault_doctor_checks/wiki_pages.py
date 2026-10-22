"""vault_doctor check: health of the LLM wiki's pages (#396, epic #383).

``/vault-ask`` files answers as pages under ``<vault>/<wiki_folder>/queries/``
(see ``hooks/wiki.py``). This check reports, per page:

- ``stale``: ``wiki.stale`` found a changed source or an unverifiable
  fingerprint. Reviewed pages get ``reviewed-stale`` instead.
- ``broken-source``: a cited source is gone (a ``missing:`` reason).
- ``reviewed-stale``: the page is marked reviewed and ``wiki.stale`` found a
  reason other than ``newer:``. ``/vault-ask`` never refreshes these on its
  own.
- ``page-unreadable``: the page or its staleness check could not be read.
  An unreadable page is shown even under ``--project`` (its project is
  unknown).

and once for the wiki:

- ``index-drift``: the index files on disk differ from a fresh
  ``wiki.render_wiki_index``. This is the only row ``--apply`` fixes: it backs
  up the current index files and rebuilds them under the wiki lock. A
  freshly set-up wiki (no pages, no index files) has no drift.

Every other row is report-only (``unresolved``, confidence 0.0).

The check reports only rows a person can act on (#440). It drops
``wiki.stale``'s ``newer:`` reasons: they come from a loose search (any one
word of the question, top 5 hits) that flags almost every page on an active
vault, and ``/vault-ask`` still uses them to refresh the page the next time
the question is asked. A page whose only reasons are ``newer:`` gets no row.
It does not list pages filed by a skill (``filed_by: auto`` stays in the
frontmatter for Dataview) or pages no note links to (pages are reached
through the wiki index, so that is the normal state).

The wiki folder comes from the config file, because ``scan`` receives only
the sessions and insights folders. The file is read at call time from
``Path.home()``, like the dispatcher's own config read; a missing file means
the default folder. A wiki that is turned off, or a folder that does not
exist yet, reports nothing. A corrupt config, an invalid ``wiki_folder``, or
a ``queries/`` folder (or subfolder) that cannot be read raises, so the
dispatcher shows a crashed check instead of a clean one.

The check lists the pages first, so an unreadable folder raises before the
index sync can drop the rows of the pages it hides. Then it syncs the vault
index (like ``/vault-ask``): index lines come from the notes table, and a
stale table would invent drift. The index is shared and belongs to the
configured ``vault_path``, so a ``--vault`` (or ``OBSIDIAN_BRAIN_VAULT``)
that names another folder is skipped with one stderr line instead of being
synced into it. Memory files are listed at most once per scan.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import sys
from pathlib import Path

from . import Issue, Result

_HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

NAME = "wiki-pages"
DESCRIPTION = (
    "Report stale, broken-source and reviewed-stale wiki pages, and rebuild "
    "a drifted wiki index"
)
# Staleness and drift are undated, so --days changes nothing here.
DEFAULT_WINDOW_DAYS = 9999
OPT_IN = False
# wiki.stale reasons the doctor leaves out (#440); /vault-ask still uses them.
_HINT_PREFIX = "newer:"


def _read_config() -> dict:
    """The config file as a dict, read now (never a cached or import-time
    path). Missing file: the plugin defaults. Corrupt file: ``ValueError``,
    because guessing the wiki folder would make a broken setup look clean."""
    from obsidian_utils import _DEFAULTS

    path = Path.home() / ".claude" / "obsidian-brain-config.json"
    cfg = {"wiki_folder": _DEFAULTS.get("wiki_folder", "claude-wiki")}
    try:
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(user, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    cfg.update(user)
    return cfg


def _same_path(a, b) -> bool:
    """True when two vault paths name one folder (``~``, symlinks, ``..``
    and a trailing slash do not matter)."""
    return Path(os.path.expanduser(str(a))).resolve() == Path(os.path.expanduser(str(b))).resolve()


def _context(vault_path: str, sessions_folder: str, insights_folder: str):
    """The wiki context, or None when the wiki is off or not created yet."""
    from obsidian_utils import indexed_folders
    import vault_index

    cfg = _read_config()
    configured = cfg.get("vault_path")
    if configured and _same_path(configured, vault_path) is False:
        # The check reads and syncs the shared index, which belongs to the
        # configured vault. Syncing another vault into it would mix the two.
        print(f"[{NAME}] skipped: --vault {vault_path} is not the configured vault {configured}; "
              "the wiki check uses the shared index of the configured vault", file=sys.stderr)
        return None
    cfg.update(vault_path=vault_path, sessions_folder=sessions_folder,
               insights_folder=insights_folder)
    folders = indexed_folders(cfg, strict=True)  # raises on an invalid wiki_folder
    wiki_folder = cfg.get("wiki_folder") or ""
    if not wiki_folder:
        print(f"[{NAME}] the wiki is turned off (wiki_folder is empty); nothing to scan",
              file=sys.stderr)
        return None
    ctx = {"vault": vault_path, "wiki_folder": os.path.normpath(wiki_folder),
           "folders": folders, "db": vault_index._default_db_path()}
    if not (Path(vault_path) / ctx["wiki_folder"]).is_dir():
        print(f"[{NAME}] no wiki folder at {Path(vault_path) / ctx['wiki_folder']}; nothing to scan",
              file=sys.stderr)
        return None
    return ctx


def _row(page, project: str, code: str, detail: str, **extra) -> Issue:
    return Issue(check=NAME, note_path=str(page), project=project, current_source="",
                 proposed_source="", reason=f"{code}: {detail}", confidence=0.0,
                 extra={"signal_class": code, "unresolved": True, **extra})


def _is_wiki_index(f: Path) -> bool:
    import wiki

    try:
        return wiki.read_page(f)[0].get("type") == wiki.INDEX_TYPE
    except (OSError, ValueError, wiki.WikiRefusal):
        return False


def _empty_wiki(ctx: dict, wiki_root: Path) -> bool:
    """True for a freshly set-up wiki: no indexed pages under ``queries/``,
    no ``index.md`` and no ``index-*.md`` typed as a wiki index."""
    import vault_index
    import wiki

    if (wiki_root / "index.md").exists():
        return False
    if any(_is_wiki_index(f) for f in wiki_root.glob("index-*.md")):
        return False
    queries = wiki_root / "queries"
    conn = vault_index._connect(ctx["db"])
    try:
        rows = conn.execute("SELECT path FROM notes WHERE type = ?", (wiki.PAGE_TYPE,)).fetchall()
    finally:
        conn.close()
    return not any(vault_index._is_under(Path(r["path"]), queries) for r in rows)


def _pages(queries: Path) -> list:
    """Every ``*.md`` under ``queries/``, sorted. A folder that cannot be
    read raises (the dispatcher then shows a crashed check): skipping it
    would hide its pages and make the index look drifted."""
    if not queries.exists():
        return []
    if not os.access(queries, os.R_OK | os.X_OK):
        raise PermissionError(f"cannot read {queries}")

    def fail(exc: OSError) -> None:
        raise exc

    out = []
    for folder, _dirs, files in os.walk(queries, onerror=fail):
        out += [Path(folder) / n for n in files if n.endswith(".md")]
    return sorted(out)


def _cites_memory(meta: dict) -> bool:
    """True when ``wiki.stale`` would list memory files for this page: it has
    ``memory_sources`` entries or ``memory:`` fingerprint keys."""
    import wiki

    names = meta.get("memory_sources")
    fps = meta.get("sources_fingerprint")
    return bool(isinstance(names, list) and names) or (
        isinstance(fps, dict) and any(str(k).startswith(wiki.MEMORY_PREFIX) for k in fps))


def _index_drift(ctx: dict, wiki_root: Path) -> dict:
    import wiki

    if _empty_wiki(ctx, wiki_root):
        return {"missing": [], "different": [], "extra": []}
    want = wiki.render_wiki_index(ctx)
    missing, different = [], []
    for name, text in want.items():
        f = wiki_root / name
        try:
            if f.read_text(encoding="utf-8") != text:
                different.append(name)
        except FileNotFoundError:
            missing.append(name)
        except (OSError, ValueError):
            different.append(name)
    extra = []
    for f in wiki_root.glob("index-*.md"):
        if f.name not in want and _is_wiki_index(f):
            extra.append(f.name)
    return {"missing": sorted(missing), "different": sorted(different), "extra": sorted(extra)}


def scan(
    vault_path: str,
    sessions_folder: str,
    insights_folder: str,
    days: int,
    project: str | None = None,
) -> list:
    import vault_index
    import wiki

    ctx = _context(vault_path, sessions_folder, insights_folder)
    if ctx is None:
        return []
    vault = Path(ctx["vault"])
    wiki_root = vault / ctx["wiki_folder"]
    # List the pages before the sync: an unreadable folder must raise here,
    # before the sync drops the index rows of the pages it cannot see.
    pages = _pages(wiki_root / "queries")
    vault_index.ensure_index(ctx["vault"], ctx["folders"], db_path=ctx["db"])
    roots = [str(vault / f) for f in ctx["folders"]]
    needle = (project or "").strip().lower()
    memory_listing = None  # listed once, when the first page cites memory
    issues: list = []

    for page in pages:
        try:
            meta, _body = wiki.read_page(page)
        except (OSError, ValueError, wiki.WikiRefusal) as exc:
            # Always shown, even under --project: its project is unknown.
            issues.append(_row(page, "", "page-unreadable", f"cannot read the page: {exc}"))
            continue
        if meta.get("type") != wiki.PAGE_TYPE:
            continue
        projects = meta.get("projects") if isinstance(meta.get("projects"), list) else []
        proj = ",".join(str(p) for p in projects)
        if needle and not any(needle in str(p).lower() for p in projects):
            continue
        if memory_listing is None and _cites_memory(meta):
            memory_listing = wiki._memory_listing()
        try:
            reasons = wiki.stale(ctx["db"], page, roots, memory_listing=memory_listing)["reasons"]
        except Exception as exc:  # noqa: BLE001 -- one bad page must not hide the rest
            issues.append(_row(page, proj, "page-unreadable", f"staleness check failed: {exc}"))
            reasons = []
        reasons = [r for r in reasons if not r.startswith(_HINT_PREFIX)]
        missing = [r for r in reasons if r.startswith("missing:")]
        if missing:
            issues.append(_row(page, proj, "broken-source", "; ".join(missing), reasons=missing))
        if wiki.is_reviewed(meta.get("reviewed")):
            if reasons:
                issues.append(_row(page, proj, "reviewed-stale",
                                   "reviewed page is stale (never auto-refreshed): " + "; ".join(reasons),
                                   reasons=reasons))
        else:
            other = [r for r in reasons if not r.startswith("missing:")]
            if other:
                issues.append(_row(page, proj, "stale", "; ".join(other), reasons=other))

    drift = _index_drift(ctx, wiki_root)
    if any(drift.values()):
        parts = [f"{k}: {', '.join(v)}" for k, v in drift.items() if v]
        issues.append(Issue(
            check=NAME, note_path=str(wiki_root / "index.md"), project="",
            current_source="", proposed_source="", confidence=1.0,
            reason="index-drift: index files differ from a fresh rebuild (" + "; ".join(parts) + ")",
            extra={"signal_class": "index-drift", "files": drift, "ctx": ctx}))
    print(f"[{NAME}] {len(issues)} issue(s)", file=sys.stderr)
    return issues


def apply(issues: list, backup_root: str) -> list:
    """Rebuild the index for ``index-drift`` rows; every other row is
    report-only and comes back ``unresolved``."""
    from note_writer import _acquire_lock, _release_lock
    import wiki

    results = []
    for i in issues:
        if i.extra.get("signal_class") != "index-drift":
            results.append(Result(check=NAME, note_path=i.note_path, status="unresolved",
                                  error=f"{NAME} {i.extra.get('signal_class')} rows are report-only"))
            continue
        ctx = i.extra["ctx"]
        wiki_root = Path(ctx["vault"]) / ctx["wiki_folder"]
        lock, err = _acquire_lock(wiki_root / ".wiki")
        if err:
            results.append(Result(check=NAME, note_path=i.note_path, status="skipped", error=err))
            continue
        backed_up = None
        try:
            stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
            backup = Path(os.path.expanduser(backup_root)) / NAME / stamp
            backup.mkdir(parents=True, exist_ok=True)
            for f in [wiki_root / "index.md", *wiki_root.glob("index-*.md")]:
                if f.is_file():
                    shutil.copy2(f, backup / f.name)
            backed_up = str(backup)
            wiki.rebuild_wiki_index(ctx)
            results.append(Result(check=NAME, note_path=i.note_path, status="applied",
                                  backup_path=backed_up))
        except Exception as exc:  # noqa: BLE001
            results.append(Result(check=NAME, note_path=i.note_path, status="error",
                                  backup_path=backed_up, error=str(exc)))
        finally:
            _release_lock(lock)
    return results
