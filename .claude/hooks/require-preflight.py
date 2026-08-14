#!/usr/bin/env python3
"""
Blocking Pre-Commit Hook: Requires Preflight Verification

Installed by /harden-repo into target repo's .claude/hooks/

This hook BLOCKS git commit commands unless a valid preflight token exists.
Claude must run `./scripts/commit-preflight.sh` before committing.

The token is:
- Created by commit-preflight.sh after checks pass
- Valid for 5 minutes
- One-time use for regular commits (consumed after validation); reusable for --amend
"""

import hashlib
import json
import os
import re
import sys
import time


def _get_token_path():
    """Get project-specific token path using a hash of the project directory."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    project_dir = os.path.realpath(project_dir)
    project_hash = hashlib.md5(project_dir.encode()).hexdigest()[:8]
    return f"/tmp/.preflight-token-{project_hash}"

TOKEN_FILE = _get_token_path()


def _targets_this_project(cmd: str) -> bool:
    """Check if the command targets a repo within this project.

    Hooks run in their own process (cwd = project dir), so git commands in
    the hook inspect the wrong repo when Claude does 'cd /other/repo && git commit'.
    Parse the cd target from the command to determine the effective repo.
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        return True  # Can't determine scope, be safe

    project_dir = os.path.realpath(project_dir)

    # Extract the first "cd /path" from the command (handles "cd X && git commit")
    cd_match = re.search(r'(?:^|[;&|]\s*)cd\s+("([^"]+)"|\'([^\']+)\'|(\S+))', cmd)
    if cd_match:
        target = cd_match.group(2) or cd_match.group(3) or cd_match.group(4)
        target = os.path.expanduser(target)
        target = os.path.expandvars(target)
        target = os.path.realpath(target)
        return target.startswith(project_dir)

    # No cd in command — assume it targets the project repo
    return True


def _warn(message):
    """Best-effort stderr write, safe when fd 2 is closed.

    Deliberately not ``print(..., file=sys.stderr)``: with fd 2 closed
    ``sys.stderr`` is ``None`` and ``print(file=None)`` falls back to STDOUT,
    which is the decision channel — a non-JSON line there is worse than
    silence.
    """
    if sys.stderr is None:
        return
    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def block(reason: str) -> None:
    """Emit a PreToolUse deny decision and exit 0 — or exit 2 if it cannot.

    Exit 0 with ``permissionDecision: "deny"`` is what BLOCKS, but ONLY if the
    JSON actually reached stdout. Exit 0 with nothing on stdout is an ALLOW, so
    an unwritable decision channel silently turns this gate off. Measured with
    fd 1 closed: all five hooks exited 0 with zero bytes on stdout and zero on
    stderr — no traceback, no warning, command permitted.

    Two mechanics make that reachable rather than theoretical:

    * ``print()`` is a documented NO-OP when ``sys.stdout`` is ``None``, which
      is what CPython sets it to when fd 1 is closed. It does not raise.
    * ``print()`` BUFFERS. Against a pipe whose reader has gone away, the write
      lands at interpreter shutdown — after ``sys.exit(0)`` has already fixed
      the exit code — where ``BrokenPipeError`` is reported as "Exception
      ignored" and changes nothing. Hence the explicit ``flush()`` INSIDE the
      try: it drags that failure back to a point where it can still be acted
      on.

    The fallback is exit 2, the one blocking signal that needs no stdout —
    with ``sys.stdout`` dropped first, or CPython's finalization re-flush fails
    and overrides the status with 120 (measured), which is non-blocking. The
    reason goes to stderr via ``sys.stderr.write`` guarded on truthiness, NOT
    ``print(..., file=sys.stderr)`` — with fd 2 closed ``sys.stderr`` is
    ``None`` and ``print(file=None)`` falls back to STDOUT, which would put a
    non-JSON line on the decision channel.
    """
    payload = json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    })
    try:
        if sys.stdout is None:
            raise OSError("stdout unavailable")
        print(payload)
        sys.stdout.flush()
    except Exception:
        _warn(f"BLOCKED: {reason}")
        # Drop the unwritable streams before exiting. Otherwise CPython retries
        # the flush during finalization, that failure makes Py_FinalizeEx fail,
        # and the interpreter OVERRIDES the exit status with 120 — measured.
        # 120 is non-zero, i.e. non-blocking, i.e. the fail-open this whole
        # function exists to close. The buffered payload is undeliverable
        # either way. stderr goes too, and only AFTER _warn() has had its
        # chance: when both streams are the same dead pipe (`2>&1 |`), dropping
        # stdout alone still exits 120 — measured.
        sys.stdout = None
        sys.stderr = None
        raise SystemExit(2)
    sys.exit(0)


def allow() -> None:
    """Allow the command to proceed."""
    sys.exit(0)



_STDIN_CAP = 1_000_000


def _payload_shape_ok(data):
    """True when the payload matches what every caller below already assumes.

    Not defensive typing for its own sake — each of these was measured failing
    OPEN on all five hooks. ``tool_input`` as a string gives ``'str' object has
    no attribute 'get'``; ``command`` as an int gives ``argument of type 'int'
    is not iterable`` at the first ``in``/``re.search``. Both are tracebacks,
    both exit non-zero, and a non-zero exit is a NON-blocking error: the gated
    command runs.
    """
    if not isinstance(data, dict):
        return False
    if not isinstance(data.get("tool_name", ""), str):
        return False
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return False
    return isinstance(tool_input.get("command", ""), str)


