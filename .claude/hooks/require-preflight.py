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
# Named without a tool prefix because `gh` takes global options in exactly the
# same shape (`gh --repo o/r pr create`), and the two gh gates carry a
# byte-identical copy of this definition (#327).
_OPT_VALUE = (
    r'(?:"[^"]*"|\'[^\']*\'|[^-\s"\'])'
    r'(?:"[^"]*"|\'[^\']*\'|[^\s"\'])*'
)
_GIT_OPTS = r'(?:\s+-[^\s]+(?:\s+' + _OPT_VALUE + r')?)*'
_GIT_COMMIT_RE = _GIT + _GIT_OPTS + r'\s+commit\b'


# Words that stand between a separator and the command that actually runs.
# `if git push …; then`, `exec git push …`, `sudo git push …` and `\git push …`
# all really do push, so the verb must stay gated behind them; `echo git push
# …` must not (#327). Block OPENERS matter as much as the `then`/`do` that
# follow them — measured, every gate allowed `if <verb>; then :; fi` while
# denying the bare verb.
_TRANSPARENT_TOKENS = frozenset({
    "!", "\\", "if", "while", "until", "then", "else", "do", "elif",
    "sudo", "env", "command", "builtin", "exec", "nohup", "nice", "time",
    "timeout", "xargs", "eval", "stdbuf",
})
# The subset of those that take arguments of their OWN before the command:
# `timeout 60 <verb>`, `nice -n 10 <verb>`, `xargs -n 1 <verb>`, `sudo -u u
# <verb>`. A separated value is a bare word, so without this list the walk
# stops at `60` and reads a real command as text — measured allowing on every
# gate (#327).
_ARG_TAKING_TOKENS = frozenset({
    "sudo", "env", "nohup", "nice", "timeout", "xargs", "stdbuf",
})
# How many words the walk will cross before giving up. Each step rescans the
# text to its left, so an unbounded walk is O(n) per step over a command line
# written by whoever is being gated; this is what keeps that bounded. It is
# not a semantic knob — running out means nothing was established, so the walk
# gives up on the GATED side.
_WALK_STEP_LIMIT = 64


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
    do run the verb on the gated side. Three groups, and every one of them was
    measured denying on ``develop`` and allowing here before it was added:

    * block openers and shell keywords — ``if <verb>; then …``,
      ``while``/``until``, ``then``/``else``/``do``/``elif``, ``!``;
    * exec wrappers — ``sudo``, ``env``, ``command``, ``builtin``, ``exec``,
      ``nohup``, ``nice``, ``time``, ``timeout``, ``xargs``, ``eval``,
      ``stdbuf``, and ``VAR=value`` assignments;
    * a bare backslash — ``\\git push …`` is the ordinary way to bypass an
      alias or a function of the same name, and the verb pattern matches at
      offset 1, leaving the escape as its own token here.

    An OPTION (``-n1``) is transparent too: it belongs to whatever precedes
    it, and that word decides. So ``echo -n git push …`` walks past ``-n``,
    reaches ``echo``, and is correctly read as text — and ``xargs -n1 git …``
    walks the same ``-n1``, reaches ``xargs``, and is gated.

    A BARE word is the hard case, because it is what a wrapper's separated
    argument looks like (``timeout 60 git push …``, ``nice -n 10 git push …``)
    AND what a command taking the verb as data looks like (``echo git push
    …``). The two are told apart by what is further left: bare words are
    carried, and count as absorbed only once an ``_ARG_TAKING_TOKENS`` wrapper
    is reached. Hit the start of the command, a separator, or a plain keyword
    with bare words still outstanding and they were arguments to something
    that is NOT a wrapper — so the verb is that thing's data, and this returns
    False.

    The walk gives up after ``_WALK_STEP_LIMIT`` words, on the gated side.
    That bound is about COST, not meaning: each step rescans the text to its
    left, so an unbounded walk is quadratic in a command line an attacker
    writes. A carry limit was tried first and proved unfalsifiable — the
    terminal check already rejects ``echo one two three four <verb>``, so
    raising the limit changed no verdict a test could see.

    Two known limits, both in the fail-open direction:

    * A verb inside a string a shell later executes (``eval "<verb>"``,
      ``bash -c "<verb>"``) is "quoted" and is dropped. That is deliberate —
      the alternative is treating every quoted mention of the verb as a
      command, which is the false deny this exists to close.
    * A QUOTED executable is not matched at all: ``"git" push …`` and
      ``g"i"t push …`` allow, here and on develop alike. Recognising them
      means joining shell words before matching, i.e. a real parser rather
      than a scanner, so the gap is documented rather than half-closed.
    """
    seps = ";&|(){}\n"
    pending_bare = 0
    for _step in range(_WALK_STEP_LIMIT):
        head = prefix.rstrip()
        # Reaching the start of the command, or a separator, settles it — but
        # only if nothing is outstanding: bare words that no wrapper ever
        # claimed were arguments to a command, and the verb is one of them.
        if not head:
            return pending_bare == 0
        if head[-1] in seps:
            return pending_bare == 0
        cut = max(head.rfind(c) for c in seps)
        token = head[cut + 1:].rsplit(None, 1)[-1]
        name, eq, _value = token.partition("=")
        if token in _ARG_TAKING_TOKENS:
            pending_bare = 0  # this is what those bare words belonged to
        elif token.startswith("-") or (eq and name.isidentifier()):
            pass  # an option or an assignment; a bare word may be its value
        elif token in _TRANSPARENT_TOKENS:
            if pending_bare:
                return False  # `time foo <verb>` runs foo, with the verb as data
        else:
            pending_bare += 1  # maybe a wrapper's argument; maybe `echo`
        prefix = head[:len(head) - len(token)]
    # More words in front of the verb than any real command line has. Nothing
    # is established either way, and an unestablished verb is a gated one.
    return True


def _argument_span(cmd: str, start: int) -> str:
    """The text of ONE command, from ``start`` to where the shell ends it.

    Every allow-side escape hatch in these gates tested a raw substring
    against the WHOLE command, while the deny side had been narrowed to the
    occurrence a shell would really run — and a gate that is narrow where it
    blocks and wide where it stands down is a gate with an off switch (#327).
    Measured: ``<push> origin main  # see refs/tags/v1`` and ``echo
    '--tags' && <push> origin main`` both stood the push gate down, and
    ``<push> --delete origin release/x && <push> origin main`` excused the
    second push with the first one's flag.

    The span ends at the first thing the shell would treat as ending this
    command — ``;``, ``&``, ``|``, a newline, a closing ``)``, ``}`` or
    backtick — or at a ``#`` that starts a comment. The backtick earns its
    place: without it ``X=`<push> origin main` `` keeps the closing backtick
    inside the span, and the last WORD of the span is then ``main` `` rather
    than ``main``, which is not a ref and not a deny. A candidate only counts
    when the text from the verb to it parses as executable, so a separator
    inside quotes (``-m "a; b"``) does not cut the span short.
    """
    i = start
    while i < len(cmd):
        c = cmd[i]
        if c in ";&|\n)}`" or (c == "#" and (i == 0 or cmd[i - 1].isspace())):
            if _shell_scan(cmd[start:i])[0] == "exec":
                return cmd[start:i]
        i += 1
    return cmd[start:]


