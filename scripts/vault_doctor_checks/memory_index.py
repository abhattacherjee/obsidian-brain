"""vault_doctor check: detect drift between ``memory/`` files and ``MEMORY.md``.

Writing a Claude Code memory entry is two steps — create
``memory/<name>.md``, then add a pointer line to ``memory/MEMORY.md`` — and
only the first is enforced by anything. Nothing fails when the second is
skipped, and the symptom (an entry that silently never gets recalled) is
invisible at write time and indefinitely afterward, because ``MEMORY.md`` is
the only entry point loaded into context each session.

``MEMORY.md`` also has a bounded read budget while the store behind it is
unbounded, so an unindexed entry is not a one-off mistake to fix once — it is
the steady state. See obsidian-brain #308.

Scope
-----
The store lives at ``~/.claude/projects/<project-dir>/memory/``, i.e. outside
the Obsidian vault. That is adjacent to but outside vault-doctor's usual
subject; the precedent is ``session-coverage``, which already walks
``~/.claude/projects/`` for JSONLs. The vault path is not read by this check.

``OPT_IN = True``: a full sweep of a real machine surfaces hundreds of rows
(26 stores / 63 orphans in one store alone at the time of writing), which
would drown every other check in the default sweep. Run it deliberately with
``--check memory-index``.

Reachability
------------
Reachability is computed the way recall actually sees it, and the two
directions are deliberately asymmetric:

- **Reachability** (does anything point AT this entry?) counts markdown links
  ``](name.md)``, ``[[wikilinks]]`` and bare ``name.md`` mentions. Being
  generous here matters: under-counting a reference invents an orphan that
  isn't one. A first pass on #308 that counted markdown links only reported
  98 orphans; adding wikilinks corrected it to 92.
- **Dangling** (does this pointer target a file that doesn't exist?) counts
  ONLY markdown links. ``MEMORY.md`` legitimately carries ``[[wikilinks]]``
  to Obsidian *vault* notes, which are not memory files and must not read as
  dangling — the live store had exactly one such link. Bare ``name.md``
  mentions are excluded for the same reason (``CLAUDE.md``, ``README.md`` and
  friends appear in prose constantly).

Orphans are reported transitively: BFS from ``MEMORY.md`` through memory-file
links. An entry reachable only from another orphan is still unreachable from
the index, and is flagged — with a distinct signal_class, because a human
following links from a recalled note could still land on it.

Report-only
-----------
Every issue is ``unresolved`` (confidence 0.0), so ``--apply`` never touches a
memory store. Writing the missing index line requires composing a one-line
hook for the entry, which is authoring work, not a mechanical repair; and the
right fix for a dangling pointer may be to restore the file rather than to
delete the line. Note that ``--min-confidence`` above 0.0 therefore hides
every row from this check.

Exit codes (via the dispatcher)
-------------------------------
- ``0`` clean, ``1`` drift found.
- ``2`` could not read a store: an unreadable ``MEMORY.md`` or ``memory/``
  directory raises out of ``scan()``, which the dispatcher contains as a
  crashed check. This is deliberate — without the index, every entry in that
  store would be reported as an orphan, so failing loud beats emitting a
  storeful of false positives.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from . import Issue, Result

NAME = "memory-index"
DESCRIPTION = (
    "Detect memory-index drift: memory/*.md entries unreachable from "
    "MEMORY.md, dangling index pointers, and MEMORY.md size against budget"
)
# The damage is cumulative and undated — an entry orphaned 4 months ago is
# exactly as invisible to recall as one orphaned today. --days is ignored.
DEFAULT_WINDOW_DAYS = 9999
OPT_IN = True

# MEMORY.md is loaded into context every session, so its size is bounded
# while the store behind it is not. Both figures are from #308: the read
# budget is ~24 KB, and compaction is requested around 17 KB.
INDEX_SIZE_SOFT_LIMIT_BYTES = 17_000
INDEX_SIZE_HARD_LIMIT_BYTES = 24_000

INDEX_NAME = "MEMORY.md"

# ``](name.md)`` / ``](./dir/name.md)`` — an explicit pointer at a local file.
_MD_LINK_RE = re.compile(r"\]\(\s*([^)\s]+\.md)\s*\)")
# ``[[name]]``, ``[[name|alias]]``, ``[[name#heading]]``.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:[|#][^\[\]]*)?\]\]")
# A bare filename anywhere in the prose. Deliberately unanchored: every hit is
# intersected with the store's real filenames before it counts, so a false
# match costs nothing while a missed one would invent an orphan.
_BARE_MD_RE = re.compile(r"[A-Za-z0-9][\w.-]*\.md")


class MemoryStoreUnreadable(Exception):
    """A memory store exists but could not be read.

    Raised rather than downgraded to an Issue: reachability is computed from
    MEMORY.md, so without it every entry in the store looks orphaned.
    """


def _link_targets(text: str) -> set[str]:
    """Every ``.md`` basename this text could be pointing at (generous)."""
    out: set[str] = set()
    for m in _MD_LINK_RE.findall(text):
        out.add(Path(m).name)
    for m in _WIKILINK_RE.findall(text):
        name = m.strip()
        if not name:
            continue
        out.add(name if name.endswith(".md") else f"{name}.md")
    for m in _BARE_MD_RE.findall(text):
        out.add(Path(m).name)
    return out


def _md_link_targets(text: str) -> set[str]:
    """Only explicit ``](name.md)`` pointers (conservative — dangling only)."""
    return {Path(m).name for m in _MD_LINK_RE.findall(text)}


def _read_text(path: Path) -> str:
    """Read a file as UTF-8, tolerating undecodable bytes.

    Encoding damage is ``encoding-corruption``'s subject, not this check's;
    replacing a bad byte cannot turn a real link into a missing one.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _project_matches(dir_name: str, project: str) -> bool:
    """Case-insensitive substring match of --project against the store's dir.

    Project dirs are Claude Code's path encoding of the cwd (e.g.
    ``-Users-me-dev-obsidian-brain``), so there is no lossless short name to
    match exactly — ``--project obsidian-brain`` is a substring filter.
    """
    return project.strip().lower() in dir_name.lower()


