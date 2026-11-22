"""Execute the SKILL.md ``note_writer.py`` blocks under a real shell.

Call sites are found by running EVERY fenced shell block of every skill under
bash with stub ``python3``, ``git`` and ``gh`` that record their argv (#522),
not by matching command text: regexes missed ``python3 -X utf8``, a ``\\``
continuation before ``write`` and tilde fences, and counted a shell comment as
a call. Execution only sees calls that ran, so a block that stops early or
sits in an untaken branch records nothing. The pinned ``_CALL_SITES`` dict
names the blocks that record a call today (with a hash of each body), and the counts (8 ``write``, 1
``append-update``) are pinned too. A new call site, or one that stops
recording, fails the count test; update ``_CALL_SITES`` when a skill adds or
removes one.

Since #517 every ``write`` call takes the note from ``--body-file`` (a file the
model writes with the Write tool), so no note text sits in the Bash command.
Only /compress's ``append-update`` still pipes its section through a heredoc.

Why this module exists (and why it is NOT part of test_note_writer.py):
``tests/test_note_writer.py`` calls the CLI with list argv and ``shell=False``,
so the heredoc-delimiter collision bug was unreachable there. Each block here is
extracted verbatim, minimally rewritten (see ``_prepare``) and run through real
``bash`` against a scratch vault, with a note body that contains a **column-0
line equal to the documented delimiter base**, exactly what a note *about this
plugin* looks like.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import pytest

import skill_shell
from skill_shell import (
    HOOKS_DIR,
    SandboxUnavailable,
    _blank_quotes_and_heredocs,
    fenced_blocks,
    note_writer_calls,
    python_script_call,
    run_with_stubs,
    sandbox_prefix,
)

_BASH = shutil.which("bash")
requires_bash = pytest.mark.skipif(_BASH is None, reason="bash not available")
# Probed once per process. Without a sandbox only the pinned call-site blocks
# run (unsandboxed_ok), so every other harness test skips (GH #526).
requires_sandbox = pytest.mark.skipif(
    _BASH is None or sandbox_prefix(Path.cwd()) is None,
    reason="no OS sandbox (sandbox-exec or bwrap) for the stub harness",
)

# Matches the opener under EITHER scheme -- with the per-invocation `<eof4>`
# placeholder or the old fixed form. Deliberately scheme-agnostic so that
# reverting to a fixed delimiter makes the execution test fail on its real
# assertions (truncated note + executed side effect) rather than crashing in
# the harness, which would prove nothing.
_HEREDOC_OPEN_RE = re.compile(r"<<'(OB_[A-Za-z0-9_]+(?:_<eof4>)?)'")
# FORM B (#278) spans multiple physical lines: `HOOKS=$(python3 -c "` opens,
# the resolver body follows, and a column-0 `")` closes it. DOTALL lets `.`
# cross those newlines; the non-greedy `.*?` stops at the first bare `")`
# line, which is unambiguous because no line inside the resolver body ends
# with that exact two-character sequence.
_HOOKS_LINE_RE = re.compile(r'^HOOKS=\$\(python3 -c "\n.*?\n"\)$', re.MULTILINE | re.DOTALL)
# The filename positional of a `write` call: 3rd quoted argument.
_WRITE_FILENAME_RE = re.compile(r'(write "\$VAULT_PATH" "\$\w+" )"[^"]*"')
_WRITE_FOLDER_RE = re.compile(r'write "\$VAULT_PATH" "\$(\w+)"')

# The hex the SKILL.md tells the model to substitute for <eof4> per run.
_EOF4 = "beef"
SENTINEL = "SENTINEL-LAST-LINE-OF-NOTE"
SIDE_EFFECT = "SIDE_EFFECT_MARKER"

FOLDERS = {
    "INSIGHTS_FOLDER": "claude-insights",
    "SESSIONS_FOLDER": "claude-sessions",
}


_SKILL_TEXTS = {
    path.parent.name: path.read_text(encoding="utf-8")
    for path in sorted(HOOKS_DIR.parent.glob("skills/*/SKILL.md"))
}
_ALL_BLOCKS = [
    (f"{skill}-{block.line}", skill, block.body)
    for skill, text in _SKILL_TEXTS.items()
    for block in fenced_blocks(text)
]
# The blocks that record a note_writer.py call when run under the stubs.
# Update when a skill adds or removes a call site.
# Each id maps to the first 16 hex of sha256(body). Only a block whose body still
# matches may run without a sandbox, so a block edited without review cannot.
_CALL_SITES = {
    "compress-250": "4669e1e4f8ac1760",
    "compress-514": "ff5167440d6540a7",
    "decide-262": "e6c3bc8558e8925f",
    "error-log-252": "73826d66a0ed0992",
    "retro-412": "0d7838eddb509c3a",
    "standup-325": "6f7118d056ab2fb9",
    "standup-582": "ff5167440d6540a7",
    "vault-import-285": "e5970c453d34ab44",
    "vault-stats-238": "23f3b08de6fd2ebe",
}
_CALL_BLOCKS = [block for block in _ALL_BLOCKS if block[0] in _CALL_SITES]


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _is_pinned(block_id: str, body: str) -> bool:
    return _CALL_SITES.get(block_id) == _body_hash(body)


def _require_pinned(block_id: str, body: str) -> None:
    """Fail (never run) when ``body`` no longer matches its pin in `_CALL_SITES`.
    The one predicate for "this block may run without a sandbox"."""
    if not _is_pinned(block_id, body):
        pytest.fail(
            f"{block_id}: the block text does not match its pin in _CALL_SITES. "
            "Review the block (it may run without a sandbox), then re-pin its hash."
        )
# A call in an untaken branch is not recorded, so a call site must not sit
# inside one.
_BRANCH_WORDS = frozenset({"if", "case", "elif", "else", "while", "until"})
# The one-line guard every call-site block opens with. It exits when it fails,
# so it cannot hide a call. Matched on a whole line: a chained `; if ...` after
# its `fi` is not part of it.
_GUARD_LINE = re.compile(r"\s*if ! test -f [^;&|]+;\s*then\s+(?:[^;]+;\s*)*exit 1;\s*fi\s*")
_COMMAND_LEADERS = frozenset({"then", "do", "time", "!"})


def _recorded_calls(block_id, block, workdir):
    result = run_with_stubs(block, workdir, unsandboxed_ok=_is_pinned(block_id, block))
    assert result.returncode == 0, f"{block_id}: {result.stderr}"
    return note_writer_calls(result.calls)


def _assert_body_file(args, block_id):
    assert "--body-file" in args, (block_id, args)
    start = args.index("--body-file")
    assert args[start + 1:start + 2] == ["<BODY_FILE>"], (block_id, args)


def _branch_words(block: str) -> set[str]:
    """Branch keywords at any command position of the block.

    Quotes and heredoc bodies are blanked first (they are text, not shell). The
    code is split on `;`, `&`, `|` and newlines, and each piece's first word is
    read after any `$(`, `(`, `{`, `then`, `do`, `!`, `VAR=` prefix and case-arm
    `pat)` label.
    """
    code = "\n".join(
        line for line in _blank_quotes_and_heredocs(block).splitlines()
        if not _GUARD_LINE.fullmatch(line)
    )
    found = set()
    for piece in re.split(r"[;&|)\n]", code.replace("$(", " ")):
        words = piece.replace("(", " ").replace("{", " ").split()
        while words and (
            words[0] in _COMMAND_LEADERS
            or words[0].endswith(")")
            or re.fullmatch(r"\w+=\S*", words[0])
        ):
            words.pop(0)
        if words and words[0] in _BRANCH_WORDS:
            found.add(words[0])
    return found


def test_a_call_inside_a_branch_is_detected():
    assert _branch_words("if x; then\n  python3 n.py write\nfi") == {"if"}
    assert _branch_words("case x in\n  a) python3 n.py ;;\nesac") == {"case"}
    assert _branch_words("x\nelse\ny") == {"else"}
    assert _branch_words("if ! test -f f; then exit 1; fi\ncat <<E\nif a\nE") == set()


@pytest.mark.parametrize(("block", "words"), [
    (':; if false; then python3 "$HOOKS/note_writer.py" write x; fi', {"if"}),
    ("echo a && while x; do y; done", {"while"}),
    ("echo a | until x; do y; done", {"until"}),
    ("x=$(if a; then b; fi)", {"if"}),
    ("echo a; ( if a; then b; fi )", {"if"}),
    ("case $x in a) if b; then c; fi ;; esac", {"case", "if"}),
    ('if ! test -f f; then echo "no" >&2; exit 1; fi; if false; then y; fi', {"if"}),
    ('if ! test -f "$H"; then echo "ERROR: no" >&2; exit 1; fi\necho ok', set()),
    ("echo 'if a' \"while b\" # if c", set()),
])
def test_branch_words_are_read_at_every_command_position(block, words):
    assert _branch_words(block) == words


def _check_call_sites(blocks, tmp_path, run=None):
    """Run ``blocks`` through ``run(block_id, block, workdir)`` (default: `run_with_stubs`) and check the recorded call
    sites and their counts."""
    assert {block_id for block_id, _skill, _block in _CALL_BLOCKS} == set(_CALL_SITES)
    recorded = set()
    counts = Counter()
    for block_id, _skill, block in blocks:
        workdir = tmp_path / block_id
        result = run(block_id, block, workdir) if run else run_with_stubs(block, workdir)
        calls = note_writer_calls(result.calls)
        if not calls:
            continue  # Other blocks may exit non-zero; only call sites are checked.
        recorded.add(block_id)
        assert result.returncode == 0, f"{block_id}: {result.stderr}"
        for args in calls:
            assert args, block_id
            counts[args[0]] += 1
            if args[0] == "write":
                _assert_body_file(args, block_id)
        if "<<" in block:
            assert [args[0] for args in calls] == ["append-update"], block_id
    assert recorded == set(_CALL_SITES)
    assert counts == {"write": 8, "append-update": 1}
    for block_id, _skill, block in _CALL_BLOCKS:
        assert _branch_words(block) == set(), block_id


@requires_sandbox
def test_note_writer_call_sites_match_the_pinned_counts(tmp_path):
    """Every block runs, so a new call site anywhere fails the pinned set."""
    _check_call_sites(_ALL_BLOCKS, tmp_path)


def _prepare(block: str, tmp_path, note_name: str, *, is_update: bool):
    """Rewrite a SKILL.md block into a runnable script, changing as little as
    possible: only what the model would itself substitute at runtime.

    - ``$HOOKS`` is pinned to this repo's hooks/ (the real line resolves the
      installed plugin cache, which is not what we are testing). The block's
      own ``test -f`` guard is left in place and must pass.
    - ``<eof4>`` gets a concrete hex, exactly as the SKILL.md instructs.
    - The filename placeholder becomes a real name.
    - The heredoc BODY is replaced with the collision fixture.
    """
    if is_update:
        raw = _HEREDOC_OPEN_RE.search(block).group(1)
    else:
        raw = "OB_NOTE_EOF_<eof4>"  # the old delimiter, now only hostile note text
    terminator = raw.replace("<eof4>", _EOF4)
    delim_base = re.sub(r"_<eof4>$", "", raw)

    script = block.replace("cd '<REPO_ROOT>'", f"cd '{tmp_path}'")
    script = _HOOKS_LINE_RE.sub(f'HOOKS="{HOOKS_DIR}"', script)
    # `.sub` is a silent no-op if the HOOKS= line ever changes shape. The
    # block's own `test -f` would then still pass -- against the INSTALLED
    # plugin cache -- and the whole harness would be testing shipped code
    # instead of the repo under test, while staying green.
    assert str(HOOKS_DIR) in script, (
        "HOOKS= line did not match _HOOKS_LINE_RE; the block would run "
        "against the installed plugin cache, not this repo"
    )
    script = script.replace("<eof4>", _EOF4)
    script = _WRITE_FILENAME_RE.sub(rf'\1"{note_name}"', script)
    # `--add-tags "claude/topic/<new-tag-1>,…"` is a template the model fills
    # in. Left literal, the angle brackets are (correctly) rejected by the tag
    # allowlist -- so substitute them here exactly as the model would, rather
    # than weakening the allowlist to accommodate a placeholder.
    script = re.sub(r"<new-tag-(\d+)>", r"tag\1", script)

    body_head = (
        "## Update (2026-07-25)\n"
        if is_update
        else "---\ntype: claude-insight\ndate: 2026-07-25\ntags:\n"
             "  - claude/insight\n---\n\n# Note about note_writer.py\n"
    )
    # The payload: a column-0 line equal to the documented delimiter base,
    # followed by text that WOULD run as shell if the heredoc terminated
    # early, then a sentinel that only survives if the heredoc did not.
    fixture_body = (
        f"{body_head}\n"
        f"This skill's block terminates with the line:\n"
        f"{delim_base}\n"
        f"touch {tmp_path}/{SIDE_EFFECT}\n"
        f"{SENTINEL}\n"
    )

    if not is_update:
        body_path = tmp_path / "body.md"
        body_path.write_text(fixture_body, encoding="utf-8")
        script = script.replace("'<BODY_FILE>'", f"'{body_path}'")
        return script, terminator, delim_base

    lines = script.split("\n")
    open_idx = next(i for i, ln in enumerate(lines) if f"<<'{terminator}'" in ln)
    close_idx = next(
        i for i, ln in enumerate(lines) if i > open_idx and ln == terminator
    )
    script = "\n".join(
        lines[:open_idx + 1] + fixture_body.split("\n")[:-1] + lines[close_idx:]
    )
    return script, terminator, delim_base


def _env(vault, match_path=None):
    env = dict(os.environ)
    env["VAULT_PATH"] = str(vault)
    env.update(FOLDERS)
    if match_path is not None:
        env["MATCH_PATH"] = str(match_path)
    return env


EXISTING_NOTE = (
    "---\n"
    "type: claude-insight\n"
    "date: 2026-01-01\n"
    "tags:\n"
    "  - claude/insight\n"
    "---\n"
    "\n"
    "# Existing\n"
    "\n"
    "Body content here.\n"
)


@requires_bash
@pytest.mark.parametrize(
    "block_id,skill_name,block", _CALL_BLOCKS, ids=[b[0] for b in _CALL_BLOCKS]
)
def test_skill_heredoc_block_survives_delimiter_in_note_body(
    tmp_path, block_id, skill_name, block
):
    """Run the real block under bash with a note body containing a column-0
    line equal to the delimiter base.

    With a FIXED delimiter this fails two ways at once: the note is truncated
    at the collision line (sentinel missing) and the following note text runs
    as shell (SIDE_EFFECT_MARKER appears). Verified by reverting the SKILL.md
    files to fixed delimiters — see dr-fix2-report.md for both runs.
    """
    vault = tmp_path / "vault"
    for folder in FOLDERS.values():
        (vault / folder).mkdir(parents=True)

    calls = _recorded_calls(block_id, block, tmp_path / "stub")
    assert len(calls) == 1, (block_id, calls)
    args, = calls
    assert args and args[0] in {"write", "append-update"}, (block_id, args)
    is_update = args[0] == "append-update"
    if is_update:
        target = vault / "claude-insights" / "existing.md"
        target.write_text(EXISTING_NOTE, encoding="utf-8")
        match_path = target
    else:
        folder_var = _WRITE_FOLDER_RE.search(block).group(1)
        target = vault / FOLDERS[folder_var] / "note.md"
        match_path = None
        if "--overwrite" in args:
            # /standup Step 6.6 upgrades an EXISTING note in place; without a
            # pre-existing file the CLI would (correctly) still write, but the
            # block would not be exercising the case it documents.
            target.write_text(EXISTING_NOTE, encoding="utf-8")

    script, _, delim_base = _prepare(block, tmp_path, "note.md", is_update=is_update)
    script_path = tmp_path / "block.sh"
    script_path.write_text(script, encoding="utf-8")

    # The block is editable skill text: contain it wherever a sandbox works.
    # Everything this test writes (the vault, block.sh, body.md, the marker)
    # sits under tmp_path.
    result = subprocess.run(
        [*(sandbox_prefix(tmp_path) or []), _BASH, str(script_path)],
        cwd=str(tmp_path),
        env=_env(vault, match_path),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "OK:" in result.stdout

    written = target.read_text(encoding="utf-8")
    # (a) the note kept the whole body, including the delimiter-base line and
    #     the text after it, verbatim
    assert f"\n{delim_base}\n" in written
    assert f"touch {tmp_path}/{SIDE_EFFECT}" in written
    # (b) the trailing sentinel survived -> no early termination
    assert SENTINEL in written
    # (c) nothing in the note body executed
    side_effects = list(tmp_path.rglob(SIDE_EFFECT))
    assert side_effects == [], f"note content executed as shell: {side_effects}"
    assert "command not found" not in result.stderr


@requires_bash
def test_every_write_call_takes_the_note_from_a_body_file(tmp_path):
    """Every write passes a body file so the hook sees no note text in Bash."""
    for block_id, _skill, block in _CALL_BLOCKS:
        for args in _recorded_calls(block_id, block, tmp_path / block_id):
            if args[:1] == ["write"]:
                _assert_body_file(args, block_id)
                assert "<<" not in block, block_id


@requires_bash
def test_skill_heredoc_block_terminator_is_unique_per_invocation(tmp_path):
    """Structural companion to the execution test: every append-update block's
    heredoc opener carries the `<eof4>` placeholder and its terminator line
    matches it exactly."""
    checked = []
    for block_id, _skill, block in _CALL_BLOCKS:
        calls = _recorded_calls(block_id, block, tmp_path / block_id)
        if [args[:1] for args in calls] != [["append-update"]]:
            continue
        m = _HEREDOC_OPEN_RE.search(block)
        assert m, f"{block_id}: no OB_... heredoc opener found"
        terminator = m.group(1)
        assert terminator.endswith("_<eof4>"), (
            f"{block_id}: FIXED heredoc terminator {terminator!r} — note content "
            "containing that exact line ends the heredoc early and executes the "
            "remainder as shell"
        )
        assert any(ln == terminator for ln in block.split("\n")), (
            f"{block_id}: no column-0 terminator line matching the opener"
        )
        checked.append(block_id)
    assert len(checked) == 1, checked


_SCRATCH_SETUP = """\
cd '<REPO_ROOT>'
HOOKS=$(python3 -c "print(1)")
if ! test -f "$HOOKS/note_writer.py"; then echo "ERROR: missing note writer" >&2; exit 1; fi
"""
_SCRATCH_WRITE = """write "$VAULT_PATH" "$INSIGHTS_FOLDER" "n.md" --body-file '<BODY_FILE>'"""


def _scratch_write_args(workdir):
    """The recorded argv; run_with_stubs points VAULT_PATH at <workdir>/vault."""
    vault = str(workdir.resolve() / "vault")
    return [["write", vault, "insights", "n.md", "--body-file", "<BODY_FILE>"]]


@requires_sandbox
def test_stub_records_write_with_boolean_flag(tmp_path):
    block = _SCRATCH_SETUP + f'python3 -B "$HOOKS/note_writer.py" {_SCRATCH_WRITE}'
    assert _recorded_calls("boolean flag", block, tmp_path) == _scratch_write_args(tmp_path)


@requires_sandbox
def test_stub_records_write_with_value_flags(tmp_path):
    block = _SCRATCH_SETUP + (
        f'python3 -X utf8 -W ignore "$HOOKS/note_writer.py" {_SCRATCH_WRITE}'
    )
    assert _recorded_calls("value flags", block, tmp_path) == _scratch_write_args(tmp_path)


@requires_sandbox
def test_stub_records_write_with_line_continuation(tmp_path):
    block = _SCRATCH_SETUP + f'python3 "$HOOKS/note_writer.py" \\\n  {_SCRATCH_WRITE}'
    assert _recorded_calls("line continuation", block, tmp_path) == _scratch_write_args(tmp_path)


@requires_sandbox
def test_stub_records_write_in_four_tilde_fence(tmp_path):
    block = _SCRATCH_SETUP + f'python3 "$HOOKS/note_writer.py" {_SCRATCH_WRITE}'
    parsed, = fenced_blocks(f"~~~~bash\n{block}\n~~~~")
    assert _recorded_calls("four-tilde fence", parsed.body, tmp_path) == _scratch_write_args(tmp_path)


@requires_sandbox
def test_stub_ignores_commented_write(tmp_path):
    block = _SCRATCH_SETUP + f'# python3 "$HOOKS/note_writer.py" {_SCRATCH_WRITE}'
    assert _recorded_calls("commented write", block, tmp_path) == []


@requires_sandbox
def test_stub_does_not_record_absolute_python_path(tmp_path):
    """A real python3 called by absolute path bypasses the stub, so it is not recorded."""
    fake = tmp_path / "run" / "abs" / "python3"
    fake.parent.mkdir(parents=True)
    # The fake marks itself with a shell builtin redirect, so the test does not
    # depend on which tools are stubs.
    fake.write_text(f"#!/bin/sh\necho ran > '{fake.parent}/ran'\n", encoding="utf-8")
    fake.chmod(0o755)
    block = _SCRATCH_SETUP + f"'{fake}' \"$HOOKS/note_writer.py\" write"
    result = run_with_stubs(block, tmp_path / "run")
    assert (fake.parent / "ran").exists()
    assert result.calls == [["python3", "-c", "print(1)"]]
    assert note_writer_calls(result.calls) == []


@requires_sandbox
def test_grep_is_a_recording_stub_that_scans_nothing(tmp_path):
    block = 'grep -rl "session_id:" "$VAULT_PATH/$SESSIONS_FOLDER/"'
    result = run_with_stubs(block, tmp_path)
    vault = tmp_path.resolve() / "vault"
    assert result.returncode == 0
    assert result.calls == [["grep", "-rl", "session_id:", f"{vault}/sessions/"]]
    assert not (tmp_path / "bin" / "grep").resolve().samefile(shutil.which("grep"))


def _pid_is_gone(pid: int) -> bool:
    """True when ``pid`` no longer exists. A zombie (killed, not yet reaped by
    its parent) counts as gone: it runs nothing."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return state.startswith("Z")


