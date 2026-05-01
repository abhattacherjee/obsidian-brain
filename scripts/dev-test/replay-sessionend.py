#!/usr/bin/env python3
"""
Drive the production SessionEnd hook code path against a captured JSONL
fixture and emit a machine-readable outcome.

Usage:
    replay-sessionend.py --jsonl PATH --cwd PATH
                         [--config PATH] [--mode sessionend|reaper]
                         [--dry-run] [--json]

Modes:
    sessionend (default) — synthesize SessionEnd hook input dict and call
                           hooks.obsidian_session_log._run() directly.
    reaper               — call hooks.obsidian_session_reaper
                           ._reap_orphaned_sessions(); requires #125.

Exit codes:
    0 = ran to completion (outcome printed; may be SKIPPED_*, OK, EXCEPTION,
        or NO_LOG_LINE_EMITTED — caller inspects outcome= field)
    1 = bug in this script (e.g., malformed --json output)
    2 = argparse error or reaper module not yet imported (pre-#125)
    3 = vault-sentinel safety stop
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "hooks"))
sys.path.insert(0, str(_REPO_ROOT))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jsonl", required=True, type=Path)
    p.add_argument("--cwd", required=True, type=str)
    p.add_argument("--config", type=Path, default=None,
                   help="Override $HOME/.claude/obsidian-brain-config.json")
    p.add_argument("--mode", choices=["sessionend", "reaper"], default="sessionend")
    p.add_argument("--dry-run", action="store_true",
                   help="Patch write_vault_note to record calls without writing")
    p.add_argument("--json", dest="emit_json", action="store_true",
                   help="Emit JSON object instead of human-readable key=value lines")
    return p.parse_args(argv)


def _check_vault_sentinel(config_path: Path) -> tuple[bool, str]:
    """Return (ok, message). If guard active and vault path looks real, ok=False."""
    if os.environ.get("_REAL_VAULT_GUARD") != "1":
        return True, ""
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except Exception:
        return True, ""  # No config to read — let later code handle it
    vault = cfg.get("vault_path", "")
    real_vault = str(Path("~/obsidian").expanduser())
    resolved = str(Path(vault).expanduser().resolve()) if vault else ""
    if resolved.startswith(real_vault):
        return False, f"refusing to run: vault path {vault} appears to be the user's real vault"
    return True, ""


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        try:
            print(json.dumps(payload, ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            print(f"BUG: JSON emit failed: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        for k, v in payload.items():
            print(f"{k}={v}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config_path = args.config or Path(os.path.expanduser("~/.claude/obsidian-brain-config.json"))
    ok, msg = _check_vault_sentinel(config_path)
    if not ok:
        print(msg, file=sys.stderr)
        return 3

    if args.mode == "reaper":
        return _run_reaper(args)
    return _run_sessionend(args)


def _hook_log_path() -> Path:
    return Path(os.path.expanduser("~/.claude/obsidian-brain-hook.log"))


def _snapshot_log_size() -> int:
    p = _hook_log_path()
    return p.stat().st_size if p.exists() else 0


def _read_new_log_lines(pre_size: int) -> list[str]:
    p = _hook_log_path()
    if not p.exists():
        return []
    with open(p, "rb") as f:
        f.seek(pre_size)
        new = f.read().decode("utf-8", errors="replace")
    return [ln for ln in new.splitlines() if ln.strip()]


def _parse_sessionend_log_line(line: str) -> dict:
    """Extract key=value pairs after the SessionEnd marker."""
    # Format: <ISO8601> SessionEnd project=X sid=Y outcome=Z msgs=N dur=M detail=...
    out: dict[str, str] = {}
    if "SessionEnd" not in line:
        return out
    tail = line.split("SessionEnd", 1)[1].strip()
    # Tokenize on space, but `detail=...` may contain spaces — capture rest after detail=.
    parts = tail.split(" ")
    i = 0
    while i < len(parts):
        kv = parts[i]
        if "=" not in kv:
            i += 1
            continue
        k, v = kv.split("=", 1)
        if k == "detail":
            v = " ".join([v] + parts[i + 1:]).strip()
            out[k] = v
            break
        out[k] = v
        i += 1
    return out


def _stage_fixture_under_projects(jsonl: Path, cwd: str) -> Path:
    """Copy the fixture to $HOME/.claude/projects/<slug>/<sid>.jsonl.

    Production `_run()` requires `transcript_path` to be inside
    `~/.claude/projects/` (containment check at obsidian_session_log.py:223).
    Without staging, every fixture replay would short-circuit with
    `SKIPPED_TRANSCRIPT_OUTSIDE_PROJECTS` and we'd never exercise the
    threshold/write/skip logic the bug actually lives in.

    The slug mimics CC's path-encoded layout: leading `-` + cwd with `/` → `-`.
    """
    slug = "-" + cwd.lstrip("/").replace("/", "-")
    projects_dir = Path(os.path.expanduser("~/.claude/projects")) / slug
    projects_dir.mkdir(parents=True, exist_ok=True)
    staged = projects_dir / f"{jsonl.stem}.jsonl"
    staged.write_bytes(jsonl.read_bytes())
    return staged


def _run_sessionend(args: argparse.Namespace) -> int:
    if not args.jsonl.exists():
        print(f"ERROR: --jsonl not found: {args.jsonl}", file=sys.stderr)
        return 2

    derived_sid = args.jsonl.stem  # basename without .jsonl
    staged_jsonl = _stage_fixture_under_projects(args.jsonl, args.cwd)

    # Optionally patch write_vault_note for --dry-run.
    vault_writes: list[tuple[str, int]] = []
    if args.dry_run:
        import obsidian_utils  # type: ignore
        original = obsidian_utils.write_vault_note

        def _record_call(path, content, *a, **kw):  # noqa: ARG001
            vault_writes.append((str(path), len(content)))
            return True

        obsidian_utils.write_vault_note = _record_call  # type: ignore[assignment]
        # Also patch in the imported namespace inside obsidian_session_log.
        try:
            import obsidian_session_log  # type: ignore
            if hasattr(obsidian_session_log, "write_vault_note"):
                obsidian_session_log.write_vault_note = _record_call  # type: ignore[assignment]
        except Exception:
            pass
    else:
        original = None  # for restoration parity

    pre = _snapshot_log_size()

    stdin_payload = {
        "session_id": derived_sid,
        "transcript_path": str(staged_jsonl.resolve()),
        "cwd": args.cwd,
    }

    payload: dict = {}
    try:
        import obsidian_session_log  # type: ignore
        # _run() reads from sys.stdin in production; we shim it to read our dict.
        import io
        original_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(stdin_payload))
        try:
            obsidian_session_log._run()
        finally:
            sys.stdin = original_stdin
    except SystemExit as exc:
        # _run() should not exit; main() does. If it does, treat code 0 as fine.
        if exc.code not in (0, None):
            payload["outcome"] = "EXCEPTION"
            payload["detail"] = f"SystemExit({exc.code})"
    except Exception as exc:
        payload["outcome"] = "EXCEPTION"
        payload["detail"] = f"{exc.__class__.__name__}: {exc}"
    finally:
        if args.dry_run and original is not None:
            import obsidian_utils  # type: ignore
            obsidian_utils.write_vault_note = original  # type: ignore[assignment]

    new_lines = _read_new_log_lines(pre)
    sessionend_lines = [ln for ln in new_lines if "SessionEnd" in ln]

    if not sessionend_lines and "outcome" not in payload:
        # CLI-side sentinel — NOT an _Outcome enum value.
        payload["outcome"] = "NO_LOG_LINE_EMITTED"
        payload["detail"] = "no SessionEnd line appended to hook log; #123 universal-emit regression"
        payload["msgs"] = ""
        payload["dur"] = ""
        payload["hook_log_line"] = ""
    elif sessionend_lines:
        parsed = _parse_sessionend_log_line(sessionend_lines[-1])
        payload.setdefault("outcome", parsed.get("outcome", "UNKNOWN"))
        payload["msgs"] = parsed.get("msgs", "")
        payload["dur"] = parsed.get("dur", "")
        payload["hook_log_line"] = sessionend_lines[-1]

    payload["vault_writes"] = vault_writes
    _emit(payload, args.emit_json)
    return 0


def _run_reaper(args: argparse.Namespace) -> int:
    try:
        # Module won't exist on develop until #125 lands.
        from obsidian_session_reaper import _reap_orphaned_sessions  # type: ignore  # noqa: F401
    except ImportError:
        print("reaper module not yet implemented (#125)", file=sys.stderr)
        return 2

    # Once #125 lands, the staging + invocation logic goes here. Until then,
    # this branch is unreachable; tests assert the exit-2 path.
    raise NotImplementedError("reaper invocation pending #125 — replay CLI needs orphan-dir staging")


if __name__ == "__main__":
    sys.exit(main())
