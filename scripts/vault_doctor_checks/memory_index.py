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

``OPT_IN = True``: a full sweep of the author's machine produced 92 rows
across 7 projects (measured 2026-08-12), which would drown every other check
in the default sweep. Run it deliberately with ``--check memory-index``.

Reachability
------------
Reachability is computed the way recall actually sees it, and the two
directions are deliberately asymmetric:

- **Reachability** (does anything point AT this entry?) counts markdown links
  ``](name.md)``, ``[[wikilinks]]`` and bare ``name.md`` mentions. Being
  generous here matters: under-counting a reference invents an orphan that
  isn't one. A first pass on #308 that counted markdown links only reported
  98 orphans; adding wikilinks corrected it to 92 (both measured 2026-08-12).
- **Dangling** (does this pointer target a file that doesn't exist?) counts
  ONLY markdown links, and only ones that name a direct child of the store.
  ``MEMORY.md`` legitimately carries ``[[wikilinks]]`` to Obsidian *vault*
  notes, which are not memory files and must not read as dangling — the live
  store had exactly one such link. Bare ``name.md`` mentions are excluded for
  the same reason (``CLAUDE.md``, ``README.md`` and friends appear in prose
  constantly).

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
- ``2`` nothing could be read at all: ``scan()`` raises
  ``MemoryStoreUnreadable`` only when *every* store it selected failed, and
  the dispatcher contains that as a crashed check.
- One store failing while others succeed does **not** raise. That store's own
  findings are dropped — without its ``MEMORY.md`` every entry in it would
  read as an orphan — and it contributes a single ``store-unreadable`` row
  naming the store and the error. The other stores' findings survive, so the
  run is exit 1 with a loud attributable row rather than an empty report.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from collections import deque
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

# Every run below is bounded. An unbounded run backtracks from every start
# position, which is quadratic in the file size: dropping the {0,200} bound
# takes _BARE_MD_RE from 0.044 s to 8.26 s on 80 KB of "a", and from 0.031 s
# to 5.87 s on 80 KB of "a." (measured here). This is hardening, not a bug
# anyone is hitting — the longest [\w.-] run across the author's 1,112 real
# memory files is 79 characters. 300 and 200 are far above any real memory
# filename or link target.

# An explicit pointer at a local file, in the CommonMark forms that name one:
# ``](name.md)``, ``](./name.md)``, ``](name.md "Title")``, ``](<name.md>)``.
# The optional title and angle brackets are not decoration — without them a
# pointer at a missing file produces no dangling row at all, which reads as
# clean.
_MD_LINK_RE = re.compile(
    r"\]\(\s*<?([^)<>\s]{1,300}\.md)>?"
    r"""(?:\s+(?:"[^"]{0,300}"|'[^']{0,300}'|\([^)]{0,300}\)))?\s*\)"""
)
# ``[[name]]``, ``[[name|alias]]``, ``[[name#heading]]``.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]{1,300})(?:[|#][^\[\]]{0,300})?\]\]")
# A bare filename anywhere in the prose. Deliberately unanchored: every hit is
# intersected with the store's real filenames before it counts, so a false
# match costs nothing while a missed one would invent an orphan. A name inside
# a path (``docs/archive/note.md``) counts, and so does one wrapped in
# underscore emphasis (``_note.md_``). The {0,200} bound is what keeps this
# linear; there is deliberately no lookbehind, because one would reject the
# ``_note.md_`` start position and invent an orphan for a note that really is
# mentioned.
_BARE_MD_RE = re.compile(r"[A-Za-z0-9][\w.-]{0,200}\.md")


class MemoryStoreUnreadable(Exception):
    """No memory store could be read.

    Raised out of ``scan()`` only when every selected store failed. A single
    failing store among several becomes a ``store-unreadable`` row instead —
    raising would discard the other stores' findings along with it.
    """


def _link_targets(text: str) -> set[str]:
    """Every ``.md`` basename this text could be pointing at (generous)."""
    out: set[str] = set()
    for m in _MD_LINK_RE.findall(text):
        out.add(Path(m).name)
    for m in _WIKILINK_RE.findall(text):
        # Basename, matching the markdown-link path above: ``[[archive/a]]``
        # points at the same entry as ``[[a]]``, and treating the whole
        # subpath as a filename would invent an orphan.
        name = Path(m.strip()).name
        if not name:
            continue
        out.add(name if name.endswith(".md") else f"{name}.md")
    for m in _BARE_MD_RE.findall(text):
        out.add(Path(m).name)
    return out


def _md_child_link_targets(text: str) -> set[str]:
    """Markdown-link targets naming a direct child of the store (dangling only).

    A leading ``./`` is stripped first: ``](./name.md)`` names a direct child
    exactly as ``](name.md)`` does, and dropping it left a real dangling
    pointer unreported.

    After that strip, a target still containing a slash is about something
    else — an external URL (``https://…/README.md``), a file above the store
    (``../outside.md``) or one in a subdirectory (``sub/deep.md``). Taking its
    basename would invent a dangling row at a path the index never claimed;
    the ``reference`` memory type is defined as pointers to external
    resources, so index lines holding URLs are expected. Recursion into
    subdirectories is deliberately not added: the store is flat by
    convention, one MEMORY.md beside its entries.
    """
    out: set[str] = set()
    for m in _MD_LINK_RE.findall(text):
        target = m[2:] if m.startswith("./") else m
        if "/" in target or "\\" in target:
            continue
        out.add(target)
    return out


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
        # iterdir(), not glob(): glob() swallows the underlying scandir
        # OSError and yields nothing, so an unreadable store would report as
        # empty and the run would exit 0 "clean".
        entries = sorted(
            p for p in store.iterdir()
            if p.suffix == ".md" and p.name != INDEX_NAME and p.is_file()
        )
    except OSError as exc:
        raise MemoryStoreUnreadable(f"{store}: {exc}") from exc

    index_path = store / INDEX_NAME
    index_exists = index_path.exists()

    if not entries and not index_exists:
        # An empty store has nothing to be orphaned and no index to outgrow —
        # every project dir Claude Code ever created carries one, so staying
        # silent here is what keeps the report about real drift.
        return []

    issues: list[Issue] = []

    def index_missing_row() -> list[Issue]:
        # One row, not one per entry: with no index at all the whole store is
        # unreachable, and N rows would say the same thing N times.
        return [_issue(
            index_path,
            project,
            f"{len(entries)} entr(y|ies), no {INDEX_NAME}",
            f"create {INDEX_NAME} with a pointer line per entry",
            f"{store} has {len(entries)} memory file(s) but no "
            f"{INDEX_NAME} — nothing in this store can ever be recalled",
            "index-missing",
            {"entry_count": len(entries)},
        )]

    if not index_exists:
        return index_missing_row()

    try:
        # Strict, unlike the entries below. A replaced byte in the index
        # silently inverts the verdict: a UTF-16 MEMORY.md decodes to
        # mojibake that matches no link at all, which reports every entry in
        # the store as an orphan and nothing as dangling. Failing here costs
        # one store (see scan()); guessing costs the whole store's report.
        index_text = index_path.read_text(encoding="utf-8")
        index_size = index_path.stat().st_size
    except FileNotFoundError:
        # Deleted between the exists() check above and this read. The store
        # is now entries with no index, which is the index-missing row, not
        # an unreadable store.
        return index_missing_row() if entries else []
    except (OSError, ValueError) as exc:
        # UnicodeDecodeError is a ValueError.
        raise MemoryStoreUnreadable(f"{index_path}: {exc}") from exc

    # Outbound links per entry. ``degraded`` holds entries we could not read
    # in full, which makes every orphan verdict in this store provisional.
    outbound: dict[str, set[str]] = {}
    degraded: list[str] = []
    # Entries that got an entry-unreadable row. They stay in entry_names so
    # an index pointer at them is not mistaken for a dangling one, but they
    # get no orphan row on top: one broken file is one finding, and "add a
    # pointer line" is not actionable while the file cannot be read.
    unreadable_names: set[str] = set()
    readable_entries: list[Path] = []
    for entry in entries:
        try:
            raw = entry.read_bytes()
        except FileNotFoundError:
            # Deleted between the listing and the read. Reporting it would
            # produce an "unreadable" row telling the user to check disk
            # health plus an orphan row, both about a file that is gone.
            continue
        except (OSError, ValueError) as exc:
            # ValueError for symmetry with the index read above: if that read
            # is ever tightened further, one bad entry must stay one row
            # rather than crashing the whole check.
            readable_entries.append(entry)
            degraded.append(entry.name)
            unreadable_names.add(entry.name)
            outbound[entry.name] = set()
            issues.append(_issue(
                entry,
                project,
                "unreadable",
                "check file permissions and disk health",
                f"could not read memory entry: {exc}",
                "entry-unreadable",
                {"read_error": str(exc)},
            ))
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            # Replacement keeps the rest of the file usable, but it can
            # destroy a link, so say so rather than degrading in silence.
            text = raw.decode("utf-8", errors="replace")
            degraded.append(entry.name)
            issues.append(_issue(
                entry,
                project,
                f"undecodable byte at offset {exc.start}",
                "re-save the entry as UTF-8",
                f"memory entry is not valid UTF-8 (byte {exc.start}); it was "
                f"read with the bad bytes replaced, so a link inside it may "
                f"have been destroyed and entries reachable only through it "
                f"may be reported as orphans",
                "entry-undecodable",
                {"byte_offset": exc.start},
            ))
        readable_entries.append(entry)
        outbound[entry.name] = _link_targets(text)

    entries = readable_entries
    entry_names = {p.name for p in entries}

    # Dangling, split from case-only mismatches. Exact matching stays
    # primary — folding case globally would hide real dangling pointers on a
    # case-sensitive filesystem.
    by_lower: dict[str, list[str]] = {}
    for name in entry_names:
        by_lower.setdefault(name.lower(), []).append(name)
    for variants in by_lower.values():
        variants.sort()

    def resolve(target: str) -> str | None:
        """The entry this target names, exact first, then case-folded.

        Case folding applies to all three link kinds, not just markdown
        links: ``[[Their-Name]]`` is the documented way one memory links
        another, so that is exactly where a case-only difference lives, and
        on the case-insensitive filesystem this runs on it resolves. Calling
        such an entry isolated states something false — there IS a pointer.
        """
        if target in entry_names:
            return target
        variants = by_lower.get(target.lower())
        return variants[0] if variants else None
    dangling: list[str] = []
    case_mismatched: list[tuple[str, list[str]]] = []
    for target in sorted(_md_child_link_targets(index_text)):
        if target == INDEX_NAME or target in entry_names:
            continue
        same_name_other_case = sorted(by_lower.get(target.lower(), []))
        if same_name_other_case:
            case_mismatched.append((target, same_name_other_case))
        else:
            dangling.append(target)

    # Transitive reachability: BFS from MEMORY.md through memory-file links.
    index_targets = _link_targets(index_text)
    directly_indexed = {r for r in map(resolve, index_targets) if r}
    # An index with text in it that names no file at all is never healthy.
    # It gets one summary row on top of the per-entry rows, not instead of
    # them: the index read above is strict, so anything reaching here
    # decoded cleanly as UTF-8 and really does name no file, which means
    # every entry really is unreachable. The per-entry rows are correct and
    # are the actionable part — they say which files need a pointer line.
    # An index that is empty or whitespace only is ordinary drift, so it
    # gets no summary row.
    index_names_nothing = bool(entries) and bool(index_text.strip()) and not index_targets
    reachable: set[str] = set()
    frontier = deque(sorted(directly_indexed))
    while frontier:
        # popleft(), not pop(): FIFO is what makes this breadth-first, which
        # is what the docs say. The reachable set is the same either way.
        name = frontier.popleft()
        if name in reachable:
            continue
        reachable.add(name)
        for target in outbound.get(name, ()):
            resolved = resolve(target)
            if resolved and resolved not in reachable:
                frontier.append(resolved)

    # Inbound-from-anywhere, used only to separate "fully isolated" from
    # "linked, but only from something the index cannot reach".
    inbound: set[str] = set()
    for name, targets in outbound.items():
        for target in targets:
            resolved = resolve(target)
            if resolved and resolved != name:
                inbound.add(resolved)

    if index_names_nothing:
        issues.append(_issue(
            index_path,
            project,
            f"{index_size} bytes, no link to any file",
            f"add a pointer line per entry to {INDEX_NAME}; if the pointer "
            f"lines were there before, check {INDEX_NAME} for format damage",
            f"{INDEX_NAME} has text in it but names no file at all, so none "
            f"of this store's {len(entries)} memory file(s) can be reached "
            f"from the index — each one is listed separately below",
            "index-names-nothing",
            {"entry_count": len(entries)},
        ))

    for entry in entries:
        if entry.name in reachable or entry.name in unreadable_names:
            continue
        isolated = entry.name not in inbound
        if isolated and not degraded:
            signal_class = "orphan-isolated"
            reason = (
                f"no pointer in {INDEX_NAME} and no inbound link from any "
                f"other memory file — unreachable by recall"
            )
        elif isolated:
            # Some entry in this store could not be read, so its outbound
            # links are unknown and "nothing links this" is not provable.
            signal_class = "orphan-unreachable"
            reason = (
                f"no pointer in {INDEX_NAME} and no inbound link from the "
                f"memory files that could be read — {len(degraded)} entry "
                f"file(s) in this store could not be read in full, so this "
                f"classification is provisional"
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

    for target in dangling:
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

    for target, matches in case_mismatched:
        issues.append(_issue(
            store / target,
            project,
            f"{INDEX_NAME} points at {target}",
            f"change the pointer to {matches[0]}",
            f"{INDEX_NAME} points at {target} but the file on disk is "
            f"{', '.join(matches)} — the link resolves on a "
            f"case-insensitive filesystem (the default on macOS) and breaks "
            f"if this store is ever read on a case-sensitive one",
            "index-case-mismatch",
            {"index_path": str(index_path), "entry_names": matches},
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

    # --project "  " strips to "", and "" is a substring of every name, so
    # it would silently scan everything while looking like a filter. Decide
    # once, here, so the loop and the summary line agree.
    filtering = bool(project and project.strip())

    issues: list[Issue] = []
    n_stores = 0
    n_skipped_by_project = 0
    n_unreadable = 0
    for proj_dir in sorted(projects_root.iterdir()):
        store = proj_dir / "memory"
        try:
            # The try/except is the guard here, not the choice of os.stat.
            # With the project dir at 0o000 on Python 3.13.3, os.stat,
            # Path.is_dir, Path.exists and Path.iterdir all raise
            # PermissionError (verified); the pre-fix code called is_dir()
            # with no try at all, so the whole check died. os.stat is just
            # the most direct way to ask, and swapping it back for is_dir()
            # inside this try would be equally safe.
            is_dir = stat.S_ISDIR(os.stat(store).st_mode)
        except (FileNotFoundError, NotADirectoryError):
            # No store here. This is the ordinary case for most project dirs.
            continue
        except OSError:
            # Something is there but we cannot stat it. Treat it as a store
            # and let _scan_store name the real error in a store-unreadable
            # row, the same way it does for an unreadable store.
            is_dir = True
        if not is_dir:
            # A memory path that is not a directory is a misconfiguration the
            # user cannot see from an empty report, so name it.
            print(
                f"[memory-index] {store} exists but is not a directory; "
                f"skipped",
                file=sys.stderr,
            )
            continue
        if filtering and not _project_matches(proj_dir.name, project):
            n_skipped_by_project += 1
            continue
        n_stores += 1
        try:
            issues.extend(_scan_store(store, proj_dir.name))
        except MemoryStoreUnreadable as exc:
            # Drop this store's findings — without its index every entry in
            # it would read as an orphan — but keep every other store's, and
            # say loudly which store went missing from the report.
            n_unreadable += 1
            issues.append(_issue(
                store,
                proj_dir.name,
                "unreadable",
                "check permissions and encoding, then re-run",
                f"could not read this memory store, so it was left out of "
                f"the report: {exc}",
                "store-unreadable",
                {"read_error": str(exc)},
            ))

    if filtering and n_stores == 0:
        # A filter that matches nothing is not a clean result — the user
        # asked about something that is not there. Without this row the run
        # is exit 0 with an empty report, which reads as "no drift" for a
        # store that was never looked at. A row rather than JSON metadata,
        # so it shows up in every output format.
        issues.append(_issue(
            projects_root,
            project.strip(),
            f"--project {project} matched 0 of "
            f"{n_skipped_by_project} memory store(s)",
            f"check the filter against the directory names under "
            f"{projects_root}",
            f"--project {project!r} matched none of the "
            f"{n_skipped_by_project} memory store(s) found, so nothing was "
            f"scanned — an empty report here means the filter missed, not "
            f"that the stores are clean",
            "project-filter-matched-nothing",
            {
                "stores_scanned": n_stores,
                "stores_filtered_out": n_skipped_by_project,
            },
        ))

    if n_stores and n_unreadable == n_stores:
        # Nothing at all could be read. There is no partial report to
        # protect, so fail loud: the dispatcher turns this into exit 2.
        raise MemoryStoreUnreadable(
            f"none of the {n_stores} memory store(s) selected could be read"
        )

    print(
        f"[memory-index] scanned {n_stores} memory store(s)"
        + (
            f" ({n_skipped_by_project} filtered out by --project {project})"
            if filtering else ""
        )
        + (f", {n_unreadable} unreadable" if n_unreadable else "")
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