def test_a_zombie_counts_as_gone():
    """A killed child its parent has not reaped still answers `kill -0`."""
    child = subprocess.Popen(["sleep", "300"], stdin=subprocess.DEVNULL)
    try:
        assert not _pid_is_gone(child.pid)  # Control: a live process is not gone.
        child.kill()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # Popen.poll() would reap it; read the state.
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child.pid)],
                capture_output=True, text=True, stdin=subprocess.DEVNULL,
            ).stdout.strip()
            if state.startswith("Z"):
                break
            time.sleep(0.05)
        os.kill(child.pid, 0)  # Still answers: it is a zombie, not reaped.
        assert _pid_is_gone(child.pid)
    finally:
        child.kill()  # A failed assertion above must not wait out the sleep.
        child.wait()


def _wait_gone(pid: int, timeout: float = 3.0) -> bool:
    """SIGKILL is asynchronous: poll until the process is gone or ``timeout`` ends."""
    deadline = time.monotonic() + timeout
    while not _pid_is_gone(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def _timeout_leaves_no_child(tmp_path, block):
    """Time out a block that backgrounds `sleep 300` and check the sleep is dead."""
    run_dir = tmp_path / "run"
    pid_file = run_dir / "tmp" / "pid"

    def hung(*_args):
        raise AssertionError("run_with_stubs did not return: the timeout did not end the run")

    old_handler = signal.signal(signal.SIGALRM, hung)
    signal.alarm(30)
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            run_with_stubs(block, run_dir, timeout=2)
        elapsed = time.monotonic() - started
        gone = pid_file.exists() and _wait_gone(int(pid_file.read_text()))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        if pid_file.exists():  # Never leave the sleep running, even on failure.
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
    assert elapsed < 15, elapsed
    assert pid_file.exists(), "the block never wrote its child's pid"
    assert gone, "the backgrounded child outlived the timeout"


@requires_sandbox
def test_a_timeout_kills_a_backgrounded_child(tmp_path):
    _timeout_leaves_no_child(tmp_path, 'sleep 300 & echo $! > "$TMPDIR/pid"; wait')


@requires_sandbox
def test_a_timeout_kills_a_child_in_its_own_process_group(tmp_path):
    """`set -m` gives each job its own group, so killpg of the block's group misses it."""
    _timeout_leaves_no_child(tmp_path, 'set -m; sleep 300 & echo $! > "$TMPDIR/pid"; wait')


@requires_sandbox
def test_a_surviving_child_cannot_hold_the_run(tmp_path):
    """Output goes to files, so a child that keeps stdout open does not block the run."""
    pid_file = tmp_path / "run" / "tmp" / "pid"
    started = time.monotonic()
    try:
        result = run_with_stubs('sleep 300 & echo $! > "$TMPDIR/pid"; echo out', tmp_path / "run")
        elapsed = time.monotonic() - started
        gone = _wait_gone(int(pid_file.read_text()))
    finally:
        if pid_file.exists():  # Never leave the sleep running, even on failure.
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
    assert elapsed < 15
    assert result.stdout == "out\n"
    assert gone


def _kill_pid_file(pid_file: Path) -> None:
    """Never leave a test's sleep running, even on failure."""
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


def test_the_sandbox_is_there_when_ci_requires_it():
    """CI sets OB_REQUIRE_SANDBOX=1 and installs bwrap, so it can never fall back
    to the unsandboxed mode without failing here (GH #526)."""
    if os.environ.get("OB_REQUIRE_SANDBOX") != "1":
        pytest.skip("OB_REQUIRE_SANDBOX is not set")
    assert sandbox_prefix(Path.cwd()) is not None


def _escape_block(outside: Path) -> str:
    """Every cheap way out of the workdir that the old per-tool guards missed."""
    return (
        f"O='{outside}'\n"
        'echo x >| "$O/clobber"\n'
        "echo a > t.txt\n"
        'sort -o "$O/sorted" t.txt\n'
        # macOS mktemp ignores TMPDIR and uses the per-user temp dir, which is
        # outside the workdir too; Linux mktemp follows TMPDIR into $O.
        'echo "MKTEMP:$(TMPDIR="$O" mktemp)"\n'
        'echo "MKTEMP:$(mktemp "$O/tmp.XXXXXX")"\n'
        '/bin/cp t.txt "$O/abs-cp"\n'
        'ln -s "$O" link; echo x > link/via-symlink\n'
        'ln "$O/f" hard; echo pwned >> hard\n'
        'sed -i.bak s/orig/sed/ "$O/f"\n'
        'rm -f "$O/f2"\n'
        'mv "$O/f3" moved\n'
        'echo x | tee "$O/tee" >/dev/null\n'
        'dd if=/dev/zero of="$O/dd" bs=1 count=1 2>/dev/null\n'
        'install t.txt "$O/inst"\n'
        "echo done\n"
    )


def _made_temp_files(stdout: str) -> list[Path]:
    return [Path(line[7:]) for line in stdout.splitlines() if line.startswith("MKTEMP:") and line[7:]]


def _outside(tmp_path: Path) -> Path:
    # A sibling of the workdir, not /tmp: an outer sandbox may deny /tmp on its
    # own and make the test pass with no harness sandbox at all.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("orig\n", encoding="utf-8")
    (outside / "f2").write_text("keep\n", encoding="utf-8")
    (outside / "f3").write_text("move\n", encoding="utf-8")
    return outside.resolve()


@requires_sandbox
def test_no_write_escapes_the_workdir(tmp_path):
    outside = _outside(tmp_path)
    result = run_with_stubs(_escape_block(outside), tmp_path / "run")
    assert result.stdout.endswith("done\n"), result.stderr
    assert _made_temp_files(result.stdout) == [], result.stdout
    assert sorted(p.name for p in outside.iterdir()) == ["f", "f2", "f3"]
    assert (outside / "f").read_text(encoding="utf-8") == "orig\n"
    assert (outside / "f2").read_text(encoding="utf-8") == "keep\n"
    assert (outside / "f3").read_text(encoding="utf-8") == "move\n"
    # No check on `moved` inside the workdir: under bwrap the rename crosses
    # mounts, so mv copies the file in and then fails to unlink the original.
    # The writes inside the workdir still happened.
    assert (tmp_path / "run" / "cwd" / "t.txt").read_text(encoding="utf-8") == "a\n"


@requires_bash
def test_without_the_sandbox_the_same_block_escapes(tmp_path):
    """Mutation check for the test above: the same block, run with real writers
    and no sandbox prefix, writes outside. So the test above can fail."""
    outside = _outside(tmp_path)
    result = skill_shell._run(_escape_block(outside), tmp_path / "run", 60, [], real_writers=True)
    made = _made_temp_files(result.stdout)
    for path in made:  # The TMPDIR form may land in the per-user temp dir.
        if not path.is_relative_to(outside):
            path.unlink()
    assert len(made) == 2, result.stdout
    names = {p.name for p in outside.iterdir()}
    assert {"clobber", "sorted", "abs-cp", "via-symlink", "f.bak"} <= names, names
    assert {"tee", "dd", "inst"} <= names, names
    assert not {"f2", "f3"} & names, names  # rm deleted f2, mv moved f3.
    assert any(name.startswith("tmp.") for name in names), names
    assert (outside / "f").read_text(encoding="utf-8") == "sed\npwned\n"


def test_a_refused_sandbox_raises_before_creating_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_shell, "_sandbox_tool", lambda: None)
    with pytest.raises(SandboxUnavailable):
        run_with_stubs(":", tmp_path / "run")
    assert not (tmp_path / "run").exists()


