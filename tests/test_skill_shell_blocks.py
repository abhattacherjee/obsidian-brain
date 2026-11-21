"""Execute the SKILL.md ``note_writer.py`` blocks under a real shell.

Since #517 every ``write`` call takes the note from ``--body-file`` (a file the
model writes with the Write tool), so no note text sits in the Bash command.
Only /compress's ``append-update`` still pipes its section through a heredoc.
Both shapes run here with the same hostile body.

Why this module exists (and why it is NOT part of test_note_writer.py):
``tests/test_note_writer.py`` invokes the CLI as
``subprocess.run([sys.executable, NOTE_WRITER, ...], input=content)`` — list
argv, ``shell=False``, content piped straight to the child's stdin. There is
no shell, no heredoc and no delimiter anywhere in that test path, so the
heredoc-delimiter collision bug was structurally unreachable by it. That
scoping is correct; the gap was that NO test in the repo ever executed a
SKILL.md bash block. ``test_skill_snippets.py`` only ``compile()``s
``python3 -c`` fragments.

Each block is extracted verbatim from its SKILL.md, minimally rewritten (see
``_prepare``) and run through real ``bash`` against a scratch vault under
``tmp_path``, with a note body that contains a **column-0 line equal to the
documented delimiter base** — exactly what a note *about this plugin* looks
like. The parameterization is over every matching block, so a new call site
is covered the moment it is added.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_HOOKS_DIR = os.path.join(_REPO_ROOT, "hooks")

_BASH = shutil.which("bash")
requires_bash = pytest.mark.skipif(_BASH is None, reason="bash not available")

# Blocks are located by walking OUTWARD from the note_writer.py invocation to
# its enclosing ```bash / ``` fence, not by pairing fences globally: several
# SKILL.md files contain unbalanced bare ``` lines inside prose/templates, and
# a global non-greedy ```bash…``` regex silently matched a prose span instead
# of the real block (which then looks like "no heredoc here" — a test that
# quietly covers nothing).
# One pattern for both the strict collector and the loose cross-check. It does not
# list interpreter flags (`python3 -B`, `-X utf8`, `-W ignore`, ...): any line with
# `note_writer.py` (optionally quoted) then whitespace then `write` or
# `append-update` counts, wherever it sits on the line. A backtick before the match
# marks a prose mention, so it is skipped.
_INVOKE_PATTERN = r"""[^`\n]*?note_writer\.py["']?[ \t]+(?:write|append-update)\b"""
_COLUMN0_INVOCATION_RE = re.compile(r"^" + _INVOKE_PATTERN)
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
_BODY_FILE_ARG = "--body-file '<BODY_FILE>'"

# The hex the SKILL.md tells the model to substitute for <eof4> per run.
_EOF4 = "beef"
SENTINEL = "SENTINEL-LAST-LINE-OF-NOTE"
SIDE_EFFECT = "SIDE_EFFECT_MARKER"

FOLDERS = {
    "INSIGHTS_FOLDER": "claude-insights",
    "SESSIONS_FOLDER": "claude-sessions",
}


def _collect_blocks():
    """Return (id, skill_name, block_text) for every SKILL.md bash block that
    invokes note_writer.py through a heredoc."""
    found = []
    for skill_path in sorted(glob.glob(os.path.join(_REPO_ROOT, "skills/*/SKILL.md"))):
        skill_name = skill_path.replace("\\", "/").split("/")[-2]
        with open(skill_path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        n = 0
        for i, line in enumerate(lines):
            if not _COLUMN0_INVOCATION_RE.match(line):
                continue
            start = next(
                j for j in range(i, -1, -1) if lines[j].strip() == "```bash"
            )
            end = next(
                j for j in range(i, len(lines)) if lines[j].strip() == "```"
            )
            block = "\n".join(lines[start + 1:end])
            assert "<<'OB_" in block or _BODY_FILE_ARG in block, (
                f"{skill_name}: note_writer.py block at line {i + 1} has neither "
                "a heredoc nor --body-file"
            )
            found.append((f"{skill_name}-{n}", skill_name, block))
            n += 1
    return found


_BLOCKS = _collect_blocks()


# A LOOSER match for the same thing `_collect_blocks` looks for: any
# note_writer.py invocation at the start of a line, indented or not, with or
# without the `python3` prefix. `_collect_blocks` requires the command at
# column 0, so a new call site written any other way would be silently
# uncovered — and a `>= 9` floor would still pass. Cross-checking the strict
# collector against this loose count makes that failure loud instead.
_LOOSE_INVOCATION_RE = re.compile(r"^" + _INVOKE_PATTERN, re.MULTILINE)


def _loose_invocation_count():
    total = 0
    for skill_path in sorted(glob.glob(os.path.join(_REPO_ROOT, "skills/*/SKILL.md"))):
        with open(skill_path, encoding="utf-8") as fh:
            total += len(_LOOSE_INVOCATION_RE.findall(fh.read()))
    return total


def test_every_note_writer_heredoc_site_is_covered():
    """Nine known call sites today (compress x2, standup x2, decide,
    error-log, retro, vault-import, vault-stats): eight ``write`` calls with
    ``--body-file`` and one ``append-update`` heredoc.

    Asserts EQUALITY against the loose count, not a `>= 9` floor: this
    harness is the only thing standing between the repo and a repeat of the
    heredoc-delimiter bug, so a call site it fails to match must fail the
    suite rather than quietly reduce coverage to nothing."""
    strict = [b[0] for b in _BLOCKS]
    loose = _loose_invocation_count()
    assert len(strict) == loose, (
        f"{loose} note_writer.py invocation(s) found by a loose scan of "
        f"skills/*/SKILL.md but only {len(strict)} extracted as runnable "
        f"blocks ({strict}). A call site is not being executed by this "
        "harness — fix _collect_blocks or the call site's formatting."
    )
    assert len(strict) >= 9, strict