def _read_hook_input(what):
    """Read the hook payload, capped, failing CLOSED on anything unusable.

    ``read(_STDIN_CAP + 1)`` makes an oversize payload an explicit condition
    rather than a silent truncation that only ever surfaces as a confusing
    parse error (note_writer's overflow-detection idiom, extended here to the
    .claude/hooks/ gates). Everything that can go wrong routes to ``block()``, because a gate
    that cannot read its input has not established that the command is safe —
    and every uncaught alternative is fail-OPEN, since an exception exits
    non-zero and a non-zero exit is a NON-blocking error under the PreToolUse
    contract.

    Four failure modes, each measured against these scripts:

    * the READ itself, caught as broadly as the parse. Invalid UTF-8 raises
      ``UnicodeDecodeError`` before ``json.loads`` is ever reached, so the
      read has to be inside a ``try`` at all — but a narrow
      ``(UnicodeDecodeError, OSError)`` is not enough either: with fd 0
      closed, CPython sets ``sys.stdin`` to ``None``, so the failure is an
      ``AttributeError`` on the attribute access and never reaches ``read()``,
      and a closed file object raises ``ValueError``, which is not an
      ``OSError``. Measured: all five hooks exited 1 on ``<&-``.
    * the PARSE. ``json.JSONDecodeError`` is not all ``json.loads`` raises:
      ~400 KB of nested ``[`` — comfortably under the cap — raises
      ``RecursionError``, which is not a ``ValueError``. Hence bare
      ``except Exception``.
    * the SHAPE. ``null``, ``[]``, ``5``, ``"5"`` and ``true`` all parse
      cleanly and then blow up on ``.get()`` in the caller.
    * the NESTED shape. See ``_payload_shape_ok``.

    The guarantee has a floor, and it is the interpreter, not this function:
    fail-closed holds from the hook's first statement onward. Hand the process
    a DIRECTORY as stdin and CPython aborts in ``init_sys_streams`` ("<stdin>
    is a directory, cannot continue") before any of this code runs — exit 1,
    which is non-blocking, and nothing in the script can intervene.
    """
    try:
        raw = sys.stdin.read(_STDIN_CAP + 1)
    except Exception:
        block(f"{what} could not read its input. Blocking as a safety measure.")
    else:
        if len(raw) > _STDIN_CAP:
            block(f"{what} received a payload over {_STDIN_CAP} bytes. "
                  "Blocking as a safety measure.")
        else:
            try:
                data = json.loads(raw)
            except Exception:
                block(f"{what} received invalid input. "
                      "Blocking as a safety measure.")
            else:
                if _payload_shape_ok(data):
                    return data
                block(f"{what} received a malformed payload. "
                      "Blocking as a safety measure.")
    # Cheap insurance, not a live bug. block() raises SystemExit, a
    # BaseException, and nothing in this tree catches BaseException or
    # bare-excepts, so this line is unreachable today (verified). It exists so
    # that if block() ever returns, callers get a block — exit 2 is the blocking
    # code — instead of an implicit `return None` they would AttributeError on,
    # which is the same fail-open class the checks above close.
    _warn(f"{what}: input handling fell through without a decision. Blocking.")
    raise SystemExit(2)


def main():
    input_data = _read_hook_input("Preflight hook")

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    command = tool_input.get("command", "")

    # Only validate git commit commands
    if tool_name != "Bash":
        allow()

    # Check if this is a git commit command
    is_commit = "git commit" in command
    is_amend = "--amend" in command

    if not is_commit:
        allow()

    # Skip this hook if the command targets a repo outside this project
    if not _targets_this_project(command):
        allow()

    # Check for skip flag (for emergencies - user must explicitly approve)
    if "SKIP_PREFLIGHT=1" in command:
        allow()

    # Check if token file exists
    if not os.path.exists(TOKEN_FILE):
        block(f"""❌ COMMIT BLOCKED: Preflight verification required!

You must run the preflight check before committing:

    ./scripts/commit-preflight.sh

This ensures:
  ✓ Secret scanning passes
  ✓ Lint passes (if configured)
  ✓ Tests pass (if configured)

The preflight creates a one-time token that allows the next commit.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Why this exists:
  Claude previously ignored hook warnings and committed without
  running tests. This mechanism ENFORCES the verification step.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Run: ./scripts/commit-preflight.sh
Then retry your commit.""")

    # Read and validate token
    try:
        with open(TOKEN_FILE, 'r') as f:
            token_data = json.load(f)
    except (json.JSONDecodeError, IOError):
        # Token file corrupted - require new preflight
        try:
            os.remove(TOKEN_FILE)
        except OSError:
            pass
        block(f"""❌ COMMIT BLOCKED: Invalid preflight token!

The token file is corrupted. Please run preflight again:

    ./scripts/commit-preflight.sh

Then retry your commit.""")

    # Check token expiry
    expires = token_data.get("expires", 0)
    current_time = int(time.time())

    if current_time > expires:
        try:
            os.remove(TOKEN_FILE)
        except OSError:
            pass
        time_ago = current_time - expires
        block(f"""❌ COMMIT BLOCKED: Preflight token expired!

Token expired {time_ago} seconds ago.

Please run preflight again to refresh:

    ./scripts/commit-preflight.sh

Then retry your commit.""")

    # Token is valid - consume it (one-time use)
    checks_run = token_data.get("checks_run", "none")
    staged_count = token_data.get("staged_files", 0)

    # For amend, we're more lenient — don't consume the token
    if not is_amend:
        try:
            os.remove(TOKEN_FILE)
        except OSError:
            pass

    # Token valid - allow commit
    # Output verification status for audit trail
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"✅ Preflight verified: {checks_run} | {staged_count} files"
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
