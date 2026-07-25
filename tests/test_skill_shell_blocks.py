"""Execute the SKILL.md ``note_writer.py`` heredoc blocks under a real shell.

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
_CMD_LINE = 'python3 "$HOOKS/note_writer.py"'
# Matches the opener under EITHER scheme -- with the per-invocation `<eof4>`
# placeholder or the old fixed form. Deliberately scheme-agnostic so that
# reverting to a fixed delimiter makes the execution test fail on its real
# assertions (truncated note + executed side effect) rather than crashing in
# the harness, which would prove nothing.
_HEREDOC_OPEN_RE = re.compile(r"<<'(OB_[A-Za-z0-9_]+(?:_<eof4>)?)'")
_HOOKS_LINE_RE = re.compile(r"^HOOKS=\$\(python3 .*\)$", re.MULTILINE)
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
            if not line.startswith(_CMD_LINE):
                continue
            start = next(
                j for j in range(i, -1, -1) if lines[j].strip() == "```bash"
            )
            end = next(
                j for j in range(i, len(lines)) if lines[j].strip() == "```"
            )
            block = "\n".join(lines[start + 1:end])
            assert "<<'OB_" in block, (
                f"{skill_name}: note_writer.py block at line {i + 1} has no heredoc"
            )
            found.append((f"{skill_name}-{n}", skill_name, block))
            n += 1
    return found


_BLOCKS = _collect_blocks()


def test_every_note_writer_heredoc_site_is_covered():
    """Nine known call sites today (compress x2, standup x2, decide,
    error-log, retro, vault-import, vault-stats). A drop below that means a
    block stopped matching the extractor — i.e. it silently lost coverage."""
    assert len(_BLOCKS) >= 9, [b[0] for b in _BLOCKS]


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
    raw = _HEREDOC_OPEN_RE.search(block).group(1)
    terminator = raw.replace("<eof4>", _EOF4)
    delim_base = re.sub(r"_<eof4>$", "", raw)

    script = _HOOKS_LINE_RE.sub(f'HOOKS="{_HOOKS_DIR}"', block)
    script = script.replace("<eof4>", _EOF4)
    script = _WRITE_FILENAME_RE.sub(rf'\1"{note_name}"', script)

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


@requires_bash
@pytest.mark.parametrize(
    "block_id,skill_name,block", _BLOCKS, ids=[b[0] for b in _BLOCKS]
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
