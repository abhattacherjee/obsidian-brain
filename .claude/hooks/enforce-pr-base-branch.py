#!/usr/bin/env python3
"""
PreToolUse Hook: Enforce correct PR base branch for Git Flow

- gh pr create: feature/* branches must target develop (--base develop)
- gh pr merge: verifies the PR's base branch matches Git Flow expectations
  before allowing the merge

Prevents the mistake of merging feature work directly to main.
"""
import json
import os
import re
import sys
import subprocess

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
_Q = r'''(?:"[^"]*"|'[^']*'|[^\s"'])'''
_GLOBAL_OPTS = (r'(?:-' + _Q + r'*(?:\s+(?:(?:"[^"]*"|'
                r"'[^']*'|[^-\s\"'])" + _Q + r'*))?\s+)*')

_PR_CREATE_VERB = r'\bgh\s+' + _GLOBAL_OPTS + r'pr\s+create\b'
_PR_MERGE_VERB = r'\bgh\s+' + _GLOBAL_OPTS + r'pr\s+merge\b'

# `gh -R/--repo` is deliberately NOT descoped the way `git -C` is: resolving
# an `owner/name` slug to a filesystem path needs a config or network lookup
# a PreToolUse gate must not do. So a `-R`-redirected `gh` command is still
# gated by this repo's rules — fail-closed, at the cost of a false deny on
# legitimate cross-repo `gh` work (#351).

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


input_data = _read_hook_input("PR-base hook")

tool_name = input_data.get("tool_name", "")
tool_input = input_data.get("tool_input", {})
command = tool_input.get("command", "")
# A "\<newline>" is a line continuation -- whitespace, not a separator --
# so `gh \<NL> pr create` is `gh pr create`. Normalise ONCE here, before
# both entry tests below and the --base regex they feed: an entry test
# exiting 0 with empty stdout is an ALLOW, and it read un-collapsed text
# before `_targets_this_project` ever got a chance to collapse it itself --
# that runs too late to save the entry test (#351 CRIT-2). Its own internal
# collapse stays; it is idempotent and is also exercised directly by tests.
command = command.replace("\\\n", " ")

if tool_name != "Bash":
    sys.exit(0)

# --- Project-scope guard ---
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
        c_dirs = re.findall(
            r'''(?:^|\s)-C\s+(?:"([^"]*)"|'([^']*)'|(\S+))''', m.group(0))
        if len(c_dirs) == 1:
            target = next(t for t in c_dirs[0] if t)
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


def deny(reason: str):
    _deny(reason)


def get_current_branch() -> str:
    try:
        return subprocess.check_output(
            ["git", "branch", "--show-current"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


# ── gh pr create: enforce --base develop for feature branches ──
# Use command-boundary regex to avoid false positives on strings containing
# "gh pr create" (e.g. echo, grep, heredocs).
if re.search(r'(?:^|[;&|]\s*)gh\s+' + _GLOBAL_OPTS + r'pr\s+create\b', command):
    # Skip if every occurrence targets a repo outside this project
    if not _targets_this_project(command, _PR_CREATE_VERB):
        sys.exit(0)
    branch = get_current_branch()
    if branch.startswith("feature/"):
        # Check if --base is specified (supports both --base X and --base=X)
        base_match = re.search(r'--base[=\s]+(\S+)', command)
        if not base_match:
            deny(
                f"❌ PR base branch not specified!\n\n"
                f"Feature branch '{branch}' must target develop.\n"
                f"Add --base develop to your gh pr create command:\n\n"
                f"  gh pr create --base develop ...\n\n"
                f"Without --base, GitHub defaults to main, which bypasses Git Flow."
            )
        elif base_match.group(1) != "develop":
            specified_base = base_match.group(1)
            deny(
                f"❌ Wrong PR base branch!\n\n"
                f"Feature branch '{branch}' targets '{specified_base}' but must target 'develop'.\n"
                f"Change --base to develop:\n\n"
                f"  gh pr create --base develop ..."
            )
    sys.exit(0)

# ── gh pr merge: verify base branch before merging ──
# Handles "gh pr merge 30", "gh pr merge --squash 30", and "gh pr merge" (no number).
if re.search(r'(?:^|[;&|]\s*)gh\s+' + _GLOBAL_OPTS + r'pr\s+merge\b', command):
    # Skip if every occurrence targets a repo outside this project
    if not _targets_this_project(command, _PR_MERGE_VERB):
        sys.exit(0)
    # Extract PR number from anywhere in the args (handles flags before number)
    pr_number_match = re.search(_PR_MERGE_VERB + r'.*?(\d+)', command)
    pr_number = pr_number_match.group(1) if pr_number_match else None

    if not pr_number:
        # Resolve PR number from current branch
        try:
            pr_number = subprocess.check_output(
                ["gh", "pr", "view", "--json", "number", "--jq", ".number"],
                stderr=subprocess.DEVNULL, text=True
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            deny(
                "⚠️ Cannot determine PR number from current branch.\n\n"
                "Merge blocked because the base-branch safety check could not run.\n"
                "Specify the PR number explicitly: gh pr merge <number> --squash"
            )

    if not pr_number:
        deny(
            "⚠️ No open PR found for the current branch.\n\n"
            "Merge blocked because the base-branch safety check could not run."
        )

    # Get both base and head ref from the PR itself (not current branch)
    try:
        pr_info = subprocess.check_output(
            ["gh", "pr", "view", pr_number, "--json", "baseRefName,headRefName",
             "--jq", ".baseRefName + \" \" + .headRefName"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
        base_ref, head_ref = pr_info.split(" ", 1)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        deny(
            f"⚠️ Cannot verify PR #{pr_number} base branch (gh pr view failed).\n\n"
            "Merge blocked because the safety check could not run.\n"
            "Common causes: gh auth expired, network issue, invalid PR number."
        )

    # Use the PR's head ref (not current branch) for Git Flow classification
    if head_ref.startswith("feature/") and base_ref != "develop":
        deny(
            f"❌ PR #{pr_number} targets '{base_ref}' but feature branches must merge to 'develop'!\n\n"
            f"PR head: {head_ref}\n"
            f"PR base: {base_ref}\n\n"
            f"Fix: close this PR and recreate with --base develop:\n"
            f"  gh pr close {pr_number}\n"
            f"  gh pr create --base develop"
        )

    # Release/hotfix branches merge to main
    if (head_ref.startswith("release/") or head_ref.startswith("hotfix/")) and base_ref != "main":
        deny(
            f"❌ PR #{pr_number} targets '{base_ref}' but {head_ref.split('/')[0]} branches must merge to 'main'!\n\n"
            f"PR head: {head_ref}\n"
            f"PR base: {base_ref}\n\n"
            f"Fix: close this PR and recreate with --base main:\n"
            f"  gh pr close {pr_number}\n"
            f"  gh pr create --base main"
        )

    sys.exit(0)

# Not a PR command — allow
sys.exit(0)
