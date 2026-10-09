"""Shared runtime CLI. Host adapters supply identity, never cwd-based guesses."""

import argparse
import json
import sys
import time
from pathlib import Path

from runtime_context import RuntimeContextError, resolve_runtime_context, using_runtime_context

MAX_INPUT_BYTES = 1000000


def read_payload(raw):
    if len(raw.encode("utf-8")) > MAX_INPUT_BYTES:
        raise RuntimeContextError("input_invalid", "Native input exceeds the 1 MB limit.")
    try:
        def object_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON field")
                result[key] = value
            return result

        def invalid_constant(value):
            raise ValueError("Nonfinite JSON value")

        value = json.loads(raw, object_pairs_hook=object_fields,
                           parse_constant=invalid_constant) if raw.strip() else {}
    except (ValueError, RecursionError) as exc:
        raise RuntimeContextError("input_invalid", "Native input must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise RuntimeContextError("input_invalid", "Native input must be a JSON object.")
    return value


def main(argv=None, stdin=None, stdout=None, stderr=None, started_at=None):
    started_at = time.monotonic() if started_at is None else started_at
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--host", required=True, choices=("claude", "codex"))
    parser.add_argument("--client", required=True)
    for flag in ("config", "resource-root", "vault", "index", "state", "session-id", "cwd", "transcript"):
        parser.add_argument("--" + flag)
    parser.add_argument("--event", choices=("session_start", "resume", "stop", "pre_compact", "session_end", "recover"))
    parser.add_argument("--max-sources", type=int, default=8)
    parser.add_argument("--skill-path")
    parser.add_argument("--operation")
    parser.add_argument("command", choices=("context", "capture", "recover", "hook", "run"))
    args = parser.parse_args(argv)
    names = {"config": "config_path", "resource_root": "resource_root", "vault": "vault_path",
             "index": "index_path", "state": "state_path", "session_id": "session_id",
             "cwd": "cwd", "transcript": "transcript_path"}
    overrides = {target: getattr(args, source) for source, target in names.items()
                 if getattr(args, source) is not None}
    try:
        if stdin is None:
            stdin = sys.stdin
        raw = stdin.read(MAX_INPUT_BYTES + 1)
        payload = read_payload(raw)
        skill_name = None
        if args.command == "run":
            if not args.skill_path or not args.operation or not args.resource_root:
                raise RuntimeContextError("input_invalid", "Select the loaded skill, resource root and operation.")
            loaded = Path(args.skill_path)
            if not loaded.is_absolute():
                raise RuntimeContextError("resources_invalid", "The loaded skill path must be absolute.")
            loaded = loaded.resolve()
            root = Path(args.resource_root).resolve()
            if (loaded.name != "SKILL.md" or loaded.parent.parent != root / "skills"
                    or not loaded.is_file() or Path(__file__).resolve().parent.parent != root):
                raise RuntimeContextError("resources_invalid", "The operation does not belong to the loaded installation.")
            skill_name = loaded.parent.name
        context = resolve_runtime_context(args.host, args.client, {} if args.command == "run" else payload, overrides,
                                          require_payload_session=args.command == "hook")
        if args.command == "run":
            from skill_procedures import run_operation
            with using_runtime_context(context):
                return run_operation(context, skill_name, args.operation, payload, stdout, stderr)
        if not 1 <= args.max_sources <= 64:
            raise RuntimeContextError("input_invalid", "max-sources must be between 1 and 64.")
        if args.command in {"capture", "hook"} and args.event is None:
            raise RuntimeContextError("input_invalid", "Select the lifecycle event explicitly.")
        if args.command == "hook":
            from native_lifecycle import dispatch
            output = dispatch(context, args.event, payload, started_at)
            if output is not None:
                stdout.write(json.dumps(output) + "\n")
            return 0
        if args.command in {"capture", "recover"}:
            from capture import CaptureEvent, capture_checkpoint, recover_registered
            with using_runtime_context(context):
                if args.command == "recover":
                    result = recover_registered(context, max_sources=args.max_sources,
                                                deadline=started_at + 1, include_active=True)
                else:
                    turn = payload.get("turn_id")
                    turn = turn if isinstance(turn, str) and turn else None
                    options = {"kind": args.event, "turn_id": turn}
                    if args.event == "session_end" and payload.get("reason") == "clear":
                        options["trigger"] = "clear"
                    result = capture_checkpoint(context, CaptureEvent(**options),
                                                started_at + (1 if args.event in {"session_start", "resume", "recover"} else 2.5))
            output = {name: getattr(result, name) for name in (
                "status", "applied_revision", "pending_sources", "loss_of_input", "warnings")}
            stdout.write(json.dumps(output) + "\n")
            return 0
        output = {name: str(getattr(context, name)) for name in (
            "host", "client", "native_session_id", "canonical_project_root", "worktree", "vault_path",
            "config_path", "resource_root", "index_path", "state_path")}
        output["transcript_path"] = str(context.transcript_path) if context.transcript_path else None
        output["session_key"] = context.session_key
        stdout.write(json.dumps(output) + "\n")
        return 0
    except RuntimeContextError as exc:
        stderr.write(json.dumps({"code": exc.code, "message": str(exc)}) + "\n")
        return 0 if args.command == "hook" else 2


if __name__ == "__main__":
    raise SystemExit(main())
