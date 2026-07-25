"""Tests for note_writer.py — deterministic `write` command (#269).

Two layers, deliberately redundant where they overlap:

- Subprocess-level (black-box) tests invoke
  ``python3 hooks/note_writer.py write <vault_path> <folder> <filename>``
  exactly as skills will, piping note content on stdin. These are the only
  tests that prove argv parsing, real stdin piping, process exit codes, and
  stdout/stderr formatting work end-to-end through the actual CLI entry
  point skills call — do not replace them with in-process calls.
- In-process tests import ``note_writer`` directly and call
  ``run_write()``/``main()`` in-process (with ``monkeypatch``), so
  coverage.py can instrument the module and branches unreachable except by
  forcing ``write_vault_note`` to fail (e.g. the ``ERROR:`` path) are
  exercised directly rather than only indirectly via a real filesystem
  failure.
"""
from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

NOTE_WRITER = Path(__file__).resolve().parent.parent / "hooks" / "note_writer.py"

STDIN_CAP_CHARS = 1_000_000

# Every `write` call site pipes a COMPLETE note (frontmatter + body), and the
# CLI now enforces that (note_writer._validate_note_content), so any test
# asserting a successful write must pipe a real note rather than a bare line.
# Rejection tests deliberately keep their own throwaway content: argument
# validation runs before content validation, so each of them still exercises
# the guard it names.
MINIMAL_FM = (
    "---\n"
    "type: claude-insight\n"
    "date: 2026-07-25\n"
    "---\n"
    "\n"
)

# Mirrors test_check_items_cli.py's convention of also inserting hooks/ onto
# sys.path directly in the test module (in addition to conftest.py's global
# insert), so `import note_writer` works the same way regardless of how the
# test module is collected.
HOOKS_DIR = os.path.join(os.path.dirname(__file__), "..", "hooks")
if HOOKS_DIR not in sys.path:
    sys.path.insert(0, HOOKS_DIR)

import note_writer  # noqa: E402


def _run_write(vault_path, folder, filename, content: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(NOTE_WRITER), "write", str(vault_path), folder, filename],
        input=content,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _run_argv(*args: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(NOTE_WRITER), *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_write_creates_file_with_verbatim_content(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "claude-sessions").mkdir()
    content = MINIMAL_FM + "# Session\n\nSome body text.\n"

    result = _run_write(vault, "claude-sessions", "note.md", content)

    dest = vault / "claude-sessions" / "note.md"
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"OK: {dest.resolve()}"
    assert dest.read_text(encoding="utf-8") == content


def test_write_preserves_leading_frontmatter_fence(tmp_path):
    """Guards the leading-fence-eaten class (cf.
    feedback_subagent_verbatim_write_leading_fence): a subagent-mediated
    write has been observed to drop the opening ``---`` delimiter, treating
    it as a prompt boundary rather than content. The deterministic CLI must
    not do that — byte 0 of the written file must be the literal ``-``."""
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)
    content = (
        "---\n"
        "type: claude-insight\n"
        "tags:\n"
        "  - claude/insight\n"
        "---\n"
        "\n"
        "# Insight\n"
        "Body.\n"
    )

    result = _run_write(vault, "claude-insights", "insight.md", content)

    dest = vault / "claude-insights" / "insight.md"
    assert result.returncode == 0, result.stderr
    written = dest.read_text(encoding="utf-8")
    assert written == content
    assert written.startswith("---\n")  # fence not eaten


def test_write_preserves_dollar_backtick_and_fenced_block_verbatim(tmp_path):
    """Content containing $VAR, backticks, and a fenced code block must
    survive verbatim — no shell/markdown interpolation anywhere in the
    write path."""
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    content = MINIMAL_FM + (
        "Ran `echo $HOME` and got a path back.\n"
        "\n"
        "```python\n"
        "print(\"hello $USER\")\n"
        "```\n"
    )

    result = _run_write(vault, "claude-sessions", "special-chars.md", content)

    dest = vault / "claude-sessions" / "special-chars.md"
    assert result.returncode == 0, result.stderr
    assert dest.read_text(encoding="utf-8") == content


def test_write_creates_missing_target_folder(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    # Note: claude-decisions does NOT exist yet under vault.
    content = MINIMAL_FM + "# Decision\nBody.\n"

    result = _run_write(vault, "claude-decisions", "decision.md", content)

    dest = vault / "claude-decisions" / "decision.md"
    assert result.returncode == 0, result.stderr
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == content


def test_write_file_mode_is_0o600(tmp_path):
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)

    result = _run_write(vault, "claude-sessions", "mode-check.md", MINIMAL_FM + "content\n")

    dest = vault / "claude-sessions" / "mode-check.md"
    assert result.returncode == 0, result.stderr
    mode = stat.S_IMODE(dest.stat().st_mode)
    assert mode == 0o600, oct(mode)


# ---------------------------------------------------------------------------
# Path traversal guard (delegated to obsidian_utils.write_vault_note)
# ---------------------------------------------------------------------------

def test_write_blocks_path_traversal_no_file_created_anywhere(tmp_path):
    """A naive implementation (join + write, no resolve()/is_relative_to()
    containment check) WOULD write this file — verified during TDD by
    running this exact test against a naive note_writer.py stub that wrote
    the file directly with no guard; it wrote to ``tmp_path/escaped.md``
    (outside vault/) and the assertions below failed. The real
    implementation delegates to write_vault_note(), which performs the
    containment check BEFORE any filesystem side effect, so no file is
    created anywhere under tmp_path."""
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    content = "malicious content\n"

    # Escapes vault/claude-sessions/ -> vault/ -> tmp_path/, landing at
    # tmp_path/escaped.md, outside the vault root.
    result = _run_write(vault, "claude-sessions", "../../escaped.md", content)

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert "OK:" not in result.stdout

    # Prove no file was created anywhere under tmp_path, not just at the
    # one guessed escape path.
    pre_existing = {vault / "claude-sessions"}
    created_files = [
        p for p in tmp_path.rglob("*")
        if p.is_file()
    ]
    assert created_files == [], f"unexpected file(s) created: {created_files}"
    assert not (tmp_path / "escaped.md").exists()


# ---------------------------------------------------------------------------
# Stdin cap
# ---------------------------------------------------------------------------

def test_write_oversize_stdin_rejected_nothing_written(tmp_path):
    """REPLACES test_write_oversize_stdin_truncated_at_cap, which asserted
    rc==0 and a file truncated to exactly the cap. That behaviour is the
    anti-goal of this CLI: the note lost its tail (e.g. ``## Session
    Metadata``) with no signal at all, and at /standup's in-place
    ``--overwrite`` site a truncated write replaces a complete note with a
    mutilated one. Oversize input is now an ERROR with no write.

    The cap VALUE is unchanged (and the character-vs-byte question is a
    separate repo-wide follow-up) -- only truncation-vs-rejection changed."""
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    oversize_content = MINIMAL_FM + "A" * (STDIN_CAP_CHARS + 500)

    result = _run_write(vault, "claude-sessions", "oversize.md", oversize_content)

    dest = vault / "claude-sessions" / "oversize.md"
    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert str(STDIN_CAP_CHARS) in result.stderr
    assert "OK:" not in result.stdout
    assert not dest.exists()


def test_write_exactly_at_cap_still_succeeds(tmp_path):
    """Boundary companion to the rejection test above: content of EXACTLY
    STDIN_CAP_CHARS characters is at the cap, not over it, and must still be
    written in full. A `> cap` check (correct) and a `>= cap` check (off by
    one) differ only on this input."""
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    padding = "A" * (STDIN_CAP_CHARS - len(MINIMAL_FM))
    content = MINIMAL_FM + padding
    assert len(content) == STDIN_CAP_CHARS

    result = _run_write(vault, "claude-sessions", "at-cap.md", content)

    dest = vault / "claude-sessions" / "at-cap.md"
    assert result.returncode == 0, result.stderr
    assert dest.read_text(encoding="utf-8") == content