def _issue(
    note_path: Path,
    project: str,
    current: str,
    proposed: str,
    reason: str,
    signal_class: str,
    extra: dict | None = None,
) -> Issue:
    payload = {"unresolved": True, "signal_class": signal_class}
    if extra:
        payload.update(extra)
    return Issue(
        check=NAME,
        note_path=str(note_path),
        project=project,
        current_source=current,
        proposed_source=proposed,
        reason=reason,
        confidence=0.0,
        extra=payload,
    )


def _scan_store(store: Path, project: str) -> list[Issue]:
    """Scan one ``memory/`` directory. Raises MemoryStoreUnreadable on I/O."""
    try:
        entries = sorted(
            p for p in store.glob("*.md")
            if p.name != INDEX_NAME and p.is_file()
        )
    except OSError as exc:
        raise MemoryStoreUnreadable(f"{store}: {exc}") from exc

    if not entries:
        # An empty store has nothing to be orphaned and no index to outgrow —
        # every project dir Claude Code ever created carries one, so staying
        # silent here is what keeps the report about real drift.
        return []

    entry_names = {p.name for p in entries}
    issues: list[Issue] = []
    index_path = store / INDEX_NAME

    if not index_path.exists():
        # One row, not one per entry: with no index at all the whole store is
        # unreachable, and N rows would say the same thing N times.
        return [_issue(
            index_path,
            project,
            f"{len(entry_names)} entr(y|ies), no {INDEX_NAME}",
            f"create {INDEX_NAME} with a pointer line per entry",
            f"{store} has {len(entry_names)} memory file(s) but no "
            f"{INDEX_NAME} — nothing in this store can ever be recalled",
            "index-missing",
            {"entry_count": len(entry_names)},
        )]

    try:
        index_text = _read_text(index_path)
        index_size = index_path.stat().st_size
    except (OSError, ValueError) as exc:
        raise MemoryStoreUnreadable(f"{index_path}: {exc}") from exc

    # Outbound links per entry, and the set of entries we could not read.
    outbound: dict[str, set[str]] = {}
    unreadable: list[Path] = []
    for entry in entries:
        try:
            outbound[entry.name] = _link_targets(_read_text(entry))
        except OSError as exc:
            unreadable.append(entry)
            outbound[entry.name] = set()
            issues.append(_issue(
                entry,
                project,
                "unreadable",
                "check file permissions and disk health",
                f"could not read memory entry: {exc}",
                "entry-unreadable",
                {"error": str(exc)},
            ))

    # Transitive reachability: BFS from MEMORY.md through memory-file links.
    index_targets = _link_targets(index_text)
    reachable: set[str] = set()
    frontier = [n for n in index_targets if n in entry_names]
    while frontier:
        name = frontier.pop()
        if name in reachable:
            continue
        reachable.add(name)
        for target in outbound.get(name, ()):
            if target in entry_names and target not in reachable:
                frontier.append(target)

    # Inbound-from-anywhere, used only to separate "fully isolated" from
    # "linked, but only from something the index cannot reach".
    inbound: set[str] = set()
    for name, targets in outbound.items():
        for target in targets:
            if target in entry_names and target != name:
                inbound.add(target)

    directly_indexed = {n for n in index_targets if n in entry_names}

    for entry in entries:
        if entry.name in reachable:
            continue
        isolated = entry.name not in inbound
        if isolated:
            signal_class = "orphan-isolated"
            reason = (
                f"no pointer in {INDEX_NAME} and no inbound link from any "
                f"other memory file — unreachable by recall"
            )
        else:
            signal_class = "orphan-unreachable"
            reason = (
                f"linked only from memory file(s) that are themselves "
                f"unreachable from {INDEX_NAME} — recall cannot reach it "
                f"from the index"
            )
        issues.append(_issue(
            entry,
            project,
            f"not reachable from {INDEX_NAME}",
            f"add a pointer line to {INDEX_NAME}: "
            f"- [Title]({entry.name}) — hook",
            reason,
            signal_class,
        ))

    # Dangling: markdown-link pointers only (see the module docstring).
    for target in sorted(_md_link_targets(index_text)):
        if target == INDEX_NAME or target in entry_names:
            continue
        issues.append(_issue(
            store / target,
            project,
            f"{INDEX_NAME} points at {target}",
            f"restore {target}, or remove its line from {INDEX_NAME}",
            f"{INDEX_NAME} has a markdown-link pointer to {target}, which "
            f"does not exist in {store}",
            "index-dangling",
            {"index_path": str(index_path)},
        ))

    if index_size >= INDEX_SIZE_SOFT_LIMIT_BYTES:
        over_hard = index_size >= INDEX_SIZE_HARD_LIMIT_BYTES
        issues.append(_issue(
            index_path,
            project,
            f"{index_size} bytes",
            f"compact {INDEX_NAME} below "
            f"{INDEX_SIZE_SOFT_LIMIT_BYTES} bytes (group related pointers "
            f"onto one line)",
            (
                f"{INDEX_NAME} is {index_size} bytes, "
                + (
                    f"over the ~{INDEX_SIZE_HARD_LIMIT_BYTES}-byte read budget"
                    if over_hard
                    else f"over the ~{INDEX_SIZE_SOFT_LIMIT_BYTES}-byte "
                         f"compaction threshold "
                         f"(budget ~{INDEX_SIZE_HARD_LIMIT_BYTES})"
                )
            ),
            "index-oversize-hard" if over_hard else "index-oversize-soft",
            {
                "index_size_bytes": index_size,
                "soft_limit_bytes": INDEX_SIZE_SOFT_LIMIT_BYTES,
                "hard_limit_bytes": INDEX_SIZE_HARD_LIMIT_BYTES,
                "entry_count": len(entry_names),
                "indexed_count": len(directly_indexed),
            },
        ))

    return issues