@requires_bash
def test_the_fallback_records_writers_and_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_shell, "_sandbox_tool", lambda: None)
    outside = _outside(tmp_path)
    block = f"mkdir -p '{outside}/d'; touch '{outside}/t'; cp a '{outside}/c'; rm -rf '{outside}/f'"
    result = run_with_stubs(block, tmp_path / "run", unsandboxed_ok=True)
    assert result.calls == [
        ["mkdir", "-p", f"{outside}/d"],
        ["touch", f"{outside}/t"],
        ["cp", "a", f"{outside}/c"],
        ["rm", "-rf", f"{outside}/f"],
    ]
    assert sorted(p.name for p in outside.iterdir()) == ["f", "f2", "f3"]
    for name in skill_shell._WRITERS:
        stub = tmp_path.resolve() / "run" / "bin" / name
        assert not stub.is_symlink(), name


@requires_bash
def test_the_recording_runner_refuses_an_edited_block_without_a_sandbox(tmp_path, monkeypatch):
    """With no sandbox the prefix is empty, so an edited body must fail before it runs."""
    monkeypatch.setattr(skill_shell, "_sandbox_tool", lambda: None)
    block_id, _skill, body = _CALL_BLOCKS[0]
    workdir = tmp_path / "run"
    with pytest.raises(pytest.fail.Exception, match=rf"{block_id}.*re-pin"):
        _run_with_recording_writers(block_id, body + "\ntouch /nonexistent/edited\n", workdir)
    assert not workdir.exists()
    # Control: the unedited, pinned body still runs.
    result = _run_with_recording_writers(block_id, body, workdir)
    assert note_writer_calls(result.calls)


