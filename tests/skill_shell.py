"""Helpers for testing shell commands in SKILL.md files."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1] / "hooks"
SHELL_LANGS = frozenset({"bash", "sh", "shell", "zsh"})
_OPENER = re.compile(r"([ \t]*)(`{3,}|~{3,})(.*)")


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
        info = words[0].lower() if words else ""
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


def run_with_stubs(block: str, workdir: Path, timeout: int = 60) -> StubRun:
    """Run a skill block with scratch paths and record the stubbed commands.

    ``python3``, ``git``, ``gh`` and ``grep`` are stubs that record their argv:
    grep is not the real one, so ``grep -r`` can never scan the filesystem. Every
    path variable the blocks read points into ``workdir``. The block runs in its
    own session; on a timeout the whole process group is killed.

    Known limits (PR #523 review): the real coreutils still run, so an absolute
    path, a `..` path or a redirection in a block could write outside
    ``workdir`` (no current block does: every shell-level write goes to ``~``,
    ``$VAULT_PATH`` or a literal placeholder); a child that leaves the process
    group can outlive the timeout; and only the branch that ran is recorded.
    """
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

name = Path(sys.argv[0]).name
with open(os.environ["OB_STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps([name, *sys.argv[1:]]) + "\\n")

if name == "python3":
    sys.dont_write_bytecode = True
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
    for name in ("python3", "git", "gh", "grep"):
        stub = bin_dir / name
        stub.write_text(stub_source, encoding="utf-8")
        stub.chmod(0o755)

    utilities = (
        "mkdir cat sed ls printf dirname basename mktemp chmod head tail "
        "tr wc date touch env sort uniq awk cut"
    )
    for name in utilities.split():
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
    proc = subprocess.Popen(
        [shutil.which("bash"), "-c", block.replace("'<REPO_ROOT>'", quoted_cwd)],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # Leftover background children.
        except (ProcessLookupError, PermissionError):
            pass
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return StubRun(calls, proc.returncode, stdout, stderr)


# `_HEREDOC_OPEN` groups: 1 is the `-` of `<<-`, 2 the quote, 3 the delimiter.
_HEREDOC_OPEN = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_<>]*)\2")


def _ends_heredoc(line: str, delimiter: str, dash: bool) -> bool:
    """`<<` ends only at the exact delimiter; `<<-` also strips leading tabs."""
    return (line.lstrip("\t") if dash else line) == delimiter


def _scan(block: str):
    """Walk the block like a shell: quotes, escapes, comments, heredoc bodies and
    nested `$(...)`, `<(...)` and `>(...)`.

    Returns (flags, subs). flags[i] is 0 for shell code and 1 for text inside a
    quote, an escape or a comment, and 2 for a heredoc body. subs lists
    [start, end] spans of every substitution body, outer before inner. An
    unclosed one runs to the end of the block.

    Known gap: a `)` in a `case` pattern inside `$(...)` still closes it early.
    """
    n = len(block)
    flags = [0] * n
    subs: list[list[int]] = []
    stack = [["cmd", 0, None]]  # ["cmd", plain paren depth, subs index] or ["dq"]
    pending: list[tuple[str, bool]] = []
    i = 0

    def push_sub(at: int) -> None:
        subs.append([at + 2, n])
        stack.append(["cmd", 0, len(subs) - 1])

    while i < n:
        ch = block[i]
        frame = stack[-1]
        if frame[0] == "dq":
            if ch == "\\":
                flags[i:i + 2] = [1] * len(flags[i:i + 2])
                i += 2
            elif ch == '"':
                stack.pop()
                i += 1
            elif block.startswith("$(", i):
                push_sub(i)
                i += 2
            else:
                flags[i] = 1
                i += 1
            continue
        if ch == "\\":
            flags[i:i + 2] = [1] * len(flags[i:i + 2])
            i += 2
        elif ch == "'":
            end = block.find("'", i + 1)
            end = n if end < 0 else end
            flags[i + 1:end] = [1] * (end - i - 1)
            i = end + 1
        elif ch == '"':
            stack.append(["dq"])
            i += 1
        elif block.startswith(("$(", "<(", ">("), i):
            push_sub(i)
            i += 2
        elif ch == "(":
            frame[1] += 1
            i += 1
        elif ch == ")":
            if frame[1]:
                frame[1] -= 1
            elif frame[2] is not None:
                subs[frame[2]][1] = i
                stack.pop()
            i += 1
        elif ch == "#" and (i == 0 or block[i - 1] in " \t\n;&|("):
            end = block.find("\n", i)
            end = n if end < 0 else end
            flags[i:end] = [1] * (end - i)
            i = end
        elif block.startswith("<<", i):
            match = _HEREDOC_OPEN.match(block, i)
            triple = block.startswith("<<<", i) or (i and block[i - 1] == "<")
            if match and not triple:
                pending.append((match.group(3), bool(match.group(1))))
                i = match.end()
            else:
                i += 3 if block.startswith("<<<", i) else 2
        elif ch == "\n" and pending:
            pos = i + 1
            for delimiter, dash in pending:
                while pos < n:
                    end = block.find("\n", pos)
                    end = n if end < 0 else end
                    if _ends_heredoc(block[pos:end], delimiter, dash):
                        pos = min(end + 1, n)
                        break
                    flags[pos:end] = [2] * (end - pos)
                    pos = min(end + 1, n)
            pending = []
            i = pos
        else:
            i += 1
    return flags, subs


def substitution_bodies(block: str) -> list[str]:
    """The text inside every `$(...)`, `<(...)` and `>(...)` (parens inside quotes,
    escapes and heredoc bodies do not count; unclosed runs to the end) and every
    backtick pair. Body text keeps its quotes."""
    _flags, subs = _scan(block)
    return [block[start:end] for start, end in subs] + re.findall(r"`([^`]*)`", block)


def _blank_quotes_and_heredocs(block: str) -> str:
    """The block with quoted text, escapes, comments and heredoc bodies replaced
    by spaces. Same length; newlines kept."""
    flags, _subs = _scan(block)
    return "".join(
        " " if flag and char != "\n" else char for char, flag in zip(block, flags)
    )


# The 2026-11-21 worktree-isolation probe refused both digit and legit in
# substitutions, so match git as a substring.
GIT_TEXT = re.compile(r"git", re.IGNORECASE)
_BRACE_GROUP = re.compile(
    r"(?:^|[;&|!(]|\b(?:then|do|else|elif)\b)[ \t]*\{(?=\s|$)", re.M
)


def isolation_problems(block: str, *, heredoc: bool = False) -> list[str]:
    """Report refused shell forms and optionally enforce a skill-specific heredoc ban.

    Known gaps (PR #523 review; no current skill block uses these forms): a `)`
    in a `case` pattern inside `$(...)`; an escaped or unmatched backtick; a
    `<<` bit shift read as a heredoc opener, which can hide a later
    substitution; `$(...)` inside an unquoted heredoc body; and a bare `{` word
    passed as an ordinary argument, which is flagged as a brace group.
    """
    problems = []
    if any(GIT_TEXT.search(body) for body in substitution_bodies(block)):
        problems.append("git inside a command substitution")
    code = _blank_quotes_and_heredocs(block)
    if re.search(r"(\|\||&&)\s*\{", code):
        problems.append("brace group after || or &&")
    # A bare group containing git was refused. Flag every `{` that starts a
    # command (not `${VAR}`, not text in a quote or heredoc) to fail closed.
    if _BRACE_GROUP.search(code):
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
    bodies = []
    lines = iter(block.splitlines())
    for line in lines:
        for match in _HEREDOC_OPEN.finditer(line):
            if match.start() and line[match.start() - 1] == "<":
                continue
            delimiter, dash = match.group(3), bool(match.group(1))
            body = []
            for body_line in lines:
                if _ends_heredoc(body_line, delimiter, dash):
                    break
                body.append(body_line)
            bodies.append("\n".join(body))
            break
    return bodies


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
