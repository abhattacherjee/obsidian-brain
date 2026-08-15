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
_GIT = r'(?:(?<![^\s;&|()<>])[^\s;&|()<>]*/)?git\b'
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
_GIT_PUSH_RE = _GIT + _GIT_OPTS + r'\s+push\b'

# One shell word of a command. Quoted spans are part of the word they sit in,
# so `-c user.name="a b"` and `'HEAD:main'` are each ONE word — which is what
# makes "is this word a ref?" and "is this word the PR number?" answerable at
# all. Kept byte-identical in both files that carry it, like the rest of this
# block.
_WORD_RE = re.compile(r'(?:"[^"]*"|\'[^\']*\'|[^\s"\'])+')

# A protected ref, matched WHOLE against one word. Not anchored to `origin`:
# `git push upstream main` and `git push git@github.com:o/r.git main` push
# main just as hard, and both were allowed by a rule that keyed off the remote
# NAME (#327). Once a push verb is established, a word among its arguments
# that IS `main`/`develop` is a ref, wherever it sits.
#
# Matching a whole word is also what keeps the ordinary branches out, in both
# directions and without lookarounds: `maintenance`, `develop-x` and
# `feature/main` are simply different words. `refs/heads/main` is the same ref
# written long, `+` is a force refspec, and the `^~@` tail covers the peel and
# reflog suffixes (`main^{}`, `main~1`, `main@{u}`) that name it too.
_PROTECTED_REF_RE = re.compile(
    r'\+?(?:refs/heads/)?(?:main|develop)(?:[\^~@].*)?')


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
# "\<newline>" is a line continuation — whitespace, not a separator — and it
# is collapsed HERE, at the one point the command is read, rather than inside
# the scope helper alone. The verb guard runs BEFORE that helper: matching the
# raw text, `\s+` never spans the backslash, so `git \` + newline + `push`
# exited 0 at the guard and the helper that knows about continuations was
# never called (#327). The helper keeps its own collapse; it is idempotent.
command = tool_input.get("command", "").replace("\\\n", " ")

# Only validate Bash commands. The push verb itself is matched further down,
# once the helpers that know where a shell would really run it exist.
if tool_name != "Bash":
    sys.exit(0)

# A command longer than this gate will analyse is blocked, not waved through.
if len(command) > _COMMAND_LENGTH_CAP:
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
    # Verified on this machine by running each one against `touch`: all three
    # execute their argument, so a verb behind them is a verb that runs.
    "caffeinate", "arch", "script",
})
# The subset of those that take arguments of their OWN before the command:
# `timeout 60 <verb>`, `nice -n 10 <verb>`, `xargs -n 1 <verb>`, `sudo -u u
# <verb>`. A separated value is a bare word, so without this list the walk
# stops at `60` and reads a real command as text — measured allowing on every
# gate (#327).
_ARG_TAKING_TOKENS = frozenset({
    "sudo", "env", "nohup", "nice", "timeout", "xargs", "stdbuf",
    # `script -q /dev/null <verb>` — the typescript FILE is a positional
    # argument of its own, so without this the walk stops at `/dev/null`.
    "script",
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

# A leading REDIRECTION is not a command name. `>/dev/null <verb>` and
# `2>/dev/null <verb>` run the verb, and the walk read the redirection as an
# unrecognised bare word and stood every gate down (#327). Matched at the
# START of a token: a `>` inside a word (`foo>out`) is that word's redirection,
# and the word before it is the command, which the bare-word rule already gets
# right.
_REDIRECTION = re.compile(r"\d*(?:>>?|<<?)|&>>?")
# An assignment PREFIX, in all three spellings bash accepts. `name=value` was
# recognised by `partition("=")[0].isidentifier()`; `name+=value` and
# `name[sub]=value` were not, so `A+=1 <verb>` and `A[0]=1 <verb>` read the
# assignment as an ordinary bare word and the verb as its argument — both ran
# the verb and both allowed (#327).
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\[[^]]*\])?\+?=")
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


