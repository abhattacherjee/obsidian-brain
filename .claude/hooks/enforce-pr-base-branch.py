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

_STDIN_CAP = 1_000_000
# How much command text these gates will analyse. A backstop, not the thing
# keeping them fast: since the scan became single-pass the worst adversarial
# shape measured 0.17s at this size, against 7.8s before it, and the guard
# exists for the tail beyond — 11.3s at 900 KB, which is still inside the
# 1 MB stdin cap, and a hook the harness kills has written no decision, which
# the PreToolUse contract reads as ALLOW (#327).
#
# It is checked BEFORE the verb is matched, so it also covers the cost of the
# matching itself. That makes it fail-closed on a long command this gate would
# otherwise have ignored — a heredoc writing a big file, say. Raising it is
# one constant, and the measured curve is in the changelog entry.
_COMMAND_LENGTH_CAP = 32 * 1024
_TOO_LONG = """⚠️ Command too long for the safety gate to analyse.

This gate blocks rather than judging {size} characters of command text
(limit {cap}). Shorten the command, or move the long part into a file
and run that.
"""

# `gh pr create` / `gh pr merge` as commands, not as text. Matched through
# _verb_occurrences() so the gate and its scope check use one matcher (#327).
# `gh` takes global options between the executable and the subcommand too —
# `gh --repo o/r pr create …` and `gh -R o/r pr merge 5` both parse and run
# (verified against the real binary). A rigid `gh\s+pr\s+create` never sees
# them, which is the same defect this change fixes for git, in the sibling
# gate (#327). The value grammar is the copy from the git gates, byte for
# byte; a drift test pins the copies together.
_OPT_VALUE = (
    r'(?:"[^"]*"|\'[^\']*\'|[^-\s"\'])'
    r'(?:"[^"]*"|\'[^\']*\'|[^\s"\'])*'
)
_GH_OPTS = r'(?:\s+-[^\s]+(?:\s+' + _OPT_VALUE + r')?)*'
# The path component may only START at a word start, and that negative
# lookbehind is what keeps this pattern LINEAR. Without it the engine tries
# `[^\s;&|()<>]*/` at every offset and scans forward to the end of the word
# looking for a `/` that is not there — quadratic, with ZERO matches needed to
# trigger it. Measured on `"a" * n`, no match anywhere: 0.029s at 4 KB, 0.45s
# at 16 KB, 7.20s at 64 KB, 115.7s at 256 KB, against a 1 MB stdin cap. The
# trigger is mundane — a base64 blob, a data URI, a minified bundle — and a
# hook the harness kills has written no decision, which the PreToolUse
# contract reads as ALLOW (#327). With the lookbehind the same sizes are
# 0.0003s / 0.0005s / 0.0017s / 0.0069s.
#
# It removes no match. The leftmost match of the old pattern always began at a
# word start already: if it matched at p with a non-empty path run, it would
# have matched at p-1 with a longer one. Verified rather than argued —
# identical start offsets on 20,022 probes, including `/usr/bin/git`, `./git`,
# `mygit`, `x/mygit`, `gitd`, `git/x`, `a/b/c/git`, `$HOME/bin/git`, `'git'`,
# `github` and 20,000 random strings over the alphabet that matters.
_GH = r'(?:(?<![^\s;&|()<>])[^\s;&|()<>]*/)?gh\b'
_GH_PR_CREATE_RE = _GH + _GH_OPTS + r'\s+pr\s+create\b'
_GH_PR_MERGE_RE = _GH + _GH_OPTS + r'\s+pr\s+merge\b'