def _verb_occurrences(cmd: str, verb: str):
    """Offsets in ``cmd`` where the shell would really RUN ``verb``.

    ONE matcher, for both consumers: the top-level guard that decides whether
    a gate looks at a command at all, and ``_targets_this_project``'s scoping.
    When those two disagree the scope helper cannot find the occurrence the
    guard matched, and ``if not verb_positions: return True`` swallows the
    divergence as "gate it" — fail closed, correct, and silent (#327).

    An occurrence is dropped only when it is provably inert: quoted inside a
    command that otherwise parses, or not in command position. Everything else
    is KEPT — including a prefix that does not parse, and every position in a
    command that does not parse — because a dropped verb means the hook exits
    0 with no decision, and the PreToolUse contract reads that as ALLOW.
    """
    # "Quoted" is a verdict about a PREFIX, and it is only evidence that the
    # verb is inert when the command as a WHOLE parses. `echo don\'t && <verb>`
    # leaves a quote open from the apostrophe onward, so every later position
    # reads as quoted — and dropping the verb there is an ALLOW, on a shape
    # develop denied. When the whole command does not parse, nothing in it is
    # provably inert.
    whole_parses = _shell_scan(cmd)[0] != "quoted"
    kept = []
    for match in re.finditer(verb, cmd):
        start = match.start()
        state, _subshells = _shell_scan(cmd[:start])
        if state == "quoted" and whole_parses:
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