def _prepare(block: str, tmp_path, vault, note_name: str):
    """Rewrite a SKILL.md block into a runnable script, changing as little as
    possible: only what the model would itself substitute at runtime.

    - ``$HOOKS`` is pinned to this repo's hooks/ (the real line resolves the
      installed plugin cache, which is not what we are testing). The block's
      own ``test -f`` guard is left in place and must pass.
    - ``<eof4>`` gets a concrete hex, exactly as the SKILL.md instructs.
    - The filename placeholder becomes a real name.
    - The heredoc BODY is replaced with the collision fixture.
    """
    body_file = _BODY_FILE_ARG in block
    if body_file:
        raw = "OB_NOTE_EOF_<eof4>"  # the old delimiter, now only hostile note text
    else:
        raw = _HEREDOC_OPEN_RE.search(block).group(1)
    terminator = raw.replace("<eof4>", _EOF4)
    delim_base = re.sub(r"_<eof4>$", "", raw)

    script = block.replace("cd '<REPO_ROOT>'", f"cd '{tmp_path}'")
    script = _HOOKS_LINE_RE.sub(f'HOOKS="{_HOOKS_DIR}"', script)
    # `.sub` is a silent no-op if the HOOKS= line ever changes shape. The
    # block's own `test -f` would then still pass -- against the INSTALLED
    # plugin cache -- and the whole harness would be testing shipped code
    # instead of the repo under test, while staying green.
    assert _HOOKS_DIR in script, (
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

    is_update = "append-update" in script
    body_head = (
        f"## Update (2026-07-25)\n"
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

    if body_file:
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
    "block_id,skill_name,block", _BLOCKS, ids=[b[0] for b in _BLOCKS]
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

    is_update = "append-update" in block
    if is_update:
        target = vault / "claude-insights" / "existing.md"
        target.write_text(EXISTING_NOTE, encoding="utf-8")
        match_path = target
    else:
        folder_var = _WRITE_FOLDER_RE.search(block).group(1)
        target = vault / FOLDERS[folder_var] / "note.md"
        match_path = None
        if "--overwrite" in block:
            # /standup Step 6.6 upgrades an EXISTING note in place; without a
            # pre-existing file the CLI would (correctly) still write, but the
            # block would not be exercising the case it documents.
            target.write_text(EXISTING_NOTE, encoding="utf-8")

    script, terminator, delim_base = _prepare(block, tmp_path, vault, "note.md")
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


_HEREDOC_BLOCKS = [b for b in _BLOCKS if _BODY_FILE_ARG not in b[2]]


def test_every_write_call_takes_the_note_from_a_body_file():
    """#517: a PreToolUse hook reads the Bash command text, so a note body in a
    heredoc can get the save refused over words the note quotes. Every
    ``write`` call passes the body as a file instead."""
    writes = [b for b in _BLOCKS if 'note_writer.py" write' in b[2]]
    assert len(writes) == 8, [b[0] for b in writes]
    for block_id, _skill, block in writes:
        assert _BODY_FILE_ARG in block, block_id
        assert "<<" not in block, block_id
    assert [b[0] for b in _HEREDOC_BLOCKS] == ["compress-0"]


@requires_bash
@pytest.mark.parametrize(
    "block_id,skill_name,block", _HEREDOC_BLOCKS, ids=[b[0] for b in _HEREDOC_BLOCKS]
)
def test_skill_heredoc_block_terminator_is_unique_per_invocation(
    block_id, skill_name, block
):
    """Structural companion to the execution test: the opener must carry the
    `<eof4>` placeholder and the terminator line must match it exactly."""
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


def test_invocation_regexes_accept_interpreter_flags():
    """Positive control: `python3 -B "$HOOKS/note_writer.py" write ...` must be
    matched by both the strict and the loose regex, else a call site written that
    way would be silently uncovered."""
    for flags in ("", "-B ", "-I -u ", "-Bu ", "-X utf8 ", "-W ignore ", "-X utf8 -W ignore "):
        line = f'python3 {flags}"$HOOKS/note_writer.py" write "$VAULT_PATH" "$X" "n.md" --body-file \'<BODY_FILE>\''
        assert _COLUMN0_INVOCATION_RE.match(line), line
        assert _LOOSE_INVOCATION_RE.findall(line), line
        assert _LOOSE_INVOCATION_RE.findall("  " + line), line
    assert not _COLUMN0_INVOCATION_RE.match('Do not copy `--overwrite` to any other `note_writer.py write` block.')


def _body_file_steps():
    """(skill, prose) for every `note_writer.py write ... --body-file` site: the
    prose runs from the preceding heading to the next one, headings outside fences."""
    found = []
    for skill_path in sorted(glob.glob(os.path.join(_REPO_ROOT, "skills/*/SKILL.md"))):
        skill = skill_path.replace("\\", "/").split("/")[-2]
        with open(skill_path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        in_fence, headings, sites = False, [], []
        for i, line in enumerate(lines):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
            elif not in_fence and line.startswith("#"):
                headings.append(i)
            if (_BODY_FILE_ARG in line
                    and re.search(r"note_writer\.py\"? write\b", line)):
                sites.append(i)
        for i in sites:
            start = max([h for h in headings if h < i], default=0)
            end = min([h for h in headings if h > i], default=len(lines))
            found.append((f"{skill}@{i + 1}", "\n".join(lines[start:end])))
    return found


def test_every_body_file_step_tells_the_model_to_write_remove_and_keep_the_file():
    """#517: each `--body-file` step says to write the body with the Write tool,
    to `rm -f` it after OK, and to keep it when the command fails."""
    steps = _body_file_steps()
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
