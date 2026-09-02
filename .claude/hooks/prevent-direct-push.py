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
import shlex
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
_GLOBAL_OPTS = r'''(?:-\S+(?:\s+(?:"[^"]*"|'[^']*'|[^-\s]\S*))?\s+)*'''

_PUSH_VERB = r'\bgit\s+' + _GLOBAL_OPTS + r'push\b'

input_data = _read_hook_input("Push hook")

tool_name = input_data.get("tool_name", "")
tool_input = input_data.get("tool_input", {})
command = tool_input.get("command", "")

# Only validate git push commands
if tool_name != "Bash" or re.search(_PUSH_VERB, command) is None:
    sys.exit(0)

# --- Project-scope guard ---
# Skip this hook if the command targets a repo outside this project.
# Hooks run in their own process (cwd = project dir), so git commands in
# the hook inspect the wrong repo when Claude does "cd /other/repo && git push".
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

if not _targets_this_project(command, _PUSH_VERB):
    sys.exit(0)

# Allow tag pushes (refs/tags/*, --tags, or explicit version tags like v1.2.3)
if "refs/tags/" in command or "--tags" in command:
    sys.exit(0)
# Allow pushing an explicit version tag (e.g., "git push origin v1.2.3")
if re.search(r'git push\s+\S+\s+v\d+\.\d+\.\d+', command):
    sys.exit(0)

# --- Branch deletion (--delete / -d) ---
#
# This gate was two unanchored substring tests — `"--delete" in command` and
# `"release/" in command or "hotfix/" in command` — so a protected ref riding
# ALONGSIDE a release ref stood the whole hook down: `--delete origin
# release/x main` deleted `main` (#333).
#
# Tightening the allowlist alone would not have fixed it, which is why there
# is a deny here as well. Nothing downstream catches that command: "origin
# main" is not a substring of it, the refspec arms below need a `:` or a
# `heads/` spelling, and on a feature branch `current_branch` is clean.
# Measured before this change: allow. A deletion is also worse than a push to
# the same branch — a bad push can be reverted, a deleted ref is gone.
#
# Both halves are decided on REF TOKENS instead. `_PROTECTED_REF` is the
# whole-ref matcher from #332, reused so that `mainline` and `maintenance`
# stay the ordinary branches they are.
_PROTECTED_REF = r'(?:main|develop)(?![A-Za-z0-9._/-])'
_PROTECTED_REF_RE = re.compile(_PROTECTED_REF)

# Longest argument string still handed to `shlex` — see `_push_invocations`.
# No real ref name approaches this; it exists only to bound a quadratic path.
_SHLEX_MAX = 100_000

# `git push` flags whose value is a SEPARATE token — see `_ref_tokens`. Only
# these five: `--force-with-lease`, `--signed` and `--recurse-submodules` take
# their value `=`-joined only, and a bare `--force-with-lease` takes none, so
# treating any of them as separate-value would consume a real ref token.
_VALUE_FLAGS = frozenset({
    "-o", "--push-option", "--repo", "--exec", "--receive-pack",
})


def _bare_ref(token: str) -> str:
    """A ref token with the decorations git accepts stripped off.

    `main`, `heads/main`, `refs/heads/main` and `+refs/heads/main` are one
    destination; a leading `:` is the old-style delete spelling.

    Also strips a leading/trailing quote character, because it can arrive
    already attached: when `shlex.split` raises on an unbalanced quote,
    `_push_invocations` falls back to a plain whitespace split, which does
    not strip quoting the way `shlex` does. Measured: a command ending in an
    unbalanced `"` immediately followed by `main` produced the token `"main`
    from that fallback, which this function did not recognise as `main` —
    the deletion gate missed it and allowed the delete. The strip has to
    happen FIRST, before `lstrip("+:")`, so a quoted qualified ref like
    `"refs/heads/main"` loses the quote before the `refs/heads/` prefix is
    tested, not after.
    """
    token = token.strip("\"'")
    token = token.lstrip("+:")
    for prefix in ("refs/heads/", "heads/"):
        if token.startswith(prefix):
            return token[len(prefix):]
    return token


