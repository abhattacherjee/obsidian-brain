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

# Git accepts global options BETWEEN the executable and the subcommand, so the
# literal "git push" this used to test for simply does not occur in
# `git -C . push origin main` — and the hook exited 0, allowing the push,
# before any other logic ran (#327). An absolute or relative path to the
# executable counts too: `/usr/bin/git push …` is a push.
#
# ONE leading dash, deliberately, not `-{1,2}`: `-[^\s]+` already matches
# `--no-pager` (a dash, then `-no-pager`), while spelling it `-{1,2}` gives the
# engine two ways to split every long option, and the command text here is
# written by whoever is being gated. Measured on a run of such options with no
# subcommand to reach: 26 of them cost 16.7s of backtracking, against 0.002s
# for 5000 of these. Capping the repetition would cap the cost too — and hand
# back a bypass, since one option past the cap stops the gate matching at all.
_GIT = r'(?:[^\s;&|()<>]*/)?git\b'
# An option's value may be quoted, and the quotes may be around only part of
# it (`-c user.name="a b"`), so a value is a run of quoted spans and bare
# characters rather than one `[^\s]+` token. The alternatives are disjoint on
# their first character, so this adds no ambiguity to backtrack through.
_GIT_OPT_VALUE = (
    r'(?:"[^"]*"|\'[^\']*\'|[^-\s"\'])'
    r'(?:"[^"]*"|\'[^\']*\'|[^\s"\'])*'
)
_GIT_OPTS = r'(?:\s+-[^\s]+(?:\s+' + _GIT_OPT_VALUE + r')?)*'
_GIT_PUSH_RE = _GIT + _GIT_OPTS + r'\s+push\b'

# A whole ref, not a prefix: `origin maintenance` and `foo:mainline` are
# ordinary branches, and both used to DENY (#327). `refs/heads/main` is the
# same ref written long, and `+main:main` is a force refspec.
_PROTECTED_REMOTE_RE = re.compile(
    r'\borigin\s+\+?(?:refs/heads/)?(?:main|develop)(?![\w./-])')
_PROTECTED_REFSPEC_RE = re.compile(
    r':(?:refs/heads/)?(?:main|develop)(?![\w./-])')


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

# Only validate Bash commands. The push verb itself is matched further down,
# once the helpers that know where a shell would really run it exist.
if tool_name != "Bash":
    sys.exit(0)

# --- Project-scope guard ---
# Skip this hook if the command targets a repo outside this project.
# Hooks run in their own process (cwd = project dir), so git commands in
# the hook inspect the wrong repo when Claude does "cd /other/repo && git push".
# Words that stand between a separator and the command that actually runs.
# `sudo git push …` and `env A=b git push …` really do push, so the verb must
# stay gated behind them; `echo git push …` must not (#327).
_TRANSPARENT_TOKENS = frozenset({
    "!", "then", "else", "do", "elif", "sudo", "env", "command", "nohup",
    "nice", "time", "xargs", "eval", "stdbuf",
})


def _shell_scan(prefix: str):
    """Where a shell would textually BE at the end of ``prefix``.

    Returns ``(state, subshells)``.

    ``state`` is one of:

    * ``"exec"`` — the position is in the command stream. A command written
      here runs here, and a ``cd`` written here moves this shell.
    * ``"quoted"`` — inside `'...'` or `"..."`, including a quote this prefix
      never closes. The text is data: a ``cd`` here is not a ``cd`` and a verb
      here is not a command.
    * ``"subst"`` — inside `$( )` or backticks. A command here really does
      RUN, but in a subshell whose cwd nothing outside it ever sees.
    * ``"broken"`` — ``prefix`` does not parse (an unbalanced ``)``). Nothing
      can be concluded from it.

    The states are distinguished, rather than collapsed into one boolean,
    because the two callers fail closed in OPPOSITE directions (#327):
    dropping a ``cd`` leaves the command in scope, dropping a VERB stands the
    gate down. So a ``cd`` counts only in ``"exec"`` — honouring one anywhere
    else stood every gate down, as ``echo "x; cd /tmp" && <verb>`` runs the
    verb right here (#326) — while a verb counts everywhere except
    ``"quoted"``, the one state that proves it is inert.

    ``subshells`` is the tuple of start offsets of the plain ``( )`` subshells
    still open. A ``cd`` inside one applies to the rest of THAT subshell and
    nothing after it closes, so a caller must check the verb is still inside
    the same ones — ``(cd /other) && <verb>`` runs the verb here.
    """
    i, n, stack, backtick = 0, len(prefix), [], False
    while i < n:
        c = prefix[i]
        if c == "\\":
            i += 2
        elif c == "'":
            j = prefix.find("'", i + 1)
            if j < 0:
                return "quoted", ()
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and prefix[j] != '"':
                j += 2 if prefix[j] == "\\" else 1
            if j >= n:
                return "quoted", ()
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
                return "broken", ()
            stack.pop()
            i += 1
        else:
            i += 1
    if backtick or any(s is None for s in stack):
        return "subst", ()
    return "exec", tuple(stack)


