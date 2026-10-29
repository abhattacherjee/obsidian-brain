#!/usr/bin/env python3
"""Known-answer retrieval evaluation against an explicit index DB copy.

Never syncs the index or reads vault notes. OR counts use a read-only
connection; search_vault retains its normal connection behavior, with access
logging disabled. Exit codes: 0 success, 1 DB failure, 2 usage/refused path.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import vault_index  # noqa: E402


CASES = (
    ("obsidian sync excluded folders cli", "obsidian-sync-excluded-folders-can-be-set-without-5336"),
    ("hook misparses compound bash commands", "vendored-pretooluse-hook-misparses-compound-bash-c-2860"),
    ("relocate unique conflict already indexed", "pr-467-fix-pass-3-capture-provisional-relocate-hit-6290"),
    ("merit floor criterion split 466", "split-the-merit-floor-criterion-c-4-out-of-465-int-8970"),
    ("preflight token expires infinity", "preflight-token-with-expires-set-to-infinity-or-na-a277"),
    ("macos firewall tailscale localhost bind", "macos-firewall-tailscale-localhost-bind-cc58"),
    ("isolate self-hosted runner macos user", "isolate-self-hosted-runner-jobs-with-one-macos-use-73e7"),
    ("snapshot summary integration decision", "decision-log-snapshot-summary-integration-de-094f"),
    ("preflight token retry claim hold", "preflight-token-retry-within-claim-hold-seconds-5-32f8"),
    ("macos has no timeout command", "macos-has-no-timeout-use-perl-alarm-59e3"),
)
MODES = (("include_provisional", True), ("exclude_provisional", False))


def _nonnegative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    if number < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return number


def _evaluate(db: Path, min_results: int | None) -> dict:
    fill = min_results is not None and min_results > 0

    rows = []
    # Escape URI metacharacters so SQLite reads exactly the resolved copy.
    path = quote(str(db), safe="/")
    conn = sqlite3.connect('file:' + path + '?mode=ro', uri=True)  # noqa: vault-db-connect -- read only the explicit DB copy; never create a missing DB
    with closing(conn):
        for query, target_stem in CASES:
            row = {"query": query, "target_stem": target_stem}
            for mode_name, include_provisional in MODES:
                sql = (
                    "SELECT count(*) FROM notes_fts f JOIN notes n ON n.rowid=f.rowid "
                    "WHERE notes_fts MATCH ?"
                )
                if not include_provisional:
                    sql += " AND " + vault_index.NOT_PROVISIONAL_SQL.format(p="n.")
                or_query = vault_index._sanitize_fts_query_or(query)
                or_candidates = conn.execute(sql, (or_query,)).fetchone()[0] if or_query else 0
                start = time.perf_counter()
                # Direct calls with a literal log_access=False (tests/test_access_log_flag.py).
                if fill:
                    results = vault_index.search_vault(
                        str(db), query, limit=15, log_access=False, caller="vault-ask",
                        include_provisional=include_provisional, min_results=min_results,
                    )
                else:
                    results = vault_index.search_vault(
                        str(db), query, limit=15, log_access=False, caller="vault-ask",
                        include_provisional=include_provisional,
                    )
                elapsed_ms = (time.perf_counter() - start) * 1000
                rank = next((i for i, result in enumerate(results, 1)
                             if target_stem in Path(result["path"]).name), None)
                row[mode_name] = dict(rank=rank, n=len(results),
                                      or_candidates=or_candidates, ms=elapsed_ms)
            rows.append(row)

    summary = {}
    for mode_name, _ in MODES:
        metrics = [row[mode_name] for row in rows]
        summary[mode_name] = {
            "rank_1": sum(m["rank"] == 1 for m in metrics),
            "ranked_below_1": sum(m["rank"] is not None and m["rank"] > 1 for m in metrics),
            "missed": sum(m["rank"] is None for m in metrics),
            "underfilled": sum(m["n"] < 5 and m["or_candidates"] >= 5 for m in metrics),
            "max_ms": max((m["ms"] for m in metrics), default=0),
        }
    return {"db": str(db), "min_results": min_results, "cases": rows, "summary": summary}


def _print_markdown(report: dict) -> None:
    print("| Query | Include provisional: rank / n / or_candidates / ms | "
          "Exclude provisional: rank / n / or_candidates / ms |")
    print("| --- | --- | --- |")
    for row in report["cases"]:
        cells = [row["query"].replace("|", "\\|").replace("\n", " ")]
        for mode_name, _ in MODES:
            m = row[mode_name]
            rank = m["rank"] if m["rank"] is not None else "None"
            cells.append(f"{rank} / {m['n']} / {m['or_candidates']} / {m['ms']:.2f}")
        print("| " + " | ".join(cells) + " |")
    print()
    for mode_name, _ in MODES:
        s = report["summary"][mode_name]
        print(f"{mode_name}: rank-1={s['rank_1']}, ranked-below-1={s['ranked_below_1']}, "
              f"missed={s['missed']}, underfilled (n < 5 and or_candidates >= 5)="
              f"{s['underfilled']}, max ms={s['max_ms']:.2f}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="index DB copy (never the live DB)")
    parser.add_argument("--min-results", type=_nonnegative_int,
                        help="forward positive N to a search_vault version supporting OR fill")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args(argv)
    try:
        db = args.db.expanduser().resolve()
        refused = {
            Path(vault_index._default_db_path()).expanduser().resolve(),
            Path("~/.claude/obsidian-brain-vault.db").expanduser().resolve(),
        }
        if db in refused or any(live.exists() and db.exists() and os.path.samefile(db, live)
                                for live in refused):
            print(f"ERROR: refusing live/default DB: {db}; use a DB copy", file=sys.stderr)
            return 2
        if not db.is_file():
            raise OSError(f"DB copy is missing or is not a file: {db}")
        if args.min_results:
            try:
                # An empty query returns [] before any DB access; an old
                # search_vault without min_results raises TypeError at the call.
                vault_index.search_vault(
                    str(db), "", limit=15, log_access=False, include_provisional=True,
                    caller="vault-ask", min_results=args.min_results,
                )
            except TypeError:
                print("ERROR: this search_vault does not support --min-results > 0; "
                      "use a version with OR fill, or omit the option", file=sys.stderr)
                return 2
        report = _evaluate(db, args.min_results)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report))
    else:
        _print_markdown(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
