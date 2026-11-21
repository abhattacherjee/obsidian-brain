"""Helpers for testing shell commands in SKILL.md files."""

from __future__ import annotations

import json
import re
import shutil
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


def run_with_stubs(block: str, workdir: Path) -> StubRun:
    """Run a skill block with scratch paths and record the stubbed commands."""
    workdir = workdir.resolve()
    bin_dir = workdir / "bin"
    home = workdir / "home"
    cwd = workdir / "cwd"
    fake_hooks = workdir / "fakehooks"
    log = workdir / "calls.jsonl"
    for directory in (bin_dir, home, cwd, fake_hooks):
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
    for name in ("python3", "git", "gh"):
        stub = bin_dir / name
        stub.write_text(stub_source, encoding="utf-8")
        stub.chmod(0o755)

    utilities = (
        "rm mkdir cat sed ls printf dirname basename mktemp chmod head tail "
        "grep tr wc date touch cp mv env sort uniq awk cut"
    )
    for name in utilities.split():
        if path := shutil.which(name):
            (bin_dir / name).symlink_to(Path(path).absolute())

    quoted_cwd = "'" + str(cwd).replace("'", "'\\''") + "'"
    result = subprocess.run(
        [shutil.which("bash"), "-c", block.replace("'<REPO_ROOT>'", quoted_cwd)],
        cwd=cwd,
        env={
            "PATH": str(bin_dir),
            "HOME": str(home),
            "OB_STUB_LOG": str(log),
            "LC_ALL": "C",
        },
        stdin=subprocess.DEVNULL,
        timeout=60,
        capture_output=True,
        text=True,
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return StubRun(calls, result.returncode, result.stdout, result.stderr)


def substitution_bodies(block: str) -> list[str]:
    """The text inside every `$( ... )` (parens balanced; unclosed runs to the end)
    and every backtick pair."""
    bodies = []
    for m in re.finditer(r"\$\(", block):
        depth, k = 1, m.end()
        while k < len(block) and depth:
            depth += {"(": 1, ")": -1}.get(block[k], 0)
            k += 1
        bodies.append(block[m.end():k])
    bodies += re.findall(r"`([^`]*)`", block)
    return bodies


# The 2026-11-21 worktree-isolation probe refused both digit and legit in
# substitutions, so match git as a substring.
GIT_TEXT = re.compile(r"git", re.IGNORECASE)


def isolation_problems(block: str, *, heredoc: bool = False) -> list[str]:
    """Report refused shell forms and optionally enforce a skill-specific heredoc ban."""
    problems = []
    if any(GIT_TEXT.search(body) for body in substitution_bodies(block)):
        problems.append("git inside a command substitution")
    if re.search(r"(\|\||&&)\s*\{", block):
        problems.append("brace group after || or &&")
    # A bare group containing git was refused. Flag every bare group to fail closed.
    if re.search(r"^[ \t]*\{(\s|$)", block, re.M):
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
_HEREDOC_OPEN = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_<>]*)\1")
_QUOTED_BRACE = re.compile(r"\{[^}\n]*[\"']")


def heredoc_bodies(block: str) -> list[str]:
    """Read each heredoc body through its delimiter or the end of the block."""
    bodies = []
    lines = iter(block.splitlines())
    for line in lines:
        for match in _HEREDOC_OPEN.finditer(line):
            if match.start() and line[match.start() - 1] == "<":
                continue
            delimiter = match.group(2)
            body = []
            for body_line in lines:
                if body_line.strip() == delimiter:
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


REFUSED_FORMS: dict[str, str] = {
    "plain": 'cd "$(git rev-parse --show-toplevel)"',
    "command": 'x=$(command git rev-parse HEAD)',
    "cd-and": 'cd "$(cd . && git rev-parse --show-toplevel)"',
    "abs-path": 'x=$(/usr/bin/git rev-parse HEAD)',
    "space": 'x=$( git rev-parse HEAD)',
    "backticks": 'x=`git rev-parse HEAD`',
    "or-group": 'test -f f || { echo missing; exit 1; }',
    "and-group-tight": 'test -f f &&{ echo present; }',
    "or-group-next-line": 'test -f f ||\n{\n  echo missing\n}',
    "python-word": 'x=$(python3 -c "\nimport subprocess\nsubprocess.run([\'git\', \'--version\'])\n")',
    "heredoc-in-sub": "out=$(FOO=1 python3 << 'PYEOF'\nprint(\"git\")\nPYEOF\n)",
    "dquoted-sub": 'cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"',
    "digit": 'x=$(echo digit)',
    "legit": """x=$(python3 -c "print('legit')")""",
    "upper": 'x=$(echo GIT)',
    "bare-group": '{ echo git word; }',
    "guard-msg": 'test -f f || { echo "ERROR: run git pull" >&2; exit 1; }',
}
