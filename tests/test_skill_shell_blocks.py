"""Execute the SKILL.md ``note_writer.py`` blocks under a real shell.

Call sites are found by running EVERY fenced shell block of every skill under
bash with stub ``python3``, ``git`` and ``gh`` that record their argv (#522),
not by matching command text: regexes missed ``python3 -X utf8``, a ``\\``
continuation before ``write`` and tilde fences, and counted a shell comment as
a call. Execution only sees calls that ran, so a block that stops early or
sits in an untaken branch records nothing. The pinned ``_CALL_SITES`` set
names the blocks that record a call today, and the counts (8 ``write``, 1
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

import os
import re
import shutil
import subprocess
import time
from collections import Counter

import pytest

from skill_shell import (
    HOOKS_DIR,
    _blank_quotes_and_heredocs,
    fenced_blocks,
    note_writer_calls,
    python_script_call,
    run_with_stubs,
)

_BASH = shutil.which("bash")
requires_bash = pytest.mark.skipif(_BASH is None, reason="bash not available")

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
_CALL_SITES = frozenset({
    "compress-250",
    "compress-514",
    "decide-262",
    "error-log-252",
    "retro-412",
    "standup-325",
    "standup-582",
    "vault-import-285",
    "vault-stats-238",
})
_CALL_BLOCKS = [block for block in _ALL_BLOCKS if block[0] in _CALL_SITES]
# A call in an untaken branch is not recorded, so a call site must not sit
# inside one.
_BRANCH_WORDS = frozenset({"if", "case", "elif", "else", "while"})


def _recorded_calls(block_id, block, workdir):
    result = run_with_stubs(block, workdir)
    assert result.returncode == 0, f"{block_id}: {result.stderr}"
    return note_writer_calls(result.calls)


def _assert_body_file(args, block_id):
    assert "--body-file" in args, (block_id, args)
    start = args.index("--body-file")
    assert args[start + 1:start + 2] == ["<BODY_FILE>"], (block_id, args)


def _branch_words(block: str) -> set[str]:
    """Branch keywords that start a command line of the block.

    Heredoc bodies are note text, not shell. The one-line `if ! test -f` guard at
    the top of each call-site block exits when it fails, so it cannot hide a call.
    """
    first_words = {
        line.split()[0] for line in _blank_quotes_and_heredocs(block).splitlines()
        if line.strip() and not line.lstrip().startswith("if ! test -f")
    }
    return first_words & _BRANCH_WORDS


def test_a_call_inside_a_branch_is_detected():
    assert _branch_words("if x; then\n  python3 n.py write\nfi") == {"if"}
    assert _branch_words("case x in\n  a) python3 n.py ;;\nesac") == {"case"}
    assert _branch_words("x\nelse\ny") == {"else"}
    assert _branch_words("if ! test -f f; then exit 1; fi\ncat <<E\nif a\nE") == set()


@requires_bash
def test_note_writer_call_sites_match_the_pinned_counts(tmp_path):
    assert {block_id for block_id, _skill, _block in _CALL_BLOCKS} == _CALL_SITES
    recorded = set()
    counts = Counter()
    for block_id, _skill, block in _ALL_BLOCKS:
        result = run_with_stubs(block, tmp_path / block_id)
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
    assert recorded == _CALL_SITES
    assert counts == {"write": 8, "append-update": 1}
    for block_id, _skill, block in _CALL_BLOCKS:
        assert _branch_words(block) == set(), block_id


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

    result = subprocess.run(
        [_BASH, str(script_path)],
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


@requires_bash
def test_stub_records_write_with_boolean_flag(tmp_path):
    block = _SCRATCH_SETUP + f'python3 -B "$HOOKS/note_writer.py" {_SCRATCH_WRITE}'
    assert _recorded_calls("boolean flag", block, tmp_path) == _scratch_write_args(tmp_path)


@requires_bash
def test_stub_records_write_with_value_flags(tmp_path):
    block = _SCRATCH_SETUP + (
        f'python3 -X utf8 -W ignore "$HOOKS/note_writer.py" {_SCRATCH_WRITE}'
    )
    assert _recorded_calls("value flags", block, tmp_path) == _scratch_write_args(tmp_path)


@requires_bash
def test_stub_records_write_with_line_continuation(tmp_path):
    block = _SCRATCH_SETUP + f'python3 "$HOOKS/note_writer.py" \\\n  {_SCRATCH_WRITE}'
    assert _recorded_calls("line continuation", block, tmp_path) == _scratch_write_args(tmp_path)


@requires_bash
def test_stub_records_write_in_four_tilde_fence(tmp_path):
    block = _SCRATCH_SETUP + f'python3 "$HOOKS/note_writer.py" {_SCRATCH_WRITE}'
    parsed, = fenced_blocks(f"~~~~bash\n{block}\n~~~~")
    assert _recorded_calls("four-tilde fence", parsed.body, tmp_path) == _scratch_write_args(tmp_path)


@requires_bash
def test_stub_ignores_commented_write(tmp_path):
    block = _SCRATCH_SETUP + f'# python3 "$HOOKS/note_writer.py" {_SCRATCH_WRITE}'
    assert _recorded_calls("commented write", block, tmp_path) == []


@requires_bash
def test_stub_does_not_record_absolute_python_path(tmp_path):
    """A real python3 called by absolute path bypasses the stub, so it is not recorded."""
    fake = tmp_path / "abs" / "python3"
    fake.parent.mkdir(parents=True)
    fake.write_text(f"#!/bin/sh\ntouch '{fake.parent}/ran'\n", encoding="utf-8")
    fake.chmod(0o755)
    block = _SCRATCH_SETUP + f"'{fake}' \"$HOOKS/note_writer.py\" write"
    result = run_with_stubs(block, tmp_path / "run")
    assert (fake.parent / "ran").exists()
    assert result.calls == [["python3", "-c", "print(1)"]]
    assert note_writer_calls(result.calls) == []


@requires_bash
def test_grep_is_a_recording_stub_that_scans_nothing(tmp_path):
    block = 'grep -rl "session_id:" "$VAULT_PATH/$SESSIONS_FOLDER/"'
    result = run_with_stubs(block, tmp_path)
    vault = tmp_path.resolve() / "vault"
    assert result.returncode == 0
    assert result.calls == [["grep", "-rl", "session_id:", f"{vault}/sessions/"]]
    assert not (tmp_path / "bin" / "grep").resolve().samefile(shutil.which("grep"))


@requires_bash
def test_a_timeout_kills_the_whole_process_group(tmp_path):
    pid_file = tmp_path / "pid"
    block = f"/bin/sleep 300 &\necho $! > '{pid_file}'\nwait"
    with pytest.raises(subprocess.TimeoutExpired):
        run_with_stubs(block, tmp_path / "run", timeout=2)
    pid = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, 9)
        pytest.fail("the backgrounded child outlived the timeout")


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