def test_the_pinned_call_site_blocks_are_unchanged():
    """The unsandboxed mode trusts a pinned block, so an edit needs a review."""
    changed = [
        block_id for block_id, _skill, body in _CALL_BLOCKS if not _is_pinned(block_id, body)
    ]
    assert changed == [], (
        f"{changed}: the block text changed. Review the block (it may run without "
        "a sandbox), then update its hash in _CALL_SITES."
    )


def _run_with_recording_writers(block_id, block, workdir):
    """The writers are recording stubs; a sandbox still wraps the run where one
    works. With no sandbox the prefix is empty, so the block must match its pin
    or the run is refused before anything is created."""
    prefix = sandbox_prefix(workdir) or []
    if not prefix:
        _require_pinned(block_id, block)
    return skill_shell._run(block, workdir.resolve(), 60, prefix, real_writers=False)


@requires_bash
def test_the_pinned_call_sites_hold_with_recording_writers(tmp_path):
    """With writers recorded instead of run (the unsandboxed mode), the pinned
    blocks still record their 8 writes and 1 append-update."""
    _check_call_sites(_CALL_BLOCKS, tmp_path, run=_run_with_recording_writers)


def test_the_probe_rejects_a_sandbox_that_denies_nothing(monkeypatch):
    monkeypatch.setattr(skill_shell, "_prefix_for", lambda tool, workdir: [])
    assert skill_shell._probe(("fake", "/nonexistent")) is False


