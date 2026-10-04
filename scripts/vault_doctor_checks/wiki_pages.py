"""vault_doctor check: health of the LLM wiki's pages (#396, epic #383).

``/vault-ask`` files answers as pages under ``<vault>/<wiki_folder>/queries/``
(see ``hooks/wiki.py``). This check reports, per page:

- ``stale``: ``wiki.stale`` found a reason other than a missing source
  (a changed source, a newer note, an unverifiable fingerprint). Reviewed
  pages get ``reviewed-stale`` instead.
- ``broken-source``: a cited source is gone (a ``missing:`` reason).
- ``reviewed-stale``: the page is marked reviewed and ``wiki.stale`` found any
  reason. ``/vault-ask`` never refreshes these on its own.
- ``auto-filed``: ``filed_by: auto``, listed so a person can review it.
- ``orphan``: no vault note links ``[[<page>]]`` except the page itself and
  the wiki's own index and log files. Only pages whose ``updated`` date is
  inside ``--days`` are checked (default: all).
- ``page-unreadable``: the page or its staleness check could not be read.

and once for the wiki:

- ``index-drift``: the index files on disk differ from a fresh
  ``wiki.render_wiki_index``. This is the only row ``--apply`` fixes: it backs
  up the current index files and rebuilds them under the wiki lock.

Every other row is report-only (``unresolved``, confidence 0.0).

The wiki folder comes from the config file, because ``scan`` receives only
the sessions and insights folders. The file is read at call time from
``Path.home()``, like the dispatcher's own config read; a missing file means
the default folder. A wiki that is turned off, or a folder that does not
exist yet, reports nothing. A corrupt config or an invalid ``wiki_folder``
raises, so the dispatcher shows a crashed check instead of a clean one.

The check syncs the vault index first (like ``/vault-ask``): index lines come
from the notes table, and a stale table would invent drift.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import sys
from pathlib import Path

from . import Issue, Result

_HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

NAME = "wiki-pages"
DESCRIPTION = (
    "Report stale, broken-source, orphan, auto-filed and reviewed-stale wiki "
    "pages, and rebuild a drifted wiki index"
)
# Staleness and drift are undated; --days limits only the orphan scan.
DEFAULT_WINDOW_DAYS = 9999
OPT_IN = False

# ``[[name]]``, ``[[name|alias]]``, ``[[name#heading]]``, ``[[folder/name]]``.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]{1,300})(?:[|#][^\[\]]{0,300})?\]\]")
_WIKI_OWN_FILES = re.compile(r"^(index(-.+)?|log-\d{4})\.md$")


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


def _context(vault_path: str, sessions_folder: str, insights_folder: str):
    """The wiki context, or None when the wiki is off or not created yet."""
    from obsidian_utils import indexed_folders
    import vault_index

    cfg = _read_config()
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


def _linked_stems(vault: Path, wiki_root: Path) -> dict:
    """``{stem: set of linking files}`` for every wikilink in the vault, minus
    links from the wiki's own index and log files."""
    links: dict = {}
    for f in vault.rglob("*.md"):
        if f.parent == wiki_root and _WIKI_OWN_FILES.match(f.name):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for target in _WIKILINK_RE.findall(text):
            stem = target.strip().rsplit("/", 1)[-1]
            if stem.endswith(".md"):
                stem = stem[:-3]
            links.setdefault(stem, set()).add(f)
    return links


def _parse_date(value) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _index_drift(ctx: dict, wiki_root: Path) -> dict:
    import wiki

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
        if f.name in want:
            continue
        try:
            if wiki.read_page(f)[0].get("type") == wiki.INDEX_TYPE:
                extra.append(f.name)
        except (OSError, ValueError, wiki.WikiRefusal):
            continue
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
    vault_index.ensure_index(ctx["vault"], ctx["folders"], db_path=ctx["db"])
    vault = Path(ctx["vault"])
    wiki_root = vault / ctx["wiki_folder"]
    roots = [str(vault / f) for f in ctx["folders"]]
    cutoff = _dt.date.today() - _dt.timedelta(days=days)
    needle = (project or "").strip().lower()
    links = None
    issues: list = []

    for page in sorted((wiki_root / "queries").rglob("*.md")):
        try:
            meta, _body = wiki.read_page(page)
        except (OSError, ValueError, wiki.WikiRefusal) as exc:
            if not needle:
                issues.append(_row(page, "", "page-unreadable", f"cannot read the page: {exc}"))
            continue
        if meta.get("type") != wiki.PAGE_TYPE:
            continue
        projects = meta.get("projects") if isinstance(meta.get("projects"), list) else []
        proj = ",".join(str(p) for p in projects)
        if needle and not any(needle in str(p).lower() for p in projects):
            continue
        try:
            reasons = wiki.stale(ctx["db"], page, roots)["reasons"]
        except Exception as exc:  # noqa: BLE001 -- one bad page must not hide the rest
            issues.append(_row(page, proj, "page-unreadable", f"staleness check failed: {exc}"))
            reasons = []
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
        if meta.get("filed_by") == "auto":
            issues.append(_row(page, proj, "auto-filed",
                               f"filed automatically by {meta.get('caller') or 'an unnamed caller'}"))
        updated = _parse_date(meta.get("updated"))
        if updated is None or updated >= cutoff:
            if links is None:
                links = _linked_stems(vault, wiki_root)
            if not (links.get(page.stem, set()) - {page}):
                issues.append(_row(page, proj, "orphan", "no vault note links this page"))

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
        try:
            stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
            backup = Path(os.path.expanduser(backup_root)) / NAME / stamp
            backup.mkdir(parents=True, exist_ok=True)
            for f in [wiki_root / "index.md", *wiki_root.glob("index-*.md")]:
                if f.is_file():
                    shutil.copy2(f, backup / f.name)
            wiki.rebuild_wiki_index(ctx)
            results.append(Result(check=NAME, note_path=i.note_path, status="applied",
                                  backup_path=str(backup)))
        except Exception as exc:  # noqa: BLE001
            results.append(Result(check=NAME, note_path=i.note_path, status="error", error=str(exc)))
        finally:
            _release_lock(lock)
    return results
