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


def _index_sessions_by_stem(vault_root: Path, sessions_folder: str) -> dict[str, Path]:
    """Map ``stem -> path`` for every ``*.md`` in ``sessions_folder``.

    Used to resolve a ``source_session_note`` wikilink to a file without a
    second glob per note.
    """
    index: dict[str, Path] = {}
    folder_path = vault_root / sessions_folder
    if not folder_path.is_dir():
        return index
    for md_file in folder_path.glob("*.md"):
        if md_file.is_file():
            index[md_file.stem] = md_file
    return index


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

    sessions_by_stem = _index_sessions_by_stem(vault_root, sessions_folder)

    for folder in (insights_folder, sessions_folder):
        folder_path = vault_root / folder
        if not folder_path.is_dir():
            continue

        for md_file in sorted(folder_path.glob("*.md")):
            if not md_file.is_file():
                continue
            if project and f"-{project}-" not in md_file.name:
                continue

            meta = read_note_metadata(str(md_file))
            if not meta:
                continue

            source_session = (meta.get("source_session") or "").strip()
            if not source_session or source_session == "unknown":
                continue

            raw_link = meta.get("source_session_note")
            if not raw_link:
                continue
            stem = _wikilink_stem(raw_link)
            if not stem:
                continue

            target_path = sessions_by_stem.get(stem)
            if target_path is None:
                # Dangling link — deliberately not reported, see module
                # docstring ("Deliberately NOT reported").
                continue

            target_meta = read_note_metadata(str(target_path)) or {}
            target_session_id = (target_meta.get("session_id") or "").strip()
            if not target_session_id:
                # Target exists but carries no session_id we can compare
                # against — nothing to cross-check.
                continue

            if target_session_id == source_session:
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