def test_append_update_oversize_stdin_rejected_note_unchanged(tmp_path):
    """Same cap, same rejection, on the append-update path -- and the
    existing note must be left byte-identical."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    before = note.read_bytes()

    result = _run_append_update(vault, note, "A" * (STDIN_CAP_CHARS + 500))

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert note.read_bytes() == before


# ---------------------------------------------------------------------------
# CLI arity / dispatch errors
# ---------------------------------------------------------------------------

def test_missing_args_exits_2_with_usage(tmp_path):
    result = _run_argv("write", str(tmp_path))  # missing folder + filename

    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


def test_no_command_exits_2_with_usage(tmp_path):
    result = _run_argv()

    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


def test_unknown_command_exits_2(tmp_path):
    result = _run_argv("bogus-command", str(tmp_path), "folder", "file.md")

    assert result.returncode == 2
    assert "unknown command" in result.stderr.lower()


# ---------------------------------------------------------------------------
# In-process tests — same claims as above, verified via direct calls so
# coverage.py can instrument note_writer.py and so the write_vault_note
# error branch can be forced without depending on a real filesystem failure.
# ---------------------------------------------------------------------------

def test_run_write_in_process_success(tmp_path, capsys):
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    content = MINIMAL_FM + "# Session\nBody.\n"

    rc = note_writer.run_write(str(vault), "claude-sessions", "note.md", content)

    dest = vault / "claude-sessions" / "note.md"
    assert rc == 0
    assert dest.read_text(encoding="utf-8") == content
    captured = capsys.readouterr()
    assert captured.out.strip() == f"OK: {dest.resolve()}"
    # write_vault_note() itself logs a "wrote <dest>" diagnostic to stderr on
    # success (obsidian_utils.py) — that's expected; just confirm no error.
    assert "ERROR" not in captured.err


def test_run_write_forced_error_path_prints_error_and_returns_1(
    tmp_path, monkeypatch, capsys
):
    """Forces obsidian_utils.write_vault_note's error branch directly —
    exercised only indirectly (via a real path-traversal block) by the
    subprocess tests above. This proves run_write()'s ERROR:/exit-1 path is
    reachable independent of *why* write_vault_note failed."""
    monkeypatch.setattr(
        note_writer, "write_vault_note", lambda *a, **k: "forced failure for test"
    )

    vault = tmp_path / "vault"
    vault.mkdir()  # must exist to clear the new _validate_vault_path guard first
    # Content must be a valid note too, so the forced write_vault_note error
    # is what produces the ERROR line -- not the content guard upstream of it.
    rc = note_writer.run_write(str(vault), "folder", "file.md", MINIMAL_FM + "content\n")

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "ERROR: forced failure for test"
    assert captured.out == ""
    # write_vault_note was faked out entirely, so it created nothing inside
    # the (pre-existing, guard-satisfying) vault directory.
    assert list(vault.iterdir()) == []


def test_main_write_dispatch_success_in_process(tmp_path, monkeypatch, capsys):
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    content = MINIMAL_FM + "in-process main() content\n"

    monkeypatch.setattr(
        sys, "argv", ["note_writer.py", "write", str(vault), "claude-sessions", "main.md"]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(content))

    with pytest.raises(SystemExit) as exc_info:
        note_writer.main()

    assert exc_info.value.code == 0
    dest = vault / "claude-sessions" / "main.md"
    assert dest.read_text(encoding="utf-8") == content
    captured = capsys.readouterr()
    assert captured.out.strip() == f"OK: {dest.resolve()}"


def test_main_unknown_command_exits_2_in_process(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["note_writer.py", "bogus-command"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    with pytest.raises(SystemExit) as exc_info:
        note_writer.main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "unknown command" in captured.err.lower()


def test_main_wrong_arity_exits_2_in_process(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["note_writer.py", "write", "only-one-arg"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    with pytest.raises(SystemExit) as exc_info:
        note_writer.main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "usage" in captured.err.lower()


def test_main_no_argv_exits_2_in_process(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["note_writer.py"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    with pytest.raises(SystemExit) as exc_info:
        note_writer.main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "usage" in captured.err.lower()


# ---------------------------------------------------------------------------
# folder/filename validation — closes a within-vault write vector
#
# write_vault_note()'s containment check only blocks writes that escape the
# VAULT ROOT. It does not constrain where *inside* the vault a write lands.
# Obsidian executes JavaScript from .obsidian/plugins/<x>/main.js, so a
# folder of ".obsidian/plugins/evil" + filename "main.js" passes containment
# cleanly and lands executable code — folder/filename are assembled by an
# LLM from session content, so they are not fully trusted inputs.
# ---------------------------------------------------------------------------

def _assert_no_files_created(tmp_path) -> None:
    created_files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert created_files == [], f"unexpected file(s) created: {created_files}"


def test_write_rejects_dotted_folder_obsidian_plugin_vector(tmp_path):
    """A valid, realistic end-to-end case for the .obsidian/plugins/evil
    vector — proven fail-first against the pre-fix note_writer.py (see fix
    report), which actually wrote executable content to
    vault/.obsidian/plugins/evil/main.js.

    NOTE: this specific filename ("main.js") does NOT isolate the
    dot-segment rule in _validate_folder — _validate_filename's own
    extension check independently rejects "main.js" regardless of the
    folder's dot-segment. See
    test_write_rejects_dotted_folder_isolated_by_dot_segment_rule below for
    the test that can ONLY be rejected by the dot-segment rule."""
    vault = tmp_path / "vault"
    vault.mkdir()

    result = _run_write(vault, ".obsidian/plugins/evil", "main.js", "malicious JS\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert "OK:" not in result.stdout
    _assert_no_files_created(tmp_path)


def test_write_rejects_dotted_folder_isolated_by_dot_segment_rule(tmp_path):
    """The load-bearing isolating case: filename "main.md" passes every
    OTHER check (bare name, .md extension, non-empty stem) — only
    _validate_folder's dot-segment rule can reject
    ".obsidian/plugins/evil" here.

    Proven fail-first empirically: with the `part.startswith(".")` branch
    in _validate_folder temporarily removed, this exact scenario (folder=
    ".obsidian/plugins/evil", filename="main.md") returned rc=0 and
    genuinely wrote the file to
    vault/.obsidian/plugins/evil/main.md (see fix report for the full
    command/output). Restoring the branch makes this test pass."""
    vault = tmp_path / "vault"
    vault.mkdir()

    result = _run_write(vault, ".obsidian/plugins/evil", "main.md", "malicious md\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert "dot" in result.stderr.lower() or "hidden" in result.stderr.lower()
    assert "OK:" not in result.stdout
    _assert_no_files_created(tmp_path)


def test_write_rejects_filename_subdirectory_separator(tmp_path):
    """Also load-bearing: proven fail-first by running this against the
    pre-fix note_writer.py with the 'notes' subdirectory pre-created under
    the target folder — the file landed at
    vault/claude-sessions/notes/x.md (see fix report). In a fresh folder
    the pre-fix code happened to error out for an unrelated reason (mkdir
    only creates the folder, not filename subdirectories), so the guard
    must not depend on that directory-state coincidence."""
    vault = tmp_path / "vault"
    (vault / "claude-sessions" / "notes").mkdir(parents=True)

    result = _run_write(vault, "claude-sessions", "notes/x.md", "subdir escape\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert not (vault / "claude-sessions" / "notes" / "x.md").exists()


def test_write_rejects_filename_path_traversal(tmp_path):
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)

    result = _run_write(vault, "claude-sessions", "../../escape.md", "x\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    _assert_no_files_created(tmp_path)


def test_write_rejects_non_md_filename_extension(tmp_path):
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)

    result = _run_write(vault, "claude-sessions", "x.txt", "x\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert not (vault / "claude-sessions" / "x.txt").exists()


def test_write_rejects_folder_traversal(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()

    result = _run_write(vault, "../outside", "y.md", "x\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    _assert_no_files_created(tmp_path)


def test_write_rejects_absolute_folder(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()

    result = _run_write(vault, "/etc", "y.md", "x\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    _assert_no_files_created(tmp_path)


def test_write_rejects_home_relative_folder(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()

    result = _run_write(vault, "~/x", "y.md", "x\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    _assert_no_files_created(tmp_path)


def test_write_rejects_empty_folder_writing_to_vault_root(tmp_path):
    """Path("vault") / "" == Path("vault") — an empty folder silently
    collapses to the vault root, defeating the "must target a named
    subfolder" contract. Not a containment escape (write_vault_note()'s
    own guard does not fire), so this needs its own explicit rejection.

    Proven fail-first: with the `folder in ("", ".")` check removed from
    _validate_folder, this exact scenario returned rc=0 and wrote
    vault/root-escape.md directly at the vault root (see fix report)."""
    vault = tmp_path / "vault"
    vault.mkdir()

    result = _run_write(vault, "", "root-escape.md", "x\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert not (vault / "root-escape.md").exists()
    _assert_no_files_created(tmp_path)


def test_write_rejects_dot_folder_writing_to_vault_root(tmp_path):
    """Path("vault") / "." == Path("vault") — same vault-root collapse as
    folder="", via a different literal. Proven fail-first the same way as
    the folder="" case above (see fix report)."""
    vault = tmp_path / "vault"
    vault.mkdir()

    result = _run_write(vault, ".", "root-escape.md", "x\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert not (vault / "root-escape.md").exists()
    _assert_no_files_created(tmp_path)


def test_write_rejects_dot_md_filename_hidden_dotfile(tmp_path):
    """filename=".md" is a bare name ending in ".md", so it passes both the
    separator check and the extension check — but it has nothing before
    the suffix, so it would create a hidden dotfile in the target folder.

    Proven fail-first: with the empty-stem check removed from
    _validate_filename, this exact scenario returned rc=0 and wrote
    vault/claude-sessions/.md (see fix report)."""
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)

    result = _run_write(vault, "claude-sessions", ".md", "x\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert not (vault / "claude-sessions" / ".md").exists()


# ---------------------------------------------------------------------------
# vault_path guard (security-critical — Path("").resolve() and a relative
# path both resolve against the process CWD, not a fixed vault root, so an
# unsubstituted `$VAULT_PATH` in a skill block would otherwise silently land
# the note inside whatever directory the CLI happens to be invoked from
# (every skill site `cd`s into the user's git repo root first). Proven
# fail-first: prior to _validate_vault_path existing, the empty-string case
# below returned rc=0 and genuinely created <cwd>/claude-insights/leak.md
# (see fix report for the exact captured output).
# ---------------------------------------------------------------------------

def test_write_rejects_empty_vault_path_does_not_leak_into_cwd(tmp_path):
    """Runs the real CLI with cwd=tmp_path — standing in for "the user's
    repo root", which every skill call site cds into before invoking the
    CLI — and asserts nothing lands there when vault_path is empty."""
    result = subprocess.run(
        [sys.executable, str(NOTE_WRITER), "write", "", "claude-insights", "leak.md"],
        input="leak content\n",
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(tmp_path),
    )

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert "OK:" not in result.stdout
    _assert_no_files_created(tmp_path)


def test_write_rejects_relative_vault_path(tmp_path):
    """A relative vault_path is exactly as unmoored as an empty one — it
    also resolves against the CWD rather than a fixed vault root."""
    (tmp_path / "some" / "dir").mkdir(parents=True)

    result = subprocess.run(
        [sys.executable, str(NOTE_WRITER), "write", "some/dir", "claude-insights", "leak.md"],
        input="x\n",
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(tmp_path),
    )

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    _assert_no_files_created(tmp_path)


def test_write_rejects_nonexistent_absolute_vault_path(tmp_path):
    missing = tmp_path / "does-not-exist"

    result = _run_write(missing, "claude-insights", "leak.md", "x\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert not missing.exists()


def test_write_succeeds_with_valid_absolute_vault_path(tmp_path):
    """The guard must not reject the ordinary case — a real, existing,
    absolute vault directory."""
    vault = tmp_path / "vault"
    vault.mkdir()

    result = _run_write(vault, "claude-insights", "ok.md", MINIMAL_FM + "x\n")

    assert result.returncode == 0, result.stderr
    assert (vault / "claude-insights" / "ok.md").exists()


def test_write_normal_folder_and_filename_still_succeeds(tmp_path):
    """Guards against the validation guard breaking the happy path."""
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)
    content = MINIMAL_FM + "# Retro\nBody.\n"

    result = _run_write(vault, "claude-insights", "2026-07-25-retro-a3f2.md", content)

    dest = vault / "claude-insights" / "2026-07-25-retro-a3f2.md"
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"OK: {dest.resolve()}"
    assert dest.read_text(encoding="utf-8") == content


# In-process counterpart for coverage of the validation helpers directly.
def test_validate_vault_path_in_process(tmp_path):
    assert note_writer._validate_vault_path("") is not None
    assert note_writer._validate_vault_path(".") is not None
    assert note_writer._validate_vault_path("relative/dir") is not None
    assert note_writer._validate_vault_path(str(tmp_path / "does-not-exist")) is not None

    existing = tmp_path / "vault"
    existing.mkdir()
    assert note_writer._validate_vault_path(str(existing)) is None


def test_validate_folder_and_filename_in_process():
    assert note_writer._validate_folder(".obsidian/plugins/evil") is not None
    assert note_writer._validate_folder("../outside") is not None
    assert note_writer._validate_folder("/etc") is not None
    assert note_writer._validate_folder("~/x") is not None
    assert note_writer._validate_folder("") is not None
    assert note_writer._validate_folder(".") is not None
    assert note_writer._validate_folder("claude-insights") is None

    assert note_writer._validate_filename("notes/x.md") is not None
    assert note_writer._validate_filename("../../escape.md") is not None
    assert note_writer._validate_filename("x.txt") is not None
    assert note_writer._validate_filename(".md") is not None
    assert note_writer._validate_filename("2026-07-25-retro-a3f2.md") is None


def test_run_write_in_process_rejects_invalid_folder_no_write_vault_note_call(
    tmp_path, monkeypatch, capsys
):
    """In-process counterpart to the validation-rejection branch in
    run_write() (lines guarded by `if validation_err:`) — proves
    write_vault_note() is never reached when validation fails, by making it
    raise if called."""

    def _boom(*a, **k):
        raise AssertionError("write_vault_note() must not be called when validation fails")

    monkeypatch.setattr(note_writer, "write_vault_note", _boom)

    vault = tmp_path / "vault"
    vault.mkdir()  # must exist to clear the new _validate_vault_path guard first
    rc = note_writer.run_write(str(vault), ".obsidian/plugins/evil", "main.js", "x\n")

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.strip().startswith("ERROR: invalid folder")
    assert captured.out == ""


# ===========================================================================
# append-update command (#269 task 2)
# ===========================================================================
#
# Same two-layer convention as `write` above: subprocess/CLI-level tests
# exercise argv parsing + real stdin piping through the actual entry point;
# in-process tests import note_writer directly (for coverage.py
# instrumentation and to force branches a real filesystem can't easily
# reach, e.g. _atomic_rewrite's error path).

BASIC_NOTE = (
    "---\n"
    "type: claude-insight\n"
    "date: 2026-01-01\n"
    "source_session: abc123\n"
    "source_session_note: \"[[2026-01-01-session-abc123]]\"\n"
    "project: obsidian-brain\n"
    "tags:\n"
    "  - claude/insight\n"
    "  - claude/topic/foo\n"
    "---\n"
    "\n"
    "# Some Insight\n"
    "\n"
    "Body content here.\n"
)

UPDATE_SECTION = "## Update (2026-07-25)\n\nNew findings from today's session.\n"


def _run_append_update(
    vault_path, note_path, update_text, last_updated=None, add_tags=None
) -> subprocess.CompletedProcess:
    args = ["append-update", str(vault_path), str(note_path)]
    if last_updated is not None:
        args += ["--last-updated", last_updated]
    if add_tags is not None:
        args += ["--add-tags", add_tags]
    return subprocess.run(
        [sys.executable, str(NOTE_WRITER), *args],
        input=update_text,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _make_note(vault: Path, folder: str, filename: str, content: str) -> Path:
    d = vault / folder
    d.mkdir(parents=True, exist_ok=True)
    p = d / filename
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Happy path: combined body insertion + last_updated + tags in one call
# ---------------------------------------------------------------------------

def test_append_update_happy_path_combines_body_and_frontmatter(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note(
        vault, "claude-insights", "insight.md",
        BASIC_NOTE + "\n## Tool Usage\n- **Read**: some/file.py\n",
    )

    result = _run_append_update(
        vault, note, UPDATE_SECTION,
        last_updated="2026-07-25", add_tags="claude/topic/bar,claude/insight",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"OK: {note.resolve()}"
    written = note.read_text(encoding="utf-8")

    # last_updated inserted after date:
    assert "date: 2026-01-01\nlast_updated: 2026-07-25\n" in written
    # only the genuinely new tag was added; the duplicate was not
    assert written.count("claude/insight") == 1
    assert "- claude/topic/bar" in written
    # update section lands before ## Tool Usage, not after
    assert written.index("## Update (2026-07-25)") < written.index("## Tool Usage")
    # original body content preserved verbatim
    assert "Body content here.\n" in written
    assert "- **Read**: some/file.py" in written


# ---------------------------------------------------------------------------
# Insertion point: each trailing-marker variant, and EOF when none present
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "marker",
    [
        "## Tool Usage",
        "## Conversation (raw)",
        "## Session Metadata",
        "## Files Touched",
        "_(Summary source: haiku)_",
    ],
)
def test_append_update_inserts_before_each_trailing_marker_variant(tmp_path, marker):
    vault = tmp_path / "vault"
    note = _make_note(
        vault, "claude-insights", "insight.md",
        BASIC_NOTE + f"\n{marker}\nsome trailing content\n",
    )

    result = _run_append_update(vault, note, UPDATE_SECTION)

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    assert written.index("## Update (2026-07-25)") < written.index(marker)
    # trailing content itself is untouched
    assert "some trailing content" in written


def test_append_update_inserts_before_first_of_several_markers(tmp_path):
    """When multiple trailing sections are present, the update section must
    land before the FIRST one encountered (top-down), not merely before
    *some* marker or wedged between two of them."""
    vault = tmp_path / "vault"
    note = _make_note(
        vault, "claude-insights", "insight.md",
        BASIC_NOTE
        + "\n_(Summary source: haiku)_\n"
        + "\n## Tool Usage\n- x\n"
        + "\n## Conversation (raw)\n**User:** hi\n",
    )

    result = _run_append_update(vault, note, UPDATE_SECTION)

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    update_idx = written.index("## Update (2026-07-25)")
    assert update_idx < written.index("_(Summary source: haiku)_")
    assert update_idx < written.index("## Tool Usage")
    assert update_idx < written.index("## Conversation (raw)")


def test_append_update_appends_at_eof_when_no_marker_present(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)

    result = _run_append_update(vault, note, UPDATE_SECTION)

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    assert written.rstrip("\n").endswith("New findings from today's session.")
    # The blank-line separator before an EOF-appended section is a real guard
    # (`elif last_line.strip() != "": prefix += [eol]`) and had no assertion
    # anywhere -- the rstrip() check above is blind to it, so the branch could
    # be deleted with the whole suite green.
    assert "Body content here.\n\n## Update (2026-07-25)" in written
    assert "Body content here.\n## Update" not in written


# ---------------------------------------------------------------------------
# last_updated: add-vs-replace
# ---------------------------------------------------------------------------

def test_append_update_last_updated_replaces_existing_value_in_place(tmp_path):
    vault = tmp_path / "vault"
    note_content = BASIC_NOTE.replace(
        "date: 2026-01-01\n", "date: 2026-01-01\nlast_updated: 2026-02-02\n"
    )
    note = _make_note(vault, "claude-insights", "insight.md", note_content)

    result = _run_append_update(vault, note, UPDATE_SECTION, last_updated="2026-07-25")

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    assert "last_updated: 2026-07-25\n" in written
    assert "last_updated: 2026-02-02" not in written
    # exactly one last_updated line -- not duplicated
    assert written.count("last_updated:") == 1


def test_append_update_last_updated_inserted_after_date_when_absent(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    assert "last_updated:" not in note.read_text(encoding="utf-8")

    result = _run_append_update(vault, note, UPDATE_SECTION, last_updated="2026-07-25")

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    assert "date: 2026-01-01\nlast_updated: 2026-07-25\n" in written


def test_append_update_omitted_last_updated_flag_is_noop(tmp_path):
    """--last-updated is optional -- omitting it must not touch the field
    at all, whether or not it was already present."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)

    result = _run_append_update(vault, note, UPDATE_SECTION)

    assert result.returncode == 0, result.stderr
    assert "last_updated:" not in note.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tags: append-only-new, no-op when absent