# One shell word of a command. Quoted spans are part of the word they sit in,
# so `-c user.name="a b"` and `'HEAD:main'` are each ONE word — which is what
# makes "is this word a ref?" and "is this word the PR number?" answerable at
# all. Kept byte-identical in both files that carry it, like the rest of this
# block.
_WORD_RE = re.compile(r'(?:"[^"]*"|\'[^\']*\'|[^\s"\'])+')


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
# "\<newline>" is a line continuation — whitespace, not a separator — and it
# is collapsed HERE, at the one point the command is read, rather than inside
# the scope helper alone. The verb guard runs BEFORE that helper: matching the
# raw text, `\s+` never spans the backslash, so `git \` + newline + `push`
# exited 0 at the guard and the helper that knows about continuations was
# never called (#327). The helper keeps its own collapse; it is idempotent.
command = tool_input.get("command", "").replace("\\\n", " ")

if tool_name != "Bash":
    sys.exit(0)

# A command longer than this gate will analyse is blocked, not waved through.
if len(command) > _COMMAND_LENGTH_CAP:
    # `_deny`, not the `deny` wrapper: that one is defined further down the
    # file, and this runs at module level before it exists.
    _deny(_TOO_LONG.format(size=len(command), cap=_COMMAND_LENGTH_CAP))


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

# No subprocess call in these hooks had a timeout, including the ones that go
# to the network. A hook that hangs is killed by the harness with no decision
# written, and no decision is an ALLOW under the PreToolUse contract (#327).
# `subprocess.TimeoutExpired` is a `SubprocessError` but NOT a
# `CalledProcessError` and NOT an `OSError`, so every handler that catches one
# of those has to be widened with it, or the timeout lands as an uncaught
# traceback — rc 1, which is non-blocking, i.e. the same fail-open by another
# route.
_GIT_TIMEOUT = 10
_GH_TIMEOUT = 30

# A leading REDIRECTION is not a command name. `>/dev/null <verb>` and
# `2>/dev/null <verb>` run the verb, and the walk read the redirection as an
# unrecognised bare word and stood every gate down (#327). Matched at the
# START of a token: a `>` inside a word (`foo>out`) is that word's redirection,
# and the word before it is the command, which the bare-word rule already gets
# right.
_REDIRECTION = re.compile(r"\d*(?:>>?|<<?)|&>>?")
# The characters that end one command and start another. `(`/`{` open a
# command context, `)`/`}`/`` ` `` close one; either way the next word is a
# command name.
_SEPS = ";&|(){}\n`"


def _is_redirection_ampersand(text: str, i: int) -> bool:
    """True when the ``&`` at ``i`` belongs to a redirection operator.

    `>&`, `<&` and `&>` are single operators; the `&` in them separates
    nothing. Reading it as the separator a bare `&` is split `2>&1 <verb>`
    into a separator and the file descriptor `1`, which the walk then carried
    as an outstanding bare word — so the verb read as that word's argument and
    every gate stood down. The same misreading cut `<push> 2>&1 origin main`
    short of its own arguments, hiding the protected ref from the one test
    that looks for it: measured allow here against deny on `develop` (#327).

    `&&` and a lone `&` stay separators. Only ADJACENCY makes an operator, in
    this as in bash: `echo a & >out <verb>` is a background `&` followed by a
    separate redirection, and the `&` there does separate.
    """
    if i and text[i - 1] in "<>":
        return True
    return i + 1 < len(text) and text[i + 1] == ">"


