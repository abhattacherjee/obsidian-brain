#!/usr/bin/env python3
"""vault_doctor — audit and repair the Obsidian vault.

Dispatches to check modules under scripts/vault_doctor_checks/.
Dry-run by default — requires --apply to write anything.

Config priority:
  1. CLI args (--vault, --sessions-folder, --insights-folder)
  2. Env vars (OBSIDIAN_BRAIN_VAULT, *_SESSIONS_FOLDER, *_INSIGHTS_FOLDER)
  3. the named legacy configuration path (read directly via json.load
     to avoid hooks/obsidian_utils.load_config()'s session-scoped cache,
     which can be stale when the CLI runs outside a live Claude Code
     session)

Exit codes:
  0 — clean, no issues
  1 — issues found (dry-run or successful apply)
  2 — apply errors, or one or more checks crashed (scan or apply;
      results incomplete — see crashed_checks in --json / "CHECK CRASHED"
      on stderr)
  3 — usage error (bad args, no config)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Make the check package importable
_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR.parent))
sys.path.insert(0, str(_SCRIPTS_DIR.parent / "hooks"))

import vault_doctor_checks  # noqa: E402


def _load_config(args) -> dict:
    """Resolve vault path + folders from args → env → obsidian-brain config file.

    Precedence (strict): CLI arg > env var > config file > default.
    """
    vault = args.vault or os.environ.get("OBSIDIAN_BRAIN_VAULT")
    env_sessions = os.environ.get("OBSIDIAN_BRAIN_SESSIONS_FOLDER")
    env_insights = os.environ.get("OBSIDIAN_BRAIN_INSIGHTS_FOLDER")
    sessions = args.sessions_folder or env_sessions
    insights = args.insights_folder or env_insights

    from runtime_context import current_runtime_context
    native = current_runtime_context()
    if native is not None:
        vault = vault or str(native.vault_path)
        sessions = sessions or native.config.get("sessions_folder", "claude-sessions")
        insights = insights or native.config.get("insights_folder", "claude-insights")

    if not vault or not sessions or not insights:
        # Fall back to config file only for values not yet resolved.
        # Read directly (bypass obsidian_utils.load_config's session cache,
        # which can be stale when the CLI runs outside a live session).
        from runtime_adapters.claude import legacy_config_path
        cfg_path = legacy_config_path()
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            if isinstance(cfg, dict):
                if not vault:
                    vault = cfg.get("vault_path", "")
                if not sessions:
                    sessions = cfg.get("sessions_folder", "claude-sessions")
                if not insights:
                    insights = cfg.get("insights_folder", "claude-insights")
        except (OSError, json.JSONDecodeError):
            pass

    # Apply defaults for any unresolved folders
    sessions = sessions or "claude-sessions"
    insights = insights or "claude-insights"

    if not vault:
        print(
            "error: no vault_path configured; set OBSIDIAN_BRAIN_VAULT "
            "or run /obsidian-setup",
            file=sys.stderr,
        )
        sys.exit(3)

    if not Path(vault).is_dir():
        print(
            f"error: vault_path does not exist or is not a directory: {vault}",
            file=sys.stderr,
        )
        sys.exit(3)

    return {"vault": vault, "sessions_folder": sessions, "insights_folder": insights}


# Extra per-check scan flags (consumed via a module's EXTRA_SCAN_FLAGS
# declaration). Kept as a module-level tuple NEXT TO the arg definitions in
# _build_parser so the parser and the main() unconsumed-flag guard can't
# drift apart: adding a new extra flag means adding it here AND adding the
# matching p.add_argument below.
_EXTRA_FLAG_NAMES = ("strict", "reconstruct")

# Range for --min-confidence (inclusive on both ends)
_MIN_CONFIDENCE_MIN = 0.0
_MIN_CONFIDENCE_MAX = 1.0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vault_doctor")
    p.add_argument("--check", dest="check", default=None, help="run only this check by name")
    p.add_argument("--days", type=int, default=None, help="override default window (days)")
    p.add_argument("--project", default=None, help="limit scan to this project name")
    p.add_argument("--vault", default=None, help="override vault path")
    p.add_argument("--sessions-folder", default=None)
    p.add_argument("--insights-folder", default=None)
    p.add_argument("--apply", action="store_true", help="apply fixes (default: dry-run)")
    p.add_argument("--yes", action="store_true", help="assume yes for all confirmations")
    p.add_argument("--discard-pending", default=None, metavar="PATH",
                   help="explicitly discard one unchanged registered pending intent; requires --apply and its SHA256")
    p.add_argument("--expected-pending-sha256", default=None, metavar="SHA256",
                   help="exact private intent digest acknowledged by --discard-pending")
    p.add_argument("--json", dest="json_out", action="store_true",
                   help="emit JSON on stdout (for skill integration)")
    p.add_argument(
        "--strict",
        action="store_true",
        help=(
            "for session-coverage: emit FAIL (not WARN) when any note references "
            "an orphaned session via source_session. Changes the issue reason "
            "prefix only — the exit code is unaffected"
        ),
    )
    p.add_argument(
        "--reconstruct",
        action="store_true",
        help=(
            "for session-coverage: mark gaps as resolvable and enable apply() to "
            "re-run the SessionEnd hook via replay-sessionend.py"
        ),
    )
    p.add_argument(
        "--min-confidence",
        type=float,
        default=0.0,
        dest="min_confidence",
        help=(
            "keep only issues with confidence >= THRESHOLD (0.0–1.0, inclusive). "
            "Default 0.0 keeps all issues. "
            "1.0 excludes issues with confidence=0.99 (use >= semantics: "
            "threshold=1.0 requires exactly 1.0). "
            "Applies to both the dry-run report and --apply — the preview always "
            "matches the apply scope. "
            "Note: unresolved issues have confidence=0.0 and are filtered out "
            "when threshold > 0.0, since their proposed repair is unknown. "
            "Reconstructable session-coverage gaps (--reconstruct) are "
            "resolvable and carry confidence=0.9, so they survive thresholds "
            "up to 0.9."
        ),
    )
    return p


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_scan(mod, cfg: dict, days: int, project: str | None, args=None) -> list:
    """Run a check module's scan(), forwarding any EXTRA_SCAN_FLAGS it declares.

    Modules that declare ``EXTRA_SCAN_FLAGS = ("flag_name", ...)`` receive those
    flags as keyword arguments, forwarded UNCONDITIONALLY (store_true flags
    yield real bools, so there is no None case to skip). A module declaring a
    flag with no matching argparse attribute is a contract violation — the
    bare getattr() raises AttributeError loudly instead of silently dropping
    the flag. Modules without ``EXTRA_SCAN_FLAGS`` are called with the
    unchanged positional signature.
    """
    extra_kwargs: dict = {}
    if args is not None:
        for flag in getattr(mod, "EXTRA_SCAN_FLAGS", ()):
            # No default: a declared flag without an argparse attribute must
            # raise AttributeError (contract violation), not be dropped.
            extra_kwargs[flag] = getattr(args, flag)
    return mod.scan(
        cfg["vault"],
        cfg["sessions_folder"],
        cfg["insights_folder"],
        days,
        project=project,
        **extra_kwargs,
    )


def _confidence_passes(issue, threshold: float) -> bool:
    """True when issue.confidence is a valid number >= threshold.

    Defensive guard: a future buggy check could emit a None/NaN/non-numeric
    confidence, and ``None >= float`` raises TypeError — a latent crash that
    would fire only when --min-confidence is first used against that check.
    Invalid values are warned about on stderr (naming the check and note) and
    treated as below threshold, i.e. counted as dropped by the caller.
    """
    c = issue.confidence
    if not isinstance(c, (int, float)) or math.isnan(c):
        print(
            f"[vault_doctor] {issue.check}: invalid confidence ({c!r}) for "
            f"{issue.note_path}; treating as below threshold",
            file=sys.stderr,
        )
        return False
    return c >= threshold


def _issue_row(i) -> dict:
    row = {
        "check": i.check,
        "note_path": i.note_path,
        "project": i.project,
        "current_source": i.current_source,
        "proposed_source": i.proposed_source,
        "reason": i.reason,
        "confidence": i.confidence,
        "unresolved": i.extra.get("unresolved", False),
        "signal_class": i.extra.get("signal_class", ""),
        "capture_signal": i.extra.get("capture_signal", ""),
        "capture_confidence": i.extra.get("capture_confidence", 0.0),
        # convergence_warning/convergence_count are deprecated as of #106
        # (UUID-first matching obsoleted the convergence guard). Kept in the
        # payload as hard-coded defaults for downstream schema stability;
        # consumers should migrate to signal_class for triage.
        "convergence_warning": i.extra.get("convergence_warning", False),
        "convergence_count": i.extra.get("convergence_count", 0),
    }
    # Conditionally surfaced extras (currently from session-coverage,
    # #98): only added when the issue's extra dict carries them, so
    # rows from other checks are byte-identical to the prior schema.
    if "sid" in i.extra:
        row["sid"] = i.extra["sid"]
    if "strict_fail" in i.extra:
        row["strict_fail"] = i.extra["strict_fail"]
    if "jsonl_path" in i.extra:
        row["jsonl_path"] = i.extra["jsonl_path"]
    if "referenced_by" in i.extra:
        row["referenced_by_count"] = len(i.extra.get("referenced_by", []))
    # memory-index (#308): counts, sizes and the diagnostic detail a consumer
    # needs to act on a row. skills/vault-doctor/SKILL.md drives that check
    # through --json, so without these it can only re-parse prose out of
    # `reason`. Conditional, like the block above: a check that does not set
    # a key gets a row byte-identical to the prior schema.
    for key in (
        "entry_count",
        "indexed_count",
        "index_size_bytes",
        "soft_limit_bytes",
        "hard_limit_bytes",
        "index_path",
        "entry_names",
        "byte_offset",
        "stores_scanned",
        "stores_filtered_out",
        # read_error, not "error": encoding-corruption already sets an
        # "error" extra, and whitelisting that name would change its rows.
        "read_error",
    ):
        if key in i.extra:
            row[key] = i.extra[key]
    return row


def _print_report_human(issues_by_check: dict, min_confidence: float = 0.0,
                        dropped_per_check: dict | None = None,
                        multi_check: bool = False,
                        dropped_per_signal_class: dict | None = None,
                        crashed_checks: list | None = None) -> None:
    dropped_per_check = dropped_per_check or {}
    dropped_per_signal_class = dropped_per_signal_class or {}
    dropped_total = sum(dropped_per_check.values())
    total = sum(len(v) for v in issues_by_check.values())
    header = f"\nvault_doctor report — {total} issue(s) across {len(issues_by_check)} check(s)"
    if min_confidence > 0.0:
        header += f" [filtered: --min-confidence {min_confidence}, dropped {dropped_total}"
        # Per-check breakdown: only when more than one check was scanned —
        # with a single --check the global count is already unambiguous.
        # This keeps a fully-filtered check attributable (it vanishes from
        # issues_by_check, so the breakdown is its only trace in the header).
        if multi_check and dropped_per_check:
            breakdown = ", ".join(f"{k}: {v}" for k, v in dropped_per_check.items())
            header += f" ({breakdown})"
        # Signal-class attribution for dropped issues (always, including
        # single-check runs): infrastructure rows like historic-unreadable
        # must stay visible when swallowed by the filter.
        if dropped_per_signal_class:
            cls_parts = "; ".join(
                f"{chk}: " + ", ".join(
                    f"{n} {cls}" for cls, n in classes.items()
                )
                for chk, classes in dropped_per_signal_class.items()
            )
            header += f"; by class: {cls_parts}"
        header += "]"
    if crashed_checks:
        header += (
            f" [{len(crashed_checks)} check(s) crashed:"
            f" {', '.join(crashed_checks)} — results incomplete]"
        )
    print(header, file=sys.stderr)
    for check_name, issues in issues_by_check.items():
        by_project: dict[str, list] = {}
        for i in issues:
            by_project.setdefault(i.project, []).append(i)
        for proj, proj_issues in sorted(by_project.items()):
            print(f"\n  Project: {proj}  [{check_name}]", file=sys.stderr)
            for i in proj_issues:
                mark = "!" if i.extra.get("unresolved") else "x"
                print(f"    {mark} {Path(i.note_path).name}", file=sys.stderr)
                print(f"      current:  {i.current_source}", file=sys.stderr)
                print(f"      proposed: {i.proposed_source or '(unresolved)'}", file=sys.stderr)
                print(f"      reason:   {i.reason}", file=sys.stderr)


def _recover_runtime_pending():
    """Native doctor calls recover facts before independent diagnostic scans."""
    import time
    from runtime_context import current_runtime_context
    context = current_runtime_context()
    if context is None:
        return None
    try:
        from capture import recover_registered
        from note_transactions import (recover_pending_mutations, coordination_migration_budget,
                                       ownership_lock, connect_coordination)
        # This explicit admin call may finish migration before bounded replay.
        with coordination_migration_budget(60):
            with ownership_lock(context, deadline=time.monotonic() + 60):
                connection = connect_coordination(context)
                connection.close()
        deadline = time.monotonic() + 1
        writes = recover_pending_mutations(context, max_operations=8, deadline=deadline)
        result = recover_registered(context, max_sources=8, deadline=deadline,
                                    include_active=True, replay_retired=True)
        output = {name: getattr(result, name) for name in (
            "status", "applied_revision", "pending_sources", "loss_of_input", "warnings")}
        output["warnings"] = list(output["warnings"]) + list(writes.warnings)
        from note_transactions import pending_mutation_inventory
        inventory = pending_mutation_inventory(context, limit=1024,
                                               deadline=time.monotonic() + 1)
        for key in ('pending_mutations', 'pending_intents', 'pending_intent_references_bounded'):
            defaults = {'pending_mutations': 0, 'pending_intents': [],
                        'pending_intent_references_bounded': True}
            output[key] = inventory.get(key, defaults[key])
        output['warnings'].extend(inventory.get('warnings', []))
        if writes.status in {"unavailable", "error", "failed"}:
            output["status"] = "unavailable"
        elif (writes.status not in {"unchanged", "applied"}
              and output["status"] not in {"unavailable", "error", "failed"}):
            output["status"] = "pending"
        if inventory['pending_mutations'] or inventory['bounded']:
            if output['status'] not in {'unavailable', 'error', 'failed'}:
                output['status'] = 'pending'
    except Exception as exc:
        output = {"status": "unavailable", "pending_sources": 1,
                  "loss_of_input": False, "warnings": [str(exc)]}
    if output["status"] != "complete" or output["pending_sources"] or output["loss_of_input"]:
        label = "capture source input lost" if output["loss_of_input"] else "capture recovery pending"
        print("[vault_doctor] " + label + ": " + json.dumps(output), file=sys.stderr)
    return output


def _inspect_runtime_pending():
    """Read existing journal state without creating files or replaying writes."""
    import contextlib
    import hashlib
    import sqlite3
    import time
    from runtime_context import current_runtime_context
    from note_transactions import coordination_location, coordination_identity_matches
    context = current_runtime_context()
    if context is None:
        return None
    output = {"status": "complete", "pending_sources": 0, "pending_mutations": 0,
              "loss_of_input": False, "warnings": []}
    deadline = time.monotonic() + 1
    try:
        journal = coordination_location(context) / "state.sqlite3"
        if any(parent.is_symlink() for parent in journal.parents) or journal.is_symlink():
            raise ValueError("The coordination journal path is not safe")
        wal = Path(str(journal) + "-wal")
        if wal.is_symlink() or (wal.exists() and wal.stat().st_size):
            raise ValueError("A live journal WAL prevents a complete read-only audit")
        if journal.is_file():
            # This selected coordination journal is not the derived vault index.
            with contextlib.closing(sqlite3.connect(journal.as_uri() + "?immutable=1", uri=True)) as connection:  # noqa: vault-db-connect
                connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'identity' not in tables or not coordination_identity_matches(context, connection):
                    raise ValueError("The coordination journal vault identity is unverified")
                pending_queries = []
                if 'checkpoints' in tables:
                    pending_queries.append("SELECT scope FROM checkpoints WHERE phase!='committed'")
                if 'source_sessions' in tables:
                    pending_queries.append("SELECT scope FROM source_sessions WHERE completeness!='complete'")
                if 'source_sessions' in tables:
                    from capture import pending_source_warnings
                    for cursor, descriptor in connection.execute("SELECT cursor,descriptor FROM source_sessions WHERE completeness!='complete' LIMIT 32"):
                        parsed_cursor, parsed_descriptor = json.loads(cursor), json.loads(descriptor)
                        if not isinstance(parsed_cursor, dict) or not isinstance(parsed_descriptor, dict):
                            raise ValueError("Retained capture metadata has an invalid shape")
                        parser_state = parsed_cursor.get('parser_state',{})
                        if not isinstance(parser_state, dict):
                            raise ValueError("Retained parser state has an invalid shape")
                        output['loss_of_input'] = output['loss_of_input'] or bool(parser_state.get('_capture_loss'))
                        warnings = pending_source_warnings(parser_state, parsed_descriptor.get('host'))
                        for warning in warnings:
                            if warning not in output['warnings']:
                                output['warnings'].append(warning)
                if pending_queries:
                    output['pending_sources'] = connection.execute(
                        'SELECT COUNT(*) FROM (' + ' UNION '.join(pending_queries) + ')').fetchone()[0]
        from note_transactions import pending_mutation_inventory
        inventory = pending_mutation_inventory(context, limit=1024, deadline=deadline)
        output['pending_mutations'] = inventory['pending_mutations']
        output['pending_intents'] = inventory.get('pending_intents', [])
        output['pending_intent_references_bounded'] = inventory.get('pending_intent_references_bounded', False)
        output['warnings'].extend(inventory.get('warnings', []))
        if inventory['bounded']:
            output['status'] = 'pending'
            output['warnings'].append('Pending intent inventory is incomplete within its audit bound')
        selected_pending = 0
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        pending = context.state_path / 'v1' / digest(str(context.vault_path)) / context.host / context.session_key / digest(str(context.canonical_project_root)) / 'pending'
        if any(parent.is_symlink() for parent in pending.parents) or pending.is_symlink():
            raise ValueError("Pending intent path is not safe")
        if pending.is_dir():
            import os
            with os.scandir(pending) as entries:
                for index, entry in enumerate(entries):
                    if index >= 1024 or time.monotonic() >= deadline:
                        raise ValueError("Pending intent audit limit reached")
                    if entry.name.endswith('.json'):
                        if not entry.is_file(follow_symlinks=False):
                            raise ValueError("Pending intent is not a regular file")
                        selected_pending += 1
        if not inventory['selected_pending_registered']:
            output['pending_mutations'] += selected_pending
        if output['pending_sources'] or output['pending_mutations']:
            output['status'] = 'pending'
    except (OSError, ValueError, sqlite3.Error) as exc:
        output['status'] = 'unavailable'
        output['warnings'].append(str(exc))
    return output


def main() -> int:
    args = _build_parser().parse_args()

    if args.days is not None and args.days <= 0:
        print(
            f"error: --days must be positive, got {args.days}",
            file=sys.stderr,
        )
        return 3

    if not (_MIN_CONFIDENCE_MIN <= args.min_confidence <= _MIN_CONFIDENCE_MAX):
        print(
            f"error: --min-confidence must be in [0.0, 1.0], got {args.min_confidence}",
            file=sys.stderr,
        )
        return 3

    if args.discard_pending or args.expected_pending_sha256:
        import re
        if (not args.apply or not args.discard_pending
                or not isinstance(args.expected_pending_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", args.expected_pending_sha256)):
            print("error: pending discard requires --apply, --discard-pending PATH, and --expected-pending-sha256 SHA256", file=sys.stderr)
            return 3
        from runtime_context import current_runtime_context
        from note_transactions import discard_pending_mutation
        context = current_runtime_context()
        if context is None:
            print("error: pending discard requires an explicit native context", file=sys.stderr)
            return 3
        acknowledgment = discard_pending_mutation(context, args.discard_pending, args.expected_pending_sha256)
        if acknowledgment.status not in {"unchanged", "applied"}:
            print("error: pending discard refused: " + "; ".join(acknowledgment.warnings), file=sys.stderr)
            return 2
        print("vault_doctor: explicitly acknowledged pending intent removed; destination note unchanged", file=sys.stderr)
        return 0

    cfg = _load_config(args)
    recovery = _recover_runtime_pending() if args.apply else _inspect_runtime_pending()
    recovery_pending = bool(recovery and (recovery["status"] != "complete" or recovery["pending_sources"] or recovery["loss_of_input"]))
    recovery_error = bool(recovery and (recovery["status"] in {"unavailable", "error", "failed"} or recovery["loss_of_input"]))

    if args.check:
        try:
            modules = [vault_doctor_checks.get_check(args.check)]
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
    else:
        modules = vault_doctor_checks.all_checks()

    if not modules:
        print("error: no checks registered", file=sys.stderr)
        return 3

    # Guard against silently-dropped extra flags: session-coverage (the only
    # EXTRA_SCAN_FLAGS consumer) is OPT_IN, so a default sweep with
    # --reconstruct/--strict would otherwise evaporate the flag with zero
    # output — the user would believe reconstruction/strict was attempted.
    consumed = {f for m in modules for f in getattr(m, "EXTRA_SCAN_FLAGS", ())}
    for flag in _EXTRA_FLAG_NAMES:
        if getattr(args, flag) and flag not in consumed:
            print(
                f"error: --{flag} is only consumed by an opt-in check that is "
                f"not selected; run with --check session-coverage",
                file=sys.stderr,
            )
            return 3

    issues_by_check: dict = {}
    # Checks whose scan() or apply() raised: contained per check so one buggy
    # check cannot take down the whole run. Any entry forces the exit-2 path
    # — the results are incomplete and must not read as clean.
    crashed_checks: list[str] = []
    for mod in modules:
        days = args.days if args.days is not None else getattr(mod, "DEFAULT_WINDOW_DAYS", 7)
        try:
            issues = _run_scan(mod, cfg, days, args.project, args=args)
        except Exception as exc:  # noqa: BLE001 — per-check crash containment
            print(
                f"[vault_doctor] CHECK CRASHED: {mod.NAME}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            crashed_checks.append(mod.NAME)
            continue
        if issues:
            issues_by_check[mod.NAME] = issues

    # Apply --min-confidence filter AFTER scan, per-check. Filtering happens
    # here in main() so check authors don't have to opt in — every Issue
    # already has a confidence field. When threshold > 0.0, unresolved issues
    # (confidence=0.0) are filtered from both the report and --apply, which is
    # intentional: the dry-run preview must match the apply scope exactly.
    # Drops are attributed per check (dropped_per_check) because a fully-
    # filtered check vanishes from issues_by_check entirely — without
    # attribution it would be indistinguishable from a clean check.
    dropped_by_confidence = 0
    dropped_per_check: dict[str, int] = {}
    # Per-signal_class attribution for dropped issues (attribution only — no
    # filter exemption): infrastructure rows (e.g. historic-unreadable) carry
    # confidence=0.0 and would otherwise vanish indistinguishably from
    # ordinary low-confidence proposals. Shape: {check: {signal_class: n}};
    # dropped issues without a signal_class only count in dropped_per_check.
    dropped_per_signal_class: dict[str, dict[str, int]] = {}
    if args.min_confidence > 0.0:
        filtered: dict = {}
        for check_name, issues in issues_by_check.items():
            kept = []
            for i in issues:
                if _confidence_passes(i, args.min_confidence):
                    kept.append(i)
                    continue
                scls = i.extra.get("signal_class", "")
                if scls:
                    per_cls = dropped_per_signal_class.setdefault(check_name, {})
                    per_cls[scls] = per_cls.get(scls, 0) + 1
            n_dropped = len(issues) - len(kept)
            if n_dropped:
                dropped_per_check[check_name] = n_dropped
                dropped_by_confidence += n_dropped
            if kept:
                filtered[check_name] = kept
        issues_by_check = filtered

    total_issues = sum(len(v) for v in issues_by_check.values())

    # JSON output for skill consumption
    if args.json_out:
        payload = {
            "timestamp": _iso_now(),
            "total_issues": total_issues,
            "issues": [
                _issue_row(i)
                for issues in issues_by_check.values() for i in issues
            ],
        }
        if recovery is not None:
            payload["capture_recovery"] = recovery
        # Conditionally add confidence-filter metadata — only present when the
        # flag was used (threshold > 0.0), so existing consumers are byte-identical
        # to prior schema. Mirrors the conditional-row-extras pattern from #98.
        # dropped_per_check attributes drops by check name — a fully-filtered
        # check has no rows in "issues", so this map is its only trace.
        if args.min_confidence > 0.0:
            payload["min_confidence"] = args.min_confidence
            payload["dropped_by_confidence"] = dropped_by_confidence
            payload["dropped_per_check"] = dropped_per_check
            # Signal-class attribution: only when at least one dropped issue
            # carried a signal_class (conditional-key convention).
            if dropped_per_signal_class:
                payload["dropped_per_signal_class"] = dropped_per_signal_class
        # Crash containment: key only present when a check crashed
        # (conditional-key convention — clean runs are byte-identical).
        if crashed_checks:
            payload["crashed_checks"] = crashed_checks
        print(json.dumps(payload, indent=2))
    else:
        _print_report_human(issues_by_check,
                            min_confidence=args.min_confidence,
                            dropped_per_check=dropped_per_check,
                            multi_check=len(modules) > 1,
                            dropped_per_signal_class=dropped_per_signal_class,
                            crashed_checks=crashed_checks)

    if total_issues == 0:
        if recovery_pending:
            detail = "capture source input was lost" if recovery["loss_of_input"] else "capture recovery remains pending"
            print("vault_doctor: diagnostics found no issues; " + detail, file=sys.stderr)
            return 2 if recovery_error else 1
        if crashed_checks:
            # NOT clean: one or more checks never finished scanning. Saying
            # "clean" would be a literal falsehood — and exit 2 (not 0) so
            # automation can't mistake an incomplete run for a healthy vault.
            print(
                f"vault_doctor: 0 issues, but {len(crashed_checks)} check(s) "
                f"crashed — results incomplete",
                file=sys.stderr,
            )
            return 2
        if dropped_by_confidence > 0:
            # All issues were filtered out — saying just "clean" would be a
            # literal falsehood. Exit stays 0 by decision (no new exit code);
            # JSON consumers disambiguate via dropped_by_confidence.
            print(
                f"vault_doctor: clean at --min-confidence {args.min_confidence} "
                f"({dropped_by_confidence} issue(s) below threshold — rerun "
                f"without the flag to see them)",
                file=sys.stderr,
            )
        else:
            print("vault_doctor: clean", file=sys.stderr)
        return 0

    if not args.apply:
        # Issues found, not applied (dry-run default). A crashed check still
        # forces exit 2 — the report above is incomplete.
        return 2 if (crashed_checks or recovery_error) else 1

    # --apply: per-project confirmation
    from runtime_context import current_runtime_context
    from session_auxiliary_state import directory
    context = current_runtime_context()
    backup_parent = (directory(context, "doctor-backups") if context is not None else
                     __import__("runtime_adapters.claude", fromlist=["legacy_doctor_backup_root"]).legacy_doctor_backup_root())
    backup_root = str(backup_parent / _iso_now().replace(':', '-'))
    print(f"\nBackup root: {backup_root}", file=sys.stderr)

    pending = [issue for rows in issues_by_check.values() for issue in rows]
    try:
        with vault_doctor_checks.repair_scope(pending):
            any_errors = False
            for mod in modules:
                issues = issues_by_check.get(mod.NAME, [])
                if not issues:
                    continue
                by_project: dict[str, list] = {}
                for i in issues:
                    by_project.setdefault(i.project, []).append(i)
                for proj, proj_issues in sorted(by_project.items()):
                    resolvable = [i for i in proj_issues if not i.extra.get("unresolved")]
                    if not resolvable:
                        continue
                    if not args.yes:
                        sys.stderr.write(
                            f"Apply {len(resolvable)} fix(es) for project '{proj}' "
                            f"in check '{mod.NAME}'? [y/N] "
                        )
                        sys.stderr.flush()
                        # readline() with no size is unbounded: input containing no
                        # newline is consumed to EOF. 1024 is far beyond any real
                        # y/N answer, so behaviour is identical for every sane input.
                        answer = sys.stdin.readline(1024).strip().lower()
                        if answer not in ("y", "yes"):
                            print(f"  skipped {proj}", file=sys.stderr)
                            continue
                    try:
                        results = mod.apply(resolvable, backup_root)
                    except Exception as exc:  # noqa: BLE001 — per-check crash containment
                        print(
                            f"[vault_doctor] APPLY CRASHED: {mod.NAME}: "
                            f"{type(exc).__name__}: {exc} — apply for this check "
                            f"aborted mid-run; some fixes may already be applied "
                            f"(check the backup root: {backup_root})",
                            file=sys.stderr,
                        )
                        traceback.print_exc(file=sys.stderr)
                        if mod.NAME not in crashed_checks:
                            crashed_checks.append(mod.NAME)
                        any_errors = True
                        # Skip this check's remaining projects (the next apply() call
                        # would most likely crash the same way) and move on to the
                        # next check.
                        break
                    for r in results:
                        status_mark = {"applied": "+", "unresolved": "!", "error": "x", "skipped": "-"}.get(
                            r.status, "?"
                        )
                        print(f"  {status_mark} {r.status}  {Path(r.note_path).name}", file=sys.stderr)
                        if r.status == "error":
                            any_errors = True
                        # Print the detail message whenever present, regardless of
                        # status — e.g. session-coverage's "skipped" Results carry the
                        # replay skip reason (SKIPPED_BELOW_THRESHOLD etc.) in error.
                        if r.error:
                            print(f"      {r.error}", file=sys.stderr)

    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[vault_doctor] repair scope failed: {exc}", file=sys.stderr)
        return 2

    return 2 if (any_errors or crashed_checks or recovery_error) else 1


if __name__ == "__main__":
    sys.exit(main())
