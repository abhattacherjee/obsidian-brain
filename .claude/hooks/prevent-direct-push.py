#!/usr/bin/env python3
"""
PreToolUse Hook: Prevent Direct Push to Protected Branches

Blocks git push to main/develop. Allows Git Flow operations,
tag pushes, and feature branch pushes.

Installed by /harden-repo into target repo's .claude/hooks/
"""
import json
import os
import re
import sys
import subprocess

_STDIN_CAP = 1_000_000


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


def _deny(reason):
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
    .claude/hooks/ gates). Everything that can go wrong denies, because a gate
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
        _deny(f"{what} could not read its input. Blocking as a safety measure.")
    else:
        if len(raw) > _STDIN_CAP:
            _deny(f"{what} received a payload over {_STDIN_CAP} bytes. "
                  "Blocking as a safety measure.")
        else:
            try:
                data = json.loads(raw)
            except Exception:
                _deny(f"{what} received invalid input. "
                      "Blocking as a safety measure.")
            else:
                if _payload_shape_ok(data):
                    return data
                _deny(f"{what} received a malformed payload. "
                      "Blocking as a safety measure.")
    # Cheap insurance, not a live bug. _deny() raises SystemExit, a
    # BaseException, and nothing in this tree catches BaseException or
    # bare-excepts, so this line is unreachable today (verified). It exists so
    # that if _deny() ever returns, callers get a block — exit 2 is the blocking
    # code — instead of an implicit `return None` they would AttributeError on,
    # which is the same fail-open class the checks above close.
    _warn(f"{what}: input handling fell through without a decision. Blocking.")
    raise SystemExit(2)


input_data = _read_hook_input("Push hook")

tool_name = input_data.get("tool_name", "")
tool_input = input_data.get("tool_input", {})
command = tool_input.get("command", "")

# Only validate git push commands
if tool_name != "Bash" or "git push" not in command:
    sys.exit(0)