def _is_separator(text: str, i: int) -> bool:
    """Does the character at ``i`` end one command and start another?"""
    return text[i] in _SEPS and not (
        text[i] == "&" and _is_redirection_ampersand(text, i))


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
    dquote = False
    while i < n:
        c = prefix[i]
        if c == "\\":
            i += 2
        elif c == "'" and not dquote:
            j = prefix.find("'", i + 1)
            if j < 0:
                return "quoted", ()
            i = j + 1
        elif c == '"':
            # A TOGGLE, not a jump over the whole span. Jumping meant nothing
            # inside `"..."` ever reached the stack, so `echo "$(<verb>)"` and
            # `` echo "`<verb>`" `` read as ordinary quoted text, the
            # occurrence was dropped, and the hook exited 0 with no decision —
            # an ALLOW on a command substitution that really does run, since
            # substitution IS performed inside double quotes. Measured against
            # `develop`, which denied all three (#327).
            dquote = not dquote
            i += 1
        elif c == "`":
            backtick = not backtick
            i += 1
        elif c == "$" and i + 1 < n and prefix[i + 1] == "(":
            stack.append(None)  # substitution — its cwd never escapes
            i += 2
        elif c == "(" and not dquote:
            stack.append(i)  # subshell — its cwd applies until it closes
            i += 1
        elif c == ")" and not dquote:
            if not stack:
                return "broken", ()
            stack.pop()
            i += 1
        else:
            i += 1
    # Order matters. The substitution test comes FIRST so that `echo "$(` is
    # "subst" and keeps its verb, rather than "quoted", which would drop it.
    if backtick or any(s is None for s in stack):
        return "subst", ()
    if dquote:
        return "quoted", ()
    return "exec", tuple(stack)



def _shell_states(cmd: str):
    """``_shell_scan``'s verdict for EVERY prefix of ``cmd``, in ONE pass.

    ``_shell_scan(cmd[:k])`` restarts at offset 0, so asking it once per match
    is quadratic in the command text. Measured on a command of repeated
    pushes: 0.33s at 14 KB, 1.72s at 32 KB, 5.27s at 56 KB — with 1 MB inside
    the stdin cap these hooks accept. A hook the harness kills has written no
    decision, and no decision is an ALLOW under the PreToolUse contract, so
    the slow path was itself a bypass (#327). A profile put 5.259s of that
    5.270s inside ``_shell_scan``, which is what this replaces.

    Entry ``k`` is exactly ``_shell_scan(cmd[:k])``, including the states that
    look surprising on their own: inside an unclosed quote it is ``quoted``,
    after an unmatched ``)`` it is ``broken`` and stays that way for every
    longer prefix, and a lone trailing ``$`` is an ordinary character because
    ``_shell_scan`` only reads ``$(`` when both characters are present.

    That equality is not a claim in a comment: ``_shell_scan`` is kept as the
    reference implementation and a test compares the two at every offset of a
    corpus of quoting, escaping and nesting shapes.

    The stack snapshot is rebuilt only when the stack actually changes, and
    shared between the offsets in between, so ordinary text costs one tuple.
    """
    exec_state = ("exec", ())
    quoted, brokenstate, substate = ("quoted", ()), ("broken", ()), ("subst", ())
    states = [exec_state]
    stack, subst_depth = [], 0
    backtick = escaped = dollar = broken = squote = dquote = False
    # The `("exec", tuple(stack))` snapshot, rebuilt only when the stack
    # changes and shared by every offset in between, so ordinary text costs
    # one tuple rather than one per character.
    snapshot, snapshot_depth = exec_state, 0

    def settle():
        # The state of this position, in `_shell_scan`'s own order: a
        # substitution wins over a quote, so `echo "$(` keeps its verb.
        nonlocal snapshot, snapshot_depth
        if backtick or subst_depth:
            return substate
        if dquote:
            return quoted
        if snapshot_depth != len(stack) or snapshot is exec_state and stack:
            snapshot, snapshot_depth = ("exec", tuple(stack)), len(stack)
        return snapshot

    for index, ch in enumerate(cmd):
        if broken:
            states.append(brokenstate)
            continue
        if squote:
            # `'...'` is literal to its closing quote, backslashes included:
            # `_shell_scan` reaches it with `find`, not by reading characters.
            if ch == "'":
                squote = False
                states.append(settle())
            else:
                states.append(quoted)
            continue
        if escaped:
            # "\<anything>" is two characters of nothing, quotes included.
            escaped = False
            dollar = False
        elif dollar and ch == "(":
            # A substitution runs even inside `"..."`, which is the whole
            # point of the double-quote fix.
            stack.append(None)  # substitution — its cwd never escapes
            subst_depth += 1
            dollar = False
        else:
            dollar = False
            if ch == "\\":
                escaped = True
            elif ch == "'" and not dquote:
                squote = True
                states.append(quoted)
                continue
            elif ch == '"':
                dquote = not dquote
            elif ch == "`":
                backtick = not backtick
            elif ch == "$":
                dollar = True
            elif ch == "(" and not dquote:
                stack.append(index)  # subshell — cwd applies until it closes
            elif ch == ")" and not dquote:
                if not stack:
                    broken = True
                    states.append(brokenstate)
                    continue
                if stack.pop() is None:
                    subst_depth -= 1
        states.append(settle())
    return states


