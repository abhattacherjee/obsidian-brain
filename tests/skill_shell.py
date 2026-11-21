"""Helpers for testing shell commands in SKILL.md files."""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1] / "hooks"
SHELL_LANGS = frozenset({"bash", "sh", "shell", "zsh"})
_OPENER = re.compile(r"([ \t]*)(`{3,}|~{3,})(.*)")
# The language is the info string's first token up to the first character that
# is not one of these, so `bash:x`, `sh,x` and `bash{.x}` read as shell.
_LANG = re.compile(r"[A-Za-z0-9_+-]*")


@dataclass(frozen=True)
class Block:
    line: int
    info: str
    body: str


def fenced_blocks(text: str, langs=SHELL_LANGS) -> list[Block]:
    """Read whole fences before filtering by language."""
    blocks = []
    lines = enumerate(text.splitlines(), start=1)
    for number, line in lines:
        match = _OPENER.fullmatch(line)
        if not match:
            continue
        indent, fence, raw_info = match.groups()
        if fence.startswith("`") and "`" in raw_info:
            continue
        words = raw_info.split()
        info = _LANG.match(words[0]).group().lower() if words else ""
        width = len(indent.expandtabs(4))
        body = []
        for _, line in lines:
            marker = line.strip(" \t")
            if marker.startswith(fence) and not marker.strip(fence[0]):
                break
            spaces = len(line) - len(line.lstrip(" "))
            body.append(line[min(width, spaces):])
        if langs is None or info in langs:
            blocks.append(Block(number, info, "\n".join(body)))
    return blocks


@dataclass(frozen=True)
class StubRun:
    calls: list[list[str]]
    returncode: int
    stdout: str
    stderr: str


def _python_target_index(argv: list[str]) -> int:
    """Find the script or mode argument in a python3 argv."""
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            return index + 1
        if arg in {"-X", "-W"}:
            index += 2
        elif arg.startswith(("-X", "-W")) or (
            arg.startswith("-") and len(arg) > 1
            and all(flag in "BIuEsSOqbdvx" for flag in arg[1:])
        ):
            index += 1
        else:
            break
    return index


def python_script_call(argv: list[str]) -> tuple[str, list[str]] | None:
    """Return the script basename and its arguments from a recorded call."""
    if not argv or argv[0] != "python3":
        return None
    index = _python_target_index(argv)
    if index >= len(argv) or argv[index].startswith("-"):
        return None
    return Path(argv[index]).name, argv[index + 1:]


def note_writer_calls(calls: list[list[str]]) -> list[list[str]]:
    """Return the CLI arguments of each recorded note_writer.py invocation."""
    return [
        script[1] for argv in calls
        if (script := python_script_call(argv)) and script[0] == "note_writer.py"
    ]


# Variables the skill blocks read without setting. Unset, `"$X/..."` becomes an
# absolute path from the filesystem root, so each one points into the workdir.
_SCRATCH_PATH_VARS = (
    "FILE_PATH", "MATCH_PATH", "NOTE_PATH", "SUMMARY_TEMP_PATH",
    "UNSUMMARIZED_PATH", "INSIGHTS_DIR", "SESSIONS_DIR",
)

# Stubs that only record their argv. A block must never run the real plugin,
# git or gh, and `grep -r` must never scan the filesystem.
_RECORDING_STUBS = "python3 git gh grep".split()
# File writers. Under the OS sandbox they are the real tools; without one they
# are recording stubs too (see `run_with_stubs`).
_WRITERS = "mkdir touch chmod ln tee dd install rm cp mv".split()
# Real tools in both modes.
_REAL_UTILITIES = (
    "cat ls printf dirname basename head tail tr wc date cut sleep "
    "mktemp sed sort uniq awk env"
).split()


class SandboxUnavailable(RuntimeError):
    """No OS sandbox can keep a block's writes inside its workdir."""