def scan(
    vault_path: str,
    sessions_folder: str,
    insights_folder: str,
    days: int,
    project: str | None = None,
) -> list[Issue]:
    """Walk ``~/.claude/projects/*/memory/`` and report index drift.

    ``vault_path``/``sessions_folder``/``insights_folder``/``days`` are part
    of the check interface and are unused here — the store is outside the
    vault and the drift is undated.
    """
    # Path.home() reads $HOME on POSIX (tests monkeypatch it) and falls back
    # to pwd-database lookups when unset — sibling-module convention.
    projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.is_dir():
        print(
            "[memory-index] ~/.claude/projects not found; nothing to scan",
            file=sys.stderr,
        )
        return []

    issues: list[Issue] = []
    n_stores = 0
    n_skipped_by_project = 0
    for proj_dir in sorted(projects_root.iterdir()):
        store = proj_dir / "memory"
        if not store.is_dir():
            continue
        if project and not _project_matches(proj_dir.name, project):
            n_skipped_by_project += 1
            continue
        n_stores += 1
        issues.extend(_scan_store(store, proj_dir.name))

    print(
        f"[memory-index] scanned {n_stores} memory store(s)"
        + (
            f" ({n_skipped_by_project} filtered out by --project {project})"
            if project else ""
        )
        + f"; {len(issues)} issue(s)",
        file=sys.stderr,
    )
    return issues


def apply(issues: list[Issue], backup_root: str) -> list[Result]:
    """Report-only — every issue is unresolved, so this never runs via --apply.

    The dispatcher filters unresolved issues out before calling apply(). The
    implementation is kept honest (rather than raising) in case a caller
    invokes it directly: it reports every issue as unresolved and writes
    nothing.
    """
    return [
        Result(
            check=NAME,
            note_path=i.note_path,
            status="unresolved",
            error=(
                "memory-index is report-only: adding an index line requires "
                "authoring a one-line hook for the entry, and a dangling "
                "pointer may need the file restored rather than the line "
                "removed"
            ),
        )
        for i in issues
    ]
