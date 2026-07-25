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

STDIN_CAP_BYTES = 1_000_000

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
    content = "# Session\n\nSome body text.\n"

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
    content = (
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
    content = "# Decision\nBody.\n"

    result = _run_write(vault, "claude-decisions", "decision.md", content)

    dest = vault / "claude-decisions" / "decision.md"
    assert result.returncode == 0, result.stderr
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == content


def test_write_file_mode_is_0o600(tmp_path):
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)

    result = _run_write(vault, "claude-sessions", "mode-check.md", "content\n")

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

def test_write_oversize_stdin_truncated_at_cap(tmp_path):
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    oversize_content = "A" * (STDIN_CAP_BYTES + 500)

    result = _run_write(vault, "claude-sessions", "oversize.md", oversize_content)

    dest = vault / "claude-sessions" / "oversize.md"
    assert result.returncode == 0, result.stderr
    written = dest.read_text(encoding="utf-8")
    assert len(written) == STDIN_CAP_BYTES
    assert written == oversize_content[:STDIN_CAP_BYTES]


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
    content = "# Session\nBody.\n"

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

    rc = note_writer.run_write(str(tmp_path / "vault"), "folder", "file.md", "content\n")

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "ERROR: forced failure for test"
    assert captured.out == ""
    # write_vault_note was faked out entirely, so nothing should exist.
    assert not (tmp_path / "vault").exists()


def test_main_write_dispatch_success_in_process(tmp_path, monkeypatch, capsys):
    vault = tmp_path / "vault"
    (vault / "claude-sessions").mkdir(parents=True)
    content = "in-process main() content\n"

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
    """The load-bearing case: proven fail-first (see fix report) by running
    this exact scenario against the pre-fix note_writer.py, which actually
    wrote executable content to vault/.obsidian/plugins/evil/main.js —
    write_vault_note()'s containment check does not fire here because the
    write never leaves the vault root."""
    vault = tmp_path / "vault"
    vault.mkdir()

    result = _run_write(vault, ".obsidian/plugins/evil", "main.js", "malicious JS\n")

    assert result.returncode == 1
    assert "ERROR" in result.stderr
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


def test_write_normal_folder_and_filename_still_succeeds(tmp_path):
    """Guards against the validation guard breaking the happy path."""
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)
    content = "# Retro\nBody.\n"

    result = _run_write(vault, "claude-insights", "2026-07-25-retro-a3f2.md", content)

    dest = vault / "claude-insights" / "2026-07-25-retro-a3f2.md"
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"OK: {dest.resolve()}"
    assert dest.read_text(encoding="utf-8") == content


# In-process counterpart for coverage of the validation helpers directly.
def test_validate_folder_and_filename_in_process():
    assert note_writer._validate_folder(".obsidian/plugins/evil") is not None
    assert note_writer._validate_folder("../outside") is not None
    assert note_writer._validate_folder("/etc") is not None
    assert note_writer._validate_folder("~/x") is not None
    assert note_writer._validate_folder("claude-insights") is None

    assert note_writer._validate_filename("notes/x.md") is not None
    assert note_writer._validate_filename("../../escape.md") is not None
    assert note_writer._validate_filename("x.txt") is not None
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

    rc = note_writer.run_write(
        str(tmp_path / "vault"), ".obsidian/plugins/evil", "main.js", "x\n"
    )

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.strip().startswith("ERROR: invalid folder")
    assert captured.out == ""
