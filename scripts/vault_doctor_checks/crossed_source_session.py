"""vault_doctor check: detect notes whose ``source_session`` and
``source_session_note`` disagree about which session produced them (#330).

Background: ``get_session_context()`` used to resolve the current session
by scanning ``~/.claude/projects/<project>/*.jsonl`` for the newest-mtime
transcript, which is not a stable key when two sessions in the same repo
are both live. A retro or insight note calls ``get_session_context()``
more than once while it is being written (once for ``source_session``,
again for the ``source_session_note`` wikilink), and if the newest-mtime
winner changed between those two calls the note ends up stamped with two
DIFFERENT sessions' identities. Task 2 of #330 makes resolution stable
within a session and task 5 stops NEW writes from crossing, but neither
touches notes already on disk. This check finds those.

Detection (crossed only — see "Deliberately not reported" below):

  1. The note has a non-empty ``source_session`` that is not the literal
     string ``"unknown"``.
  2. The note has a ``source_session_note`` wikilink (``[[stem]]``).
  3. That wikilink resolves to a note in ``sessions_folder`` that EXISTS.
  4. That target note's own ``session_id`` frontmatter is present and
     DIFFERS from the source note's ``source_session``.

All four must hold. This is a hard, deterministic fact once the target
note is read — not a heuristic guess — so it is reported at high
confidence (0.95) even though ``apply()`` refuses to repair it (see below).

Deliberately NOT reported: a ``source_session_note`` wikilink whose target
does not exist at all (a "dangling" link). The live vault has 40 of those,
almost all snapshots written by ``obsidian_context_snapshot.py`` at
PreCompact — which fires BEFORE SessionEnd writes the parent session note,
so the backlink is a deliberate forward reference, not damage. Reporting
those here would bury the one real signal (a crossing) in 40 rows of
expected, harmless noise. ``snapshot-integrity`` and issue #214 already own
that space. See ``docs/plans/330-session-id-crossing.md``.

Where this scans: both ``insights_folder`` and ``sessions_folder`` are
scanned for the STAMPED fields (``source_session`` / ``source_session_note``)
— in practice only insight/decision/error-fix notes carry them today (see
``templates/insight.md``, ``templates/decision.md``,
``templates/error-fix.md``), but scanning both folders costs one extra
glob and does not depend on that staying true. Wikilink TARGETS are
resolved by indexing ``sessions_folder`` ONLY: ``source_session_note``
always names a session note (the note the retro/insight/error-fix was
captured FROM), never another insight, so there is nothing to gain — and a
name collision to risk — by also indexing ``insights_folder`` as a
resolution target.

``apply()`` never writes: see its docstring for why an auto-repair here
would be actively dangerous, not just unimplemented.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import Issue, Result

# Path bootstrap: add hooks/ so we can import the plugin's shared frontmatter
# parser. Same pattern as project_name_normalization.py.
_HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))
from obsidian_utils import read_note_metadata  # noqa: E402

NAME = "crossed-source-session"
DESCRIPTION = (
    "Detect notes whose source_session and source_session_note wikilink "
    "target disagree about which session produced them (#330)"
)
# The damage is historic and undated — a note crossed 4 months ago is exactly
# as mis-attributed as one crossed today — so this check scans the whole
# vault regardless of --days (mirrors memory_index.py's DEFAULT_WINDOW_DAYS
# comment for the same reason).
DEFAULT_WINDOW_DAYS = 9999  # unbounded — the damage is historic; scan all notes

# Confidence for a crossed-source-session finding. Deliberately high (not
# 0.0/"unresolved" like a WARN-only guess) because the finding itself is a
# deterministic fact: the note names one session_id and its own backlink
# resolves to a note that says a different one. What is genuinely unknown
# is which of the two ids is correct — that is why apply() still refuses to
# write (see apply()'s docstring) — but the DETECTION is not a guess, and a
# confidence of 0.0 would make it vanish under any --min-confidence > 0,
# which is not the outcome we want for the one real crossing in the vault.
_CONFIDENCE = 0.95


def _wikilink_stem(raw: str) -> str:
    """Strip a ``[[stem]]`` wikilink (and any leftover quoting) down to
    ``stem``. ``read_note_metadata`` already strips a SURROUNDING pair of
    quotes from the raw scalar (frontmatter written as
    ``source_session_note: "[[stem]]"`` comes back as ``[[stem]]``), so only
    the brackets remain to strip here.
    """
    val = raw.strip()
    if val.startswith("[[") and val.endswith("]]"):
        val = val[2:-2]
    return val.strip().strip('"').strip("'").strip()


def _list_md_files(folder_path: Path) -> tuple[list[Path], str | None]:
    """List the ``*.md`` files directly in ``folder_path``, or ``([], error)``
    if the directory itself could not be listed.

    ``Path.glob()`` silently SWALLOWS ``OSError`` raised while walking a
    directory (an unreadable subdirectory just vanishes from the results —
    no exception, no partial-scan signal), while ``Path.iterdir()`` raises
    it. Using ``glob("*.md")`` here would make an unreadable
    ``sessions_folder`` look like an EMPTY one: the stem index in
    ``_index_sessions_by_stem`` would come back empty, every resolvable
    ``source_session_note`` link would look dangling (which this check
    deliberately does not report — see the module docstring), and the whole
    check would report CLEAN having actually scanned nothing. An empty
    directory and a directory that could not be read must never look the
    same. Same precedent as ``session_coverage.py``'s ``_index_session_notes``,
    which uses ``iterdir()`` for exactly this reason.
    """
    try:
        entries = sorted(
            p for p in folder_path.iterdir()
            if p.name.endswith(".md") and p.is_file()
        )
    except OSError as exc:
        return [], str(exc)
    return entries, None


def _index_sessions_by_stem(
    vault_root: Path, sessions_folder: str
) -> tuple[dict[str, Path], int, str | None]:
    """Map ``stem -> path`` for every ``*.md`` in ``sessions_folder``.

    Used to resolve a ``source_session_note`` wikilink to a file without a
    second glob per note.

    Returns ``(index, n_listed, list_error)``. ``list_error`` is set (and
    ``index`` empty) when the directory itself could not be listed — the
    caller must treat that as "the index is not trustworthy", never as "no
    session notes exist" (see ``_list_md_files``).
    """
    index: dict[str, Path] = {}
    folder_path = vault_root / sessions_folder
    if not folder_path.is_dir():
        return index, 0, None
    entries, err = _list_md_files(folder_path)
    if err is not None:
        return index, 0, err
    for md_file in entries:
        index[md_file.stem] = md_file
    return index, len(entries), None


def scan(
    vault_path: str,
    sessions_folder: str,
    insights_folder: str,
    days: int,
    project: str | None = None,
) -> list[Issue]:
    """Find notes whose source_session disagrees with their
    source_session_note wikilink target's own session_id.
    """
    vault_root = Path(vault_path)
    issues: list[Issue] = []

    sessions_by_stem, n_indexed, index_err = _index_sessions_by_stem(
        vault_root, sessions_folder
    )
    if index_err is not None:
        print(
            f"[{NAME}] WARNING: could not list session notes under "
            f"{vault_root / sessions_folder}: {index_err}; every "
            f"source_session_note wikilink will read as unresolvable this "
            f"run (not reported — dangling links are excluded by design — "
            f"but a genuine crossing under an unreadable target could be "
            f"missed)",
            file=sys.stderr,
        )

    n_scanned = 0
    n_skipped = 0  # source note itself unreadable/unparsable
    n_no_stamp = 0  # no usable source_session / source_session_note to check
    n_dangling = 0  # link resolves to nothing (by design, not reported)
    n_target_unreadable = 0  # link resolves to a note we couldn't read/parse
    n_clean = 0  # checked and matched

    for folder in (insights_folder, sessions_folder):
        folder_path = vault_root / folder
        if not folder_path.is_dir():
            continue

        entries, folder_err = _list_md_files(folder_path)
        if folder_err is not None:
            print(
                f"[{NAME}] WARNING: could not list {folder_path}: "
                f"{folder_err}; notes in this folder are NOT scanned this "
                f"run",
                file=sys.stderr,
            )
            continue

        for md_file in entries:
            if project and f"-{project}-" not in md_file.name:
                continue

            n_scanned += 1
            meta = read_note_metadata(str(md_file))
            if not meta:
                n_skipped += 1
                print(
                    f"[{NAME}] WARNING: could not parse {md_file}; skipped "
                    f"(not counted as clean, not counted as crossed)",
                    file=sys.stderr,
                )
                continue

            source_session = (meta.get("source_session") or "").strip()
            if not source_session or source_session == "unknown":
                n_no_stamp += 1
                continue

            raw_link = meta.get("source_session_note")
            if not raw_link:
                n_no_stamp += 1
                continue
            stem = _wikilink_stem(raw_link)
            if not stem:
                n_no_stamp += 1
                continue

            target_path = sessions_by_stem.get(stem)
            if target_path is None:
                # Dangling link — deliberately not reported, see module
                # docstring ("Deliberately NOT reported"). Also the shape
                # every link takes when index_err fired above.
                n_dangling += 1
                continue

            target_meta = read_note_metadata(str(target_path))
            if not target_meta:
                # Target note exists but could not be read/parsed — cannot
                # compare, and unlike a dangling link this IS worth a
                # diagnostic: the note is present on disk, so "cannot tell"
                # is a scan gap, not the normal pre-SessionEnd shape.
                n_target_unreadable += 1
                print(
                    f"[{NAME}] WARNING: could not parse link target "
                    f"{target_path} (linked from {md_file}); cannot check "
                    f"for a crossing",
                    file=sys.stderr,
                )
                continue
            target_session_id = (target_meta.get("session_id") or "").strip()
            if not target_session_id:
                # Target exists and parsed, but carries no session_id we can
                # compare against — nothing to cross-check.
                n_no_stamp += 1
                continue

            if target_session_id == source_session:
                n_clean += 1
                continue

            issues.append(Issue(
                check=NAME,
                note_path=str(md_file),
                project=meta.get("project", "") or (project or ""),
                current_source=f"source_session: {source_session}",
                proposed_source=(
                    f"source_session_note target [[{stem}]] has "
                    f"session_id: {target_session_id}"
                ),
                reason=(
                    f"source_session ({source_session}) does not match the "
                    f"session_id ({target_session_id}) of the note its own "
                    f"source_session_note wikilink [[{stem}]] resolves to"
                ),
                confidence=_CONFIDENCE,
                extra={
                    "source_session": source_session,
                    "target_session_id": target_session_id,
                    "target_note": str(target_path),
                },
            ))

    # End-of-scan stderr summary, unconditional (even when nothing was
    # scanned) — a silent scan is indistinguishable from a scan that never
    # ran. The buckets partition n_scanned exactly (n_crossed is len(issues)).
    n_crossed = len(issues)
    print(
        f"[{NAME}] scanned {n_scanned} note(s) ({n_indexed} session note(s) "
        f"indexed as link targets): {n_crossed} crossed, {n_clean} clean, "
        f"{n_no_stamp} no source_session/link to check, {n_dangling} "
        f"dangling link (not reported), {n_target_unreadable} target "
        f"unreadable, {n_skipped} source note unreadable",
        file=sys.stderr,
    )

    return issues


def apply(issues: list[Issue], backup_root: str) -> list[Result]:
    """Never repairs anything — always returns ``status="skipped"``.

    Deciding WHICH of ``source_session`` and the wikilink target's
    ``session_id`` is the correct attribution needs evidence this check does
    not have (which of the two resolutions during the original write was
    the transient, wrong one). Auto-picking either side could rewrite a
    genuine, correct attribution into a wrong one. This is a report-only
    check: a human reads the two ids and the two note paths and decides.
    ``backup_root`` is accepted for interface parity with every other
    check's ``apply()`` but is never used — nothing is ever backed up
    because nothing is ever written.
    """
    return [
        Result(
            check=NAME,
            note_path=issue.note_path,
            status="skipped",
            error=(
                "crossed-source-session is detection-only: choosing between "
                f"source_session={issue.extra.get('source_session')!r} and "
                f"the linked note's session_id="
                f"{issue.extra.get('target_session_id')!r} needs evidence "
                "this check does not have, so no repair is attempted. "
                "Review both notes by hand: "
                f"{issue.note_path} vs {issue.extra.get('target_note')}"
            ),
        )
        for issue in issues
    ]