# ---------------------------------------------------------------------------

def test_append_update_new_tag_appended_duplicate_skipped(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)

    result = _run_append_update(
        vault, note, UPDATE_SECTION, add_tags="claude/insight,claude/topic/new-one"
    )

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    # the duplicate ("claude/insight" was already present) was not re-added
    assert written.count("claude/insight\n") == 1
    assert "  - claude/topic/new-one\n" in written
    # new tag preserves the existing block's 2-space indentation
    tags_block = written.split("tags:\n", 1)[1].split("---", 1)[0]
    assert "claude/topic/new-one" in tags_block


def test_append_update_no_tags_block_does_not_crash(tmp_path):
    vault = tmp_path / "vault"
    note_content = BASIC_NOTE.replace(
        "tags:\n  - claude/insight\n  - claude/topic/foo\n", ""
    )
    assert "tags:" not in note_content
    note = _make_note(vault, "claude-insights", "insight.md", note_content)

    result = _run_append_update(vault, note, UPDATE_SECTION, add_tags="claude/topic/new-one")

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    assert "tags:" not in written  # nothing to merge into -- no block invented
    assert "## Update (2026-07-25)" in written  # body append still happened


def test_append_update_empty_add_tags_value_is_rejected(tmp_path):
    """REPLACES test_append_update_empty_add_tags_is_noop, which asserted
    rc==0 for `--add-tags ""`. A flag PRESENT with an empty value is the
    same shape as the `$VAULT_PATH=""` bug this branch already shipped a fix
    for -- an unsubstituted variable, not a deliberate "no tags". Omitting
    the flag entirely remains the supported no-op (see
    test_append_update_omitted_add_tags_flag_is_noop), so nothing legitimate
    loses a path here."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    before = note.read_bytes()

    result = _run_append_update(vault, note, UPDATE_SECTION, add_tags="")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert note.read_bytes() == before


def test_append_update_omitted_add_tags_flag_is_noop(tmp_path):
    """The supported no-op: no --add-tags flag at all. The tags block must
    come back byte-identical while the body update still lands."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    before = note.read_text(encoding="utf-8")

    result = _run_append_update(vault, note, UPDATE_SECTION)

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    before_tags = before.split("tags:\n", 1)[1].split("---", 1)[0]
    after_tags = written.split("tags:\n", 1)[1].split("---", 1)[0]
    assert before_tags == after_tags
    assert "## Update (2026-07-25)" in written


# ---------------------------------------------------------------------------
# `date`, `source_session`, `source_session_note`, `type` are never touched
# ---------------------------------------------------------------------------

def test_append_update_never_touches_protected_frontmatter_fields(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)

    result = _run_append_update(
        vault, note, UPDATE_SECTION,
        last_updated="2026-07-25", add_tags="claude/topic/new-one",
    )

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    assert "type: claude-insight\n" in written
    assert "date: 2026-01-01\n" in written
    assert "source_session: abc123\n" in written
    assert 'source_session_note: "[[2026-01-01-session-abc123]]"\n' in written


# ---------------------------------------------------------------------------
# Malformed/absent frontmatter fails loudly -- file unchanged on disk
# ---------------------------------------------------------------------------

def test_append_update_no_frontmatter_at_all_fails_loudly_file_unchanged(tmp_path):
    vault = tmp_path / "vault"
    content = "# Just a title\n\nNo frontmatter at all here.\n"
    note = _make_note(vault, "claude-insights", "insight.md", content)
    before = note.read_bytes()

    result = _run_append_update(vault, note, UPDATE_SECTION)

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert note.read_bytes() == before


def test_append_update_missing_closing_fence_fails_loudly_file_unchanged(tmp_path):
    vault = tmp_path / "vault"
    content = "---\ntype: claude-insight\ndate: 2026-01-01\n\n# No closing fence\n"
    note = _make_note(vault, "claude-insights", "insight.md", content)
    before = note.read_bytes()

    result = _run_append_update(vault, note, UPDATE_SECTION)

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert note.read_bytes() == before


def test_append_update_missing_date_field_when_last_updated_requested_fails_loudly(tmp_path):
    """--last-updated needs a `date:` line to insert after when
    `last_updated:` is not already present. If frontmatter has no `date:`
    line, this must fail loudly rather than silently skip the insertion or
    half-write the file (proves the failure is caught before the single
    write call, not after)."""
    vault = tmp_path / "vault"
    content = (
        "---\ntype: claude-insight\nproject: obsidian-brain\n---\n\n"
        "# No date field\nBody.\n"
    )
    note = _make_note(vault, "claude-insights", "insight.md", content)
    before = note.read_bytes()

    result = _run_append_update(vault, note, UPDATE_SECTION, last_updated="2026-07-25")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert note.read_bytes() == before


# ---------------------------------------------------------------------------
# File mode
# ---------------------------------------------------------------------------

