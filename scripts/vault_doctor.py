#!/usr/bin/env python3
"""vault_doctor — audit and repair the Obsidian vault.

Dispatches to check modules under scripts/vault_doctor_checks/.
Dry-run by default — requires --apply to write anything.

Config priority:
  1. CLI args (--vault, --sessions-folder, --insights-folder)
  2. Env vars (OBSIDIAN_BRAIN_VAULT, *_SESSIONS_FOLDER, *_INSIGHTS_FOLDER)
  3. ~/.claude/obsidian-brain-config.json (read directly via json.load
     to avoid hooks/obsidian_utils.load_config()'s session-scoped cache,
     which can be stale when the CLI runs outside a live Claude Code
     session)

Exit codes:
  0 — clean, no issues
  1 — issues found (dry-run or successful apply)
  2 — apply errors (one or more fixes failed)
  3 — usage error (bad args, no config)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make the check package importable
_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))

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

    if not vault or not sessions or not insights:
        # Fall back to config file only for values not yet resolved.
        # Read directly (bypass obsidian_utils.load_config's session cache,
        # which can be stale when the CLI runs outside a live session).
        cfg_path = Path.home() / ".claude" / "obsidian-brain-config.json"
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
            "when threshold > 0.0, since their proposed repair is unknown."
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


def _print_report_human(issues_by_check: dict, min_confidence: float = 0.0,
                        dropped_per_check: dict | None = None,
                        multi_check: bool = False) -> None:
    dropped_per_check = dropped_per_check or {}
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
        header += "]"
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

    cfg = _load_config(args)

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
    for mod in modules:
        days = args.days if args.days is not None else getattr(mod, "DEFAULT_WINDOW_DAYS", 7)
        issues = _run_scan(mod, cfg, days, args.project, args=args)
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
    if args.min_confidence > 0.0:
        filtered: dict = {}
        for check_name, issues in issues_by_check.items():
            kept = [i for i in issues if _confidence_passes(i, args.min_confidence)]
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
            return row

        payload = {
            "timestamp": _iso_now(),
            "total_issues": total_issues,
            "issues": [
                _issue_row(i)
                for issues in issues_by_check.values() for i in issues
            ],
        }
        # Conditionally add confidence-filter metadata — only present when the
        # flag was used (threshold > 0.0), so existing consumers are byte-identical
        # to prior schema. Mirrors the conditional-row-extras pattern from #98.
        # dropped_per_check attributes drops by check name — a fully-filtered
        # check has no rows in "issues", so this map is its only trace.
        if args.min_confidence > 0.0:
            payload["min_confidence"] = args.min_confidence
            payload["dropped_by_confidence"] = dropped_by_confidence
            payload["dropped_per_check"] = dropped_per_check
        print(json.dumps(payload, indent=2))
    else:
        _print_report_human(issues_by_check,
                            min_confidence=args.min_confidence,
                            dropped_per_check=dropped_per_check,
                            multi_check=len(modules) > 1)

    if total_issues == 0:
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
        return 1  # issues found, not applied (dry-run default)

    # --apply: per-project confirmation
    backup_root = os.path.expanduser(
        f"~/.claude/obsidian-brain-doctor-backup/{_iso_now().replace(':', '-')}"
    )
    print(f"\nBackup root: {backup_root}", file=sys.stderr)

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
                answer = sys.stdin.readline().strip().lower()
                if answer not in ("y", "yes"):
                    print(f"  skipped {proj}", file=sys.stderr)
                    continue
            results = mod.apply(resolvable, backup_root)
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

    return 2 if any_errors else 1


if __name__ == "__main__":
    sys.exit(main())
