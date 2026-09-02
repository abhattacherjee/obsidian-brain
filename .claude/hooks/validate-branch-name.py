#!/usr/bin/env python3
"""
PreToolUse Hook: Validate Git Flow Branch Naming

Enforces branch naming conventions: feature/*, release/v*, hotfix/*.
Validates semantic versioning for release branches.

Installed by /harden-repo into target repo's .claude/hooks/
"""
import json
import os
import sys
import re


def _shell_scan(prefix: str):
    """Where a shell would textually BE at the end of ``prefix``.

    Returns ``(executes, subshells)``.

    ``executes`` is False when the position is inside `'...'`, `"..."`,
    backticks or a ``$( )`` substitution, or when ``prefix`` does not parse
    (unterminated quote, unbalanced ``)``). A ``cd`` at such a position is
    either plain text or runs in a shell whose cwd nothing outside it ever
    sees, so honouring it stood every gate down: ``echo "x; cd /tmp" && <verb>``
    runs the verb right here (#326).

    ``subshells`` is the tuple of start offsets of the plain ``( )`` subshells
    still open. A ``cd`` inside one applies to the rest of THAT subshell and
    nothing after it closes, so a caller must check the verb is still inside
    the same ones — ``(cd /other) && <verb>`` runs the verb here.

    Anything unparseable returns ``(False, ())``: the ``cd`` is dropped, which
    leaves the command IN scope, the fail-closed direction.
    """
    i, n, stack, backtick = 0, len(prefix), [], False
    while i < n:
        c = prefix[i]
        if c == "\\":
            i += 2
        elif c == "'":
            j = prefix.find("'", i + 1)
            if j < 0:
                return False, ()
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and prefix[j] != '"':
                j += 2 if prefix[j] == "\\" else 1
            if j >= n:
                return False, ()
            i = j + 1
        elif c == "`":
            backtick = not backtick
            i += 1
        elif c == "$" and i + 1 < n and prefix[i + 1] == "(":
            stack.append(None)  # substitution — its cwd never escapes
            i += 2
        elif c == "(":
            stack.append(i)  # subshell — its cwd applies until it closes
            i += 1
        elif c == ")":
            if not stack:
                return False, ()
            stack.pop()
            i += 1
        else:
            i += 1
    if backtick or any(s is None for s in stack):
        return False, ()
    return True, tuple(stack)