def test_the_probe_rejects_a_sandbox_that_fails_to_start(monkeypatch):
    monkeypatch.setattr(skill_shell, "_prefix_for", lambda tool, workdir: ["/usr/bin/false"])
    assert skill_shell._probe(("fake", "/usr/bin/false")) is False


def test_the_probe_is_false_when_the_temp_dir_cannot_be_made(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("read-only temp dir")

    monkeypatch.setattr(skill_shell.tempfile, "mkdtemp", refuse)
    assert skill_shell._probe(("sandbox-exec", "/usr/bin/true")) is False


@pytest.mark.skipif(skill_shell._sandbox_tool() is None, reason="no OS sandbox")
def test_the_probe_passes_paths_as_arguments_not_shell_source(monkeypatch):
    """A temp dir whose name holds `$`, `"` and a backtick must not break the probe."""
    real_mkdtemp = tempfile.mkdtemp
    monkeypatch.setattr(
        skill_shell.tempfile, "mkdtemp",
        lambda prefix="": real_mkdtemp(prefix=prefix + '$x"`'),
    )
    assert skill_shell._probe(skill_shell._sandbox_tool()) is True


_CONNECT_PROBE = (
    "import socket, sys\n"
    "try:\n"
    "    socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=2).close()\n"
    "    print('CONNECTED')\n"
    "except OSError as exc:\n"
    "    print('BLOCKED:', exc)\n"
)


def _connect_block(port: int) -> str:
    return f"'{sys.executable}' -c \"{_CONNECT_PROBE}\" {port}\n"


def _accepted(listener: socket.socket) -> bool:
    listener.settimeout(0.5)
    try:
        listener.accept()[0].close()
    except OSError:  # socket.timeout is an OSError.
        return False
    return True


@requires_sandbox
def test_the_sandbox_denies_network(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        result = run_with_stubs(_connect_block(port), tmp_path / "run", timeout=20)
        accepted = _accepted(listener)
    assert result.stdout.startswith("BLOCKED"), (result.stdout, result.stderr)
    assert not accepted


@requires_sandbox
def test_without_the_sandbox_the_same_block_connects(tmp_path):
    """Mutation check for the test above: with no prefix, the connection works."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        result = skill_shell._run(
            _connect_block(port), tmp_path / "run", 20, [], real_writers=True
        )
        accepted = _accepted(listener)
    assert result.stdout == "CONNECTED\n", (result.stdout, result.stderr)
    assert accepted


_UNIX_CONNECT_PROBE = (
    "import socket, sys\n"
    "s = socket.socket(socket.AF_UNIX)\n"
    "s.settimeout(2)\n"
    "try:\n"
    "    s.connect(sys.argv[1])\n"
    "    print('CONNECTED')\n"
    "except OSError as exc:\n"
    "    print('BLOCKED:', exc)\n"
)


def _unix_connect_block(path: str) -> str:
    return f"'{sys.executable}' -c \"{_UNIX_CONNECT_PROBE}\" '{path}'\n"


@contextlib.contextmanager
def _unix_listener():
    """A listening AF_UNIX socket in a short /tmp directory (macOS limits the
    path to ~104 bytes). Yields (listener, socket path); cleans up after."""
    short = tempfile.mkdtemp(prefix="obs", dir="/tmp")
    try:
        path = os.path.join(short, "s")
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(path)
            listener.listen(1)
            yield listener, path
    finally:
        shutil.rmtree(short, ignore_errors=True)


@requires_sandbox
@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="known limit: bwrap does not hide pathname Unix sockets (GH #526)",
)
def test_the_sandbox_denies_unix_sockets(tmp_path):
    with _unix_listener() as (listener, path):
        result = run_with_stubs(_unix_connect_block(path), tmp_path / "run", timeout=20)
        accepted = _accepted(listener)
    assert result.stdout.startswith("BLOCKED"), (result.stdout, result.stderr)
    assert not accepted


@requires_sandbox
def test_without_the_sandbox_the_same_block_connects_to_a_unix_socket(tmp_path):
    """Mutation check for the test above: with no prefix, the connection works."""
    with _unix_listener() as (listener, path):
        result = skill_shell._run(
            _unix_connect_block(path), tmp_path / "run", 20, [], real_writers=True
        )
        accepted = _accepted(listener)
    assert result.stdout == "CONNECTED\n", (result.stdout, result.stderr)
    assert accepted


def test_the_probe_rejects_a_missing_sandbox_binary(monkeypatch):
    monkeypatch.setattr(
        skill_shell, "_prefix_for", lambda tool, workdir: ["/nonexistent/sandbox-exec"]
    )
    assert skill_shell._probe(("fake", "/nonexistent/sandbox-exec")) is False


def _broken_listing(error=PermissionError("ps: launch refused")):
    def listing():
        raise error
    return listing


@requires_sandbox
@pytest.mark.parametrize("error", [
    PermissionError("ps: launch refused"),
    ValueError("invalid literal for int()"),
    subprocess.SubprocessError("ps failed"),
], ids=["PermissionError", "ValueError", "SubprocessError"])
def test_a_failed_process_listing_keeps_the_result(tmp_path, monkeypatch, error):
    """X-025: cleanup must not replace the block's result, and the block's own
    group is still killed. A `set -m` child needs the listing to be found."""
    monkeypatch.setattr(skill_shell, "_list_pids", _broken_listing(error))
    pid_file = tmp_path / "run" / "tmp" / "pid"
    try:
        result = run_with_stubs(
            'sleep 300 & echo $! > "$TMPDIR/pid"; echo out; exit 3', tmp_path / "run"
        )
        gone = _wait_gone(int(pid_file.read_text()))
    finally:
        _kill_pid_file(pid_file)
    assert (result.returncode, result.stdout) == (3, "out\n")
    assert gone


@requires_sandbox
def test_a_failed_process_listing_keeps_the_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_shell, "_list_pids", _broken_listing())
    with pytest.raises(subprocess.TimeoutExpired):
        run_with_stubs("sleep 30", tmp_path / "run", timeout=1)


def test_a_failing_ps_is_a_listing_error(monkeypatch):
    class Failed:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(skill_shell.subprocess, "run", lambda *a, **k: Failed())
    with pytest.raises(OSError):
        skill_shell._list_pids()


def test_python_script_call():
    for flags in (
        [], ["-B"], ["-I", "-u"], ["-Bu"], ["-BIuEsSOqbdvx"],
        ["-X", "utf8"], ["-W", "ignore"], ["-Xutf8", "-Wignore"],
        ["-X", "utf8", "-W", "ignore"],
    ):
        argv = ["python3", *flags, "/hooks/note_writer.py", "write", "-v"]
        assert python_script_call(argv) == ("note_writer.py", ["write", "-v"]), argv

    for argv in (
        ["python3", "-B", "-c", "print(1)"],
        ["python3", "-X", "utf8", "-m", "note_writer.py"],
        ["python3", "-Wignore", "-", "note_writer.py"],
        ["git", "note_writer.py", "write"],
        ["python3"],
        [],
    ):
        assert python_script_call(argv) is None, argv


def _body_file_steps(tmp_path):
    """Find the surrounding Markdown step for each recorded body-file write."""
    found = []
    for block_id, skill, block in _CALL_BLOCKS:
        calls = _recorded_calls(block_id, block, tmp_path / block_id)
        if not any(args[:1] == ["write"] and "--body-file" in args for args in calls):
            continue
        block_line = int(block_id.rsplit("-", 1)[1])
        text = _SKILL_TEXTS[skill]
        fenced_lines = {
            line
            for fence in fenced_blocks(text, langs=None)
            for line in range(fence.line, fence.line + len(fence.body.splitlines()) + 1)
        }
        lines = text.splitlines()
        headings = [
            line for line, content in enumerate(lines, 1)
            if line not in fenced_lines and content.startswith("#")
        ]
        start = max((line for line in headings if line < block_line), default=1)
        end = min((line for line in headings if line > block_line), default=len(lines) + 1)
        found.append((f"{skill}@{block_line}", "\n".join(lines[start - 1:end - 1])))
    return found


@requires_bash
def test_every_body_file_step_tells_the_model_to_write_remove_and_keep_the_file(tmp_path):
    """Each body-file step tells the model to write, remove and retain the draft."""
    steps = _body_file_steps(tmp_path)
    assert len(steps) == 8, [s[0] for s in steps]
    for site, prose in steps:
        assert "**Write tool**" in prose, site
        assert "After an `OK:` line, run `rm -f '<BODY_FILE>'`" in prose, site
        retro = site.startswith("retro@")
        draft = ("`<WORK>/retro-note-<draft-id>.md`" if retro
                 else "`~/.claude/obsidian-brain/<draft-id>-<filename>`")
        assert draft in prose, site
        assert "`<draft-id>` is 8 random hex characters you pick once per run" in prose, site
        assert "keep `<BODY_FILE>`" in prose, site
