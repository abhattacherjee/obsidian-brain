#!/usr/bin/env python3
"""
Pre-PR Hook: Block PR Creation Unless Changelog is Updated

Installed by /harden-repo into target repo's .claude/hooks/

This hook triggers before `gh pr create` commands. When the PR targets
develop (or any branch), it BLOCKS the PR if the changelog has no
meaningful entries under [Unreleased]. This ensures every PR includes
a changelog update.
"""

import json
import os
import re
import subprocess
import sys

# `gh pr create` as a command, not as text: the verb is matched through
# _verb_occurrences() so this gate and its scope check agree (#327).
_GH_PR_CREATE_RE = r'(?:[^\s;&|()<>]*/)?gh\b\s+pr\s+create\b'


def get_branch_commits():
    """Get commits on current branch not in the merge base."""
    try:
        cwd = os.environ.get("CLAUDE_PROJECT_DIR", ".")
        # Detect base: prefer develop, fall back to main
        for base in ["origin/develop", "origin/main"]:
            check = subprocess.run(
                ["git", "rev-parse", "--verify", base],
                capture_output=True, text=True, cwd=cwd
            )
            if check.returncode == 0:
                result = subprocess.run(
                    ["git", "log", f"{base}..HEAD", "--oneline", "--no-merges"],
                    capture_output=True, text=True, cwd=cwd
                )
                if result.returncode == 0:
                    commits = result.stdout.strip().split('\n')
                    return [c for c in commits if c]
        return []
    except Exception:
        return []


def check_changelog_modified_on_branch():
    """Check if CHANGELOG.md was modified on the current branch vs base."""
    try:
        cwd = os.environ.get("CLAUDE_PROJECT_DIR", ".")
        # Check against develop first, fall back to main
        for base in ["origin/develop", "origin/main"]:
            check = subprocess.run(
                ["git", "rev-parse", "--verify", base],
                capture_output=True, text=True, cwd=cwd
            )
            if check.returncode == 0:
                result = subprocess.run(
                    ["git", "diff", "--name-only", f"{base}...HEAD", "--", "CHANGELOG.md"],
                    capture_output=True, text=True, cwd=cwd
                )
                if result.returncode == 0:
                    return bool(result.stdout.strip()), base
        return None, None  # Could not determine
    except Exception:
        return None, None


def check_changelog_has_unreleased_entries():
    """Check if CHANGELOG.md has meaningful NEW entries under [Unreleased]."""
    try:
        changelog_path = os.path.join(
            os.environ.get("CLAUDE_PROJECT_DIR", "."),
            "CHANGELOG.md"
        )
        if not os.path.exists(changelog_path):
            return False, "CHANGELOG.md not found"

        with open(changelog_path, 'r') as f:
            content = f.read()

        # Find the [Unreleased] section
        unreleased_match = re.search(r'## \[Unreleased\]\s*\n(.*?)(?=\n## \[|$)', content, re.DOTALL)
        if not unreleased_match:
            return False, "[Unreleased] section not found in CHANGELOG.md"

        unreleased_body = unreleased_match.group(1)

        # Check if there are any list items (actual entries) under [Unreleased]
        # List items start with "- " after optional whitespace
        entries = [line.strip() for line in unreleased_body.split('\n')
                   if line.strip().startswith('- ')]

        if not entries:
            return False, "[Unreleased] section has no entries"

        # Entries exist — but are they new on this branch or stale?
        modified, base = check_changelog_modified_on_branch()
        if modified is None:
            # Can't determine — trust the entries exist
            return True, f"{len(entries)} entries found under [Unreleased]"
        elif modified:
            return True, f"{len(entries)} entries found under [Unreleased] (modified on this branch)"
        else:
            # Entries exist but CHANGELOG.md wasn't modified on this branch — stale!
            return False, (
                f"[Unreleased] has {len(entries)} entries, but CHANGELOG.md was NOT "
                f"modified on this branch (compared to {base}). "
                f"These are stale entries from a previous release. "
                f"Add a new entry for your changes"
            )
    except (PermissionError, OSError) as e:
        return None, f"Cannot read CHANGELOG.md: {e}"
    except Exception as e:
        return None, f"Unexpected error checking changelog: {e}"


def get_pr_base_branch(command):
    """Extract the --base branch from gh pr create command, default to develop."""
    base_match = re.search(r'--base\s+(\S+)', command)
    if base_match:
        return base_match.group(1)
    return "develop"  # default base for Git Flow


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


def block(reason):
    """Deny the command. See ``_emit`` for how the decision reaches stdout."""
    _emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, reason)


def add_context(message):
    """Add advisory context without blocking — same channel, same rules."""
    _emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": message
        }
    })


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
    input_data = _read_hook_input("Changelog hook")

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    command = tool_input.get("command", "")

    # Only check for gh pr create commands
    if tool_name != "Bash":
        sys.exit(0)

    if not _verb_occurrences(command, _GH_PR_CREATE_RE):
        sys.exit(0)

    # Skip if targeting a different repo
    if not _targets_this_project(command, _GH_PR_CREATE_RE):
        sys.exit(0)

    # Check if changelog has entries under [Unreleased]
    has_entries, reason = check_changelog_has_unreleased_entries()

    # None means filesystem error — block with the actual error, not changelog instructions
    if has_entries is None:
        block(f"❌ PR BLOCKED: {reason}")
    elif has_entries:
        add_context(f"✅ Changelog check: {reason}")
    else:
        # Get commits for context in the error message
        commits = get_branch_commits()
        commit_count = len(commits)
        commit_summary = '\n'.join(f"  - {c}" for c in commits[:5])
        if commit_count > 5:
            commit_summary += f"\n  ... and {commit_count - 5} more"

        base = get_pr_base_branch(command)

        block(f"""❌ PR BLOCKED: Changelog not updated!

{reason}

This PR targets '{base}' and has {commit_count} commit(s):
{commit_summary}

You MUST add entries under the [Unreleased] section in CHANGELOG.md
before creating this PR.

Example:
  ## [Unreleased]

  ### Added
  - Description of new feature

  ### Fixed
  - Description of bug fix

Update CHANGELOG.md, stage it, amend your commit, then retry.""")


if __name__ == "__main__":
    main()