def _emit(payload, deny_reason=None):
    """The ONE way this hook writes to the decision channel.

    Two ways to write it, only one of which knew the rules, is the shape that
    produced four consecutive defects in #325 — each fix hardened one path and
    left its sibling on a bare ``print()`` (#327).

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
    try:
        if sys.stdout is None:
            raise OSError("stdout unavailable")
        print(json.dumps(payload))
        sys.stdout.flush()
    except Exception:
        if deny_reason is not None:
            _warn(f"BLOCKED: {deny_reason}")
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
        # An advisory payload had ALLOW as its intended outcome and an empty
        # stdout already means allow, so exit 0 — but only after dropping the
        # streams, or CPython's finalization flush overrides the status with
        # 120 and reports "Exception ignored" on a path where nothing is wrong.
        raise SystemExit(2 if deny_reason is not None else 0)
    sys.exit(0)


def block(reason: str) -> None:
    """Deny the command. See ``_emit`` for how the decision reaches stdout."""
    _emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, reason)


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
    # "\<newline>" is a line continuation — whitespace, not a separator — and it
    # is collapsed HERE, at the one point the command is read, rather than inside
    # the scope helper alone. The verb guard runs BEFORE that helper: matching the
    # raw text, `\s+` never spans the backslash, so `git \` + newline + `push`
    # exited 0 at the guard and the helper that knows about continuations was
    # never called (#327). The helper keeps its own collapse; it is idempotent.
    command = tool_input.get("command", "").replace("\\\n", " ")

    # Only validate git commit commands
    if tool_name != "Bash":
        allow()

    # Check if this is a git commit command
    is_commit = bool(_verb_occurrences(command, _GIT_COMMIT_RE))
    is_amend = "--amend" in command

    if not is_commit:
        allow()

    # Skip this hook if the command targets a repo outside this project
    if not _targets_this_project(command, _GIT_COMMIT_RE):
        allow()

    # Check for skip flag (for emergencies - user must explicitly approve).
    # An assignment the shell would really make, not the characters anywhere in
    # the text: `git commit -m "document SKIP_PREFLIGHT=1 escape hatch"` and
    # `grep -r 'SKIP_PREFLIGHT=1' . && git commit -m wip` both disabled the
    # gate, the first by ACCIDENT, just by writing about it (#327).
    #
    # "Would really make" is the same question as "would really run", so it is
    # answered by the same walk: a separator-anchored regex was too narrow and
    # denied `env SKIP_PREFLIGHT=1 <commit>`, an ordinary spelling of the hatch
    # that develop allowed. `echo SKIP_PREFLIGHT=1 && <commit>` still denies —
    # `echo` is not a wrapper, so the assignment is its argument, not an
    # assignment at all.
    for _skip in re.finditer(r'SKIP_PREFLIGHT=1(?=\s|$)', command):
        if (_shell_scan(command[:_skip.start()])[0] == "exec"
                and _at_command_position(command[:_skip.start()])):
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
        # A token that PARSES is not yet a token that can be read: `[]`, `5`
        # and `"x"` all decode cleanly and then raise AttributeError on
        # .get() — a traceback, rc 1, and rc 1 is a NON-blocking error, so the
        # unverified commit proceeded. Same for an `expires` that is not a
        # number, which raises TypeError at the comparison instead (#327).
        if not isinstance(token_data, dict):
            raise ValueError("token is not an object")
        if isinstance(token_data.get("expires", 0), bool) or not isinstance(
            token_data.get("expires", 0), (int, float)
        ):
            raise ValueError("token expiry is not a number")
    except (ValueError, OSError):
        # ValueError covers json.JSONDecodeError and the shape checks above;
        # OSError covers IOError, which is an alias for it.
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
    _emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"✅ Preflight verified: {checks_run} | {staged_count} files"
        }
    })


if __name__ == "__main__":
    main()