def _push_invocations(cmd: str):
    r"""One list of argument tokens per `git push` in ``cmd``.

    Split on shell separators first, so each push in a compound command is
    judged on its OWN arguments rather than on the whole string — which is the
    mistake the substring allowlist made. ``shlex`` is what strips the quotes,
    so `"refs/heads/main"` is the same token as `refs/heads/main`; a segment
    it cannot parse falls back to a whitespace split, which does NOT strip
    quotes and can mis-split — see ``_bare_ref``, which strips a stray quote
    character off a token from that path so both consumers below still
    recognise it. Extra ref tokens are still safe either way: a spurious one
    can only add a deny or withhold the stand-down, never grant one. What is
    not safe is a MALFORMED one that a downstream check fails to recognise as
    protected, which is why the quote is stripped rather than left for the
    caller to trip over.

    "\<newline>" is a line continuation, i.e. whitespace, not the command
    separator a bare newline is — same reasoning as `_targets_this_project`,
    which collapses it for the same reason. Collapse it here too, before the
    `re.split`: otherwise a delete command wrapped across a line with a
    trailing backslash splits into two segments, the second (holding the
    protected ref) has no push verb, and the ref is silently dropped instead
    of being read as part of the same invocation.

    ``shlex`` is only reached when there is quoting for it to strip, and only
    below ``_SHLEX_MAX``. It accumulates a token one character at a time, so
    it is quadratic in the length of a SINGLE argument: a ~900 KB one took
    5.6s against 0.03s for the same command before this gate existed
    (measured), and the payload cap upstream is 1 MB. Both shortcuts land on
    the same whitespace split the ``ValueError`` path uses, and its tokens are
    normalised by ``_bare_ref`` — including the quotes, which is why skipping
    ``shlex`` cannot hide a protected ref.
    """
    cmd = cmd.replace("\\\n", " ")
    invocations = []
    for part in re.split(r'[;&|\n]+', cmd):
        m = re.search(_PUSH_VERB, part)
        if not m:
            continue
        tail = part[m.end():]
        if len(tail) > _SHLEX_MAX or ('"' not in tail and "'" not in tail):
            invocations.append(tail.split())
            continue
        try:
            invocations.append(shlex.split(tail))
        except ValueError:
            invocations.append(tail.split())
    return invocations


def _is_delete(tokens) -> bool:
    """True when these arguments delete a ref.

    `-d` is bundleable: git's parse-options reads `-fd` as `--force --delete`,
    so a test for the exact token `-d` misses it. Any single-dash token
    containing a `d` counts — no other single-dash `git push` flag carries
    one, and over-reading here only ever adds a deny.
    """
    return any(
        t == "--delete"
        or (len(t) > 1 and t[0] == "-" and t[1] != "-" and "d" in t)
        for t in tokens
    )


def _ref_tokens(tokens):
    """The refs an invocation acts on: every non-flag token after the remote.

    A flag taking a SEPARATE value consumes the token after it, so that token
    is neither the remote nor a ref. Without this, `-o ci.skip --delete origin
    release/x` read `ci.skip` as the remote and `origin` as a ref, no ref was
    a `release/` one, the stand-down was withheld, and a legitimate release
    cleanup run from `develop` was DENIED — measured during the #333 review.

    Only an UNPREFIXED token is consumed as a value. A `-`-prefixed one is
    left to be read as a flag, so an over-broad entry in `_VALUE_FLAGS`
    cannot swallow a real option — and any token this declines to consume can
    only reappear as an extra ref, which is the fail-closed direction. The
    `=`-joined spellings (`--repo=origin`) need no entry at all: they are a
    single token that starts with `-`, so they already read as flags.
    """
    non_flags = []
    skip_next = False
    for t in tokens:
        if skip_next:
            skip_next = False
            if not t.startswith("-"):
                continue  # consumed as the preceding flag's value
        if t in _VALUE_FLAGS:
            skip_next = True
        if not t.startswith("-"):
            non_flags.append(t)
    return non_flags[1:]