def _targets_this_project(cmd: str, verb: str) -> bool:
    """Check if the command targets a repo within this project.

    Hooks run in their own process (cwd = project dir), so the git state the
    hook inspects is the wrong repo's when Claude does 'cd /other/repo && ...'.
    Parse the cd targets from the command to determine the effective repo.

    ``verb`` is the regex the caller already matched to decide this command is
    worth gating. It is used as a PATTERN, not a literal: a caller that
    interpolates anything into it — a path, a branch name — must wrap that in
    ``re.escape()``, or the positions computed here silently shift and the
    scoping decision is made about the wrong offsets.

    Only a ``cd`` that PRECEDES an occurrence of the verb can move it out of
    this project — a ``cd`` after the verb cannot change where the verb already
    acted, and honouring one turned every gate in this repo off (#326). Where
    several ``cd``s precede an occurrence the last one wins, as in the shell:
    ``cd /a && cd /b && <verb>`` runs in ``/b``.

    A ``cd`` only counts when ``&&`` is the ONLY thing between it and the verb.
    That is what makes the descoping provable: ``&&`` runs the verb only if the
    ``cd`` succeeded, whereas ``cd /elsewhere ; <verb>`` runs the verb HERE the
    moment the ``cd`` fails — and whether it fails is not knowable from the
    command text. It must also be a ``cd`` the shell would really execute, and
    one whose subshell the verb is still inside; see ``_shell_scan``.

    Returns True (gate the command) unless EVERY occurrence of the verb is
    provably somewhere else; a command that touches this project at all must
    be gated. Every failure to establish scope — no CLAUDE_PROJECT_DIR, an
    unresolvable path, a verb this function cannot find — also returns True,
    because False here means the hook exits 0 without a decision, which the
    PreToolUse contract reads as ALLOW.
    """
    # "\<newline>" is a line continuation, i.e. whitespace — not the command
    # separator a bare newline is. Collapse it before any position is computed,
    # so a legitimately descoped command that merely wraps lines is not read as
    # having a non-"&&" connector.
    cmd = cmd.replace("\\\n", " ")

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        return True  # Can't determine scope, be safe

    try:
        project_dir = os.path.realpath(project_dir)
    except (ValueError, OSError):
        return True  # Unresolvable project dir — can't scope, be safe

    try:
        verb_matches = list(re.finditer(verb, cmd))
    except re.error:
        return True  # Unusable verb pattern — can't scope, be safe
    if not verb_matches:
        # The caller matched this verb but this function cannot find it: the
        # two matchers disagree, so gate rather than guess.
        return True

    # Every "cd <target>" the shell would really run, keyed by where it takes
    # effect and by the subshells it is nested in.
    cd_matches = []
    for m in re.finditer(
        r'(?:^|[;&|(]\s*)(?P<cd>cd)\s+'
        r'("(?P<dq>[^"]+)"|\'(?P<sq>[^\']+)\'|(?P<bare>\S+))', cmd
    ):
        executes, subshells = _shell_scan(cmd[:m.start("cd")])
        if not executes:
            continue
        target = m.group("dq") or m.group("sq") or m.group("bare")
        cd_matches.append((m.start(), m.end(), target, subshells))

    for m in verb_matches:
        position = m.start()
        # `git -C <path>` retargets THIS invocation at another checkout — the
        # same thing `cd <path> &&` does for the rest of the line, and it is
        # inside the matched text because the verb pattern spans the global
        # options. Anything ambiguous (several `-C`s, whose effects compound
        # relative to each other; an unresolvable path) falls through to the
        # `cd` logic below and ends up gated, which is fail-closed.
        #
        # `m.group(0)` has no idea it might itself be sitting inside a
        # QUOTED VALUE of an unrelated option: `-c a.b="x -C /elsewhere"` is
        # ONE shell argument -- git reads it back as a single config value,
        # not a second `-C` -- so a naive search over `m.group(0)` found
        # this decoy and let it descope the whole command (#351 CRIT-4),
        # the same bypass class as #326 one layer deeper. `_shell_scan`
        # already answers "would a shell really be executing here, or is
        # this inside quotes/backticks/a substitution"; give the `-C`
        # extraction that same test, keyed by its ABSOLUTE offset in `cmd`
        # (`position + cd.start()`, not an offset into `m.group(0)`). A
        # `-C` the scan cannot vouch for is not provably real, so it is
        # dropped here and the occurrence falls through to the `cd` logic
        # below, exactly like `len(c_dirs) != 1` -- gated.
        c_dirs = [
            cd for cd in re.finditer(
                r'''(?<![^\s])-C\s+(?:"([^"]*)"|'([^']*)'|(\S+))''',
                m.group(0))
            if _shell_scan(cmd[:position + cd.start()])[0]
        ]
        if len(c_dirs) == 1:
            # `-C ""` is documented git behaviour ("if <path> is present but
            # empty, the current working directory is left unchanged") and is
            # rc 0 on git 2.50.1 -- all three capture groups are then the
            # empty string, and the old `next(t for t in c_dirs[0] if t)`
            # raised StopIteration with no target to fall back to. An
            # uncaught exception exits non-zero, which is a NON-blocking
            # error under the PreToolUse contract -- the crash itself was the
            # fail-open (#351 NEW-2). An empty/unresolved `-C` value is
            # ambiguous, not provably out of scope, so it falls through to
            # the `cd` logic below exactly like `len(c_dirs) != 1` -- gated.
            target = next((t for t in c_dirs[0].groups() if t), None)
            if target is not None:
                try:
                    target = os.path.realpath(
                        os.path.expandvars(os.path.expanduser(target)))
                except (ValueError, OSError):
                    return True
                if not (target == project_dir
                        or target.startswith(project_dir + os.sep)):
                    continue  # this occurrence provably acts on another checkout

        preceding = [c for c in cd_matches if c[0] < position]
        if not preceding:
            # No cd before this occurrence — it runs in the session cwd, which
            # IS this project.
            return True
        _, cd_end, target, subshells = preceding[-1]
        # Only an unbroken run of "&&" carries the cd's effect to the verb.
        connectors = re.findall(r'[;&|\n]+', cmd[cd_end:position])
        if not connectors or any(c.strip() != "&&" for c in connectors):
            return True
        if subshells:
            # The cd ran inside "( )". Its cwd is gone once that closes, so the
            # verb has to still be inside every subshell the cd was inside.
            verb_executes, verb_subshells = _shell_scan(cmd[:position])
            if not verb_executes or verb_subshells[:len(subshells)] != subshells:
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