def _at_command_position(prefix: str) -> bool:
    """True when a word starting at the end of ``prefix`` is a COMMAND.

    ``echo git push origin main`` is not a push, it is an argument — but a
    gate that matches its verb as text denies it anyway, and that false deny
    blocked this project's own tooling repeatedly (#327). A verb is a command
    at the start of the line, or straight after a separator.

    Walking back over TRANSPARENT tokens is what keeps the forms that really
    do run the verb on the gated side: ``sudo git …``, ``env A=b git …``,
    ``xargs -n1 git …``, ``time git …``. An option (``-n1``) is transparent
    too — it belongs to whatever precedes it, and that word decides. So
    ``echo -n git push …`` walks past ``-n``, reaches ``echo``, and is
    correctly read as text.

    Known limit, in the fail-open direction: a verb inside a string a shell
    later executes (``eval "<verb>"``, ``bash -c "<verb>"``) is "quoted" and
    is dropped. That is deliberate — the alternative is treating every quoted
    mention of the verb as a command, which is the false-deny this closes.
    """
    seps = ";&|(){}\n"
    while True:
        head = prefix.rstrip()
        if not head:
            return True  # first word of the command line
        if head[-1] in seps:
            return True  # straight after a separator
        cut = max(head.rfind(c) for c in seps)
        token = head[cut + 1:].rsplit(None, 1)[-1]
        name, eq, _value = token.partition("=")
        if not (token in _TRANSPARENT_TOKENS
                or token.startswith("-")
                or (eq and name.isidentifier())):
            return False
        prefix = head[:len(head) - len(token)]


def _verb_occurrences(cmd: str, verb: str):
    """Offsets in ``cmd`` where the shell would really RUN ``verb``.

    ONE matcher, for both consumers: the top-level guard that decides whether
    a gate looks at a command at all, and ``_targets_this_project``'s scoping.
    When those two disagree the scope helper cannot find the occurrence the
    guard matched, and ``if not verb_positions: return True`` swallows the
    divergence as "gate it" — fail closed, correct, and silent (#327).

    An occurrence is dropped only when it is provably inert: quoted, or not in
    command position. Everything else is KEPT, including a prefix that does
    not parse, because a dropped verb means the hook exits 0 with no decision
    and the PreToolUse contract reads that as ALLOW.
    """
    kept = []
    for match in re.finditer(verb, cmd):
        start = match.start()
        state, _subshells = _shell_scan(cmd[:start])
        if state == "quoted":
            continue
        if not _at_command_position(cmd[:start]):
            continue
        kept.append(start)
    return kept


def _targets_this_project(cmd: str, verb: str) -> bool:
    """Check if the command targets a repo within this project.

    Hooks run in their own process (cwd = project dir), so the git state the
    hook inspects is the wrong repo's when Claude does 'cd /other/repo && ...'.
    Parse the cd targets from the command to determine the effective repo.

    ``verb`` is the regex the caller already matched to decide this command is
    worth gating. It is used as a PATTERN, not a literal: a caller that
    interpolates anything into it — a path, a branch name — must wrap that in
    ``re.escape()``, or the positions computed here silently shift and the
    scoping decision is made about the wrong offsets. It must be the SAME
    pattern the caller matched, and it is applied through the same
    ``_verb_occurrences()``, so the two cannot disagree about where the verb is.

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
        verb_positions = _verb_occurrences(cmd, verb)
    except re.error:
        return True  # Unusable verb pattern — can't scope, be safe
    if not verb_positions:
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
        state, subshells = _shell_scan(cmd[:m.start("cd")])
        if state != "exec":
            continue
        target = m.group("dq") or m.group("sq") or m.group("bare")
        cd_matches.append((m.start(), m.end(), target, subshells))

    for position in verb_positions:
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
            verb_state, verb_subshells = _shell_scan(cmd[:position])
            if verb_state != "exec" or verb_subshells[:len(subshells)] != subshells:
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

# Only validate git push commands
if not _verb_occurrences(command, _GIT_PUSH_RE):
    sys.exit(0)

if not _targets_this_project(command, _GIT_PUSH_RE):
    sys.exit(0)

# Allow tag pushes (refs/tags/*, --tags, or explicit version tags like v1.2.3)
if "refs/tags/" in command or "--tags" in command:
    sys.exit(0)
# Allow pushing an explicit version tag (e.g., "git push origin v1.2.3")
if re.search(_GIT_PUSH_RE + r'\s+\S+\s+v\d+\.\d+\.\d+', command):
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
except (subprocess.CalledProcessError, OSError):
    # OSError, not FileNotFoundError: a `git` on PATH that exists but is not
    # executable raises PermissionError, which is an OSError and was NOT
    # caught — an uncaught exception exits 1, and rc 1 is a NON-blocking error
    # under the PreToolUse contract, so the push proceeded (#327). The ""
    # fallback below is safe in the deny direction.
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
    except (subprocess.CalledProcessError, OSError):
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
            except (subprocess.CalledProcessError, OSError):
                pass

# Check if command or current branch targets protected branches.
# Refspec pushes — "HEAD:main", "mybranch:develop", "+main:main",
# "HEAD:refs/heads/main" — are pushes to a protected branch that never say
# "origin main". They were already listed here, and then a second `if`
# re-tested a STRICTLY NARROWER condition that no refspec form could satisfy,
# so every one of them fell through to allow (#327). There is one condition
# now, and it is this one.
targets_protected = (
    _PROTECTED_REMOTE_RE.search(command) is not None or
    _PROTECTED_REFSPEC_RE.search(command) is not None or
    current_branch in ["main", "develop"]
)

# Block direct push to main/develop (including force pushes)
if targets_protected:
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