def _protected_delete_refs(cmd: str):
    """Every protected ref ``cmd`` would delete, across all of its pushes."""
    found = []
    for tokens in _push_invocations(cmd):
        if not _is_delete(tokens):
            continue
        found.extend(
            t for t in _ref_tokens(tokens)
            if _PROTECTED_REF_RE.fullmatch(_bare_ref(t))
        )
    return found


def _is_release_cleanup_only(cmd: str) -> bool:
    """True when EVERY push here deletes nothing but release/hotfix refs.

    "Every", not "any": one release ref on the line no longer buys a
    stand-down for whatever else is on it.
    """
    invocations = _push_invocations(cmd)
    if not invocations:
        return False
    for tokens in invocations:
        refs = _ref_tokens(tokens)
        if not _is_delete(tokens) or not refs:
            return False
        if not all(
            _bare_ref(r).startswith(("release/", "hotfix/")) for r in refs
        ):
            return False
    return True


_deleted_protected = _protected_delete_refs(command)
if _deleted_protected:
    _deny("""❌ Deleting a protected branch is not allowed!

Refused ref(s): {refs}

Protected branches:
  - main (production)
  - develop (integration)

There is no revert for this. A bad push to a protected branch can be undone;
a deleted ref is gone.

To clean up a release or hotfix branch, name only those refs:

  git push origin --delete release/<version>""".format(
        refs=", ".join(_deleted_protected)))

# Allow branch deletion (--delete) for release/hotfix cleanup
if _is_release_cleanup_only(command):
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
# Also detect refspec pushes like "HEAD:main" or "mybranch:develop".
#
# The refspec arms match the protected name as a WHOLE ref. A plain
# `":main" in command` substring test also fires on `foo:mainline` and
# `HEAD:maintenance`, which are ordinary branches — that costs a false deny
# on legitimate work, and this condition only started deciding anything once
# the narrowing `if` below was removed.
# Two arms, because git spells the same destination several ways:
#   * after a colon, the destination of a refspec, optionally qualified —
#     `HEAD:main`, `HEAD:heads/main`, `HEAD:refs/heads/main`;
#   * a QUALIFIED ref standing alone, where source and destination are the
#     same — `git push origin refs/heads/main`, `+refs/heads/main`. This arm
#     requires the `heads/` prefix on purpose: a bare `main` here would match
#     the word anywhere in the command, and `"origin main"` above already
#     covers the unqualified spelling.
# The left boundary includes the quote characters: `origin "refs/heads/main"`
# is one argument to git, but a quote is not whitespace, so a boundary of
# `[\s+]` alone let the quoted spelling through while the bare one denied.
# The colon arm needs no such boundary — it anchors on the `:` itself, which
# is why `'HEAD:main'` already denied.
# _PROTECTED_REF is defined with the deletion gate above, which needs it first.
_PROTECTED_REFSPEC_RE = re.compile(
    r':(?:(?:refs/)?heads/)?' + _PROTECTED_REF
    + r'''|(?:^|[\s+'"])(?:refs/)?heads/''' + _PROTECTED_REF
)

targets_protected = (
    "origin main" in command or
    "origin develop" in command or
    _PROTECTED_REFSPEC_RE.search(command) is not None or
    current_branch in ["main", "develop"]
)

# Block direct push to main/develop (including force pushes).
#
# `targets_protected` above is the whole test. It used to be followed by a
# second `if` that re-checked a STRICTLY NARROWER condition — the current
# branch, or the literal "origin main"/"origin develop" — which no refspec
# spelling satisfies. So the `:main` and `:develop` arms were computed, and
# then discarded: `git push origin HEAD:main`, `+main:main`, `--force` and
# `--force-with-lease` all fell through to allow, under a comment claiming
# refspec pushes were detected (#327).
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
