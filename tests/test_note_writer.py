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

    rc = note_writer.run_write(
        str(tmp_path / "vault"), ".obsidian/plugins/evil", "main.js", "x\n"
    )

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
    assert "Body content here.\n" in written  # original content preserved


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


def test_append_update_empty_add_tags_is_noop(tmp_path):
    vault = tmp_path / "vault"
    note = _make_note(vault, "claude-insights", "insight.md", BASIC_NOTE)
    before = note.read_text(encoding="utf-8")

    result = _run_append_update(vault, note, UPDATE_SECTION, add_tags="")

    assert result.returncode == 0, result.stderr
    written = note.read_text(encoding="utf-8")
    # tags block byte-identical to before (only the body section was added)
    before_tags = before.split("tags:\n", 1)[1].split("---", 1)[0]
    after_tags = written.split("tags:\n", 1)[1].split("---", 1)[0]
    assert before_tags == after_tags


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