def _heredoc_delimiter(text: str, i: int):
    """Read the here-doc introduced at ``i``, where ``text[i:i+2]`` is ``<<``.

    Returns ``(delimiter, strips_tabs, end)``, ``end`` being just past the
    delimiter word, or None when this is not a here-doc after all.

    A here-doc BODY is data — the shell hands it to a command's stdin, it does
    not execute it — and modelling that is what closes two holes at once. One
    apostrophe in a body flipped the scanner into `quoted` and a second flipped
    it back, and the SKIP_PREFLIGHT hatch was honoured from inside a body
    (#327). `<<<` is a here-STRING, not a here-doc, and is deliberately not
    matched here.
    """
    j = i + 2
    strips_tabs = False
    if j < len(text) and text[j] == "-":
        strips_tabs = True
        j += 1
    while j < len(text) and text[j] in " \t":
        j += 1
    if j >= len(text):
        return None
    quote = ""
    if text[j] in "'\"":
        quote = text[j]
        j += 1
    start = j
    while j < len(text):
        ch = text[j]
        if quote:
            if ch == quote:
                break
        elif ch in " \t\n;&|()<>":
            break
        j += 1
    delimiter = text[start:j]
    if quote:
        if j >= len(text):
            return None
        j += 1
    if not delimiter:
        return None
    return delimiter, strips_tabs, j


def _heredoc_body_end(text: str, start: int, delimiter: str,
                      strips_tabs: bool):
    """``(offset just past the body, was the terminator line seen)``.

    A body that is never terminated runs to the end of the text, which is what
    the shell would wait for and what keeps this total. The flag matters
    because both cases can end at the same offset, and they are different
    states: a body that ENDED puts the shell back in the command stream, while
    one still waiting for its terminator does not. Reading them as the same
    left the offset just past the terminator line marked as body — so a verb
    written there was dropped, and `cat <<EOF` + a body + `EOF` + the verb
    allowed. Found by the differential oracle, not by hand (#327).
    """
    i = start
    while i < len(text):
        end_of_line = text.find("\n", i)
        line = text[i:] if end_of_line < 0 else text[i:end_of_line]
        if (line.lstrip("\t") if strips_tabs else line) == delimiter:
            return (len(text) if end_of_line < 0 else end_of_line + 1), True
        if end_of_line < 0:
            return len(text), False
        i = end_of_line + 1
    return len(text), False


def _is_comment_start(text: str, i: int) -> bool:
    """Does the ``#`` at ``i`` begin a comment?

    Only at the start of a WORD, which is where bash starts one. Callers check
    the quoting themselves, because `#` inside `"..."` is an ordinary
    character — and it was exactly that difference that made an earlier
    attempt to handle comments in the command-position walk unsafe: the walk
    cannot see quotes, and this scanner can.
    """
    return i == 0 or text[i - 1] in " \t\n;&|()"


