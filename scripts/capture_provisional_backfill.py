#!/usr/bin/env python3
"""One-off: run #455 capture selection over auto notes saved before it (#455).

Auto notes captured before #455 have no status, so every one of them is in
the default /recall and /vault-ask pool. Per session, this keeps the 10 best
by merit (rubric v2 score, novelty) promoted and marks the rest
`status: provisional` with `demoted_by: backlog`. A note that was ever opened
(a search or tool-open access, a hand /compress update, or another
session's Read of it) stays promoted outside the cap. No note is deleted.

Running it again over the same window is safe: notes this tool selected
earlier (`selected_by: backlog`) are selected again, and notes the live
selection promoted are skipped but count against their session's cap of 10.

Usage:
    python3 scripts/capture_provisional_backfill.py                       # dry run
    python3 scripts/capture_provisional_backfill.py --scores scores_v2.json --score   # fill the score cache
    python3 scripts/capture_provisional_backfill.py --apply               # back up, then write

Options:
    --since YYYY-MM-DD   first capture day, inclusive (default 2026-10-18)
    --until YYYY-MM-DD   last capture day, inclusive (default 2026-10-23)
    --scores FILE        extra read-only scores, {note file name: 1-10}
                         (e.g. the spike's scores_v2.json)
    --cache FILE         where new Haiku scores are kept (default
                         ~/.claude/obsidian-brain/capture/backlog-scores.json)
    --score              score unscored notes with Haiku and save the cache;
                         changes no note
    --apply              score what is unscored, back up the DB and every
                         touched note, then write
    --json               print the report as JSON
    --db PATH            index DB (default: $OBSIDIAN_BRAIN_DB or
                         ~/.claude/obsidian-brain-vault.db)
    --backup-dir DIR     where --apply writes the DB copy
                         (default ~/.claude/obsidian-brain/)

The dry run reads only: it calls no model and writes nothing. Its counts use
score 5 for unscored notes and it prints their token estimate.

Exit codes: 0 ok, 1 refused or failed (no config or vault, no DB, unreadable
scores, backup failed; nothing written), 2 usage error.
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))

import capture_backlog  # noqa: E402
import capture_state  # noqa: E402
import obsidian_utils  # noqa: E402
import vault_index  # noqa: E402
from obsidian_utils import indexed_folders  # noqa: E402


def _day(text: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}")


def _report(planned: dict, mode: str) -> str:
    lines = [f"capture backlog ({mode}): {planned['total']} notes captured {planned['since']}..{planned['until']}",
             f"unscored: {planned['unscored']} (about {planned['token_estimate']} input tokens to score)"]
    cols = ("promoted", "provisional", "kept_opened")
    lines.append("project".ljust(32) + "".join(c.rjust(13) for c in cols))
    for name, row in sorted(planned["projects"].items()):
        lines.append(name[:32].ljust(32) + "".join(str(row[c]).rjust(13) for c in cols))
    lines.append("session".ljust(32) + "".join(c.rjust(13) for c in cols))
    for sid, row in sorted(planned["sessions"].items(), key=lambda kv: (kv[1]["project"], kv[0])):
        lines.append(f"{sid[:8]} {row['project']}"[:32].ljust(32) + "".join(str(row[c]).rjust(13) for c in cols))
    return "\n".join(lines)


def _public(planned: dict) -> dict:
    out = {k: v for k, v in planned.items() if k != "notes"}
    out["notes"] = [{"note": n["name"], "session": n["session"], "project": n["project"], "want": n["want"],
                     "why": n["why"], "merit": n["merit"], "score": n["score"]} for n in planned["notes"]]
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="capture_provisional_backfill.py",
                                     description="Run #455 capture selection over pre-#455 auto notes.")
    parser.add_argument("--since", type=_day, default=datetime.date(2026, 10, 18))
    parser.add_argument("--until", type=_day, default=datetime.date(2026, 10, 23))
    parser.add_argument("--scores", type=Path, default=None)
    parser.add_argument("--cache", type=Path, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--score", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--db", default=None)
    parser.add_argument("--backup-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.since > args.until:
        parser.error("--since is after --until")
    try:
        config = obsidian_utils.load_config()
        if not config.get("vault_path") or not Path(config["vault_path"]).is_dir():
            raise capture_backlog.BacklogError("no vault: run /obsidian-setup")
        db = args.db or vault_index._default_db_path()
        if not Path(db).is_file():
            raise capture_backlog.BacklogError(f"index DB not found: {db}")
        cache = args.cache or capture_state.capture_dir() / "backlog-scores.json"
        cached = capture_backlog.load_scores(cache)
        scores = {**capture_backlog.load_scores(args.scores), **cached}
        if args.apply:
            backup = capture_backlog.backup_db(db, args.backup_dir)
            vault_index.ensure_index(config["vault_path"], indexed_folders(config), db_path=db)
        if args.score or args.apply:
            notes = capture_backlog.notes_in_window(config, args.since, args.until)
            capture_backlog.score_missing(notes, scores, cache)
        planned = capture_backlog.plan(config, args.since, args.until, scores, db)
        result = capture_backlog.apply(config, planned, db) if args.apply else None
    except capture_backlog.BacklogError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- reported, never a traceback
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    label = "apply" if args.apply else "score" if args.score else "dry run"
    if args.json:
        out = _public(planned)
        if result is not None:
            out.update(result, db_backup=backup)
        print(json.dumps(out, indent=1, default=str))
    else:
        print(_report(planned, label))
        if result is not None:
            print(f"written: {result['written']}; errors: {len(result['errors'])}")
            print(f"DB backup: {backup}\nnote backup: {result['note_backup']}")
            for err in result["errors"]:
                print(f"  {err['note']}: {err['error']}")
    return 1 if result is not None and result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