def test_append_update_file_mode_is_0o600_after_rewrite(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    os.chmod(note, 0o644)  # start from a different mode to prove it's normalized

    result = _run_append_update(vault, note, UPDATE_SECTION)

    assert result.returncode == 0, result.stderr
    mode = stat.S_IMODE(note.stat().st_mode)
    assert mode == 0o600, oct(mode)


# ---------------------------------------------------------------------------
# Path containment guard (security-critical -- proven load-bearing via a
# scratch-copy isolation experiment; see report for the rc/stderr/mutation
# evidence). With the containment check(s) removed, this exact scenario
# (note_path resolving outside the vault root) returned rc=0 and genuinely
# rewrote the out-of-vault file in place.
# ---------------------------------------------------------------------------

def test_append_update_blocks_note_path_outside_vault_root(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text(BASIC_NOTE, encoding="utf-8")
    before = outside.read_bytes()

    result = _run_append_update(vault, outside, UPDATE_SECTION)

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert "OK:" not in result.stdout
    assert outside.read_bytes() == before


def test_append_update_blocks_traversal_via_dotdot_segments(tmp_path):
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)
    outside = tmp_path / "escaped.md"
    outside.write_text(BASIC_NOTE, encoding="utf-8")
    before = outside.read_bytes()

    escaping_note_path = vault / "claude-insights" / ".." / ".." / "escaped.md"
    result = _run_append_update(vault, escaping_note_path, UPDATE_SECTION)

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert outside.read_bytes() == before


# ---------------------------------------------------------------------------
# Dot-segment guard (security-critical -- isolated from containment: the
# path below IS fully contained within the vault root, so only the
# dot-segment rule can reject it). Proven load-bearing via the same
# scratch-copy method: with only the dot-segment loop removed, this exact
# scenario returned rc=0 and genuinely rewrote
# vault/.obsidian/plugins/evil/main.md in place (see report).
# ---------------------------------------------------------------------------

def test_append_update_rejects_dotted_segment_isolated_from_containment(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note(vault, ".obsidian/plugins/evil", "main.md", BASIC_NOTE)
    before = note.read_bytes()

    result = _run_append_update(vault, note, UPDATE_SECTION)

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert "dot" in result.stderr.lower() or "hidden" in result.stderr.lower()
    assert note.read_bytes() == before


# ---------------------------------------------------------------------------
# Must-exist / must-be-a-file guards
#
# NOTE (concern, documented honestly rather than overclaimed): these two
# guards are NOT independently exploitable the way the containment/
# dot-segment guards are -- run_append_update() only ever opens the target
# in read ("r") mode, so removing either check just delays the identical
# failure by one step (open() itself raises FileNotFoundError /
# IsADirectoryError, caught by the same `except OSError` branch, same rc=1,
# same "no file created/mutated" outcome). Verified empirically via the
# same scratch-copy method used for the two guards above. They exist for a
# clearer, more specific error message and defense-in-depth, not because
# removing them would let a write through.
# ---------------------------------------------------------------------------

def test_append_update_rejects_nonexistent_note_never_creates_it(tmp_path):
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)
    missing = vault / "claude-insights" / "does-not-exist.md"

    result = _run_append_update(vault, missing, UPDATE_SECTION)

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert not missing.exists()


def test_append_update_rejects_directory_as_note_path(tmp_path):
    vault = tmp_path / "vault"
    not_a_file = vault / "claude-insights" / "looks-like-a-note.md"
    not_a_file.mkdir(parents=True)

    result = _run_append_update(vault, not_a_file, UPDATE_SECTION)

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert not_a_file.is_dir()  # untouched, not replaced with a file


# ---------------------------------------------------------------------------
# vault_path guard (same rationale as the `write`-side tests above: an
# empty or relative vault_path resolves against the CWD, not a fixed vault
# root, so _resolve_note_path()'s own containment check would otherwise
# pass trivially for any note_path that happens to sit under the CWD).
# ---------------------------------------------------------------------------

def test_append_update_rejects_empty_vault_path_does_not_touch_cwd(tmp_path):
    note = _make_note(tmp_path, "claude-insights", "note.md", BASIC_NOTE)
    before = note.read_bytes()

    result = subprocess.run(
        [sys.executable, str(NOTE_WRITER), "append-update", "", str(note)],
        input=UPDATE_SECTION,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(tmp_path),
    )

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert "OK:" not in result.stdout
    assert note.read_bytes() == before


def test_append_update_rejects_relative_vault_path(tmp_path):
    note = _make_note(tmp_path, "claude-insights", "note.md", BASIC_NOTE)
    before = note.read_bytes()

    result = subprocess.run(
        [sys.executable, str(NOTE_WRITER), "append-update", ".", str(note)],
        input=UPDATE_SECTION,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(tmp_path),
    )

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert note.read_bytes() == before


def test_append_update_rejects_nonexistent_absolute_vault_path(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "note.md", BASIC_NOTE)
    before = note.read_bytes()

    result = _run_append_update(tmp_path / "does-not-exist", note, UPDATE_SECTION)

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert note.read_bytes() == before


def test_append_update_succeeds_with_valid_absolute_vault_path(tmp_path):
    """The guard must not reject the ordinary case."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "note.md", BASIC_NOTE)

    result = _run_append_update(vault, note, UPDATE_SECTION)

    assert result.returncode == 0, result.stderr
    assert "OK:" in result.stdout


# ---------------------------------------------------------------------------
# CLI arity / flag-parsing errors
# ---------------------------------------------------------------------------

def test_append_update_missing_note_path_arg_exits_2_with_usage(tmp_path):
    result = _run_argv("append-update", str(tmp_path))  # missing note_path

    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


def test_append_update_unknown_flag_exits_2(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)

    result = _run_argv("append-update", str(vault), str(note), "--bogus-flag", "x")

    assert result.returncode == 2
    assert "unknown flag" in result.stderr.lower()


def test_append_update_flag_missing_value_exits_2(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)

    result = _run_argv("append-update", str(vault), str(note), "--last-updated")

    assert result.returncode == 2
    assert "requires a value" in result.stderr.lower()


# ---------------------------------------------------------------------------
# In-process tests -- pure helper functions (direct coverage)
# ---------------------------------------------------------------------------

def test_split_frontmatter_returns_none_quad_when_malformed():
    assert note_writer._split_frontmatter([]) == (None, None, None, None)
    assert note_writer._split_frontmatter(["# no frontmatter\n"]) == (None, None, None, None)
    assert note_writer._split_frontmatter(["---\n", "type: x\n"]) == (None, None, None, None)


def test_split_frontmatter_happy_path():
    lines = ["---\n", "type: x\n", "date: 2026-01-01\n", "---\n", "\n", "# Title\n"]
    open_fence, fm, close_fence, body = note_writer._split_frontmatter(lines)
    assert open_fence == "---\n"
    assert fm == ["type: x\n", "date: 2026-01-01\n"]
    assert close_fence == "---\n"
    assert body == ["\n", "# Title\n"]


def test_apply_last_updated_replace_vs_insert_in_process():
    fm_with = ["date: 2026-01-01\n", "last_updated: 2026-02-02\n"]
    out, err = note_writer._apply_last_updated(fm_with, "2026-07-25")
    assert err is None
    assert out == ["date: 2026-01-01\n", "last_updated: 2026-07-25\n"]

    fm_without = ["type: x\n", "date: 2026-01-01\n", "project: y\n"]
    out2, err2 = note_writer._apply_last_updated(fm_without, "2026-07-25")
    assert err2 is None
    assert out2 == ["type: x\n", "date: 2026-01-01\n", "last_updated: 2026-07-25\n", "project: y\n"]

    fm_no_date = ["type: x\n", "project: y\n"]
    out3, err3 = note_writer._apply_last_updated(fm_no_date, "2026-07-25")
    assert out3 is None
    assert "date" in err3


def test_apply_add_tags_in_process():
    fm = ["tags:\n", "  - claude/insight\n", "project: y\n"]
    out = note_writer._apply_add_tags(fm, ["claude/insight", "claude/topic/new"])
    assert out == [
        "tags:\n", "  - claude/insight\n", "  - claude/topic/new\n", "project: y\n",
    ]

    # No tags: block -- no-op, no crash
    fm_no_tags = ["type: x\n", "project: y\n"]
    assert note_writer._apply_add_tags(fm_no_tags, ["claude/topic/new"]) == fm_no_tags

    # Empty add_tags -- no-op
    assert note_writer._apply_add_tags(fm, []) == fm


def test_is_trailing_marker_in_process():
    assert note_writer._is_trailing_marker("## Tool Usage\n") is True
    assert note_writer._is_trailing_marker("## Conversation (raw)\n") is True
    assert note_writer._is_trailing_marker("## Session Metadata\n") is True
    assert note_writer._is_trailing_marker("## Files Touched\n") is True
    assert note_writer._is_trailing_marker("_(Summary source: haiku)_\n") is True
    assert note_writer._is_trailing_marker("## Something Else\n") is False


def test_resolve_note_path_in_process(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)

    resolved, err = note_writer._resolve_note_path(str(vault), str(note))
    assert err is None
    assert resolved == note.resolve()

    _, err_outside = note_writer._resolve_note_path(str(vault), str(tmp_path / "outside.md"))
    assert err_outside is not None

    dotted = _make_note(vault, ".obsidian/plugins/evil", "main.md", BASIC_NOTE)
    _, err_dotted = note_writer._resolve_note_path(str(vault), str(dotted))
    assert err_dotted is not None

    _, err_missing = note_writer._resolve_note_path(
        str(vault), str(vault / "claude-insights" / "missing.md")
    )
    assert err_missing is not None


def test_parse_append_update_flags_in_process():
    assert note_writer._parse_append_update_flags([]) == (None, None, None)
    assert note_writer._parse_append_update_flags(
        ["--last-updated", "2026-07-25", "--add-tags", "a,b"]
    ) == ("2026-07-25", "a,b", None)
    lu, tags, err = note_writer._parse_append_update_flags(["--last-updated"])
    assert lu is None and tags is None and "requires a value" in err
    lu2, tags2, err2 = note_writer._parse_append_update_flags(["--add-tags"])
    assert lu2 is None and tags2 is None and "requires a value" in err2
    lu3, tags3, err3 = note_writer._parse_append_update_flags(["--nope", "x"])
    assert "unknown flag" in err3


# ---------------------------------------------------------------------------
# In-process tests -- run_append_update()/main() dispatch
# ---------------------------------------------------------------------------

def test_run_append_update_in_process_success(tmp_path, capsys):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)

    rc = note_writer.run_append_update(str(vault), str(note), UPDATE_SECTION)

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == f"OK: {note.resolve()}"
    assert "## Update (2026-07-25)" in note.read_text(encoding="utf-8")


def test_run_append_update_forced_write_error_path_file_unchanged(
    tmp_path, monkeypatch, capsys
):
    """Forces _atomic_rewrite's error branch directly, mirroring
    test_run_write_forced_error_path_prints_error_and_returns_1 for `write`.
    Proves the ERROR:/exit-1 path is reachable independent of *why* the
    write failed, and that the note is left byte-identical when it is."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    before = note.read_bytes()

    monkeypatch.setattr(
        note_writer, "_atomic_rewrite", lambda *a, **k: "forced failure for test"
    )

    rc = note_writer.run_append_update(str(vault), str(note), UPDATE_SECTION)

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "ERROR: forced failure for test"
    assert captured.out == ""
    assert note.read_bytes() == before


def test_main_append_update_dispatch_success_in_process(tmp_path, monkeypatch, capsys):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)

    monkeypatch.setattr(
        sys, "argv",
        ["note_writer.py", "append-update", str(vault), str(note), "--last-updated", "2026-07-25"],
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(UPDATE_SECTION))

    with pytest.raises(SystemExit) as exc_info:
        note_writer.main()

    assert exc_info.value.code == 0
    written = note.read_text(encoding="utf-8")
    assert "## Update (2026-07-25)" in written
    assert "last_updated: 2026-07-25" in written