# Writes the sandbox allows outside the workdir: the null device, the run's own
# output descriptors and the terminal. Nothing else.
_SBPL_DEVICES = (
    '(literal "/dev/null") (literal "/dev/zero") (literal "/dev/stdout") '
    '(literal "/dev/stderr") (literal "/dev/dtracehelper") '
    '(regex #"^/dev/fd/") (regex #"^/dev/tty")'
)


def _sbpl_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _prefix_for(tool: tuple[str, str], workdir: str) -> list[str]:
    kind, path = tool
    if kind == "sandbox-exec":
        profile = (
            "(version 1)(allow default)"
            f"(deny file-write* (require-not (require-any (subpath {_sbpl_string(workdir)})"
            f" {_SBPL_DEVICES})))"
        )
        return [path, "-p", profile]
    # bwrap: the whole filesystem read-only, the workdir writable, a private
    # /dev. No --unshare-pid and no --new-session: the block keeps host pids and
    # the launcher's session, which `_kill_session` and the timeout rely on.
    return [path, "--ro-bind", "/", "/", "--dev", "/dev", "--bind", workdir, workdir]


def _probe(tool: tuple[str, str]) -> bool:
    """True when ``tool`` really denies a write outside a directory and allows
    one inside it. A tool that is present but refused (nested `sandbox-exec`
    exits 71 inside another sandbox; `bwrap` exits 1 without user namespaces)
    fails here."""
    base = Path(tempfile.mkdtemp(prefix="ob-sandbox-probe-")).resolve()
    try:
        inside, outside = base / "inside", base / "outside"
        inside.mkdir()
        outside.mkdir()
        script = f'echo ok > "{inside}/ok"; echo bad > "{outside}/bad"; exit 0'
        try:
            subprocess.run(
                [*_prefix_for(tool, str(inside)), "/bin/sh", "-c", script],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return (inside / "ok").exists() and not (outside / "bad").exists()
    finally:
        shutil.rmtree(base, ignore_errors=True)


@functools.lru_cache(maxsize=None)
def _sandbox_tool() -> tuple[str, str] | None:
    """The OS sandbox this machine can use, probed once per process."""
    if sys.platform == "darwin":
        candidates = [("sandbox-exec", shutil.which("sandbox-exec"))]
    elif sys.platform.startswith("linux"):
        candidates = [("bwrap", shutil.which("bwrap"))]
    else:
        candidates = []
    for kind, path in candidates:
        if path and _probe((kind, path)):
            return kind, path
    return None


def sandbox_prefix(workdir: Path) -> list[str] | None:
    """The argv prefix that runs a command with writes allowed only inside
    ``workdir`` (resolved), or None when this machine has no working sandbox."""
    tool = _sandbox_tool()
    return None if tool is None else _prefix_for(tool, str(Path(workdir).resolve()))


def _list_pids() -> list[int]:
    """Every pid on the machine. Raises OSError when the listing fails."""
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid="], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if listing.returncode != 0:
        raise OSError(f"ps exited {listing.returncode}")
    return [int(text) for text in listing.stdout.split()]


def _kill_session(sid: int) -> None:
    """Kill every process of session ``sid`` (the block's bash is its leader), so a
    child in its own process group (`set -m`) cannot outlive the run. Returns when
    none is left or after 3 s. Never raises: when the process listing fails, the
    block's own group is still killed, but a `set -m` child (another group, same
    session) can then survive. A child that calls setsid() itself escapes: it is
    in no group or session of ours."""
    try:
        os.killpg(sid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + 3
    while True:
        try:
            pids = _list_pids()
        except (OSError, ValueError, subprocess.SubprocessError):
            return
        left = []
        for pid in pids:
            try:
                if pid > 1 and pid != os.getpid() and os.getsid(pid) == sid:
                    left.append(pid)
            except (ProcessLookupError, PermissionError):
                continue
        if not left or time.monotonic() > deadline:
            return
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.05)


def run_with_stubs(
    block: str, workdir: Path, timeout: int = 60, *, unsandboxed_ok: bool = False
) -> StubRun:
    """Run a skill block with scratch paths and record the stubbed commands.

    The block runs under an OS sandbox (`sandbox-exec` on macOS, `bwrap` on
    Linux) that denies every file write outside ``workdir``, whatever tool or
    path makes it: a redirect, `sort -o`, `mktemp`, `/bin/cp`, a symlink or a
    hardlink out of the workdir. ``python3``, ``git``, ``gh`` and ``grep`` are
    stubs that record their argv and do nothing else; file writers and text
    tools are the real ones. Every path variable the blocks read points into
    ``workdir``.

    With no working sandbox (inside another sandbox, or Linux without a usable
    `bwrap`) it raises `SandboxUnavailable` before it creates anything, unless
    ``unsandboxed_ok`` is set. That mode is for the pinned, reviewed call-site
    blocks only: the writers become recording stubs, but nothing contains a
    redirect or the real `sed`, `sort`, `awk`, `mktemp` and `env`.

    The block runs in its own session with job control off (no ``-i``/``-m``);
    output goes to files, not pipes, so a child that outlives the block cannot
    hold us. On a timeout, and after every run, the group and then the whole
    session are killed, and a failed cleanup never replaces the run's result.

    Known limits: a child that calls setsid() itself outlives the run; only the
    branch that ran is recorded.
    """
    prefix = sandbox_prefix(workdir)
    if prefix is None and not unsandboxed_ok:
        raise SandboxUnavailable(
            "no OS sandbox (sandbox-exec or bwrap) can contain this block's writes"
        )
    return _run(block, workdir, timeout, prefix or [], real_writers=prefix is not None)


def _run(block: str, workdir: Path, timeout: int, prefix: list[str], *, real_writers: bool) -> StubRun:
    """`run_with_stubs` without the sandbox check. ``prefix`` wraps the bash call."""
    workdir = workdir.resolve()
    bin_dir = workdir / "bin"
    home = workdir / "home"
    cwd = workdir / "cwd"
    fake_hooks = workdir / "fakehooks"
    tmp = workdir / "tmp"
    vault = workdir / "vault"
    log = workdir / "calls.jsonl"
    for directory in (bin_dir, home, cwd, fake_hooks, tmp, vault):
        directory.mkdir(parents=True)
    log.touch()
    for hook in HOOKS_DIR.iterdir():
        (fake_hooks / hook.name).touch()

    stub_source = f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
name = Path(sys.argv[0]).name
with open(os.environ["OB_STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps([name, *sys.argv[1:]]) + "\\n")

if name == "python3":
    sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
    from skill_shell import _python_target_index, python_script_call

    argv = [name, *sys.argv[1:]]
    index = _python_target_index(argv)
    target = argv[index] if index < len(argv) else ""
    if target == "-" or target.startswith("-c"):
        print({str(fake_hooks)!r})
    if (target == "-" or python_script_call(argv)) and not sys.stdin.isatty():
        try:
            sys.stdin.read()
        except (OSError, ValueError):
            pass
"""
    stubs = list(_RECORDING_STUBS)
    real = list(_REAL_UTILITIES)
    (real if real_writers else stubs).extend(_WRITERS)
    for name in stubs:
        stub = bin_dir / name
        stub.write_text(stub_source, encoding="utf-8")
        stub.chmod(0o755)
    for name in real:
        if path := shutil.which(name):
            (bin_dir / name).symlink_to(Path(path).absolute())

    quoted_cwd = "'" + str(cwd).replace("'", "'\\''") + "'"
    (vault / "sessions").mkdir()
    (vault / "insights").mkdir()
    env = {
        "PATH": str(bin_dir),
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "VAULT_PATH": str(vault),
        "SESSIONS_FOLDER": "sessions",
        "INSIGHTS_FOLDER": "insights",
        "OB_STUB_LOG": str(log),
        "LC_ALL": "C",
    }
    for name in _SCRATCH_PATH_VARS:
        env[name] = str(workdir / "scratch" / name.lower())
    out_path, err_path = workdir / "stdout.txt", workdir / "stderr.txt"
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        proc = subprocess.Popen(
            [*prefix, shutil.which("bash"), "-c", block.replace("'<REPO_ROOT>'", quoted_cwd)],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        raise
    finally:
        _kill_session(proc.pid)  # Leftover background children, any group.
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return StubRun(
        calls,
        proc.returncode,
        out_path.read_text(encoding="utf-8", errors="replace"),
        err_path.read_text(encoding="utf-8", errors="replace"),
    )


# `_HEREDOC_OPEN` groups: 1 is the `-` of `<<-`, 2 a quote, 3 a backslash,
# 4 the delimiter. A quote or a backslash makes the body inert.
_HEREDOC_OPEN = re.compile(r"<<(-?)[ \t]*(['\"]?)(\\?)([A-Za-z_][A-Za-z0-9_<>]*)\2")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NOT_WORD_START = frozenset("_$-./~:@%+=,{")
# Words after which another command can start.
_BEFORE_COMMAND = frozenset({"then", "do", "else", "elif", "if", "while", "until", "time"})


def _ends_heredoc(line: str, delimiter: str, dash: bool) -> bool:
    """`<<` ends only at the exact delimiter; `<<-` also strips leading tabs."""
    return (line.lstrip("\t") if dash else line) == delimiter


def _at_command_position(text: str, i: int) -> bool:
    """True when a command word can start at ``i``: line start, after `;`, `&`,
    `|`, `(`, `{`, `!`, or after a reserved word such as `then` or `do`."""
    j = i
    while j and text[j - 1] in " \t":
        j -= 1
    if not j or text[j - 1] in "\n;&|({!":
        return True
    k = j
    while k and text[k - 1].isalpha():
        k -= 1
    before = text[k - 1] if k else "\n"
    return text[k:j] in _BEFORE_COMMAND and not (before.isalnum() or before in "_$-./")


def _arith_end(block: str, start: int) -> int:
    """Where the `((` at ``start`` closes with `))`, or -1 when it is not arithmetic."""
    depth = 0
    j = start + 2
    while j < len(block):
        if block[j] == "(":
            depth += 1
        elif block[j] == ")":
            if depth:
                depth -= 1
            elif block.startswith("))", j):
                return j + 2
            else:
                return -1
        j += 1
    return -1


def _heredoc_body_subs(block: str, start: int, end: int, subs: list, ticks: list) -> None:
    """Record `$(...)` and backtick spans in an unquoted heredoc body, which the
    shell expands. Parens are counted without reading quotes: an unclosed span
    runs to the end of the body."""
    i = start
    while i < end:
        ch = block[i]
        if ch == "\\":
            i += 2
        elif block.startswith("$(", i):
            depth, j = 0, i + 2
            while j < end:
                if block[j] == "\\":
                    j += 1
                elif block[j] == "(":
                    depth += 1
                elif block[j] == ")":
                    if not depth:
                        break
                    depth -= 1
                j += 1
            subs.append([i + 2, min(j, end)])
            i += 2
        elif ch == "`":
            j = i + 1
            while j < end and block[j] != "`":
                j += 2 if block[j] == "\\" else 1
            ticks.append([i + 1, min(j, end)])
            i = j + 1
        else:
            i += 1


def _scan(block: str):
    """Walk the block like a shell: quotes, escapes, comments, heredoc bodies,
    arithmetic, `case` arms, and nested `$(...)`, `<(...)`, `>(...)` and backticks.

    Returns (flags, subs, ticks, bodies). flags[i] is 0 for shell code, 1 for text
    inside a quote, an escape or a comment, and 2 for a heredoc body. subs lists
    [start, end] spans of every parenthesis substitution body, outer before
    inner; ticks lists backtick spans (an escaped backtick does not close one);
    bodies lists (start, end, quoted) for heredoc bodies. An unclosed span runs
    to the end of the block. `<<` inside `((...))` or `$((...))` is a shift, and
    a `)` ending a `case` arm pattern does not close a substitution.
    """
    n = len(block)
    flags = [0] * n
    subs: list[list[int]] = []
    ticks: list[list[int]] = []
    bodies: list[tuple[int, int, bool]] = []
    arith: list[tuple[int, int]] = []

    def frame(kind: str, idx=None) -> dict:
        # case: one state per open `case` ("head", "pat" or "body").
        return {"k": kind, "depth": 0, "idx": idx, "case": []}

    stack = [frame("cmd")]
    pending: list[tuple[str, bool, bool]] = []
    i = 0

    def push_sub(at: int) -> None:
        subs.append([at + 2, n])
        stack.append(frame("cmd", len(subs) - 1))

    def push_tick(at: int) -> None:
        ticks.append([at + 1, n])
        stack.append(frame("bt", len(ticks) - 1))

    def note_arith(at: int) -> None:
        end = _arith_end(block, at)
        if end > 0:
            arith.append((at, end))

    while i < n:
        ch = block[i]
        top = stack[-1]
        if top["k"] == "dq":
            if ch == "\\":
                flags[i:i + 2] = [1] * len(flags[i:i + 2])
                i += 2
            elif ch == '"':
                stack.pop()
                i += 1
            elif block.startswith("$(", i):
                push_sub(i)
                i += 2
            elif ch == "`":
                push_tick(i)
                i += 1
            else:
                flags[i] = 1
                i += 1
            continue
        cases = top["case"]
        if ch == "\\":
            flags[i:i + 2] = [1] * len(flags[i:i + 2])
            i += 2
        elif ch == "'":
            end = block.find("'", i + 1)
            end = n if end < 0 else end
            flags[i + 1:end] = [1] * (end - i - 1)
            i = end + 1
        elif ch == '"':
            stack.append(frame("dq"))
            i += 1
        elif ch == "`":
            if top["k"] == "bt":
                ticks[top["idx"]][1] = i
                stack.pop()
            else:
                push_tick(i)
            i += 1
        elif block.startswith(("$(", "<(", ">("), i):
            if block.startswith("$((", i):
                note_arith(i + 1)
            push_sub(i)
            i += 2
        elif ch == "(":
            if block.startswith("((", i) and _at_command_position(block, i):
                note_arith(i)
            if cases and cases[-1] == "pat" and not top["depth"]:
                pass  # The optional leading paren of `(pattern)`.
            else:
                top["depth"] += 1
            i += 1
        elif ch == ")":
            if cases and cases[-1] == "pat" and not top["depth"]:
                cases[-1] = "body"  # The paren that ends a case pattern.
            elif top["depth"]:
                top["depth"] -= 1
            elif top["k"] == "cmd" and top["idx"] is not None:
                subs[top["idx"]][1] = i
                stack.pop()
            i += 1
        elif ch == "#" and (i == 0 or block[i - 1] in " \t\n;&|("):
            end = block.find("\n", i)
            end = n if end < 0 else end
            if any(active["k"] == "bt" for active in stack):
                pos = i + 1
                while pos < end and block[pos] != "`":
                    pos += 2 if block[pos] == "\\" else 1
                if pos < end:
                    end = pos
                    # The backtick bounds any nested substitution spans, too.
                    # Leave its frame on top for the backtick branch to close.
                    while stack[-1]["k"] != "bt":
                        nested = stack.pop()
                        if nested["k"] == "cmd":
                            subs[nested["idx"]][1] = end
            flags[i:end] = [1] * (end - i)
            i = end
        elif ch == ";" and cases and cases[-1] == "body" and block.startswith((";;", ";&"), i):
            cases[-1] = "pat"
            i += 3 if block.startswith(";;&", i) else 2
        elif block.startswith("<<", i):
            match = _HEREDOC_OPEN.match(block, i)
            triple = block.startswith("<<<", i) or (i and block[i - 1] == "<")
            shift = any(start <= i < end for start, end in arith)
            if match and not triple and not shift:
                quoted = bool(match.group(2) or match.group(3))
                pending.append((match.group(4), bool(match.group(1)), quoted))
                i = match.end()
            else:
                i += 3 if block.startswith("<<<", i) else 2
        elif ch == "\n" and pending:
            pos = i + 1
            for delimiter, dash, quoted in pending:
                body_start = pos
                body_end = n
                while pos < n:
                    end = block.find("\n", pos)
                    end = n if end < 0 else end
                    if _ends_heredoc(block[pos:end], delimiter, dash):
                        body_end = max(body_start, pos - 1)
                        pos = min(end + 1, n)
                        break
                    flags[pos:end] = [2] * (end - pos)
                    pos = min(end + 1, n)
                bodies.append((body_start, body_end, quoted))
                if not quoted:
                    _heredoc_body_subs(block, body_start, body_end, subs, ticks)
            pending = []
            i = pos
        elif ch.isascii() and (ch.isalpha() or ch == "_"):
            start = i
            i = _WORD.match(block, i).end()
            word = block[start:i]
            before = block[start - 1] if start else "\n"
            if before.isalnum() or before in _NOT_WORD_START:
                continue
            prev = block[:start].rstrip(" \t")[-1:]
            if word == "case" and (
                _at_command_position(block, start)
                or (cases and cases[-1] == "body" and prev == ")")
            ):
                cases.append("head")
            elif word == "in" and cases and cases[-1] == "head":
                cases[-1] = "pat"
            elif word == "esac" and cases and _at_command_position(block, start):
                # `esac)` after `;;` ends the case, then the substitution; an
                # `esac` pattern (`esac) ...`) is the one form read wrong.
                if cases[-1] == "body" or block[i:].lstrip(" \t")[:1] != "|":
                    cases.pop()
        else:
            i += 1
    return flags, subs, ticks, bodies


def substitution_bodies(block: str) -> list[str]:
    """The text inside every `$(...)`, `<(...)` and `>(...)` (parens inside quotes,
    escapes, heredoc bodies and `case` patterns do not count; unclosed runs to
    the end), then every backtick pair (an escaped backtick does not close one;
    an unmatched one runs to the end), and `$(...)` or backticks in an unquoted
    heredoc body. Body text keeps its quotes."""
    _flags, subs, ticks, _bodies = _scan(block)
    return [block[start:end] for start, end in subs + ticks]


def _blank_quotes_and_heredocs(block: str) -> str:
    """The block with quoted text, escapes, comments and heredoc bodies replaced
    by spaces. Same length; newlines kept."""
    flags = _scan(block)[0]
    return "".join(
        " " if flag and char != "\n" else char for char, flag in zip(block, flags)
    )


# The 2026-11-21 worktree-isolation probe refused both digit and legit in
# substitutions, so match git as a substring.
GIT_TEXT = re.compile(r"git", re.IGNORECASE)
_BRACE_START = re.compile(r"(?:^|[;&|!(])[ \t]*\{(?=\s|$)", re.M)
_BRACE_AFTER_WORD = re.compile(r"\b(?:then|do|else|elif)\b[ \t]*\{(?=\s|$)")


def _has_brace_group(code: str) -> bool:
    """A `{` that starts a command. A reserved word counts only where one can
    appear: `echo then {` passes `{` to echo."""
    return bool(_BRACE_START.search(code)) or any(
        _at_command_position(code, match.start())
        for match in _BRACE_AFTER_WORD.finditer(code)
    )


def isolation_problems(block: str, *, heredoc: bool = False) -> list[str]:
    """Report refused shell forms and optionally enforce a skill-specific heredoc ban.

    Known limits (no current skill block uses these forms): git text built at
    run time (`$cmd` where cmd is "git ..."); an extglob `@(a|b)` or a pattern
    that is the word `esac` inside a `case`, which end the pattern or the case
    early; and a `((` that is two nested subshells rather than arithmetic.
    """
    problems = []
    if any(GIT_TEXT.search(body) for body in substitution_bodies(block)):
        problems.append("git inside a command substitution")
    code = _blank_quotes_and_heredocs(block)
    if re.search(r"(\|\||&&)\s*\{", code):
        problems.append("brace group after || or &&")
    # A bare group containing git was refused. Flag every `{` that starts a
    # command (not `${VAR}`, not text in a quote or heredoc) to fail closed.
    if _has_brace_group(code):
        problems.append("brace group")
    if heredoc and "<<" in block:
        problems.append("heredoc")
    return problems


# In a worktree-isolated session on Claude Code 2.1.330 (2026-11-21),
# heredocs containing one- or two-key dicts (including empty-list values)
# and f-strings with quoted dict subscripts were refused. Empty dicts, lists,
# f-strings with `{exc}`, and `{a,b}` in a `cat << 'EOF'` body ran. The same
# dict also ran under `python3 -c`. The harness cause is a hypothesis: the
# probes showed refusal when a heredoc line had a quote after `{` before `}`.
_QUOTED_BRACE = re.compile(r"\{[^}\n]*[\"']")


def heredoc_bodies(block: str) -> list[str]:
    """Read each heredoc body through its delimiter or the end of the block."""
    return [block[start:end] for start, end, _quoted in _scan(block)[3]]


def heredoc_brace_problems(block: str) -> list[str]:
    """Return heredoc lines that contain a quote inside an opening brace."""
    return [
        line.strip()
        for body in heredoc_bodies(block)
        for line in body.splitlines()
        if _QUOTED_BRACE.search(line)
    ]


_SUB = "git inside a command substitution"
_GROUP = "brace group"
_AFTER_OR = "brace group after || or &&"

# name -> (block, a problem isolation_problems must report for it)
REFUSED_FORMS: dict[str, tuple[str, str]] = {
    "plain": ('cd "$(git rev-parse --show-toplevel)"', _SUB),
    "command": ('x=$(command git rev-parse HEAD)', _SUB),
    "cd-and": ('cd "$(cd . && git rev-parse --show-toplevel)"', _SUB),
    "abs-path": ('x=$(/usr/bin/git rev-parse HEAD)', _SUB),
    "space": ('x=$( git rev-parse HEAD)', _SUB),
    "backticks": ('x=`git rev-parse HEAD`', _SUB),
    "or-group": ('test -f f || { echo missing; exit 1; }', _AFTER_OR),
    "and-group-tight": ('test -f f &&{ echo present; }', _AFTER_OR),
    "or-group-next-line": ('test -f f ||\n{\n  echo missing\n}', _AFTER_OR),
    "python-word": (
        'x=$(python3 -c "\nimport subprocess\nsubprocess.run([\'git\', \'--version\'])\n")',
        _SUB,
    ),
    "heredoc-in-sub": ("out=$(FOO=1 python3 << 'PYEOF'\nprint(\"git\")\nPYEOF\n)", _SUB),
    "dquoted-sub": ('cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"', _SUB),
    "digit": ('x=$(echo digit)', _SUB),
    "legit": ("""x=$(python3 -c "print('legit')")""", _SUB),
    "upper": ('x=$(echo GIT)', _SUB),
    "bare-group": ('{ echo git word; }', _GROUP),
    "guard-msg": ('test -f f || { echo "ERROR: run git pull" >&2; exit 1; }', _AFTER_OR),
    "group-after-semicolon": ('true; { echo git; }', _GROUP),
    "group-after-then": ('if x; then { echo git; }; fi', _GROUP),
    "group-after-pipe": ('echo | { cat; }', _GROUP),
    "group-after-bang": ('! { echo; }', _GROUP),
    "group-after-amp": ('echo & { echo; }', _GROUP),
    "group-after-do": ('for i in 1; do { echo; }; done', _GROUP),
    "group-in-subshell": ('( { echo; } )', _GROUP),
    "paren-in-quotes": ("value=$(printf '%s' ')' && git rev-parse HEAD)", _SUB),
    "paren-in-dquotes": ('x=$(echo ")" ; git x)', _SUB),
    "process-substitution": ('diff <(git log) f', _SUB),
}
