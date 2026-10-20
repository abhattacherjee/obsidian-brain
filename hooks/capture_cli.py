#!/usr/bin/env python3
"""capture_cli.py -- command line for auto-capture (#425).

The Captures mod, the lifecycle hooks and detached workers all go through
this one entry point. Every command prints JSON on stdout. Exit 0 is ok,
2 is a usage error, 1 is a failed command (its error goes to stderr).

    launch <sid> [--transcript P] [--project N] [--if-due]   {"launched": bool}
    run-batch <sid> <transcript> <project>                   {"outcome": str}
    sweep [--limit 3]                                        {"launched": [sid, ...]}
    ledger-list [--session SID] [--limit 50]                 [row, ...]
    reject <id>                                              the updated row
    status [--session SID]                                   {"next_batch_min": int|null, "pending_sweep": int}
    split-updates [--apply] [--json]                         text report (JSON with --json); exit 1 if a
                                                             note failed, 2 on a config without a vault (#427)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import capture_reject  # noqa: E402
import capture_slice  # noqa: E402
import capture_split  # noqa: E402
import capture_state  # noqa: E402
import capture_worker  # noqa: E402
import obsidian_utils  # noqa: E402



def pending_sessions(limit: int, exclude: str | None = None,
                     config: dict | None = None) -> list[tuple[str, Path]]:
    """Ended transcripts since install with unread bytes that pass ``eligible``, oldest first.

    A transcript written to within the last capture interval is skipped: its
    session is probably still running, and its own mod captures it. A "not
    eligible" verdict is kept as ``checked_size`` so the transcript is not
    parsed again until it grows.
    """
    config = config or obsidian_utils.load_config()
    quiet_s = int(config.get("auto_capture_interval_minutes", 10)) * 60
    since, now = capture_state.installed_at(), time.time()
    found = []
    for path in capture_worker._TRANSCRIPTS_ROOT.glob("*/*.jsonl"):
        sid = path.stem
        if sid == exclude or path.parent.name.endswith("-capture-worker-cwd"):
            continue  # the current session, or a capture worker's own transcript
        try:
            st = path.stat()
            if st.st_mtime < since or now - st.st_mtime < quiet_s:
                continue
            state = capture_state.get_state(sid)
        except (OSError, ValueError):
            continue  # unreadable file, or a stem that is not a valid session id
        if st.st_size <= state["offset"]:
            continue
        allow = config.get("auto_capture_headless_projects") or []
        if st.st_size == state["checked_size"] and state["project"] not in allow:
            continue  # judged not eligible at this size, and its project is still not listed
        if capture_state.claim_is_live(sid):
            continue
        project = state["project"] or capture_worker.project_for(path)
        if (capture_slice.first_user_text(path).startswith(capture_worker.SENTINEL)
                or not capture_worker.eligible(path, project, config)):
            capture_state.set_state(sid, checked_size=st.st_size, project=project)
            continue
        found.append((st.st_mtime, sid, path))
    found.sort()
    return [(sid, path) for _, sid, path in found[:limit]]


def sweep(limit: int = 3, exclude: str | None = None) -> list[str]:
    """Launch a batch for up to ``limit`` pending sessions."""
    if os.environ.get(capture_worker.DISABLE_ENV_VAR) == "1":
        return []  # skip the transcript scan too, not just the spawn
    launched = []
    for sid, path in pending_sessions(limit, exclude=exclude):
        if capture_worker.launch(sid, path, None, if_due=False):
            launched.append(sid)
    return launched


def write_cli_path() -> None:
    """Tell the mod where this script lives (it has no plugin-root variable)."""
    capture_state._atomic_write(capture_state.capture_dir() / "cli_path", str(Path(__file__).resolve()))


def status(session: str | None) -> dict:
    config = obsidian_utils.load_config()
    next_min = None
    if session:
        last = capture_state.get_state(session)["last_batch_at"]
        if last:
            interval = int(config.get("auto_capture_interval_minutes", 10))
            next_min = max(0, math.ceil(interval - (time.time() - last) / 60))
    vault = str(config.get("vault_path") or "")
    return {"next_batch_min": next_min, "pending_sweep": len(pending_sessions(1000, exclude=session)),
            "vault": Path(vault).name if vault else ""}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="capture_cli.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("launch")
    a.add_argument("sid")
    a.add_argument("--transcript")
    a.add_argument("--project")
    a.add_argument("--if-due", action="store_true")
    r = sub.add_parser("run-batch")
    r.add_argument("sid")
    r.add_argument("transcript")
    r.add_argument("project")
    s = sub.add_parser("sweep")
    s.add_argument("--limit", type=int, default=3)
    ll = sub.add_parser("ledger-list")
    ll.add_argument("--session")
    ll.add_argument("--limit", type=int, default=50)
    ll.add_argument("--since", type=float)
    rj = sub.add_parser("reject")
    rj.add_argument("id")
    st = sub.add_parser("status")
    st.add_argument("--session")
    sp = sub.add_parser("split-updates")
    sp.add_argument("--apply", action="store_true")
    sp.add_argument("--json", action="store_true")
    return p


def split_updates(apply: bool, as_json: bool) -> int:
    """The ``split-updates`` command: 0 ok, 1 when a note failed, 2 when the config has no vault."""
    try:
        report = capture_split.split_updates(obsidian_utils.load_config(), apply=apply)
    except capture_split.ConfigError as exc:
        print(f"[obsidian-brain] capture split-updates: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report) if as_json else capture_split.format_report(report))
    failed = [n for n in report["notes"] if n["error"]]
    for n in failed:
        print(f"[obsidian-brain] capture split-updates: {n['note']}: {n['error']}", file=sys.stderr)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    if args.cmd == "split-updates":
        return split_updates(args.apply, args.json)
    try:
        if args.cmd == "launch":
            path = Path(args.transcript) if args.transcript else None
            out = {"launched": capture_worker.launch(args.sid, path, args.project, if_due=args.if_due)}
        elif args.cmd == "run-batch":
            out = {"outcome": capture_worker.run_batch(args.sid, Path(args.transcript), args.project)}
        elif args.cmd == "sweep":
            out = {"launched": sweep(args.limit)}
        elif args.cmd == "ledger-list":
            out = capture_state.ledger_list(args.session, args.limit, args.since)
        elif args.cmd == "reject":
            out = capture_reject.reject(args.id)
        else:
            out = status(args.session)
    except (KeyError, ValueError) as exc:
        print(f"[obsidian-brain] capture {args.cmd}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