# `git` and `gh` accept GLOBAL options between the executable and the
# subcommand: `git -C . push`, `git -c k=v push`, `git --no-pager push`,
# `gh --repo o/r pr create`. A literal `"git push" in command` never matches
# those, so the gate exited 0 with an empty stdout — an ALLOW under the
# PreToolUse contract, with every check below it skipped (#351, #327 item 2).
#
# A global option is always `-`-prefixed (this holds for every documented
# git and gh global option), and may take a SEPARATE value: `-C .`,
# `-c k=v`, `-R o/r`. The quoted alternatives matter because a path with a
# space (`-C "/a b"`) otherwise ends the run mid-argument and the verb stops
# matching — the same fail-open this pattern exists to close.
#
# Over-matching here can only ever ADD a gate, never remove one, so the
# pattern is deliberately permissive.
# A shell WORD is a run of non-space chars in which a quoted run may itself
# contain spaces, so `-c user.name="A B"` is ONE argument. Matching only a
# fully-quoted value (`-C "/a b"`) left the commoner half-quoted spelling
# (`-c k="v w"`) stranded mid-argument: the option-value alternatives ended
# at the first bare space, the required trailing `\s+` then landed on the
# quote-terminated remainder, which is neither a `-`-prefixed option nor the
# verb, and the match failed closed into an ALLOW (#351 CRIT-1).
#
# A quote char that never finds its matching close (`user.name=O'Brien`, a
# real git idiom and rc 0 on the real binary) made the two alternatives
# below FAIL outright rather than degrade: excluding quote chars from the
# catch-all left `_Q*`/the value continuation unable to advance past a lone
# `'`, which stopped the option/value token short and stranded the required
# trailing `\s+` on a non-space character -- another route to the same
# ALLOW (#351 NEW-1). The fix direction only ever ADDS a gate: a balanced
# quoted run is still preferred (tried first in the alternation, so a space
# inside `"A B"` still bridges), and only an UNBALANCED quote falls through
# to matching as an ordinary character, same as the old permissive `\S+`.
#
# That widened catch-all made a quote char both a balanced-run OPENER and
# an ordinary character inside a `(?:...)*`repeat, so a run of unbalanced
# quotes let the engine try both interpretations at every position and
# backtrack over all of them when the tail failed to match -- catastrophic
# backtracking. Measured on the shipped 4d67581 pattern: a 43-byte command
# (34 double quotes) took 932ms; the reviewer measured over 10s at 40. A
# PreToolUse hook runs on every Bash call with a 1MB stdin cap, so this was
# a live DoS reachable by an ordinary command containing unbalanced quotes
# (#351 CRIT-3). Fixed by making the alternatives DISJOINT with a lookahead
# instead of widening a shared catch-all: `"(?![^"]*")` matches a `"` as a
# literal character ONLY when no closing `"` exists ahead of it, so a given
# quote character is never simultaneously eligible for the balanced-run
# alternative AND the catch-all -- nothing left to backtrack between.
_Q = r'''(?:"[^"]*"|'[^']*'|"(?![^"]*")|'(?![^']*')|[^\s"'])'''
_GLOBAL_OPTS = (
    r'(?:-' + _Q + r'*(?:\s+(?:(?:"[^"]*"|'
    r"'[^']*'|\"(?![^\"]*\")|'(?![^']*')|[^-\s\"'])"
    + _Q + r'*))?\s+)*'
)

_CHECKOUT_VERB = r'\bgit\s+' + _GLOBAL_OPTS + r'checkout\s+-b\b'

input_data = _read_hook_input("Branch-name hook")

tool_name = input_data.get("tool_name", "")
tool_input = input_data.get("tool_input", {})
command = tool_input.get("command", "")
# A "\<newline>" is a line continuation -- whitespace, not a separator --
# so `git \<NL> checkout -b x` is `git checkout -b x`. Normalise ONCE here,
# before the entry test below: it exits 0 with empty stdout on no match,
# which is an ALLOW, and it read un-collapsed text before
# `_targets_this_project` ever got a chance to collapse it itself -- that
# runs too late to save the entry test (#351 CRIT-2). Its own internal
# collapse stays; it is idempotent and is also exercised directly by tests.
command = command.replace("\\\n", " ")

# Only validate git checkout -b commands
if tool_name != "Bash" or re.search(_CHECKOUT_VERB, command) is None:
    sys.exit(0)

# Skip if targeting a different repo
if not _targets_this_project(command, _CHECKOUT_VERB):
    sys.exit(0)

# Extract branch name
match = re.search(_CHECKOUT_VERB + r'\s+([^\s]+)', command)
if not match:
    sys.exit(0)

branch_name = match.group(1).strip("'\"")  # Strip shell quotes

# Allow main and develop branches
if branch_name in ["main", "develop"]:
    sys.exit(0)

# Validate Git Flow naming convention
if not re.match(r'^(feature|release|hotfix)/', branch_name):
    reason = f"""❌ Invalid Git Flow branch name: {branch_name}

Git Flow branches must follow these patterns:
  • feature/<descriptive-name>
  • release/v<MAJOR>.<MINOR>.<PATCH>
  • hotfix/<descriptive-name>

Examples:
  ✅ feature/user-authentication
  ✅ release/v1.2.0
  ✅ hotfix/critical-security-fix

Invalid:
  ❌ {branch_name} (missing Git Flow prefix)
  ❌ feat/something (use 'feature/' not 'feat/')
  ❌ fix/bug (use 'hotfix/' not 'fix/')

💡 Use Git Flow commands instead:
  /feature <name>  - Create feature branch
  /release <version> - Create release branch
  /hotfix <name>   - Create hotfix branch"""

    _deny(reason)

# Validate release version format
if branch_name.startswith("release/"):
    if not re.match(r'^release/v\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$', branch_name):
        reason = f"""❌ Invalid release version: {branch_name}

Release branches must follow semantic versioning:
  release/vMAJOR.MINOR.PATCH[-prerelease]

Valid examples:
  ✅ release/v1.0.0
  ✅ release/v2.1.3
  ✅ release/v1.0.0-beta.1

Invalid:
  ❌ release/1.0.0 (missing 'v' prefix)
  ❌ release/v1.0 (incomplete version)
  ❌ {branch_name}

💡 Use: /release v1.2.0"""

        _deny(reason)

# Allow the command
sys.exit(0)