def _shell_scan(prefix: str):
    """Where a shell would textually BE at the end of ``prefix``.

    Returns ``(state, subshells)``.

    ``state`` is one of:

    * ``"exec"`` — the position is in the command stream. A command written
      here runs here, and a ``cd`` written here moves this shell.
    * ``"quoted"`` — inside `'...'` or `"..."`, including a quote this prefix
      never closes. The text is data: a ``cd`` here is not a ``cd`` and a verb
      here is not a command.
    * ``"comment"`` — after an unquoted ``#`` that starts a word, up to the end
      of that line. Data, and PROVABLY so: the shell discards it.
    * ``"heredoc"`` — inside a here-doc body. Data as far as this shell is
      concerned, but a command that READS it (``bash <<EOF``) runs it, so this
      is deliberately NOT the same state as ``"comment"``.
    * ``"subst"`` — inside `$( )` or backticks. A command here really does
      RUN, but in a subshell whose cwd nothing outside it ever sees. A
      ``${...}`` parameter expansion is tracked on the same stack, so the
      ``}`` that closes one is not mistaken for the end of a command.
    * ``"broken"`` — ``prefix`` does not parse (an unbalanced ``)``). Nothing
      can be concluded from it.

    The states are distinguished, rather than collapsed into one boolean,
    because the callers fail closed in OPPOSITE directions (#327): dropping a
    ``cd`` leaves the command in scope, dropping a VERB stands the gate down.
    So a ``cd`` counts only in ``"exec"`` — honouring one anywhere else stood
    every gate down, as ``echo "x; cd /tmp" && <verb>`` runs the verb right
    here (#326) — while a verb counts everywhere except ``"quoted"`` and
    ``"comment"``, the two states that prove it is inert.

    ``subshells`` is the tuple of start offsets of the plain ``( )`` subshells
    still open. A ``cd`` inside one applies to the rest of THAT subshell and
    nothing after it closes, so a caller must check the verb is still inside
    the same ones — ``(cd /other) && <verb>`` runs the verb here.

    This restarts at offset 0 on every call and is the REFERENCE
    implementation; ``_shell_states`` is the single-pass version every gate
    consults, and a test compares the two at every offset of a corpus.
    """
    i, n = 0, len(prefix)
    stack, backtick, dquote = [], False, False
    pending = []
    while i < n:
        c = prefix[i]
        if pending and c == "\n":
            i += 1
            terminated = False
            for delimiter, strips_tabs in pending:
                i, terminated = _heredoc_body_end(prefix, i, delimiter,
                                                  strips_tabs)
            pending = []
            if i >= n and not terminated:
                return "heredoc", ()
            continue
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
            # `` echo "`<verb>`" `` read as ordinary quoted text and the
            # occurrence was dropped — an ALLOW on a substitution that really
            # runs, since substitution IS performed inside double quotes.
            dquote = not dquote
            i += 1
        elif c == "`":
            backtick = not backtick
            i += 1
        elif c == "$" and i + 1 < n and prefix[i + 1] == "(":
            stack.append(None)  # substitution — its cwd never escapes
            i += 2
        elif c == "$" and i + 1 < n and prefix[i + 1] == "{":
            # A parameter expansion is not a command context, but its `}` must
            # not read as the end of one: `<push> origin ${FORCE} main` had its
            # span cut at the brace, and the protected ref was never seen.
            stack.append("{")
            i += 2
        elif c == "(" and not dquote:
            stack.append(i)  # subshell — its cwd applies until it closes
            i += 1
        elif c == ")" and not dquote:
            if not stack or stack[-1] == "{":
                return "broken", ()
            stack.pop()
            i += 1
        elif c == "}" and not dquote and stack and stack[-1] == "{":
            stack.pop()
            i += 1
        elif (c == "#" and not dquote and not backtick
              and _is_comment_start(prefix, i)):
            j = prefix.find("\n", i)
            if j < 0:
                return "comment", ()
            i = j
        elif (c == "<" and not dquote and prefix[i:i + 2] == "<<"
              and prefix[i:i + 3] != "<<<"):
            found = _heredoc_delimiter(prefix, i)
            if found is None:
                i += 2
            else:
                delimiter, strips_tabs, end = found
                pending.append((delimiter, strips_tabs))
                i = end
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
    the slow path was itself a bypass (#327).

    Entry ``k`` is ``_shell_scan(cmd[:k])``, and a test pins that at every
    offset of a corpus rather than trusting this sentence. The two disagree in
    exactly one place, which that test excludes and names: offsets strictly
    INSIDE a here-doc introducer (between ``<<`` and the end of its delimiter
    word). A truncated prefix makes ``_shell_scan`` fall back to ordinary
    quote scanning there, while this has the whole word in hand; the vector is
    the stricter of the two, since ``exec`` keeps a verb where ``quoted``
    drops it.

    The stack snapshot is rebuilt only when the stack actually CHANGES, and
    shared between the offsets in between. The key is the stack itself: keying
    it on the stack's LENGTH returned a stale tuple whenever a pop and a push
    left the depth unchanged, which ``_shell_states("(`)(`")[5]`` did — the
    shortest counterexample is five characters long, which is why the corpus
    is exhaustive to five.
    """
    exec_state = ("exec", ())
    quoted, brokenstate = ("quoted", ()), ("broken", ())
    substate, commentstate = ("subst", ()), ("comment", ())
    heredocstate = ("heredoc", ())
    states = [exec_state]
    stack, subst_depth = [], 0
    backtick = escaped = dollar = broken = squote = dquote = comment = False
    pending, body_end, body_terminated = [], -1, False
    snapshot, snapshot_stack = exec_state, ()

    def settle():
        # The state of this position, in `_shell_scan`'s own order: a
        # substitution wins over a quote, so `echo "$(` keeps its verb.
        nonlocal snapshot, snapshot_stack
        if backtick or subst_depth:
            return substate
        if dquote:
            return quoted
        current = tuple(stack)
        if current != snapshot_stack:
            snapshot, snapshot_stack = ("exec", current), current
        return snapshot

    index = 0
    while index < len(cmd):
        ch = cmd[index]
        if broken:
            states.append(brokenstate)
            index += 1
            continue
        if index < body_end:
            # `body_end` is the offset just PAST the body, so the state AT it
            # is the command stream again — but only when the body actually
            # ended. A body still waiting for its terminator runs to the end
            # of the text and every offset in it is still inside it.
            states.append(heredocstate
                          if index + 1 < body_end or not body_terminated
                          else settle())
            index += 1
            continue
        if comment:
            if ch == "\n":
                comment = False
                states.append(settle())
            else:
                states.append(commentstate)
            index += 1
            continue
        if squote:
            # `'...'` is literal to its closing quote, backslashes included:
            # `_shell_scan` reaches it with `find`, not by reading characters.
            if ch == "'":
                squote = False
                states.append(settle())
            else:
                states.append(quoted)
            index += 1
            continue
        if escaped:
            # "\<anything>" is two characters of nothing, quotes included.
            escaped = False
            dollar = False
            states.append(settle())
            index += 1
            continue
        if pending and ch == "\n":
            index += 1
            body_end, body_terminated = index, False
            for delimiter, strips_tabs in pending:
                body_end, body_terminated = _heredoc_body_end(
                    cmd, body_end, delimiter, strips_tabs)
            pending = []
            # The offset just past the newline is already inside the body, and
            # that is what `_shell_scan` reports for a prefix ending there —
            # unless the body is empty AND terminated, which puts the shell
            # straight back in the command stream.
            states.append(heredocstate
                          if index < body_end or not body_terminated
                          else settle())
            continue
        if dollar and ch in "({":
            # A substitution runs even inside `"..."`, which is the whole
            # point of the double-quote fix.
            stack.append(None if ch == "(" else "{")
            if ch == "(":
                subst_depth += 1
            dollar = False
            states.append(settle())
            index += 1
            continue
        dollar = False
        if ch == "\\":
            escaped = True
        elif ch == "'" and not dquote:
            squote = True
            states.append(quoted)
            index += 1
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
            if not stack or stack[-1] == "{":
                broken = True
                states.append(brokenstate)
                index += 1
                continue
            if stack.pop() is None:
                subst_depth -= 1
        elif ch == "}" and not dquote and stack and stack[-1] == "{":
            stack.pop()
        elif (ch == "#" and not dquote and not backtick
              and _is_comment_start(cmd, index)):
            comment = True
            states.append(commentstate)
            index += 1
            continue
        elif (ch == "<" and not dquote and cmd[index:index + 2] == "<<"
              and cmd[index:index + 3] != "<<<"):
            found = _heredoc_delimiter(cmd, index)
            if found is not None:
                delimiter, strips_tabs, end = found
                pending.append((delimiter, strips_tabs))
                while index < end:
                    states.append(settle())
                    index += 1
                continue
        states.append(settle())
        index += 1
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
        redirect = _REDIRECTION.match(token)
        if token in _ARG_TAKING_TOKENS:
            pending_bare = 0  # this is what those bare words belonged to
        elif redirect:
            # `>out <verb>` carries its target; a bare `>` took the one word to
            # its right, and that word is not the verb's command.
            if redirect.end() == len(token):
                pending_bare = max(0, pending_bare - 1)
        elif token.startswith("-") or _ASSIGNMENT.match(token):
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


def _argument_span(cmd: str, start: int, states=None) -> str:
    """The text of ONE command, from ``start`` to where the shell ends it.

    Every allow-side escape hatch in these gates tested a raw substring
    against the WHOLE command, while the deny side had been narrowed to the
    occurrence a shell would really run — and a gate that is narrow where it
    blocks and wide where it stands down is a gate with an off switch (#327).

    A candidate ends the command only when the shell is at the SAME level
    there as it is at ``start``, which is what the state vector answers. The
    earlier version asked ``_shell_scan(cmd[start:i])`` per candidate, and
    that was wrong twice over. It was quadratic — 1.09s at 5 KB, 4.23s at
    10 KB, 16.97s at 20 KB, all of it under the 32 KB length cap, and a hook
    the harness kills writes no decision. And it could not tell an OPENING
    delimiter from a closing one: ``<push> origin `:` main`` ended the span at
    the first backtick and ``<push> origin ${FORCE} main`` ended it at the
    brace, so in both the protected ref sat outside the span the gate reads.

    A backtick therefore ends the span only when it CLOSES the substitution
    the span is inside — which is what ``X=`<push> origin main` `` needs, and
    what an opening backtick must not do.
    """
    if states is None:
        states = _shell_states(cmd)
    here = states[start]
    i = start
    while i < len(cmd):
        if states[i] == here:
            c = cmd[i]
            if (c in ";|\n)}"
                    or (c == "&" and not _is_redirection_ampersand(cmd, i))
                    or (c == "#" and (i == 0 or cmd[i - 1].isspace()))):
                return cmd[start:i]
            if (c == "`" and here[0] == "subst"
                    and states[i + 1][0] != "subst"):
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
    # develop denied.
    #
    # That parity rule closes the ODD case and nothing else, which is how a
    # comment carrying ONE apostrophe stood all five gates down: `# don\'t` +
    # newline + the verb + newline + `# it\'s fine` has two, so the command as
    # a whole parses, the verb reads as quoted, and the occurrence was dropped
    # (#327). What closes it is the scanner knowing what a comment IS — and
    # the same is true of a here-doc body, where the trick works just as well.
    #
    # The two are not the same verdict, though. A comment is PROVABLY inert:
    # the shell discards it, so its verb is dropped outright, with no appeal
    # to whether the rest of the command parses. A here-doc body is data to
    # THIS shell but a command that reads it (`bash <<EOF`) runs it, so a verb
    # there is kept and gated.
    states = _shell_states(cmd)
    whole_parses = states[-1][0] != "quoted"
    kept = []
    for match in re.finditer(verb, cmd):
        start = match.start()
        state, _subshells = states[start]
        if state == "comment":
            continue
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

# Only validate git push commands
occurrences = _verb_occurrences(command, _GIT_PUSH_RE)
if not occurrences:
    sys.exit(0)

if not _targets_this_project(command, _GIT_PUSH_RE):
    sys.exit(0)

_VERSION_TAG_RE = re.compile(r'v\d+\.\d+\.\d+[-+.\w]*')


def _is_git_flow_push(span: str) -> bool:
    """Pushes this gate deliberately permits, judged on ONE push's own WORDS.

    Scoping each hatch to its own command was half the fix (#327). Within that
    span the tests were still substrings, so hatch text sitting in the SAME
    push's argument list stood the gate down — and all four of these are real
    git that really does push main:

        <push> origin refs/tags/v1 main
        <push> --tags origin main          (--tags pushes tags AS WELL AS the refspec)
        <push> --delete origin release/x main
        <push> --force origin main refs/tags/v1

    Two changes. A hatch must now match a whole ARGUMENT WORD — `--tags` and
    `--delete` as words, and a `refs/tags/…` that IS a pushed ref rather than
    any word containing that text. And a hatch no longer decides on its own:
    the caller consults it only for a push that names NO protected ref, so a
    push carrying both a hatch word and `main` is denied on the `main`.

    The word list STOPS at the first redirection, because a redirect and its
    target are not arguments to `git push`. Without that, `<push> origin main
    2> refs/tags/err` and `<push> origin main <(echo --tags)` read a hatch out
    of a filename and a subshell. Truncating can only make this return False,
    which is the safe direction for an allow-side test.
    """
    words = []
    for match in _WORD_RE.finditer(span):
        word = match.group()
        if _REDIRECTION.match(word):
            break
        words.append(word.replace('"', "").replace("'", ""))
    # Tags are not branches: --tags, a refs/tags/* ref, or a version tag.
    if "--tags" in words:
        return True
    if any(word.startswith("refs/tags/") for word in words):
        return True
    if any(_VERSION_TAG_RE.fullmatch(word) for word in words):
        return True
    # Branch deletion for release/hotfix cleanup.
    if "--delete" in words and any(
            word.startswith(("release/", "hotfix/")) for word in words):
        return True
    return False


def _pushes_a_protected_ref(span: str) -> bool:
    """A protected ref among this push's arguments.

    Word by word, rather than by position in the text. A position test cannot
    tell `origin 'main'` — a quoted ARGUMENT, and a real push to main — from
    the `main` inside `-o "deploy to main"`, which is one word of a sentence.
    Whole words separate them: the first IS the ref, the second is not a word
    at all.

    Quotes are dropped before the comparison because a shell drops them: the
    word `'main'` and the word `main` name the same branch. A refspec is
    tried by its destination as well as whole, since `HEAD:main` and
    `+main:main` push main under a name that is not `main`.
    """
    for word in _WORD_RE.findall(span):
        word = word.replace('"', "").replace("'", "")
        if _PROTECTED_REF_RE.fullmatch(word):
            return True
        if ":" in word and _PROTECTED_REF_RE.fullmatch(word.rsplit(":", 1)[-1]):
            return True
    return False


# Get current branch
try:
    current_branch = subprocess.check_output(
        ["git", "branch", "--show-current"],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=_GIT_TIMEOUT,
    ).strip()
except (subprocess.SubprocessError, OSError):
    # The `""` fallback that used to live here was documented as "safe in the
    # deny direction" and was not: `""` starts with neither `release/` nor
    # `hotfix/` and is not in `["main", "develop"]`, so a push whose only
    # offence is the branch it is ON — `git push` and `git push -f` from
    # `main` — sailed through. Measured with a non-executable `git` on PATH:
    # both denied with a working git and both ALLOWED with a broken one
    # (#327). Failing to determine the branch is not evidence about the
    # branch, so it blocks.
    #
    # rc 0 with EMPTY output is a different state and keeps its old meaning: a
    # detached HEAD genuinely has no branch name, and `git branch
    # --show-current` reports that by succeeding and printing nothing.
    _deny("""⚠️ Cannot determine the current branch.

Push blocked because the protected-branch check could not run.
`git branch --show-current` failed, timed out, or git is not executable.

Fix git, then retry the push.""")

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
            text=True,
            timeout=_GIT_TIMEOUT,
        )
        # Check if the merge message references a Git Flow branch
        merge_msg = subprocess.check_output(
            ["git", "log", "-1", "--format=%s", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_GIT_TIMEOUT,
        ).strip()
        # main: only release/hotfix merges (features never merge to main)
        # develop: feature/release/hotfix merges + main sync
        if current_branch == "main":
            allowed = ["release/", "hotfix/"]
        else:
            allowed = ["feature/", "release/", "hotfix/", "Merge main into develop"]
        if any(pattern in merge_msg for pattern in allowed):
            sys.exit(0)
    except (subprocess.SubprocessError, OSError):
        # HEAD is not a merge commit — check for version bump after Git Flow finish
        if current_branch == "develop":
            try:
                recent_msgs = subprocess.check_output(
                    ["git", "log", "-5", "--format=%s", "HEAD"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=_GIT_TIMEOUT,
                ).strip()
                if any(p in recent_msgs for p in ["release/", "hotfix/"]):
                    sys.exit(0)
            except (subprocess.SubprocessError, OSError):
                pass

# Judge each push the command runs, on its own arguments.
#
# Refspec pushes — "HEAD:main", "mybranch:develop", "+main:main",
# "HEAD:refs/heads/main" — are pushes to a protected branch that never say
# "origin main". They were listed in the condition below and then a second
# `if` re-tested a STRICTLY NARROWER one that no refspec form could satisfy,
# so every one of them fell through to allow (#327). There is one condition
# now, and it is per occurrence: a command that runs several pushes is judged
# once for each, so no push can be excused by another push's flags.
# Computed ONCE for the whole command and handed to every span: the
# helper recomputes it otherwise, which is O(text) per occurrence and
# was the third quadratic site in these gates (#327).
_states = _shell_states(command)
for _occurrence in occurrences:
    _span = _argument_span(command, _occurrence, _states)
    # A protected ref among this push's own arguments is decided FIRST, and no
    # hatch is consulted for it. The hatch used to be tested before anything
    # else, so a single `--tags` or `refs/tags/v1` word in the same argument
    # list excused a push of `main` sitting right beside it (#327). What the
    # hatch is for is a push that names no protected ref and is denied only
    # because of the branch it is ON — a release tag push from `main`.
    if not _pushes_a_protected_ref(_span):
        if current_branch not in ["main", "develop"]:
            continue
        if _is_git_flow_push(_span):
            continue
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
