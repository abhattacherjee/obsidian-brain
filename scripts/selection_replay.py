#!/usr/bin/env python3
"""Replay selection on ledger, index and hook-log copies; never read vault notes.

Ledger fields and vectors are final snapshots, not historical versions: a
superseding ledger row retains its creation ts. Only arrivals and access-log
stickiness have recoverable times. Hand edits, rejection times, issue closures,
and historical vectors cannot be reconstructed from these inputs.

Exit codes: 0 success, 1 unreadable/invalid inputs or no replayable sessions,
2 bad arguments or refused live paths.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import capture_select  # noqa: E402
import capture_state  # noqa: E402
import vault_index  # noqa: E402


LIMITATIONS = [
    "Final merged ledger fields and DB vectors apply at every run; creation ts does not date field changes.",
    "Only access-log stickiness is reconstructed; hand edits, issue closures and historical frontmatter are unavailable.",
    "Final merit averages sessions with nonempty final selection picks; sticky notes are outside those picks.",
    "Skipped sid counts count distinct saved-line prefixes; rows_without_vector counts distinct eligible ledger rows.",
    "Replay times are saved lines plus promoted/demoted lines: a slice whose selection moved nothing writes no line, so it is not replayed on its own; its rows join the next replayed selection.",
    "A replay time whose pool equals the previous replayed pool is skipped, because live runs one selection per slice.",
    "Ledger ts is fractional but log times are whole seconds: a row created later in the same second as a selection is counted in it.",
]


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"ERROR: {message}\n")


def _bonus(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("bonus must be a finite non-negative number") from None
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("bonus must be a finite non-negative number")
    return value


def _number(value, label: str) -> float:
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and math.isfinite(value))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"invalid {label}: expected finite epoch seconds or vector weight")
    return float(value)


def _read_ledger(path: Path, skipped: Counter) -> list[dict]:
    """The _merged_rows merge, including first-ts preservation and bad-row skips."""
    merged = {}
    with path.open(encoding="utf-8") as source:
        for line, raw in enumerate(source, 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                skipped["invalid_ledger_rows"] += 1
                continue
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                skipped["invalid_ledger_rows"] += 1
                continue
            cid = row["id"]
            if cid in merged:
                first_ts = merged[cid].get("ts")
                merged[cid].update(row)
                merged[cid]["ts"] = first_ts
            else:
                merged[cid] = row
            if "ts" not in merged[cid]:
                raise ValueError(f"{path}:{line}: ledger row {cid!r} has no creation ts")
    return list(merged.values())


def _row_in(row_ts: float, when: float) -> bool:
    """Ledger ts is fractional; hook-log times are truncated to whole seconds."""
    return math.floor(row_ts) <= when


def _read_logs(paths: list[Path]) -> tuple[float, dict[str, list[float]], dict[str, Counter], dict[str, list[float]]]:
    saved = defaultdict(list)
    move_times = defaultdict(list)
    moves = defaultdict(Counter)
    first = None
    for path in paths:
        with path.open(encoding="utf-8") as source:
            for line, raw in enumerate(source, 1):
                parts = raw.split(maxsplit=2)
                relevant = (len(parts) >= 2 and parts[1] == "Capture"
                            and re.search(r"\boutcome=(?:saved|promoted|demoted)\b", raw))
                try:
                    when = dt.datetime.fromisoformat(parts[0])
                    if when.tzinfo is None:
                        raise ValueError("timestamp needs a timezone")
                    timestamp = when.timestamp()
                except (IndexError, ValueError, OverflowError):
                    if relevant:
                        raise ValueError(f"{path}:{line}: invalid capture log timestamp") from None
                    continue
                first = timestamp if first is None else min(first, timestamp)
                if not relevant:
                    continue
                fields = dict(re.findall(r"(?:^|\s)(sid|outcome)=(\S+)", raw))
                sid, outcome = fields.get("sid", ""), fields.get("outcome", "")
                if len(sid) != 8:
                    raise ValueError(f"{path}:{line}: expected an eight-character sid")
                if outcome.startswith("saved"):
                    if not re.fullmatch(r"saved:\d+", outcome):
                        raise ValueError(f"{path}:{line}: invalid saved outcome")
                    saved[sid].append(timestamp)
                elif outcome in ("promoted", "demoted"):
                    detail = re.search(r"\bdetail=(.*)", raw)
                    values = dict(part.split("=", 1) for part in detail[1].strip().split(";")
                                  if "=" in part) if detail else {}
                    if not values.get("cid") or not values.get("why"):
                        raise ValueError(f"{path}:{line}: move needs cid and why")
                    move_times[sid].append(timestamp)
                    if values["why"] in ("pick", "displaced", "below-cap"):
                        moves[sid][values["cid"]] += 1
    if not saved:
        raise ValueError("0 sessions in window: no saved lines in hook logs")
    for times in saved.values():
        times.sort()
    return first, saved, moves, move_times


def _read_db(db: Path, basenames: set[str]) -> tuple[dict, dict]:
    vectors, sticky, seen = {}, {}, set()
    quoted_path = quote(str(db), safe="/")
    conn = sqlite3.connect('file:' + quoted_path + '?mode=ro', uri=True)  # noqa: vault-db-connect -- read only the explicit copy; no creation, migrations or vault access
    with closing(conn):
        for path, raw in conn.execute("SELECT path, tfidf_vector FROM notes"):
            if not isinstance(path, str):
                raise ValueError("notes.path must be text")
            name = Path(path).name
            if name not in basenames:
                continue
            if name in seen:
                raise ValueError(f"ambiguous DB basename: {name}")
            seen.add(name)
            if raw is None or raw == "":
                continue
            try:
                vector = json.loads(raw)
            except (ValueError, TypeError):
                raise ValueError(f"invalid vector JSON for {name}") from None
            if vector is None:
                continue
            if not isinstance(vector, dict):
                raise ValueError(f"invalid vector for {name}: expected an object")
            for weight in vector.values():
                _number(weight, f"vector for {name}")
            if any(vector.values()):
                vectors[name] = vector
        placeholders = ",".join("?" for _ in capture_select.STICKY_CONTEXTS)
        for path, timestamp in conn.execute(
                f"SELECT note_path, timestamp FROM access_log WHERE context_type IN ({placeholders})",
                capture_select.STICKY_CONTEXTS):
            if not isinstance(path, str):
                raise ValueError("access_log.note_path must be text")
            name = Path(path).name
            if name in basenames:
                timestamp = _number(timestamp, f"access timestamp for {name}")
                sticky[name] = min(timestamp, sticky.get(name, timestamp))
    return vectors, sticky


def _replay(items: list[dict], times: list[float], bonus: float) -> dict:
    """Selection moves only: becoming sticky freezes a note without adding a move."""
    promoted, counts, picks = set(), Counter(), []
    previous_pool = None
    for timestamp in times:
        pool = [item for item in items if _row_in(item["ts"], timestamp) and timestamp < item["sticky_at"]]
        pool_ids = {item["id"] for item in pool}
        if pool_ids == previous_pool:
            continue  # live runs one selection per slice; no new rows or stickiness change means no new run
        previous_pool = pool_ids
        picks = capture_select.select([
            {key: item[key] for key in ("id", "vector", "ts")}
            | {"merit": item["merit"] + (bonus if item["id"] in promoted else 0.0)}
            for item in pool
        ])
        picked = set(picks)
        for item in pool:
            cid = item["id"]
            if (cid in picked) != (cid in promoted):
                counts[cid] += 1
        promoted = picked
    base = {item["id"]: item["merit"] for item in items}
    return {"moves_by_note": dict(counts), "final_picks": picks,
            "final_merit": sum(base[cid] for cid in picks) / len(picks) if picks else None}


def _move_metrics(counts) -> dict:
    counts = list(counts)
    return {"notes_moved": sum(n > 0 for n in counts),
            "notes_moved_more_than_once": sum(n > 1 for n in counts),
            "notes_moved_more_than_twice": sum(n > 2 for n in counts),
            "max_moves_on_one_note": max(counts, default=0), "total_moves": sum(counts)}


def _evaluate(ledger: Path, db: Path, logs: list[Path], bonuses: list[float]) -> dict:
    skipped = Counter(dict(ambiguous_sid=0, unmapped_sid=0, rows_without_vector=0,
                           sessions_without_vectors=0, sessions_without_candidates=0,
                           invalid_ledger_rows=0))
    rows = _read_ledger(ledger, skipped)
    first_log, saved, live_moves, move_times = _read_logs(logs)
    by_session, prefixes, first_new = defaultdict(list), defaultdict(set), {}
    for row in rows:
        session = row.get("session_id")
        if not isinstance(session, str) or not session:
            if row.get("kind") == "new":
                raise ValueError(f"new ledger row {row['id']!r} needs session_id")
            continue
        prefixes[session[:8]].add(session)
        if row.get("kind") == "new":
            timestamp = _number(row.get("ts"), f"creation ts for {row['id']}")
            first_new[session] = min(timestamp, first_new.get(session, timestamp))
            if row.get("status") == "active":
                if not isinstance(row.get("note_path"), str) or not row["note_path"]:
                    raise ValueError(f"active new ledger row {row['id']!r} needs note_path")
                if row.get("type") is not None and not isinstance(row["type"], str):
                    raise ValueError(f"active new ledger row {row['id']!r} has invalid type")
                # plan_session also excludes the rejected home, independent of ledger status.
                if (not row.get("duplicate_of") and not row.get("superseded_by")
                        and "_rejected" not in Path(row["note_path"]).parts):
                    by_session[session].append(row)

    mapped = {}
    for sid, times in saved.items():
        matches = prefixes[sid]
        if len(matches) > 1:
            skipped["ambiguous_sid"] += 1
        elif not matches:
            skipped["unmapped_sid"] += 1
        else:
            mapped[next(iter(matches))] = sorted(set(times) | set(move_times.get(sid, ())))
    if not mapped:
        raise ValueError(f"0 sessions in window map to ledger; skipped={dict(skipped)}")

    basenames = {Path(row["note_path"]).name for session in mapped for row in by_session[session]}
    vectors, sticky = _read_db(db, basenames)
    arms = [{"bonus": bonus, "sessions": {}} for bonus in dict.fromkeys([0.0, *bonuses])]
    for session, times in sorted(mapped.items()):
        available = [row for row in by_session[session] if _row_in(row["ts"], times[-1])]
        if not available:
            skipped["sessions_without_candidates"] += 1
            continue
        items = []
        for row in available:
            name = Path(row["note_path"]).name
            if name not in vectors:
                skipped["rows_without_vector"] += 1
                continue
            items.append({"id": row["id"], "ts": row["ts"], "vector": vectors[name],
                          "merit": capture_select.merit(row.get("score"), row.get("novelty"), row.get("type")),
                          "sticky_at": sticky.get(name, math.inf)})
        if not items:
            skipped["sessions_without_vectors"] += 1
            continue
        for arm in arms:
            arm["sessions"][session] = _replay(items, times, arm["bonus"])
    if not arms[0]["sessions"]:
        raise ValueError(f"0 replayable sessions in window; skipped={dict(skipped)}")

    for arm in arms:
        sessions = arm["sessions"].values()
        merits = [s["final_merit"] for s in sessions if s["final_merit"] is not None]
        arm.update(_move_metrics(n for s in sessions for n in s["moves_by_note"].values()))
        arm.update(sessions_replayed=len(arm["sessions"]), sessions_with_final_picks=len(merits),
                   mean_final_merit=sum(merits) / len(merits) if merits else None)
    baseline = arms[0]["mean_final_merit"]
    for arm in arms:
        arm["merit_drop"] = (baseline - arm["mean_final_merit"]
                             if baseline is not None and arm["mean_final_merit"] is not None else None)

    table, replay_counts, live_counts = [], [], []
    for session, result in arms[0]["sessions"].items():
        if first_new[session] < first_log:
            continue
        replay = _move_metrics(result["moves_by_note"].values())
        live = _move_metrics(live_moves[session[:8]].values())
        replay_counts.extend(result["moves_by_note"].values())
        live_counts.extend(live_moves[session[:8]].values())
        table.append({"session_id": session, "arm_0": replay, "live": live,
                      "gap": replay["total_moves"] - live["total_moves"]})
    table.sort(key=lambda row: (-abs(row["gap"]), row["session_id"]))
    return {"inputs": {"ledger": str(ledger), "db": str(db), "logs": [str(p) for p in logs]},
            "window_start_utc": dt.datetime.fromtimestamp(first_log, dt.timezone.utc).isoformat(),
            "arms": arms, "validation": {"sessions": len(table),
                "arm_0": _move_metrics(replay_counts), "live": _move_metrics(live_counts),
                "largest_gaps": table[:10]}, "skipped": dict(skipped), "limitations": LIMITATIONS}


def _print_markdown(report: dict) -> None:
    def cell(value):
        return f"{value:.6f}" if isinstance(value, float) else str(value) if value is not None else "n/a"

    print("| Bonus | Sessions | Notes moved | >1 move | >2 moves | Max moves | Mean final merit | Merit drop |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    keys = ("bonus", "sessions_replayed", "notes_moved", "notes_moved_more_than_once",
            "notes_moved_more_than_twice", "max_moves_on_one_note", "mean_final_merit", "merit_drop")
    for arm in report["arms"]:
        print("| " + " | ".join(cell(arm[key]) for key in keys) + " |")
    validation = report["validation"]
    print(f"\nValidation: {validation['sessions']} sessions fully inside the log window "
          f"starting {report['window_start_utc']}.\n")
    print("| Source | Notes with >1 move | Total moves |")
    print("| --- | --- | --- |")
    for name in ("arm_0", "live"):
        metrics = validation[name]
        print(f"| {name} | {metrics['notes_moved_more_than_once']} | {metrics['total_moves']} |")
    print("\nLargest absolute gaps (gap = arm 0 total moves minus live):\n")
    print("| Session | Arm 0 >1 | Live >1 | Arm 0 moves | Live moves | Gap |")
    print("| --- | --- | --- | --- | --- | --- |")
    for row in validation["largest_gaps"]:
        a, b = row["arm_0"], row["live"]
        session = row["session_id"].replace("|", "\\|").replace("\n", " ")
        print(f"| {session} | {a['notes_moved_more_than_once']} | {b['notes_moved_more_than_once']} "
              f"| {a['total_moves']} | {b['total_moves']} | {row['gap']} |")
    print("\nSkipped: " + ", ".join(f"{key}={value}" for key, value in report["skipped"].items()))
    print("\nInputs: " + json.dumps(report["inputs"]))
    print("\n" + "\n".join(f"- {limitation}" for limitation in report["limitations"]))


def main(argv=None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True, help="ledger copy")
    parser.add_argument("--db", type=Path, required=True, help="index DB copy")
    parser.add_argument("--log", type=Path, action="append", required=True, help="hook-log copy; repeatable")
    parser.add_argument("--bonus", type=_bonus, action="append", default=[], help="non-negative incumbent bonus; repeatable")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args(argv)
    try:
        ledger, db = args.ledger.expanduser().resolve(), args.db.expanduser().resolve()
        refused_dbs = {Path(vault_index._default_db_path()).expanduser().resolve(),
                       Path("~/.claude/obsidian-brain-vault.db").expanduser().resolve()}
        # capture_dir() mkdirs. Derive its exact path without calling the writing helper.
        refused_ledgers = {(Path(capture_state.obsidian_utils._SECURE_DIR) / "capture"
                            / capture_state.LEDGER_NAME).expanduser().resolve(),
                           Path("~/.claude/obsidian-brain/capture/captures.jsonl").expanduser().resolve()}
        def same_file(path, live_paths):
            return any(live.exists() and path.exists() and os.path.samefile(path, live)
                       for live in live_paths)

        if (db in refused_dbs or ledger in refused_ledgers
                or same_file(db, refused_dbs) or same_file(ledger, refused_ledgers)):
            print("ERROR: refusing live/default DB or ledger; use copies", file=sys.stderr)
            return 2
        logs = [path.expanduser().resolve() for path in args.log]
        for path in [ledger, db, *logs]:
            if not path.is_file():
                raise OSError(f"input is missing or is not a file: {path}")
        report = _evaluate(ledger, db, logs, args.bonus)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, allow_nan=False))
    else:
        _print_markdown(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
