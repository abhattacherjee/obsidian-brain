"""Shared runtime CLI. Host adapters supply identity, never cwd-based guesses."""

import argparse
import json
import sys
import time

from runtime_context import RuntimeContextError, resolve_runtime_context, using_runtime_context

MAX_INPUT_BYTES = 1000000


def read_payload(raw):
    if len(raw.encode("utf-8")) > MAX_INPUT_BYTES:
        raise RuntimeContextError("input_invalid", "Native input exceeds the 1 MB limit.")
    try:
        value = json.loads(raw) if raw.strip() else {}
    except ValueError as exc:
        raise RuntimeContextError("input_invalid", "Native input must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise RuntimeContextError("input_invalid", "Native input must be a JSON object.")
    return value


def main(argv=None, stdin=None, stdout=None, stderr=None, started_at=None):
    started_at = time.monotonic() if started_at is None else started_at
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, choices=("claude", "codex"))
    parser.add_argument("--client", required=True)
    for flag in ("config", "resource-root", "vault", "index", "state", "session-id", "cwd", "transcript"):
        parser.add_argument("--" + flag)
    parser.add_argument("--event", choices=("session_start", "resume", "stop", "pre_compact", "session_end", "recover"))
    parser.add_argument("--max-sources", type=int, default=8)
    parser.add_argument("command", choices=("context", "capture", "recover", "hook"))
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
        context = resolve_runtime_context(args.host, args.client, payload, overrides)
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