# --- Project-scope guard ---
# Skip this hook if the command targets a repo outside this project.
# Hooks run in their own process (cwd = project dir), so git commands in
# the hook inspect the wrong repo when Claude does "cd /other/repo && git push".
def _targets_this_project(cmd: str, verb: str) -> bool:
    """Check if the command targets a repo within this project.

    Hooks run in their own process (cwd = project dir), so the git state the
    hook inspects is the wrong repo's when Claude does 'cd /other/repo && ...'.
    Parse the cd targets from the command to determine the effective repo.

    ``verb`` is the regex the caller already matched to decide this command is
    worth gating. Only a ``cd`` that PRECEDES an occurrence of that verb can
    move it out of this project — a ``cd`` after the verb cannot change where
    the verb already acted, and honouring one turned every gate in this repo
    off (#326). Where several ``cd``s precede an occurrence the last one wins,
    as in the shell: ``cd /a && cd /b && <verb>`` runs in ``/b``.

    A ``cd`` only counts when ``&&`` is the ONLY thing between it and the verb.
    That is what makes the descoping provable: ``&&`` runs the verb only if the
    ``cd`` succeeded, whereas ``cd /elsewhere ; <verb>`` runs the verb HERE the
    moment the ``cd`` fails — and whether it fails is not knowable from the
    command text.

    Returns True (gate the command) unless EVERY occurrence of the verb is
    provably somewhere else; a command that touches this project at all must
    be gated. Every failure to establish scope — no CLAUDE_PROJECT_DIR, an
    unresolvable path, a verb this function cannot find — also returns True,
    because False here means the hook exits 0 without a decision, which the
    PreToolUse contract reads as ALLOW.
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        return True  # Can't determine scope, be safe

    try:
        project_dir = os.path.realpath(project_dir)
    except (ValueError, OSError):
        return True  # Unresolvable project dir — can't scope, be safe

    try:
        verb_positions = [m.start() for m in re.finditer(verb, cmd)]
    except re.error:
        return True  # Unusable verb pattern — can't scope, be safe
    if not verb_positions:
        # The caller matched this verb but this function cannot find it: the
        # two matchers disagree, so gate rather than guess.
        return True

    # Every "cd <target>" in the command, keyed by where it takes effect.
    cd_matches = [
        (m.start(), m.end(), m.group(2) or m.group(3) or m.group(4))
        for m in re.finditer(
            r'(?:^|[;&|]\s*)cd\s+("([^"]+)"|\'([^\']+)\'|(\S+))', cmd
        )
    ]

    for position in verb_positions:
        preceding = [(end, t) for start, end, t in cd_matches if start < position]
        if not preceding:
            # No cd before this occurrence — it runs in the session cwd, which
            # IS this project.
            return True
        cd_end, target = preceding[-1]
        # Only an unbroken run of "&&" carries the cd's effect to the verb.
        connectors = re.findall(r'[;&|\n]+', cmd[cd_end:position])
        if not connectors or any(c.strip() != "&&" for c in connectors):
            return True
        try:
            target = os.path.expanduser(target)
            target = os.path.expandvars(target)
            target = os.path.realpath(target)
        except (ValueError, OSError):
            return True  # Unresolvable target — assume it is this project
        # os.sep matters: without it "/x/proj-evil" reads as inside "/x/proj".
        if target == project_dir or target.startswith(project_dir + os.sep):
            return True

    # Every occurrence of the verb provably runs outside this project.
    return False

if not _targets_this_project(command, r"git push"):
    sys.exit(0)

# Allow tag pushes (refs/tags/*, --tags, or explicit version tags like v1.2.3)
if "refs/tags/" in command or "--tags" in command:
    sys.exit(0)
# Allow pushing an explicit version tag (e.g., "git push origin v1.2.3")
if re.search(r'git push\s+\S+\s+v\d+\.\d+\.\d+', command):
    sys.exit(0)

# Allow branch deletion (--delete) for release/hotfix cleanup
if "--delete" in command and ("release/" in command or "hotfix/" in command):
    sys.exit(0)

# Get current branch
try:
    current_branch = subprocess.check_output(
        ["git", "branch", "--show-current"],
        stderr=subprocess.DEVNULL,
        text=True
    ).strip()
except (subprocess.CalledProcessError, FileNotFoundError):
    current_branch = ""

# Allow Git Flow finish operations
# Release/hotfix branches push to both main and develop
is_release_or_hotfix_finish = (
    current_branch.startswith("release/") or
    current_branch.startswith("hotfix/")
)

if is_release_or_hotfix_finish:
    sys.exit(0)

# Git Flow finish: on main or develop, HEAD is a merge from a Git Flow branch
if current_branch in ["main", "develop"]:
    try:
        # Check if HEAD is a merge commit (has 2+ parents)
        subprocess.check_output(
            ["git", "rev-parse", "HEAD^2"],
            stderr=subprocess.DEVNULL,
            text=True
        )
        # Check if the merge message references a Git Flow branch
        merge_msg = subprocess.check_output(
            ["git", "log", "-1", "--format=%s", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()
        # main: only release/hotfix merges (features never merge to main)
        # develop: feature/release/hotfix merges + main sync
        if current_branch == "main":
            allowed = ["release/", "hotfix/"]
        else:
            allowed = ["feature/", "release/", "hotfix/", "Merge main into develop"]
        if any(pattern in merge_msg for pattern in allowed):
            sys.exit(0)
    except subprocess.CalledProcessError:
        # HEAD is not a merge commit — check for version bump after Git Flow finish
        if current_branch == "develop":
            try:
                recent_msgs = subprocess.check_output(
                    ["git", "log", "-5", "--format=%s", "HEAD"],
                    stderr=subprocess.DEVNULL,
                    text=True
                ).strip()
                if any(p in recent_msgs for p in ["release/", "hotfix/"]):
                    sys.exit(0)
            except subprocess.CalledProcessError:
                pass

# Check if command or current branch targets protected branches
# Also detect refspec pushes like "HEAD:main" or "mybranch:develop"
targets_protected = (
    "origin main" in command or
    "origin develop" in command or
    ":main" in command or
    ":develop" in command or
    current_branch in ["main", "develop"]
)

# Block direct push to main/develop (including force pushes)
if targets_protected:
    if current_branch in ["main", "develop"] or "origin main" in command or "origin develop" in command:
        reason = f"""❌ Direct push to main/develop is not allowed!

Protected branches:
  - main (production)
  - develop (integration)

Git Flow workflow:
  1. Create a feature branch:
     git checkout -b feature/<name>

  2. Make your changes and commit

  3. Push feature branch:
     git push origin feature/<name>

  4. Create pull request:
     gh pr create

  5. After PR approval, merge via GitHub

For releases:
  git checkout -b release/v<version> develop
  (prepare release, then merge to main + tag + merge back to develop)

For hotfixes:
  git checkout -b hotfix/<name> main
  (fix + merge to main + tag + merge back to develop)

Current branch: {current_branch}

💡 If the superpowers plugin is installed, use /feature, /release, /hotfix, /finish for automated workflows."""

        _deny(reason)

# Allow the command
sys.exit(0)