def test_main_append_update_flag_error_exits_2_in_process(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["note_writer.py", "append-update", "vault", "note.md", "--bogus"]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    with pytest.raises(SystemExit) as exc_info:
        note_writer.main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "unknown flag" in captured.err.lower()


# ---------------------------------------------------------------------------
# In-process counterparts for run_append_update()'s error branches.
#
# The subprocess-level tests above (path-outside-vault, dotted-segment,
# missing note, directory-as-note, no-frontmatter, no-closing-fence,
# missing-date-field) prove these work end-to-end through the real CLI, but
# each subprocess spawns a brand-new Python process invisible to
# coverage.py in the pytest run. These in-process counterparts hit the same
# branches directly so they're instrumented, per this repo's two-layer
# convention (see test_run_write_forced_error_path_prints_error_and_returns_1
# for the `write` command's equivalent).
# ---------------------------------------------------------------------------

def test_run_append_update_in_process_rejects_path_outside_vault(tmp_path, capsys):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text(BASIC_NOTE, encoding="utf-8")
    before = outside.read_bytes()

    rc = note_writer.run_append_update(str(vault), str(outside), UPDATE_SECTION)

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.strip().startswith("ERROR: path traversal blocked")
    assert outside.read_bytes() == before


def test_run_append_update_in_process_rejects_directory_as_note_path(tmp_path, capsys):
    """Also exercises _resolve_note_path's is_file() branch (line coverage
    for the 'not a regular file' rejection) in-process."""
    vault = tmp_path / "vault"
    not_a_file = vault / "claude-insights" / "looks-like-a-note.md"
    not_a_file.mkdir(parents=True)

    rc = note_writer.run_append_update(str(vault), str(not_a_file), UPDATE_SECTION)

    assert rc == 1
    captured = capsys.readouterr()
    assert "not a regular file" in captured.err
    assert not_a_file.is_dir()


def test_run_append_update_in_process_unreadable_file_reports_cannot_read(
    tmp_path, capsys
):
    """Forces the `except OSError` branch around the read (distinct from
    _resolve_note_path's existence/is_file checks, which already passed --
    this file genuinely exists and is a regular file, but is unreadable)."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    os.chmod(note, 0o000)
    try:
        rc = note_writer.run_append_update(str(vault), str(note), UPDATE_SECTION)
    finally:
        os.chmod(note, 0o600)  # restore so tmp_path cleanup can remove it

    if rc == 0:
        pytest.skip("read succeeded despite chmod 0o000 (likely running as root)")
    captured = capsys.readouterr()
    assert captured.err.strip().startswith("ERROR: cannot read")


def test_run_append_update_in_process_malformed_frontmatter_variants(tmp_path, capsys):
    vault = tmp_path / "vault"

    no_fm = _make_note(
        vault, "claude-insights", "no-fm.md", "# No frontmatter\nBody.\n"
    )
    rc1 = note_writer.run_append_update(str(vault), str(no_fm), UPDATE_SECTION)
    assert rc1 == 1
    assert "malformed or missing frontmatter" in capsys.readouterr().err

    no_close = _make_note(
        vault, "claude-insights", "no-close.md",
        "---\ntype: x\ndate: 2026-01-01\n\n# No closing fence\n",
    )
    rc2 = note_writer.run_append_update(str(vault), str(no_close), UPDATE_SECTION)
    assert rc2 == 1
    assert "malformed or missing frontmatter" in capsys.readouterr().err


def test_run_append_update_in_process_missing_date_field_reports_fm_err(tmp_path, capsys):
    vault = tmp_path / "vault"
    note = _make_note(
        vault, "claude-insights", "insight.md",
        "---\ntype: x\nproject: y\n---\n\n# No date field\nBody.\n",
    )

    rc = note_writer.run_append_update(
        str(vault), str(note), UPDATE_SECTION, last_updated="2026-07-25"
    )

    assert rc == 1
    assert "date" in capsys.readouterr().err


def test_run_append_update_in_process_inserts_before_marker_hitting_loop_break(
    tmp_path, capsys
):
    """Exercises the insertion_idx scan loop's break branch directly (the
    subprocess-level marker-variant tests above cover the same logic but
    aren't visible to coverage.py)."""
    vault = tmp_path / "vault"
    note = _make_note(
        vault, "claude-insights", "insight.md",
        BASIC_NOTE + "\n## Tool Usage\n- x\n",
    )

    rc = note_writer.run_append_update(str(vault), str(note), UPDATE_SECTION)

    assert rc == 0
    written = note.read_text(encoding="utf-8")
    assert written.index("## Update (2026-07-25)") < written.index("## Tool Usage")


def test_atomic_rewrite_forces_inner_and_outer_exception_branches(
    tmp_path, monkeypatch, capsys
):
    """Forces os.rename to fail AFTER the temp file has already been written
    and chmod'd, driving _atomic_rewrite through its inner except (unlink
    the temp file, re-raise) and outer except (format the error string) in
    one call -- the forced-error test above only fakes _atomic_rewrite's
    return value wholesale and never actually runs its body."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    before = note.read_bytes()

    def _boom_rename(*a, **k):
        raise OSError("forced rename failure for test")

    monkeypatch.setattr(note_writer.os, "rename", _boom_rename)

    err = note_writer._atomic_rewrite(note, "new content\n")

    assert err is not None
    assert "forced rename failure" in err
    assert note.read_bytes() == before
    # the temp file was cleaned up, not left behind
    leftover = list(note.parent.glob(".ob-*.md.tmp"))
    assert leftover == []


# ===========================================================================
# Fix round 1 (post-review): CRLF preservation, flow-style tags, EOF
# blank-line separator when the source file has no trailing newline at all.
#
# Each bug was reproduced fail-first against the pre-fix code (git show
# HEAD:hooks/note_writer.py, loaded as an isolated scratch module) before
# being fixed here -- see the fix report for the exact repro output. These
# tests encode the same scenarios directly against the shipped module.
# ===========================================================================

CRLF_NOTE = (
    "---\r\n"
    "type: claude-insight\r\n"
    "date: 2026-01-01\r\n"
    "source_session: abc123\r\n"
    "project: obsidian-brain\r\n"
    "tags:\r\n"
    "  - claude/insight\r\n"
    "---\r\n"
    "\r\n"
    "# Some Insight\r\n"
    "\r\n"
    "Body content here.\r\n"
    "More body.\r\n"
)


def _make_note_bytes(vault: Path, folder: str, filename: str, raw_bytes: bytes) -> Path:
    d = vault / folder
    d.mkdir(parents=True, exist_ok=True)
    p = d / filename
    p.write_bytes(raw_bytes)
    return p


# ---------------------------------------------------------------------------
# Important 1: CRLF notes must not be silently rewritten to LF.
# ---------------------------------------------------------------------------

def test_append_update_preserves_crlf_line_endings_everywhere(tmp_path):
    """The inserted section must ITSELF use CRLF (not just leave the rest of
    the file alone), and every byte outside the inserted region must be
    unchanged -- compared as actual bytes, not lines."""
    vault = tmp_path / "vault"
    note = _make_note_bytes(vault, "claude-insights", "insight.md", CRLF_NOTE.encode("utf-8"))
    before = note.read_bytes()

    result = _run_append_update(vault, note, UPDATE_SECTION, last_updated="2026-07-25")

    assert result.returncode == 0, result.stderr
    after = note.read_bytes()

    # No bare LF anywhere -- every line ending in the whole file is CRLF.
    assert b"\r\n" in after
    stripped_of_crlf = after.replace(b"\r\n", b"")
    assert b"\n" not in stripped_of_crlf, "found a bare LF outside of CRLF pairs"

    # The inserted section itself uses CRLF.
    assert b"## Update (2026-07-25)\r\n\r\nNew findings from today's session.\r\n" in after

    # Content outside the inserted region is byte-identical to `before`,
    # not merely line-equal: every original line (as bytes, with its
    # original \r\n) is present verbatim in `after`.
    for original_line in before.splitlines(keepends=True):
        assert original_line in after, f"original line altered/missing: {original_line!r}"

    # last_updated was inserted using CRLF too, immediately after date:
    assert b"date: 2026-01-01\r\nlast_updated: 2026-07-25\r\n" in after


def test_append_update_lf_note_unaffected_no_stray_cr(tmp_path):
    """Mirror case: an LF-only note must not gain any CRLF -- a future
    change to the eol-detection default can't silently flip which ending is
    used for a plain-LF note."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)

    result = _run_append_update(vault, note, UPDATE_SECTION, last_updated="2026-07-25")

    assert result.returncode == 0, result.stderr
    after = note.read_bytes()
    assert b"\r" not in after


def test_detect_line_ending_in_process():
    assert note_writer._detect_line_ending("a\r\nb\r\nc\r\n") == "\r\n"
    assert note_writer._detect_line_ending("a\nb\nc\n") == "\n"
    assert note_writer._detect_line_ending("no newlines at all") == "\n"
    # Mixed, CRLF-majority -> CRLF; mixed, LF-majority -> LF.
    assert note_writer._detect_line_ending("a\r\nb\r\nc\n") == "\r\n"
    assert note_writer._detect_line_ending("a\r\nb\nc\n") == "\n"


def test_normalize_eol_in_process():
    # Genuine \r\n and \n terminators are collapsed/expanded correctly.
    assert note_writer._normalize_eol("a\nb\r\nc\n", "\r\n") == "a\r\nb\r\nc\r\n"
    assert note_writer._normalize_eol("a\r\nb\r\n", "\n") == "a\nb\n"
    assert note_writer._normalize_eol("a\nb\n", "\n") == "a\nb\n"
    # A trailing bare \r (not part of a \r\n pair) is left untouched by
    # both replacements -- see test_normalize_eol_does_not_touch_bare_cr_in_process
    # for the dedicated bare-\r regression coverage.
    assert note_writer._normalize_eol("a\nb\r\nc\r", "\r\n") == "a\r\nb\r\nc\r"


def test_atomic_rewrite_writes_content_bytes_verbatim_no_translation(tmp_path):
    """_atomic_rewrite's own newline="" write path: content containing
    literal CRLF sequences must land on disk exactly as given, byte for
    byte -- not translated in either direction."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    content = "line one\r\nline two\r\nline three\r\n"

    err = note_writer._atomic_rewrite(note, content)

    assert err is None
    assert note.read_bytes() == content.encode("utf-8")


# ---------------------------------------------------------------------------
# Important 2: flow-style `tags: [a, b]` must not silently no-op.
# ---------------------------------------------------------------------------

FLOW_TAGS_NOTE = BASIC_NOTE.replace(
    "tags:\n  - claude/insight\n  - claude/topic/foo\n",
    "tags: [claude/insight, claude/topic/foo]\n",
)

EMPTY_FLOW_TAGS_NOTE = BASIC_NOTE.replace(
    "tags:\n  - claude/insight\n  - claude/topic/foo\n",
    "tags: []\n",
)


def test_append_update_flow_style_tags_gains_new_tag(tmp_path):
    vault = tmp_path / "vault"
    assert "tags: [" in FLOW_TAGS_NOTE
    note = _make_note(vault, "claude-insights", "insight.md", FLOW_TAGS_NOTE)

    result = _run_append_update(vault, note, UPDATE_SECTION, add_tags="claude/topic/new-one")

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    assert "tags: [claude/insight, claude/topic/foo, claude/topic/new-one]" in written


def test_append_update_flow_style_duplicate_tag_not_readded(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", FLOW_TAGS_NOTE)

    result = _run_append_update(
        vault, note, UPDATE_SECTION, add_tags="claude/insight,claude/topic/new-one"
    )

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    assert written.count("claude/insight") == 1  # not re-added
    assert "claude/topic/new-one" in written


def test_append_update_flow_style_empty_list(tmp_path):
    vault = tmp_path / "vault"
    assert "tags: []" in EMPTY_FLOW_TAGS_NOTE
    note = _make_note(vault, "claude-insights", "insight.md", EMPTY_FLOW_TAGS_NOTE)

    result = _run_append_update(vault, note, UPDATE_SECTION, add_tags="claude/topic/new-one")

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    assert "tags: [claude/topic/new-one]" in written


def test_append_update_flow_style_all_requested_tags_already_present_is_noop(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", FLOW_TAGS_NOTE)
    before = note.read_text(encoding="utf-8")

    result = _run_append_update(vault, note, UPDATE_SECTION, add_tags="claude/insight")

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    before_tags_line = [l for l in before.splitlines() if l.startswith("tags:")][0]
    after_tags_line = [l for l in written.splitlines() if l.startswith("tags:")][0]
    assert before_tags_line == after_tags_line


def test_append_update_block_style_tags_unaffected_by_flow_support(tmp_path):
    """Regression guard: adding flow-style support must not change block-
    style behavior at all."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)

    result = _run_append_update(vault, note, UPDATE_SECTION, add_tags="claude/topic/new-one")

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    assert "  - claude/topic/new-one\n" in written
    assert "tags: [" not in written  # still block style, not converted


def test_apply_add_tags_flow_style_in_process():
    fm = ["project: y\n", "tags: [a, b]\n"]
    out = note_writer._apply_add_tags(fm, ["b", "c"])
    assert out == ["project: y\n", "tags: [a, b, c]\n"]

    fm_empty = ["tags: []\n"]
    out_empty = note_writer._apply_add_tags(fm_empty, ["a", "b"])
    assert out_empty == ["tags: [a, b]\n"]

    # Every requested tag already present -- byte-identical no-op.
    fm_dup = ["tags: [a, b]\n"]
    assert note_writer._apply_add_tags(fm_dup, ["a"]) == fm_dup

    # Quoted existing items -- new item rendered with the same quote char.
    fm_quoted = ['tags: ["a", "b"]\n']
    out_quoted = note_writer._apply_add_tags(fm_quoted, ["c"])
    assert out_quoted == ['tags: ["a", "b", "c"]\n']


def test_append_update_flow_style_quoted_tags_new_tag_matches_quote_style(tmp_path):
    vault = tmp_path / "vault"
    note_content = BASIC_NOTE.replace(
        "tags:\n  - claude/insight\n  - claude/topic/foo\n",
        'tags: ["claude/insight", "claude/topic/foo"]\n',
    )
    note = _make_note(vault, "claude-insights", "insight.md", note_content)

    result = _run_append_update(vault, note, UPDATE_SECTION, add_tags="claude/topic/new-one")

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    assert '"claude/topic/new-one"' in written


def test_split_flow_items_and_unquote_tag_in_process():
    assert note_writer._split_flow_items("") == []
    assert note_writer._split_flow_items("  ") == []
    assert note_writer._split_flow_items("a, b, c") == ["a", " b", " c"]

    assert note_writer._unquote_tag('"a"') == "a"
    assert note_writer._unquote_tag("'a'") == "a"
    assert note_writer._unquote_tag(" a ") == "a"
    assert note_writer._unquote_tag("a") == "a"


# ---------------------------------------------------------------------------
# Minor 3: EOF insertion when the source file has no trailing newline.
# ---------------------------------------------------------------------------

NO_TRAILING_NEWLINE_NOTE = BASIC_NOTE.rstrip("\n")


def test_append_update_eof_no_trailing_newline_gets_blank_line_separator(tmp_path):
    vault = tmp_path / "vault"
    assert not NO_TRAILING_NEWLINE_NOTE.endswith("\n")
    note = _make_note(vault, "claude-insights", "insight.md", NO_TRAILING_NEWLINE_NOTE)

    result = _run_append_update(vault, note, UPDATE_SECTION)

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    # A genuine blank line separates the last original body line from the
    # inserted heading -- not run straight onto it.
    assert "Body content here.\n\n## Update (2026-07-25)" in written
    assert "Body content here.\n## Update" not in written


def test_run_append_update_in_process_eof_no_trailing_newline(tmp_path, capsys):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", NO_TRAILING_NEWLINE_NOTE)

    rc = note_writer.run_append_update(str(vault), str(note), UPDATE_SECTION)

    assert rc == 0
    written = note.read_text(encoding="utf-8")
    assert "Body content here.\n\n## Update (2026-07-25)" in written


def test_append_update_stdin_update_text_without_trailing_newline_gets_one(tmp_path):
    """`update_text` itself (the stdin payload) may not end with a newline
    -- the CLI must still terminate it properly rather than running the
    trailing marker straight onto the update section's last line.

    The fixture MUST have a trailing marker. With an EOF append, the `eol`
    element in `prefix + [block_text, eol] + ...` supplies the newline on its
    own, so an "ends with \\n" assertion is satisfied whether or not the
    `block_text += eol` guard exists (verified: deleting the guard left the
    old version of this test green). Only a following marker line makes the
    difference observable -- separation, not mere termination."""
    vault = tmp_path / "vault"
    note = _make_note(
        vault, "claude-insights", "insight.md", BASIC_NOTE + "\n## Tool Usage\n- x\n"
    )
    no_trailing_nl_update = "## Update (2026-07-25)\n\nNo trailing newline here."
    assert not no_trailing_nl_update.endswith("\n")

    result = _run_append_update(vault, note, no_trailing_nl_update)

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    assert "No trailing newline here.\n\n## Tool Usage" in written
    assert "No trailing newline here.\n## Tool Usage" not in written


def test_run_append_update_in_process_update_text_without_trailing_newline(tmp_path):
    """In-process counterpart of the subprocess test above -- a subprocess
    call is invisible to coverage.py, so this exercises the
    `if not block_text.endswith(eol): block_text += eol` branch directly.

    Same fixture requirement as its subprocess twin: a trailing marker must
    follow the insertion point, otherwise the assertion is satisfied by the
    separator element rather than by the guard (the docstring here previously
    claimed to exercise the branch while asserting something that could not
    distinguish it)."""
    vault = tmp_path / "vault"
    note = _make_note(
        vault, "claude-insights", "insight.md", BASIC_NOTE + "\n## Tool Usage\n- x\n"
    )
    no_trailing_nl_update = "## Update (2026-07-25)\n\nNo trailing newline here."

    rc = note_writer.run_append_update(str(vault), str(note), no_trailing_nl_update)

    assert rc == 0
    written = note.read_text(encoding="utf-8")
    assert "No trailing newline here.\n\n## Tool Usage" in written


# ===========================================================================
# Fix round 2 (post re-review): a bare `\r` (not part of `\r\n`) inside the
# appended update-section text -- e.g. a pasted terminal progress-bar
# redraw in a fenced code block -- must survive byte-intact, not get
# silently converted into a real line break. Regression introduced by the
# EOL work in fix round 1 (`_normalize_eol`'s blanket `.replace("\r", "\n")`)
# and by using `str.splitlines()` on the note's own text (which treats a
# bare `\r` as a line break too). Reproduced fail-first against the code at
# HEAD (commit 5920fe7) before being fixed -- see the fix report.
# ===========================================================================

UPDATE_WITH_BARE_CR = (
    "## Update (2026-07-25)\n"
    "\n"
    "```\n"
    "Downloading... 10%\rDownloading... 50%\rDownloading... 100%\n"
    "```\n"
)


def test_append_update_bare_cr_in_appended_content_survives_lf_note(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)

    result = _run_append_update(vault, note, UPDATE_WITH_BARE_CR)

    assert result.returncode == 0, result.stderr
    after = note.read_bytes()
    assert b"10%\rDownloading... 50%\rDownloading... 100%\n" in after


def test_append_update_bare_cr_in_appended_content_survives_crlf_note(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note_bytes(vault, "claude-insights", "insight.md", CRLF_NOTE.encode("utf-8"))

    result = _run_append_update(vault, note, UPDATE_WITH_BARE_CR)

    assert result.returncode == 0, result.stderr
    after = note.read_bytes()
    # The bare \r is untouched; the update text's own genuine "\n"
    # terminators were normalized to the note's CRLF convention, but the
    # literal "10%\rDownloading" substring (no real terminator there) must
    # appear verbatim, not turned into "10%\r\nDownloading" or "10%\nDownloading".
    assert b"10%\rDownloading... 50%\rDownloading... 100%\r\n" in after
    assert b"10%\r\nDownloading" not in after
    assert b"10%\nDownloading" not in after


def test_append_update_crlf_terminated_update_text_matches_lf_note_ending(tmp_path):
    """The update text's own GENUINE `\\r\\n` terminators (as opposed to a
    bare `\\r`) must still be normalized to the destination note's actual
    line ending -- this is existing, correct behavior from fix round 1 and
    must not regress."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    crlf_terminated_update = "## Update (2026-07-25)\r\n\r\nSome content.\r\n"

    result = _run_append_update(vault, note, crlf_terminated_update)

    assert result.returncode == 0, result.stderr
    after = note.read_bytes()
    assert b"\r" not in after  # note is LF-only; no CRLF should leak in
    assert b"## Update (2026-07-25)\n\nSome content.\n" in after


def test_append_update_crlf_terminated_update_text_matches_crlf_note_ending(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note_bytes(vault, "claude-insights", "insight.md", CRLF_NOTE.encode("utf-8"))
    crlf_terminated_update = "## Update (2026-07-25)\r\n\r\nSome content.\r\n"

    result = _run_append_update(vault, note, crlf_terminated_update)

    assert result.returncode == 0, result.stderr
    after = note.read_bytes()
    assert b"## Update (2026-07-25)\r\n\r\nSome content.\r\n" in after


def test_normalize_eol_does_not_touch_bare_cr_in_process():
    # Bare \r survives both directions -- only real terminators are touched.
    assert note_writer._normalize_eol("a\rb\n", "\n") == "a\rb\n"
    assert note_writer._normalize_eol("a\rb\n", "\r\n") == "a\rb\r\n"
    # Genuine \r\n terminators are still collapsed/expanded correctly.
    assert note_writer._normalize_eol("a\r\nb\r\n", "\n") == "a\nb\n"
    assert note_writer._normalize_eol("a\nb\n", "\r\n") == "a\r\nb\r\n"
    # Mixed: a bare \r right next to a genuine \r\n later in the string.
    assert note_writer._normalize_eol("a\rb\r\nc", "\n") == "a\rb\nc"


def test_split_lines_lf_crlf_in_process():
    # Bare \r is NOT a line boundary -- stays attached to whatever line it's in.
    lines = note_writer._split_lines_lf_crlf("foo\rbar\n")
    assert lines == ["foo\rbar\n"]

    # \r\n and \n are both recognized as terminators, each on its own line.
    lines2 = note_writer._split_lines_lf_crlf("a\r\nb\nc")
    assert lines2 == ["a\r\n", "b\n", "c"]

    # Lossless reconstruction via straight join, regardless of test case.
    for text in ("foo\rbar\n", "a\r\nb\nc", "", "no newline at all", "\r\n\r\n"):
        assert "".join(note_writer._split_lines_lf_crlf(text)) == text


# ===========================================================================
# Content-layer guards (deep-review round 1)
#
# The argument layer was already well validated; nothing validated the
# CONTENT piped in on stdin or the values interpolated into YAML. Every test
# below pins one of those guards. Each was proven fail-first by removing the
# guard and re-running (see dr-fix1-report.md for the captured rc/stderr).
# ===========================================================================

# --- write: empty / malformed note content --------------------------------

def test_write_rejects_empty_stdin_no_zero_byte_note(tmp_path):
    """A 0-byte note reported as `OK:` is the worst outcome this CLI can
    produce: /retro then arms its Stop-hook classification gate pointing at
    an empty file, and the user is told the retro was saved."""
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)

    result = _run_write(vault, "claude-insights", "empty.md", "")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert "OK:" not in result.stdout
    assert not (vault / "claude-insights" / "empty.md").exists()


def test_write_rejects_whitespace_only_stdin(tmp_path):
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)

    result = _run_write(vault, "claude-insights", "blank.md", "   \n\n\t\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert not (vault / "claude-insights" / "blank.md").exists()


def test_write_rejects_content_without_opening_frontmatter_fence(tmp_path):
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)

    result = _run_write(
        vault, "claude-insights", "nofm.md", "# Just a title\n\nBody.\n"
    )

    assert result.returncode == 1
    assert "frontmatter" in result.stderr.lower()
    assert not (vault / "claude-insights" / "nofm.md").exists()


def test_write_rejects_indented_frontmatter_fence(tmp_path):
    """The indented-heredoc corruption class, closed permanently: an
    indented `   ---` is not a frontmatter fence, so a wrongly-indented
    heredoc body now fails loudly instead of landing a note whose
    frontmatter Obsidian cannot parse."""
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)
    indented = "   ---\n   type: claude-insight\n   ---\n\n   # Title\n"

    result = _run_write(vault, "claude-insights", "indented.md", indented)

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert not (vault / "claude-insights" / "indented.md").exists()


def test_write_rejects_content_without_closing_frontmatter_fence(tmp_path):
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)
    unterminated = "---\ntype: claude-insight\ndate: 2026-07-25\n\n# Title\nBody.\n"

    result = _run_write(vault, "claude-insights", "unterminated.md", unterminated)

    assert result.returncode == 1
    assert "closing" in result.stderr.lower()
    assert not (vault / "claude-insights" / "unterminated.md").exists()


def test_validate_note_content_in_process():
    assert note_writer._validate_note_content("") is not None
    assert note_writer._validate_note_content("  \n\t\n") is not None
    assert note_writer._validate_note_content("# no fm\n") is not None
    assert note_writer._validate_note_content("---\ntype: x\n") is not None
    assert note_writer._validate_note_content("---\ntype: x\n---\nbody\n") is None
    # CRLF-authored note: the fence check must not be defeated by \r\n.
    assert note_writer._validate_note_content("---\r\ntype: x\r\n---\r\nbody\r\n") is None


# --- write: overwrite guard -----------------------------------------------

def test_write_refuses_to_clobber_existing_note(tmp_path):
    """Claude Code's Write tool refused to overwrite a file it had not Read,
    so a filename-hash collision used to be loud. Without this guard the
    CLI conversion silently destroyed the existing insight and printed
    `OK:`."""
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)
    dest = vault / "claude-insights" / "collide.md"
    original = MINIMAL_FM + "ORIGINAL IMPORTANT CONTENT\n"
    dest.write_text(original, encoding="utf-8")

    result = _run_write(vault, "claude-insights", "collide.md", MINIMAL_FM + "new\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert "overwrite" in result.stderr.lower()
    assert dest.read_text(encoding="utf-8") == original


def test_write_overwrite_flag_replaces_existing_note(tmp_path):
    """/standup Step 6.6 upgrades a session note in place and is the ONE
    call site that passes --overwrite."""
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    dest = vault / "claude-sessions" / "session.md"
    dest.write_text(MINIMAL_FM + "old\n", encoding="utf-8")
    new_content = MINIMAL_FM + "upgraded with AI summary\n"

    result = _run_argv(
        "write", str(vault), "claude-sessions", "session.md", "--overwrite",
        stdin=new_content,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"OK: {dest.resolve()}"
    assert dest.read_text(encoding="utf-8") == new_content


def test_write_unknown_flag_exits_2(tmp_path):
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)

    result = _run_argv(
        "write", str(vault), "claude-sessions", "x.md", "--clobber",
        stdin=MINIMAL_FM + "body\n",
    )

    assert result.returncode == 2
    assert "unknown flag" in result.stderr.lower()


# --- write: hidden-note filenames -----------------------------------------

@pytest.mark.parametrize("filename", ["..md", ".secret.md", ".md"])
def test_write_rejects_leading_dot_filenames(tmp_path, filename):
    """`..md` and `.secret.md` passed the old length-based check and were
    written -- a note invisible to Obsidian, reported as saved."""
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)

    result = _run_write(vault, "claude-insights", filename, MINIMAL_FM + "body\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert not (vault / "claude-insights" / filename).exists()


def test_validate_filename_rejects_hidden_names_in_process():
    assert note_writer._validate_filename("..md") is not None
    assert note_writer._validate_filename(".secret.md") is not None
    assert note_writer._validate_filename(".md") is not None
    assert note_writer._validate_filename("a.md") is None


# --- append-update: empty content -----------------------------------------

def test_append_update_rejects_empty_stdin_note_unchanged(tmp_path):
    """Empty content used to return `OK:`, bump last_updated and append only
    blank lines -- the note LOOKED freshly updated but gained nothing, and
    /compress deliberately skips a verification re-read on the strength of
    this exit code."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    before = note.read_bytes()

    result = _run_append_update(
        vault, note, "", last_updated="2026-07-25", add_tags="claude/topic/x"
    )

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert "OK:" not in result.stdout
    assert note.read_bytes() == before  # no last_updated bump, no tag, no body


def test_append_update_rejects_whitespace_only_stdin_note_unchanged(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    before = note.read_bytes()

    result = _run_append_update(vault, note, "\n   \n\t\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert note.read_bytes() == before


# --- append-update: tag validation ----------------------------------------

def test_append_update_rejects_tag_with_newline_frontmatter_injection(tmp_path):
    """The reported reproduction: a newline inside one CSV item injected an
    arbitrary frontmatter key that overrode the note's own `type`."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    before = note.read_bytes()

    result = _run_append_update(
        vault, note, UPDATE_SECTION, add_tags="claude/topic/a\ntype: hijacked"
    )

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    written = note.read_text(encoding="utf-8")
    assert "type: hijacked" not in written
    assert note.read_bytes() == before


@pytest.mark.parametrize(
    "bad_tag",
    [
        "foo: bar",            # turns the tags sequence into a sequence of maps
        "claude/topic/a#x",    # starts a YAML comment
        'claude/"quoted"',     # unbalances a flow-style tags: [...] line
        "claude/'quoted'",
        "claude/topic/a\r",    # bare CR
        "claude/topic/a b",    # internal whitespace
    ],
)
def test_append_update_rejects_malformed_tags(tmp_path, bad_tag):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    before = note.read_bytes()

    result = _run_append_update(vault, note, UPDATE_SECTION, add_tags=bad_tag)

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert note.read_bytes() == before


def test_append_update_rejects_malformed_tag_on_flow_style_note(tmp_path):
    """Validation must run BEFORE either merge path -- flow-style notes are
    just as injectable as block-style ones."""
    vault = tmp_path / "vault"
    flow_note = BASIC_NOTE.replace(
        "tags:\n  - claude/insight\n  - claude/topic/foo\n",
        "tags: [claude/insight, claude/topic/foo]\n",
    )
    assert "tags: [" in flow_note
    note = _make_note(vault, "claude-insights", "insight.md", flow_note)
    before = note.read_bytes()

    result = _run_append_update(
        vault, note, UPDATE_SECTION, add_tags="claude/topic/a\ntype: hijacked"
    )

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert note.read_bytes() == before


def test_append_update_accepts_conventional_csv_spacing(tmp_path):
    """`a, b` (a space after the comma) is conventional CSV spacing, not a
    malformed tag -- the guard must not reject the ordinary case."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)

    result = _run_append_update(
        vault, note, UPDATE_SECTION, add_tags="claude/topic/one, claude/topic/two"
    )

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    assert "  - claude/topic/one\n" in written
    assert "  - claude/topic/two\n" in written


def test_validate_tags_in_process():
    assert note_writer._validate_tags(None) == ([], None)

    tags, err = note_writer._validate_tags("a/b, c/d")
    assert err is None and tags == ["a/b", "c/d"]

    for bad in ("", "a,,b", "a,", "a\nb", "a: b", "a#b", "a'b", 'a"b', "a b"):
        tags, err = note_writer._validate_tags(bad)
        assert err is not None, bad
        assert tags is None, bad


# --- append-update: --last-updated validation -----------------------------

def test_append_update_rejects_empty_last_updated_value(tmp_path):
    """`--last-updated "$TODAY"` with TODAY unset yields a PRESENT flag with
    an empty value -- previously indistinguishable from "flag omitted", so
    the bump was skipped with no signal."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    before = note.read_bytes()

    result = _run_append_update(vault, note, UPDATE_SECTION, last_updated="")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert note.read_bytes() == before


@pytest.mark.parametrize("bad_date", ["yesterday", "2026-7-25", "2026-07-25\ntype: x"])
def test_append_update_rejects_malformed_last_updated_value(tmp_path, bad_date):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    before = note.read_bytes()

    result = _run_append_update(vault, note, UPDATE_SECTION, last_updated=bad_date)

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert note.read_bytes() == before


def test_validate_last_updated_in_process():
    assert note_writer._validate_last_updated(None) is None
    assert note_writer._validate_last_updated("2026-07-25") is None
    assert note_writer._validate_last_updated("") is not None
    assert note_writer._validate_last_updated("   ") is not None
    assert note_writer._validate_last_updated("2026-7-5") is not None
    assert note_writer._validate_last_updated("2026-07-25 extra") is not None


# --- append-update: closing-fence weld ------------------------------------

def test_append_update_frontmatter_only_note_no_trailing_newline(tmp_path):
    """With an empty body AND no trailing newline the separator branch never
    ran, so the closing fence was welded onto the update heading
    (`---## Update (...)`), leaving the frontmatter unterminated -- Obsidian
    loses the note's type and tags -- at exit 0."""
    vault = tmp_path / "vault"
    content = "---\ntype: claude-insight\ndate: 2026-01-01\n---"  # no trailing \n
    note = _make_note(vault, "claude-insights", "fm-only.md", content)

    result = _run_append_update(vault, note, UPDATE_SECTION)

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    assert "---## Update" not in written
    # Frontmatter still terminated by a fence on its own line.
    assert written.startswith("---\ntype: claude-insight\ndate: 2026-01-01\n---\n")
    assert "## Update (2026-07-25)" in written
    # And it still parses as frontmatter for the CLI's own splitter.
    lines = note_writer._split_lines_lf_crlf(written)
    _open, fm, _close, _body = note_writer._split_frontmatter(lines)
    assert fm is not None
    assert "type: claude-insight\n" in fm


def test_append_update_frontmatter_only_note_crlf_no_trailing_newline(tmp_path):
    """Same case on a CRLF note -- the terminator this adds must be the
    note's own line ending, not a hardcoded \\n."""
    vault = tmp_path / "vault"
    content = "---\r\ntype: claude-insight\r\ndate: 2026-01-01\r\n---"
    note = _make_note(vault, "claude-insights", "fm-only-crlf.md", content)

    result = _run_append_update(vault, note, UPDATE_SECTION)

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8", newline="")
    assert written.startswith(
        "---\r\ntype: claude-insight\r\ndate: 2026-01-01\r\n---\r\n"
    )
    assert "\n---## Update" not in written


# --- append-update: fenced-code-block-aware insertion scan ----------------

def test_append_update_ignores_trailing_marker_inside_code_fence(tmp_path):
    """A note quoting a session-note template contains `## Tool Usage`
    inside a fence. Without fence tracking the update section was wedged
    INSIDE that fence -- rendered as literal code, with the real body pushed
    out past it, at exit 0."""
    vault = tmp_path / "vault"
    body = (
        "\n# Insight\n\n"
        "```markdown\n"
        "## Tool Usage\n"
        "- **Bash**: 12\n"
        "```\n"
        "\nReal body continues here.\n"
    )
    note = _make_note(vault, "claude-insights", "quoted.md", BASIC_NOTE + body)

    result = _run_append_update(vault, note, UPDATE_SECTION)

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    # The fenced block is untouched and intact...
    assert "```markdown\n## Tool Usage\n- **Bash**: 12\n```\n" in written
    # ...and the update landed after it (EOF), not inside it.
    assert written.index("## Update (2026-07-25)") > written.index("```markdown")
    assert written.index("## Update (2026-07-25)") > written.index("Real body continues here.")


def test_append_update_uses_real_marker_after_a_code_fence(tmp_path):
    """Fence tracking must not blind the scan to a genuine marker that
    appears AFTER a closed fence."""
    vault = tmp_path / "vault"
    body = (
        "\n# Insight\n\n"
        "```markdown\n"
        "## Tool Usage\n"
        "```\n"
        "\n## Session Metadata\n- id: abc\n"
    )
    note = _make_note(vault, "claude-insights", "both.md", BASIC_NOTE + body)

    result = _run_append_update(vault, note, UPDATE_SECTION)

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    assert written.index("## Update (2026-07-25)") < written.index("## Session Metadata")
    assert written.index("## Update (2026-07-25)") > written.index("```markdown")


def test_find_insertion_index_fence_variants_in_process():
    def idx(text):
        return note_writer._find_insertion_index(note_writer._split_lines_lf_crlf(text))

    # Marker inside a tilde fence is ignored; EOF is used instead.
    assert idx("~~~\n## Tool Usage\n~~~\n") == 3
    # An info-string line (```python) opens but does NOT close a fence.
    assert idx("```python\n## Tool Usage\n```\n## Files Touched\n") == 3
    # A longer closing fence is valid; a shorter one is not.
    assert idx("```\n## Tool Usage\n````\n## Files Touched\n") == 3
    assert idx("````\n## Tool Usage\n```\n## Files Touched\nx\n") == 5
    # Unclosed fence swallows the rest -- degrades to append-at-EOF.
    assert idx("```\n## Tool Usage\nx\n") == 3
    # No fence at all: first marker wins, top-down.
    assert idx("intro\n## Tool Usage\n## Files Touched\n") == 1


# ---------------------------------------------------------------------------
# In-process counterparts for the new guards, following this module's
# convention: the subprocess tests above prove the real CLI behaviour, these
# let coverage.py instrument the same branches (a subprocess run is not
# instrumented).
# ---------------------------------------------------------------------------

def test_validate_update_text_in_process():
    assert note_writer._validate_update_text("") is not None
    assert note_writer._validate_update_text("\n  \t\n") is not None
    assert note_writer._validate_update_text("## Update\n") is None


def test_run_write_in_process_refuses_existing_note(tmp_path, capsys):
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)
    dest = vault / "claude-insights" / "x.md"
    dest.write_text(MINIMAL_FM + "original\n", encoding="utf-8")

    rc = note_writer.run_write(
        str(vault), "claude-insights", "x.md", MINIMAL_FM + "new\n"
    )

    assert rc == 1
    assert "already exists" in capsys.readouterr().err
    assert dest.read_text(encoding="utf-8") == MINIMAL_FM + "original\n"

    rc2 = note_writer.run_write(
        str(vault), "claude-insights", "x.md", MINIMAL_FM + "new\n", overwrite=True
    )
    assert rc2 == 0
    assert dest.read_text(encoding="utf-8") == MINIMAL_FM + "new\n"


def test_run_append_update_in_process_content_and_flag_errors(tmp_path, capsys):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "n.md", BASIC_NOTE)
    before = note.read_bytes()

    # empty update text
    assert note_writer.run_append_update(str(vault), str(note), "") == 1
    assert "empty" in capsys.readouterr().err

    # malformed --last-updated
    assert note_writer.run_append_update(
        str(vault), str(note), UPDATE_SECTION, "not-a-date"
    ) == 1
    assert "last-updated" in capsys.readouterr().err

    # malformed tag
    assert note_writer.run_append_update(
        str(vault), str(note), UPDATE_SECTION, None, "bad: tag"
    ) == 1
    assert "invalid tag" in capsys.readouterr().err

    assert note.read_bytes() == before


def test_run_append_update_in_process_frontmatter_only_no_trailing_newline(tmp_path):
    """In-process counterpart of the closing-fence weld fix."""
    vault = tmp_path / "vault"
    note = _make_note(
        vault, "claude-insights", "fm.md", "---\ntype: x\ndate: 2026-01-01\n---"
    )

    assert note_writer.run_append_update(str(vault), str(note), "## Update (x)\n") == 0
    written = note.read_text(encoding="utf-8")
    assert "---## Update" not in written
    assert written.startswith("---\ntype: x\ndate: 2026-01-01\n---\n\n")


def test_read_stdin_capped_reports_oversize_in_process(monkeypatch):
    monkeypatch.setattr(
        sys, "stdin", io.StringIO("A" * (note_writer.STDIN_CAP_CHARS + 10))
    )
    text, oversized = note_writer._read_stdin_capped()
    assert oversized is True
    assert len(text) == note_writer.STDIN_CAP_CHARS

    monkeypatch.setattr(sys, "stdin", io.StringIO("A" * note_writer.STDIN_CAP_CHARS))
    text2, oversized2 = note_writer._read_stdin_capped()
    assert oversized2 is False
    assert len(text2) == note_writer.STDIN_CAP_CHARS


# ===========================================================================
# Containment check (hooks/obsidian_utils.py write_vault_note) — the outermost
# reachable layer.
#
# The pre-existing traversal tests (`../../escaped.md`, `../outside`) name this
# guard in their docstrings but are satisfied by _validate_filename /
# _validate_folder firing first, so deleting the containment check entirely
# left the whole suite green. A SYMLINK component inside the vault is the one
# vector that passes every argument validator — bare dot-free folder, bare
# *.md filename — and can ONLY be stopped by resolve() + is_relative_to().
# ===========================================================================

def test_write_symlinked_folder_cannot_escape_vault(tmp_path):
    """`folder` is a bare, dot-free, relative name and `filename` a bare *.md,
    so both validators pass. Only write_vault_note()'s resolve() +
    is_relative_to() containment check can reject this.

    Proven fail-first: with the containment check at
    hooks/obsidian_utils.py deleted, this returned rc=0 and genuinely created
    the file OUTSIDE the vault root (see dr-fix2-report.md)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "linkdir").symlink_to(outside, target_is_directory=True)

    result = _run_write(vault, "linkdir", "evil.md", MINIMAL_FM + "pwned\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert "traversal" in result.stderr.lower()
    assert not (outside / "evil.md").exists()
    assert not (vault / "linkdir" / "evil.md").exists()


def test_write_symlinked_folder_escape_blocked_in_process(tmp_path, capsys):
    """In-process mirror so coverage.py sees run_write's error branch for this
    vector (the subprocess run above is not instrumented)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "linkdir").symlink_to(outside, target_is_directory=True)

    rc = note_writer.run_write(str(vault), "linkdir", "evil.md", MINIMAL_FM + "x\n")

    assert rc == 1
    assert "ERROR" in capsys.readouterr().err
    assert list(outside.iterdir()) == []


def test_append_update_symlinked_note_cannot_escape_vault(tmp_path):
    """append-update mirror: a symlink INSIDE the vault pointing at a file
    outside it. The path is bare and dot-free, so only _resolve_note_path's
    resolve() + is_relative_to() containment check rejects it. The outside
    file must come back byte-identical."""
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.md"
    target.write_text(BASIC_NOTE, encoding="utf-8")
    before = target.read_bytes()

    link = vault / "claude-insights" / "link.md"
    link.symlink_to(target)

    result = _run_append_update(vault, link, UPDATE_SECTION, last_updated="2026-07-25")

    assert result.returncode == 1
    assert "traversal" in result.stderr.lower()
    assert target.read_bytes() == before


def test_append_update_symlinked_folder_cannot_escape_vault(tmp_path):
    """Same escape via a symlinked DIRECTORY component rather than the note
    file itself."""
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "note.md"
    target.write_text(BASIC_NOTE, encoding="utf-8")
    before = target.read_bytes()
    (vault / "linkdir").symlink_to(outside, target_is_directory=True)

    result = _run_append_update(vault, vault / "linkdir" / "note.md", UPDATE_SECTION)

    assert result.returncode == 1
    assert "traversal" in result.stderr.lower()
    assert target.read_bytes() == before


# ---------------------------------------------------------------------------
# Frontmatter/body injection through flag values (named cases from the
# test-integrity review).
# ---------------------------------------------------------------------------

def test_append_update_rejects_last_updated_forging_frontmatter_key(tmp_path):
    """`--last-updated $'2026-07-25\\ntype: pwned'` previously injected an
    arbitrary frontmatter key at rc=0 — overwriting `type`, which this module's
    own docstrings list as never-touched."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    before = note.read_bytes()

    result = _run_append_update(
        vault, note, UPDATE_SECTION, last_updated="2026-07-25\ntype: pwned"
    )

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    written = note.read_text(encoding="utf-8")
    assert "type: pwned" not in written
    assert "type: claude-insight" in written
    assert note.read_bytes() == before


def test_append_update_rejects_tag_forging_closing_frontmatter_fence(tmp_path):
    """`--add-tags $'ok\\n---\\nEVIL BODY'` previously forged a closing `---`
    fence inside the tags block and injected body content, restructuring the
    note at rc=0."""
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    before = note.read_bytes()

    result = _run_append_update(
        vault, note, UPDATE_SECTION, add_tags="ok\n---\nEVIL BODY"
    )

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    written = note.read_text(encoding="utf-8")
    assert "EVIL BODY" not in written
    # frontmatter still has exactly its original two fences
    assert written.count("\n---\n") == 1
    assert note.read_bytes() == before


# ---------------------------------------------------------------------------
# Tag-block indentation detection (was untested; zero-indent produced mixed
# indentation because `indent = m.group("indent") or indent` treats a
# legitimately empty indent as "not found" and falls back to the 2-space
# default).
# ---------------------------------------------------------------------------

def test_apply_add_tags_detects_four_space_indent():
    fm = ["tags:\n", "    - a\n", "    - b\n"]
    out = note_writer._apply_add_tags(fm, ["c"])
    assert out == ["tags:\n", "    - a\n", "    - b\n", "    - c\n"]


def test_apply_add_tags_detects_zero_indent_no_mixing():
    """Zero indent is valid YAML (and what yaml.dump emits). The new tag must
    match it rather than getting the hardcoded 2-space default, which produced
    `tags:\\n- a\\n- b\\n  - c` — a block with mixed indentation."""
    fm = ["tags:\n", "- a\n", "- b\n"]
    out = note_writer._apply_add_tags(fm, ["c"])
    assert out == ["tags:\n", "- a\n", "- b\n", "- c\n"]


def test_apply_add_tags_empty_block_uses_two_space_default():
    """No existing items to learn from — the documented 2-space default."""
    fm = ["tags:\n"]
    out = note_writer._apply_add_tags(fm, ["c"])
    assert out == ["tags:\n", "  - c\n"]


def test_append_update_four_space_tag_block_end_to_end(tmp_path):
    vault = tmp_path / "vault"
    note_content = BASIC_NOTE.replace(
        "tags:\n  - claude/insight\n  - claude/topic/foo\n",
        "tags:\n    - claude/insight\n    - claude/topic/foo\n",
    )
    note = _make_note(vault, "claude-insights", "insight.md", note_content)

    result = _run_append_update(vault, note, UPDATE_SECTION, add_tags="claude/topic/new")

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    assert "    - claude/topic/new\n" in written
    assert "  - claude/topic/new\n" not in written.replace("    - claude/topic/new\n", "")