def _at_command_position(cmd: str, start: int) -> bool:
    """True when the word beginning at ``start`` in ``cmd`` is a COMMAND.

    Indices rather than a ``cmd[:start]`` slice, and no step looks further
    left than the nearest boundary. The walk has always been bounded at
    ``_WALK_STEP_LIMIT`` WORDS, but each step used to copy the whole text to
    its left (``rstrip``) and scan all of it (``rfind``) to find a separator
    that only matters when it touches the token — so a command with no
    separators in it cost O(text) per step, per occurrence. Measured
    end-to-end on repeated pushes with no separator: 7.8s at 32 KB and 488s at
    256 KB, both of them a hang the harness ends with no decision, which the
    PreToolUse contract reads as ALLOW (#327).

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

    Three shapes put NO word in front of the verb at all, and each one stood
    all five gates down until it was handled here — every one measured denying
    on ``develop`` and allowing before this fix (#327):

    Word boundaries are exactly the characters the strip above uses, and
    nothing else. Splitting on ``str.isspace()`` instead — which is what
    ``rsplit(None, 1)`` did — treats a non-breaking space as a boundary, which
    neither this scanner's strip nor bash's IFS does: bash's IFS is space, tab
    and newline, so ``a<NBSP>b <verb>`` is the single word ``a<NBSP>b``
    followed by the verb as its ARGUMENT. One set for both means the token can
    never come back empty, which is what an earlier cut needed a guard for.

    * a NEWLINE is a separator and it IS in ``seps`` — but ``prefix.rstrip()``
      removed it before the test below could see it, so ``echo starting`` +
      newline + the verb walked on to ``starting`` and read a live command as
      text. Only whitespace that is not itself a separator may be stripped. A
      two-line Bash command is the ordinary case, which made this the widest
      hole of the three; it survived every test because a newline with nothing
      before it denies correctly, and no test put text on the first line.
    * a BACKTICK is a separator too, and it was missing while ``(`` was
      present — so ``$(<verb>)`` was gated and `` `<verb>` `` was not.
    * a leading REDIRECTION is not a name: ``>/dev/null <verb>``,
      ``2>/dev/null <verb>`` and ``<in <verb>`` all run the verb. The two
      spellings consume different amounts: an operator with its target
      attached (``>out``) claims no word of its own, while a bare operator
      claims exactly the one word to its right — so ``echo a > out <verb>`` is
      still echo's data, with only ``out`` credited to the ``>``.

    A ``#`` gets NO special case, deliberately. Everything after an unquoted
    ``#`` on a line is a comment, so a verb there is inert and "allow" is the
    honest verdict — but a rule that allows on SEEING a ``#`` token is
    quote-blind, and ``env "A=x # y" <verb>`` really does run the verb (this
    scanner splits that into the tokens ``y"``, ``#``, ``"A=x``). Left as an
    ordinary bare word, the verdict comes from the real command word further
    left, which is the word that decides in the shell too: ``echo x #
    <verb>`` allows because of ``echo``, and ``sudo # <verb>`` denies. A ``#``
    on the line BEFORE the verb is settled by the newline, not by this.

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
    pending_bare = 0
    end = start
    for _step in range(_WALK_STEP_LIMIT):
        # Every whitespace character EXCEPT the newline, which is a separator
        # the next lines are about to test for. A bare `.rstrip()` here removed
        # it first, and that one call opened all five gates to any multi-line
        # command (#327).
        while end > 0 and cmd[end - 1] in " \t\r\f\v":
            end -= 1
        # Reaching the start of the command, or a separator, settles it — but
        # only if nothing is outstanding: bare words that no wrapper ever
        # claimed were arguments to a command, and the verb is one of them.
        if end == 0:
            return pending_bare == 0
        if _is_separator(cmd, end - 1):
            return pending_bare == 0
        # The token runs back to the nearest boundary, which is whichever of a
        # separator or a space comes first. Nothing beyond it can change this
        # step, which is what keeps the step local.
        token_start = end
        while token_start > 0:
            if (cmd[token_start - 1] in " \t\r\f\v"
                    or _is_separator(cmd, token_start - 1)):
                break
            token_start -= 1
        token = cmd[token_start:end]
        name, eq, _value = token.partition("=")
        redirect = _REDIRECTION.match(token)
        if token in _ARG_TAKING_TOKENS:
            pending_bare = 0  # this is what those bare words belonged to
        elif redirect:
            # `>out <verb>` carries its target; a bare `>` took the one word to
            # its right, and that word is not the verb's command.
            if redirect.end() == len(token):
                pending_bare = max(0, pending_bare - 1)
        elif token.startswith("-") or (eq and name.isidentifier()):
            pass  # an option or an assignment; a bare word may be its value
        elif token in _TRANSPARENT_TOKENS:
            if pending_bare:
                return False  # `time foo <verb>` runs foo, with the verb as data
        else:
            pending_bare += 1  # maybe a wrapper's argument; maybe `echo`
        end = token_start
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
        if (c in ";|\n)}`"
                or (c == "&" and not _is_redirection_ampersand(cmd, i))
                or (c == "#" and (i == 0 or cmd[i - 1].isspace()))):
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
    states = _shell_states(cmd)
    whole_parses = states[-1][0] != "quoted"
    kept = []
    for match in re.finditer(verb, cmd):
        start = match.start()
        state, _subshells = states[start]
        if state == "quoted" and whole_parses:
            continue
        if not _at_command_position(cmd, start):
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
    states = _shell_states(cmd)
    cd_matches = []
    for m in re.finditer(
        r'(?:^|[;&|(]\s*)(?P<cd>cd)\s+'
        r'("(?P<dq>[^"]+)"|\'(?P<sq>[^\']+)\'|(?P<bare>\S+))', cmd
    ):
        state, subshells = states[m.start("cd")]
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
            verb_state, verb_subshells = states[position]
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


# Flags of `gh pr merge` that take a SEPARATED value. The word after one of
# these belongs to it, so it is not the positional PR number — which is how
# `gh pr merge -t "Merge PR 331" 296 --squash` came to be checked as #331 and
# merged as #296 (#327).
# Flags of `gh pr create` that take a SEPARATED value. The word after one of
# these belongs to it, so a `--base` written inside it is a mention and not a
# base: `gh pr create --title "use --base main"` really has no base at all,
# and reading one out of the title reported the wrong reason for the block.
_VALUE_TAKING_CREATE_FLAGS = frozenset({
    "-t", "--title", "-b", "--body", "-F", "--body-file", "-H", "--head",
    "-R", "--repo", "-a", "--assignee", "-l", "--label", "-p", "--project",
    "-m", "--milestone", "-r", "--reviewer",
})


def _base_values_in_span(span: str):
    """Every `--base` this create really passes, in order.

    `re.search` took the FIRST match, and `gh` honours the LAST, so
    `gh pr create --base develop --base main` created a feature->main PR and
    allowed — while the reversed spelling denied, which is the asymmetry that
    proves it was first-match rather than a rule (#327).

    Read by word, like the PR number on the merge path: `--base=X` carries its
    own value, a bare `--base` takes the next word, and the value of any other
    value-taking flag is skipped so a `--base` written inside a title or a body
    is not mistaken for one.
    """
    values, skip_next, want_base = [], False, False
    for word in _WORD_RE.findall(span):
        if want_base:
            want_base = False
            values.append(word.replace('"', "").replace("'", ""))
            continue
        if skip_next:
            skip_next = False
            continue
        if word == "--base":
            want_base = True
        elif word.startswith("--base="):
            values.append(
                word[len("--base="):].replace('"', "").replace("'", ""))
        elif word in _VALUE_TAKING_CREATE_FLAGS:
            skip_next = True
    return values


_VALUE_TAKING_MERGE_FLAGS = frozenset({
    "-b", "--body", "-t", "--subject", "-R", "--repo",
    "--body-file", "--match-head-commit",
})


def _pr_numbers_in_span(span: str):
    """Every PR number this merge really names, in order.

    The number used to be the first digit run anywhere in the span, so any
    digit inside a flag's VALUE hijacked the check. Proven against a stubbed
    `gh` where #331 targets develop (compliant) and #296 targets main (must
    block): `gh pr merge -t "Merge PR 331" 296 --squash` and `gh pr merge
    --body "closes 331" 296 --squash` both verified #331 and merged #296
    (#327).

    Only BARE words count. `_WORD_RE` keeps a quoted span inside the word it
    sits in, so a word carrying a quote is a value, not an argument this gate
    should read; a word after a value-taking flag is that flag's value even
    unquoted; and a word starting with `-` is a flag. What is left is the
    positional argument, which is the PR.

    Returns a list rather than one number so the caller can refuse to guess
    when a span names two different PRs.
    """
    numbers, skip_next = [], False
    for word in _WORD_RE.findall(span):
        if skip_next:
            skip_next = False
            continue
        if word in _VALUE_TAKING_MERGE_FLAGS:
            skip_next = True
            continue
        # The quote test is defence in depth and nothing more: `_WORD_RE`
        # keeps a quoted span inside its word, so a quoted number arrives here
        # as `"331"` and `.isdigit()` is already False for it. A mutation
        # proved that — deleting this clause turns no test red — so it is
        # documented rather than claimed as a guard. It earns its place only
        # if `_WORD_RE` ever stops carrying the quotes.
        if word.startswith("-") or '"' in word or "'" in word:
            continue
        if word.isdigit():
            numbers.append(word)
    return numbers


def deny(reason: str):
    _deny(reason)


def get_current_branch() -> str:
    """The current branch, or a BLOCK if it cannot be determined.

    Returning `""` here stood the gate down entirely: `"".startswith("feature/")`
    is False, so a broken or timed-out `git` meant no base check ran at all.
    That is the same fail-open as an uncaught exception, just quieter. A
    detached HEAD — rc 0, empty output — still returns `""`, and that state
    genuinely has no branch to classify (#327).
    """
    try:
        return subprocess.check_output(
            ["git", "branch", "--show-current"],
            stderr=subprocess.DEVNULL, text=True, timeout=_GIT_TIMEOUT
        ).strip()
    except (subprocess.SubprocessError, OSError):
        deny("""⚠️ Cannot determine the current branch.

PR command blocked because the base-branch check could not run.
`git branch --show-current` failed, timed out, or git is not executable.

Fix git, then retry.""")


# ── gh pr create: enforce --base develop for feature branches ──
# Use command-boundary regex to avoid false positives on strings containing
# "gh pr create" (e.g. echo, grep, heredocs).
# The walrus keeps the scope check INSIDE a branch whose own test matched the
# verb — the #326 invariant, and what its structural test asserts — while
# still handing the occurrences to the body, which needs them to read `--base`
# from the arguments of the create being run. Assigning on the line above
# would satisfy the invariant in fact and break the test that proves it.
if (_create_occurrences := _verb_occurrences(command, _GH_PR_CREATE_RE)):
    # Skip if every occurrence targets a repo outside this project
    if not _targets_this_project(command, _GH_PR_CREATE_RE):
        sys.exit(0)
    branch = get_current_branch()
    if branch.startswith("feature/"):
        # Every create the command runs, not just the first. Judging
        # `_create_occurrences[0]` alone let a compliant create launder every
        # later one on the same line: `gh pr create --base develop && gh pr
        # create --base main --title x` opened a feature->main PR and allowed,
        # which is precisely what this gate exists to stop (#327). The push
        # gate was fixed to loop; these two were left behind.
        for _occurrence in _create_occurrences:
            # Check if --base is specified (supports both --base X and --base=X),
            # among the arguments of the create being run — searching the whole
            # command let ANY earlier mention satisfy the check for a create that
            # had no --base of its own, and GitHub then defaults that PR to main,
            # which is exactly what this gate exists to stop: `echo --base develop
            # && gh pr create --title x` was an allow (#327).
            _bases = _base_values_in_span(
                _argument_span(command, _occurrence))
            # Two different bases in one create is not a base this gate can
            # check. `gh` would honour the last; refusing is unambiguous, and
            # a flag repeated with the SAME value still passes.
            if len(set(_bases)) > 1:
                deny(
                    "⚠️ Cannot tell which base branch this PR targets.\n\n"
                    f"The command passes --base more than once "
                    f"({', '.join(_bases)}).\n"
                    "Pass one --base."
                )
            # The LAST one, which is the one `gh` honours. Mutation-proved
            # unobservable while the check above stands: two different bases
            # never reach here, so `[-1]` and `[0]` cannot differ. Kept
            # because it is what `gh` does, not because a test needs it.
            base_match = _bases[-1] if _bases else None
            if not base_match:
                deny(
                    f"❌ PR base branch not specified!\n\n"
                    f"Feature branch '{branch}' must target develop.\n"
                    f"Add --base develop to your gh pr create command:\n\n"
                    f"  gh pr create --base develop ...\n\n"
                    f"Without --base, GitHub defaults to main, which bypasses Git Flow."
                )
            elif base_match != "develop":
                specified_base = base_match
                deny(
                    f"❌ Wrong PR base branch!\n\n"
                    f"Feature branch '{branch}' targets '{specified_base}' but must target 'develop'.\n"
                    f"Change --base to develop:\n\n"
                    f"  gh pr create --base develop ..."
                )
    sys.exit(0)

# ── gh pr merge: verify base branch before merging ──
# Handles "gh pr merge 30", "gh pr merge --squash 30", and "gh pr merge" (no number).
if (_merge_occurrences := _verb_occurrences(command, _GH_PR_MERGE_RE)):
    # Skip if every occurrence targets a repo outside this project
    if not _targets_this_project(command, _GH_PR_MERGE_RE):
        sys.exit(0)
    # Every merge the command runs, for the same reason the creates above are
    # all judged: one compliant merge must not excuse a later one.
    for _occurrence in _merge_occurrences:
        _span = _argument_span(command, _occurrence)
        _numbers = _pr_numbers_in_span(_span)
        if len(set(_numbers)) > 1:
            deny(
                "⚠️ Cannot tell which PR this merge is for.\n\n"
                f"The command names more than one PR number ({', '.join(_numbers)}), "
                "so the base-branch safety check cannot be run against the right "
                "one.\n"
                "Merge one PR per command: gh pr merge <number> --squash"
            )
        pr_number = _numbers[0] if _numbers else None

        if not pr_number:
            # Resolve PR number from current branch
            try:
                pr_number = subprocess.check_output(
                    ["gh", "pr", "view", "--json", "number", "--jq", ".number"],
                    stderr=subprocess.DEVNULL, text=True, timeout=_GH_TIMEOUT
                ).strip()
            except (subprocess.SubprocessError, OSError):
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
                stderr=subprocess.DEVNULL, text=True, timeout=_GH_TIMEOUT
            ).strip()
            base_ref, head_ref = pr_info.split(" ", 1)
        except (subprocess.SubprocessError, OSError, ValueError):
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
