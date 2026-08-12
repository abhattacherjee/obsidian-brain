# tests/test_obsidian_utils.py
"""Tests for obsidian_utils.py — config, metadata, messages, I/O, upgrade, sampling."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

# Tests that shell out to `git` need it on PATH; skip cleanly otherwise so
# CI/dev environments without git don't hard-fail (Copilot R5).
_GIT_AVAILABLE = shutil.which("git") is not None
_REQUIRES_GIT = pytest.mark.skipif(
    not _GIT_AVAILABLE, reason="git binary not available on PATH"
)

import frontmatter
import obsidian_utils


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unique_sid() -> str:
    """Return a unique string to use as a fake session ID (bypasses cache)."""
    return f"test-sid-{uuid.uuid4().hex}"


# ===========================================================================
# Section 1: Config & session context
# ===========================================================================


class TestLoadConfig:
    def test_load_config_valid(self, tmp_path, monkeypatch):
        """Write a valid config JSON, verify it merges with defaults."""
        config_file = tmp_path / "obsidian-brain-config.json"
        user_cfg = {
            "vault_path": str(tmp_path / "vault"),
            "sessions_folder": "my-sessions",
        }
        config_file.write_text(json.dumps(user_cfg), encoding="utf-8")

        monkeypatch.setattr(obsidian_utils, "_CONFIG_PATH", config_file)
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        result = obsidian_utils.load_config()

        assert result["vault_path"] == str(tmp_path / "vault")
        assert result["sessions_folder"] == "my-sessions"
        # Default keys still present
        assert result["insights_folder"] == "claude-insights"
        assert result["min_messages"] == 3
        assert result["summary_model"] == "haiku"

    def test_load_config_missing(self, tmp_path, monkeypatch):
        """Monkeypatch to nonexistent path — defaults should be returned."""
        monkeypatch.setattr(
            obsidian_utils, "_CONFIG_PATH", tmp_path / "no-such-config.json"
        )
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        result = obsidian_utils.load_config()

        assert result["vault_path"] == ""
        assert result["sessions_folder"] == "claude-sessions"
        assert result["min_messages"] == 3
        assert result["auto_log_enabled"] is True

    def test_get_project_name(self):
        """Test get_project_name with a path and with empty string."""
        assert obsidian_utils.get_project_name("/home/user/my-project") == "my-project"
        assert obsidian_utils.get_project_name("") == "unknown"

    def test_load_config_summary_pipeline_user_override(self, tmp_path, monkeypatch):
        """User config with summary_pipeline=subagent must surface through load_config."""
        config_file = tmp_path / "obsidian-brain-config.json"
        config_file.write_text(json.dumps({"summary_pipeline": "subagent"}), encoding="utf-8")

        monkeypatch.setattr(obsidian_utils, "_CONFIG_PATH", config_file)
        # Use a unique sid per call so the session-scoped config cache never
        # bleeds between test invocations (mirrors the pattern used throughout
        # this class — see _get_session_id_fast mock above).
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        cfg = obsidian_utils.load_config()
        assert cfg["summary_pipeline"] == "subagent"

    def test_load_config_summary_pipeline_default_is_auto(self, tmp_path, monkeypatch):
        """No user override → summary_pipeline defaults to 'auto'."""
        config_file = tmp_path / "obsidian-brain-config.json"
        config_file.write_text(json.dumps({"vault_path": str(tmp_path)}), encoding="utf-8")

        monkeypatch.setattr(obsidian_utils, "_CONFIG_PATH", config_file)
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        cfg = obsidian_utils.load_config()
        assert cfg["summary_pipeline"] == "auto"


class TestGetWorkspaceRoots:
    """Tests for the get_workspace_roots() helper (R13 C5 — config-driven workspace roots)."""

    def test_reads_workspace_roots_from_config(self, tmp_path, monkeypatch):
        """Config with workspace_roots returns tilde-expanded, existing dirs."""
        ws1 = tmp_path / "ws1"
        ws2 = tmp_path / "ws2"
        ws1.mkdir()
        ws2.mkdir()

        config_file = tmp_path / "obsidian-brain-config.json"
        config_file.write_text(
            json.dumps({"workspace_roots": [str(ws1), str(ws2)]}),
            encoding="utf-8",
        )
        config_file.chmod(0o600)

        monkeypatch.setattr(obsidian_utils, "_CONFIG_PATH", config_file)
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        roots = obsidian_utils.get_workspace_roots()
        assert str(ws1) in roots
        assert str(ws2) in roots

    def test_falls_back_to_defaults_when_key_absent(self, tmp_path, monkeypatch):
        """Config without workspace_roots key returns historical defaults (if they exist)."""
        config_file = tmp_path / "obsidian-brain-config.json"
        config_file.write_text(json.dumps({"vault_path": str(tmp_path)}), encoding="utf-8")
        config_file.chmod(0o600)

        # Create the historical default dirs so they pass the isdir filter
        home = os.path.expanduser("~")
        default1 = os.path.join(home, "dev", "claude_workspace")
        default2 = os.path.join(home, "projects")

        monkeypatch.setattr(obsidian_utils, "_CONFIG_PATH", config_file)
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        roots = obsidian_utils.get_workspace_roots()
        # We can't assert the exact paths since the test machine may not have them,
        # but every returned root must exist on disk.
        for r in roots:
            assert os.path.isdir(r), f"get_workspace_roots returned non-existent dir: {r}"
        # Roots must be the defaults — verify by checking they are a subset of the expected set
        assert all(r in {default1, default2} for r in roots)

    def test_filters_out_missing_directories(self, tmp_path, monkeypatch):
        """Paths that don't exist on disk are excluded from the returned list."""
        existing = tmp_path / "real-ws"
        existing.mkdir()
        missing = tmp_path / "ghost-ws"  # not created

        config_file = tmp_path / "obsidian-brain-config.json"
        config_file.write_text(
            json.dumps({"workspace_roots": [str(existing), str(missing)]}),
            encoding="utf-8",
        )
        config_file.chmod(0o600)

        monkeypatch.setattr(obsidian_utils, "_CONFIG_PATH", config_file)
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        roots = obsidian_utils.get_workspace_roots()
        assert str(existing) in roots
        assert str(missing) not in roots


# ===========================================================================
# Section 2: Frontmatter parsing
# ===========================================================================


class TestReadNoteMetadata:
    def test_read_note_metadata_valid(self, sample_session_note, monkeypatch):
        """Parse valid frontmatter and verify fields + tags."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        meta = obsidian_utils.read_note_metadata(str(sample_session_note))

        assert meta is not None
        assert meta["type"] == "claude-session"
        assert meta["date"] == "2026-04-10"
        assert meta["session_id"] == "test-session-id-1234"
        assert meta["project"] == "test-project"
        assert meta["status"] == "summarized"
        assert "claude/session" in meta["tags"]
        assert "claude/project/test-project" in meta["tags"]
        assert "claude/auto" in meta["tags"]

    def test_read_note_metadata_no_frontmatter(self, tmp_path, monkeypatch):
        """File without --- markers should return None."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        note = tmp_path / "plain.md"
        note.write_text("# Just a heading\n\nNo frontmatter here.\n", encoding="utf-8")

        result = obsidian_utils.read_note_metadata(str(note))
        assert result is None

    def test_read_note_metadata_empty_file(self, tmp_path, monkeypatch):
        """Empty file should return None."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        note = tmp_path / "empty.md"
        note.write_text("", encoding="utf-8")

        result = obsidian_utils.read_note_metadata(str(note))
        assert result is None


# ---------------------------------------------------------------------------
# #283: read_note_metadata used to read only the first 40 lines and never
# checked that a closing '---' was actually found. Both failure modes were
# measured on the live vault: 26 notes lost their `tags` (the block starts
# past line 40) and 28 notes had no closing fence within 40 lines, so body
# prose was harvested into fields. These tests pin both, plus the bound the
# fix is allowed to keep (a bounded read, not a whole-file slurp).
# ---------------------------------------------------------------------------


class TestReadNoteMetadataFrontmatterBounds:
    def test_tags_block_past_line_40_is_recovered(self, tmp_path, monkeypatch):
        """A `tags:` block starting below line 40 must still parse.

        Restoring the old `if i >= 40: break` bound makes this fail: the
        reader never reaches the tags block or the closing fence.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        filler = "\n".join(f"field_{i:02d}: value-{i:02d}" for i in range(45))
        note = tmp_path / "deep.md"
        note.write_text(
            "---\n"
            "type: claude-session\n"
            f"{filler}\n"
            "project: deep-project\n"
            "tags:\n"
            "  - claude/session\n"
            "  - claude/project/deep-project\n"
            "---\n"
            "\n# Body\n",
            encoding="utf-8",
        )

        meta = obsidian_utils.read_note_metadata(str(note))

        assert meta is not None
        assert meta["project"] == "deep-project"
        # Type, not only value: a future "just reuse vault_index's parser"
        # refactor would return tags as a comma-joined STRING, which still
        # compares unequal here but for a reason nobody would recognise.
        assert isinstance(meta["tags"], list)
        assert meta["tags"] == ["claude/session", "claude/project/deep-project"]

    def test_unclosed_fence_returns_none_and_forges_no_fields(
        self, tmp_path, monkeypatch
    ):
        """The issue's own fixture: an opening fence, no closing fence.

        Without the split_frontmatter guard the old parser walked into the
        body and manufactured fields out of prose — `status` here is a
        sentence, not a field, and `Note:` is a paragraph opener.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        note = tmp_path / "unclosed.md"
        note.write_text(
            "---\n"
            "type: session\n"
            "# My Note\n"
            "\n"
            "Note: this is body prose\n"
            "status: NOT REALLY A FIELD\n",
            encoding="utf-8",
        )

        meta = obsidian_utils.read_note_metadata(str(note))

        assert meta is None, f"expected None for an unfenced note, got {meta!r}"

    def test_unclosed_fence_result_is_cached_as_no_frontmatter(
        self, tmp_path, monkeypatch
    ):
        """Second call must also return None (sentinel cached, not a dict)."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: "fixed-sid-283")

        note = tmp_path / "unclosed-cached.md"
        note.write_text("---\ntype: session\n# My Note\n\nprose\n", encoding="utf-8")

        assert obsidian_utils.read_note_metadata(str(note)) is None
        assert obsidian_utils.read_note_metadata(str(note)) is None

    def test_bare_cr_inside_a_value_is_not_a_line_terminator(
        self, tmp_path, monkeypatch
    ):
        """`newline=""` guarantee: a bare \\r inside a value stays in the value.

        This fixture is built so universal-newline mode gives a DIFFERENT
        parse, not just different whitespace: with newline=None the \\r is
        translated to \\n before split_lines_lf_crlf runs, splitting one
        `title:` line into two frontmatter lines and forging a `status`
        field. Dropping `newline=""` from _read_frontmatter_region makes
        both assertions below fail.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        note = tmp_path / "bare-cr.md"
        note.write_bytes(
            b'---\ntype: claude-session\ntitle: "before\rstatus: forged"\n---\n\nbody\n'
        )

        meta = obsidian_utils.read_note_metadata(str(note))

        assert meta is not None
        assert meta["title"] == "before\rstatus: forged"
        assert "status" not in meta, (
            "a bare \\r was treated as a line break, forging a status field"
        )

    def test_frontmatter_over_the_line_limit_returns_none(self, tmp_path, monkeypatch):
        """Past MAX_FRONTMATTER_LINES the block is rejected, not truncated."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        limit = obsidian_utils.MAX_FRONTMATTER_LINES
        bulk = "\n".join(f"field_{i:05d}: v{i}" for i in range(limit + 100))
        note = tmp_path / "oversized.md"
        note.write_text(f"---\n{bulk}\n---\n\n# Body\n", encoding="utf-8")

        assert obsidian_utils.read_note_metadata(str(note)) is None

    def test_read_stops_at_the_closing_fence_on_a_realistic_note(
        self, tmp_path, monkeypatch
    ):
        """The bound that has to fire on REAL notes: the closing fence.

        This is the p90 vault shape — fence at line 4, ~300 lines / ~90 KB of
        body — and it is the shape the line-count cap never touches: 0 of 2098
        live notes exceed MAX_FRONTMATTER_LINES, so a reader that stops only
        on the newline count reads 100% of every note in the vault (measured:
        23.9 MB of 24.1 MB) and makes the reaper's SessionStart scan 47-53x
        slower. Assert the read is a block or two, not a file.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        note = tmp_path / "realistic.md"
        body = "".join("word " * 60 + "\n" for _ in range(300))  # ~90 KB
        note.write_text(
            "---\ntype: claude-session\nproject: realistic\nstatus: summarized\n---\n"
            + body,
            encoding="utf-8",
        )
        file_size = note.stat().st_size
        assert file_size > 80_000, file_size

        spy = _install_read_spy(monkeypatch, str(note))

        meta = obsidian_utils.read_note_metadata(str(note))

        assert meta is not None and meta["project"] == "realistic"
        assert spy["chars"] > 0, "spy never saw a read — instrumentation broke"
        # ONE block, not two. The fence is at line 4, so a second block means
        # a regression — and the 100%-headroom version of this assertion let
        # the missing-block-seam rewind through unnoticed, since that mutant
        # costs exactly one extra block on this shape. The 2-block case has
        # its own fixture now
        # (test_closing_fence_straddling_a_read_block_boundary_is_found).
        assert spy["chars"] <= obsidian_utils._FRONTMATTER_READ_BLOCK, (
            f"read {spy['chars']} of {file_size} chars — the read did not stop "
            "at the closing fence"
        )

    def test_read_is_bounded_when_the_fence_never_arrives(
        self, tmp_path, monkeypatch
    ):
        """Pathological backstop: frontmatter-shaped lines with no fence.

        Nothing stops the fence-scan here, so MAX_FRONTMATTER_LINES has to.
        The threshold is the contract — MAX + 1 lines plus at most the block
        that carried the last of them — not a fraction of the file size.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        line_len = 400  # + "\n"
        note = tmp_path / "no-fence.md"
        filler = "".join(f"key_{i:05d}: " + "z" * (line_len - 12) + "\n"
                         for i in range(2 * obsidian_utils.MAX_FRONTMATTER_LINES))
        note.write_text("---\n" + filler, encoding="utf-8")
        file_size = note.stat().st_size

        spy = _install_read_spy(monkeypatch, str(note))

        meta, reason = obsidian_utils.read_note_metadata_detailed(str(note))

        assert meta is None
        assert reason.startswith("frontmatter exceeds"), reason
        budget = ((obsidian_utils.MAX_FRONTMATTER_LINES + 1) * (line_len + 1)
                  + obsidian_utils._FRONTMATTER_READ_BLOCK)
        assert spy["chars"] <= budget, (
            f"read {spy['chars']} of {file_size} chars, budget {budget}"
        )

    def test_read_is_capped_in_bytes_for_a_file_with_no_newlines(
        self, tmp_path, monkeypatch
    ):
        """The newline cap counts "\n", so a file without any never trips it.

        A CR-only (classic-Mac) note or a single-line minified blob has zero
        newlines; without the character cap this is read to EOF at any size.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        note = tmp_path / "cr-only.md"
        # write_bytes, not write_text(newline=...): the latter is 3.13+ only.
        note.write_bytes(b"---\r" + b"x" * 2_500_000)
        file_size = note.stat().st_size
        assert file_size > obsidian_utils._FRONTMATTER_MAX_CHARS

        spy = _install_read_spy(monkeypatch, str(note))

        assert obsidian_utils.read_note_metadata(str(note)) is None
        assert spy["chars"] > 0, "spy never saw a read — instrumentation broke"
        assert spy["chars"] <= (obsidian_utils._FRONTMATTER_MAX_CHARS
                                + obsidian_utils._FRONTMATTER_READ_BLOCK), (
            f"read {spy['chars']} of {file_size} chars — the byte cap is inert"
        )

    def test_fence_at_exactly_the_line_limit_parses(self, tmp_path, monkeypatch):
        """Fence at index exactly MAX_FRONTMATTER_LINES is INSIDE the bound.

        split_frontmatter reads up to index MAX_FRONTMATTER_LINES, so the
        region reader must hand it MAX + 1 lines, not MAX. Slicing one line
        short converts this well-formed note into "no closing '---'" — the
        exact misdiagnosis frontmatter.py's docstring calls out as the one
        that sends someone to repair a file that is not broken.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        limit = obsidian_utils.MAX_FRONTMATTER_LINES
        # index 0 = open fence, 1..limit-1 = fields, index `limit` = close fence
        fields = "".join(f"field_{i:05d}: v{i}\n" for i in range(1, limit))
        note = tmp_path / "fence-at-limit.md"
        note.write_text("---\n" + fields + "---\n\n# Body\n", encoding="utf-8")

        meta, reason = obsidian_utils.read_note_metadata_detailed(str(note))

        assert reason is None, reason
        assert meta is not None
        assert meta[f"field_{limit - 1:05d}"] == f"v{limit - 1}"

    def test_fence_one_past_the_line_limit_is_reported_as_a_size_error(
        self, tmp_path, monkeypatch
    ):
        """Fence at index MAX + 1 is out of bounds — and must say WHY.

        "frontmatter exceeds N lines" tells the user their note may be fine
        and the limit is the problem; "no closing '---'" tells them their
        note is corrupt. Reporting the wrong one is the whole point of
        keeping split_frontmatter's distinct diagnoses.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        limit = obsidian_utils.MAX_FRONTMATTER_LINES
        fields = "".join(f"field_{i:05d}: v{i}\n" for i in range(1, limit + 1))
        note = tmp_path / "fence-past-limit.md"
        note.write_text("---\n" + fields + "---\n\n# Body\n", encoding="utf-8")

        meta, reason = obsidian_utils.read_note_metadata_detailed(str(note))

        assert meta is None
        assert reason.startswith("frontmatter exceeds"), (
            f"expected the SIZE diagnosis, got {reason!r}"
        )

    def test_unreadable_file_is_not_cached_as_no_frontmatter(
        self, tmp_path, monkeypatch
    ):
        """A transient read failure must not pin "no frontmatter" for the session.

        The cache is keyed by session id, so caching the sentinel here would
        make every later read of this path in the same session return None —
        a note that exists and parses fine would stay invisible until the
        session ended. Replacing that branch with `cache_set(...); return None`
        passes every other test in the suite; this is the one that catches it.
        """
        import builtins
        import errno

        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: "fixed-sid-283-eio")

        note = tmp_path / "flaky.md"
        note.write_text(
            "---\ntype: claude-session\nproject: flaky\n---\n\n# Body\n",
            encoding="utf-8",
        )

        real_open = builtins.open
        state = {"failed": False}

        def flaky_open(file, *args, **kwargs):
            if str(file) == str(note) and not state["failed"]:
                state["failed"] = True
                raise OSError(errno.EIO, "Input/output error", str(file))
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", flaky_open)

        first = obsidian_utils.read_note_metadata(str(note))
        second = obsidian_utils.read_note_metadata(str(note))

        assert state["failed"], "the flaky open never fired — instrumentation broke"
        assert first is None
        assert second is not None and second["project"] == "flaky", (
            "the OSError was cached as 'no frontmatter' and pinned for the session"
        )

    def test_unreadable_file_reason_names_the_cause_not_the_path(self, tmp_path):
        """The reason has to say WHY, without leaking the absolute vault path.

        str(OSError) renders as "[Errno 2] No such file or directory:
        '/Users/.../secret-note.md'"; that string flows into discovery_errors
        and from there into the model's context. exc.strerror does not.
        """
        missing = tmp_path / "does-not-exist.md"

        meta, reason = obsidian_utils.read_note_metadata_detailed(str(missing))

        assert meta is None
        assert reason.startswith("unreadable file: "), reason
        assert "No such file" in reason, reason
        assert str(missing) not in reason, f"leaked the absolute path: {reason!r}"

    def test_corrupt_utf8_is_accepted_verbatim_as_the_replacement_char(
        self, tmp_path, monkeypatch
    ):
        """errors="replace" is a deliberate trade, pinned in BOTH directions.

        The upside (a bad byte no longer takes out the whole note) is already
        covered. The downside is that corruption is SILENTLY ACCEPTED: the
        field keeps parsing and its value now carries U+FFFD. Assert that,
        so switching to errors="strict" — or adding a validity check —
        registers as a behaviour change rather than passing unnoticed.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        note = tmp_path / "corrupt.md"
        note.write_bytes(
            b"---\ntype: claude-session\nproject: caf\xff\xfe\nstatus: summarized\n---\n"
        )

        meta = obsidian_utils.read_note_metadata(str(note))

        assert meta is not None
        assert meta["project"] == "caf\ufffd\ufffd", repr(meta["project"])
        assert meta["status"] == "summarized"

    def test_all_crlf_note_with_a_deep_tags_block_parses_cleanly(
        self, tmp_path, monkeypatch
    ):
        """End-to-end CRLF: every terminator is \r\n, tags start past line 40.

        Covers the two halves together — the CRLF-aware splitter and the
        removal of the 40-line bound — and asserts no value keeps a stray
        \r, which is what a splitter that only knows "\n" would leave behind.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        filler = "".join(f"field_{i:02d}: value-{i:02d}\r\n" for i in range(45))
        note = tmp_path / "crlf.md"
        note.write_bytes(
            (
                "---\r\n"
                "type: claude-session\r\n"
                + filler
                + "project: crlf-project\r\n"
                "tags:\r\n"
                "  - claude/session\r\n"
                "  - claude/project/crlf-project\r\n"
                "---\r\n"
                "\r\n"
                "# Body\r\n"
            ).encode("utf-8")
        )

        meta = obsidian_utils.read_note_metadata(str(note))

        assert meta is not None
        assert meta["project"] == "crlf-project"
        assert meta["field_44"] == "value-44"
        assert isinstance(meta["tags"], list)
        assert meta["tags"] == ["claude/session", "claude/project/crlf-project"]
        assert not any("\r" in v for v in meta.values() if isinstance(v, str))
        assert not any("\r" in t for t in meta["tags"])

    def test_frontmatter_line_longer_than_one_read_block(
        self, tmp_path, monkeypatch
    ):
        """A single frontmatter line wider than the 8 KB read block.

        The fence then lands in a later block, so the block-boundary handling
        (rewind the fence search, never treat a partial trailing line as a
        line) is what has to hold.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        wide = "w" * (obsidian_utils._FRONTMATTER_READ_BLOCK * 3)
        note = tmp_path / "wide.md"
        note.write_text(
            f"---\ntype: claude-session\nsummary: {wide}\nproject: wide\n---\n\n# Body\n",
            encoding="utf-8",
        )

        meta = obsidian_utils.read_note_metadata(str(note))

        assert meta is not None
        assert meta["project"] == "wide"
        assert meta["summary"] == wide

    def test_column_zero_yaml_comment_inside_frontmatter_returns_none(
        self, tmp_path, monkeypatch
    ):
        """Shape-check tightening, pinned as intended rather than discovered.

        `# note` at column 0 is not blank, not `key:`-shaped, not a `- ` item
        and not an indented continuation, so split_frontmatter stops there and
        the whole note is refused — where develop returned a partial dict of
        the fields above the comment. 0 live vault notes do this; the point of
        the test is that the change is recorded, not that it is desirable.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        note = tmp_path / "commented.md"
        note.write_text(
            "---\n"
            "type: claude-session\n"
            "# note: this is a YAML comment at column 0\n"
            "project: commented\n"
            "---\n"
            "\n# Body\n",
            encoding="utf-8",
        )

        meta, reason = obsidian_utils.read_note_metadata_detailed(str(note))

        assert meta is None
        assert reason.startswith("malformed or missing frontmatter (no closing"), reason

    def test_body_horizontal_rule_never_closes_an_unfenced_block(
        self, tmp_path, monkeypatch
    ):
        """A `----------` rule in the BODY must not be read as a closing fence.

        _CLOSING_FENCE_MARKERS carries the fence's OWN terminator
        ("\n---\n"/"\n---\r\n") for exactly this note. Shorten it to a bare
        "\n---" prefix and _find_closing_fence_end matches inside the rule,
        cutting `text` mid-line so the truncated tail reads exactly "---" —
        which split_frontmatter then accepts as a real closing fence. The text
        is severed BEFORE the shape check can ever see the prose, so
        frontmatter.py's "a paragraph is not frontmatter" backstop never runs
        and every field above is forged. That is #283 itself, re-entered
        through the new fence finder, which is why the terminator is pinned
        here and not only described in a docstring.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        note = tmp_path / "hr-in-body.md"
        note.write_text(
            "---\n"
            "type: claude-session\n"
            "project: forged\n"
            "status: summarized\n"
            "----------\n"
            "\n"
            "real body prose here\n",
            encoding="utf-8",
        )

        meta, reason = obsidian_utils.read_note_metadata_detailed(str(note))

        assert meta is None, (
            f"a body horizontal rule was accepted as a closing fence: {meta!r}"
        )
        assert reason.startswith("malformed or missing frontmatter (no closing"), reason

    def test_closing_fence_with_trailing_spaces_does_not_close_the_block(
        self, tmp_path, monkeypatch
    ):
        """`---␣␣` is not a closing fence — the promise, pinned.

        _peek_frontmatter_field's docstring states this tightening (the shared
        parser compares with rstrip("\r\n"), not .strip()), and
        read_note_metadata_detailed inherits it. Nothing tested it: a marker
        tuple that matched a bare "\n---" prefix would treat this line as a
        fence and hand back the fields above it.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        note = tmp_path / "fence-trailing-spaces.md"
        note.write_text(
            "---\n"
            "type: claude-session\n"
            "project: trailing-spaces\n"
            "---  \n"
            "\n"
            "# Body\n",
            encoding="utf-8",
        )

        meta, reason = obsidian_utils.read_note_metadata_detailed(str(note))

        assert meta is None, f"'---  ' was accepted as a closing fence: {meta!r}"
        assert reason.startswith("malformed or missing frontmatter (no closing"), reason
        # Same note through the lighter peek path, which is where the promise
        # is actually written down.
        assert obsidian_utils._peek_frontmatter_field(note, "project") is None

    def test_char_capped_read_forges_no_fence_and_reports_the_size(
        self, tmp_path, monkeypatch
    ):
        """The character cap: a truncation fragment reading "---" is not a fence.

        Two guards meet here and the fixture is built to need both.

        1. The read stops mid-line at the cap, leaving a trailing element that
           is a FRAGMENT, not a line. Here it reads exactly "---" at index 4 —
           well inside the MAX_FRONTMATTER_LINES+1 slice, so the slice cannot
           save us — and split_frontmatter would accept it as a closing fence
           and hand back the fields above. Skipping the `lines.pop()` forges
           `project: forged-by-fragment`.
        2. The verdict must name the SIZE. Deriving it from the truncated
           prefix gives "no closing '---'", i.e. accusing a note whose fence
           may sit just past the cut — frontmatter.py's own docstring calls
           that the failure that "sends someone to repair a file that is not
           broken". _FRONTMATTER_TOO_LARGE_REASON is what
           vault_index._classify_parse_failure maps to frontmatter_too_long.

        The existing CR-only byte-cap test cannot cover either: its fragment
        is 2.5 MB of "x", so the shape check rejects the note long before the
        fragment matters.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        cap = obsidian_utils._FRONTMATTER_MAX_CHARS
        block = obsidian_utils._FRONTMATTER_READ_BLOCK
        # The read stops at the first block boundary at or past the cap, so
        # place the fragment by OFFSET rather than by guessing a file size.
        stop = ((cap + block - 1) // block) * block

        head = ("---\n"
                "type: claude-session\n"
                "project: forged-by-fragment\n")
        tail = "\n---"
        # An indented continuation: frontmatter-shaped (so the scan does not
        # stop on it) and newline-free (so the LINE cap never fires and the
        # character cap is the one under test).
        pad = "  " + "z" * (stop - len(head) - len(tail) - 2)
        note = tmp_path / "char-capped.md"
        note.write_text(
            head + pad + tail + "-------\nreal body prose\n" + "q" * 100,
            encoding="utf-8",
        )
        assert note.stat().st_size > cap

        meta, reason = obsidian_utils.read_note_metadata_detailed(str(note))

        assert meta is None, (
            f"a truncation fragment was accepted as a closing fence: {meta!r}"
        )
        assert obsidian_utils._classify_note_parse_failure(reason) == (
            "frontmatter_too_long"
        ), f"the size cap was reported as a fence verdict: {reason!r}"

    def test_over_cap_file_with_no_opening_fence_still_reports_no_opening_fence(
        self, tmp_path, monkeypatch
    ):
        """The size caveat must not overwrite a verdict truncation cannot reach.

        "does not open with a '---' fence" is derived from lines[0], which is
        always inside the FIRST 8 KB block — no read cap can change it. Letting
        the caveat win here relabels a file that is not a note at all (a pasted
        minified blob, an exported log) as an oversized NOTE, and that is not
        cosmetic: gather_session_evidence filters exactly the no_opening_fence
        reason out of discovery_errors, so the relabel makes that filter
        size-dependent and re-raises /retro's "evidence discovery partially or
        fully failed" banner — in every project, every session, permanently —
        for any such file over the char cap.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        note = tmp_path / "data-dump.md"
        # One newline, then a single line long enough to trip the CHARACTER
        # cap (the line cap counts "\n"s and never fires here).
        note.write_text(
            "# Data dump\n" + "x" * (obsidian_utils._FRONTMATTER_MAX_CHARS + 500_000),
            encoding="utf-8",
        )
        assert note.stat().st_size > obsidian_utils._FRONTMATTER_MAX_CHARS

        meta, reason = obsidian_utils.read_note_metadata_detailed(str(note))

        assert meta is None
        assert obsidian_utils._classify_note_parse_failure(reason) == (
            "no_opening_fence"
        ), f"the size caveat overwrote a definitive verdict: {reason!r}"

    def test_over_cap_file_with_a_shape_violation_still_reports_no_closing_fence(
        self, tmp_path, monkeypatch
    ):
        """Same guard, second masked verdict — and this one inverts the advice.

        The "stopped at a line that is not frontmatter" verdict names a line
        that was actually read and inspected (here index 2, "# Title"), so
        truncation cannot invalidate it either. Overwritten by the caveat, a
        note whose frontmatter demonstrably dies at line 3 is reported as
        "the note may be fine; this is a size limit, not a missing fence".
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        note = tmp_path / "broken-note.md"
        note.write_text(
            "---\ntype: claude-insight\n# Title\n"
            + "x" * (obsidian_utils._FRONTMATTER_MAX_CHARS + 500_000),
            encoding="utf-8",
        )
        assert note.stat().st_size > obsidian_utils._FRONTMATTER_MAX_CHARS

        meta, reason = obsidian_utils.read_note_metadata_detailed(str(note))

        assert meta is None
        assert obsidian_utils._classify_note_parse_failure(reason) == (
            "no_closing_fence"
        ), f"the size caveat overwrote a definitive verdict: {reason!r}"

    def test_size_caveat_gate_matches_the_exhaustion_reason_exactly(self):
        """Exact equality, not startswith — the trap this gate is built around.

        The shape-stop verdict SHARES the bare-exhaustion verdict's prefix
        ("malformed or missing frontmatter (no closing '---'") and differs only
        by the "; stopped at ..." tail. A `startswith` gate would therefore
        re-admit the shape-stop case while looking correct, and the two tests
        above are the only thing that would notice. Pin the relationship
        directly, against REAL split_frontmatter output rather than literals,
        so the trap is documented where the gate is.
        """
        shape_stop = frontmatter.split_frontmatter(
            frontmatter.split_lines_lf_crlf("---\ntype: x\n# Title\n")
        )[4]
        exhausted = frontmatter.split_frontmatter(
            frontmatter.split_lines_lf_crlf("---\ntype: x\n")
        )[4]

        assert exhausted == frontmatter.NO_CLOSING_FENCE_EXHAUSTED_REASON
        assert shape_stop != frontmatter.NO_CLOSING_FENCE_EXHAUSTED_REASON
        assert shape_stop.startswith(
            "malformed or missing frontmatter (no closing '---'"
        ), (
            "the shared prefix is gone, so a startswith gate would no longer "
            "be wrong here — re-derive what this test is protecting"
        )

    def test_closing_fence_straddling_a_read_block_boundary_is_found(
        self, tmp_path, monkeypatch
    ):
        """The rewind: a fence split across two 8 KB reads must still be seen.

        `scan_from = max(0, len(text) - 5)` is what makes the next pass
        re-examine the boundary. Drop the rewind (`scan_from = len(text)`) and
        the fence at block1-tail "x\n--" / block2-head "-\nbo" is missed
        FOREVER: the read then runs on to the line cap — measured 40,960 chars
        against 16,384 here — which is the 47-53x reaper regression this stop
        exists to prevent, and it is silent, because the note still parses.
        So the assertion has to be on how much was READ, not on the metadata.
        """
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        block = obsidian_utils._FRONTMATTER_READ_BLOCK
        head = "---\ntype: claude-session\nproject: seam\nsummary: "
        # "\n---\n" starts at block - 3, so the first read ends mid-marker.
        pad = "s" * (block - 3 - len(head))
        body = "".join(f"prose line {i} with several words\n" for i in range(2000))
        text = head + pad + "\n---\n" + body
        assert text.index("\n---\n") == block - 3
        note = tmp_path / "seam.md"
        note.write_text(text, encoding="utf-8")

        spy = _install_read_spy(monkeypatch, str(note))

        meta = obsidian_utils.read_note_metadata(str(note))

        assert meta is not None and meta["project"] == "seam"
        assert spy["chars"] > 0, "spy never saw a read — instrumentation broke"
        # Two blocks: the first ends inside the marker, the second completes
        # it. Anything more means the rewind did not happen.
        assert spy["chars"] <= block * 2, (
            f"read {spy['chars']} chars of {note.stat().st_size} — the fence "
            "was missed at the block seam"
        )

    def test_cache_hit_reports_the_same_reason_as_the_cache_miss(
        self, tmp_path, monkeypatch
    ):
        """"A cache hit is as informative as a miss" — the docstring's promise.

        Reachable in one /retro: gather_session_evidence and
        build_context_brief both scan the insights folder in the same session,
        so the second read of a poisoned note is a cache hit. Drop the reason
        from the hit (`return None, None`) and that second pass records
        nothing — the note vanishes from the bundle with an empty
        discovery_errors, which is the exact failure the reason exists to
        prevent.
        """
        monkeypatch.setattr(
            obsidian_utils, "_get_session_id_fast", lambda: "fixed-sid-283-cachehit"
        )

        note = tmp_path / "broken-twice.md"
        note.write_text(
            "---\ntype: claude-insight\n# My Note\n\nprose, not frontmatter\n",
            encoding="utf-8",
        )

        first_meta, first_reason = obsidian_utils.read_note_metadata_detailed(str(note))
        second_meta, second_reason = obsidian_utils.read_note_metadata_detailed(str(note))

        assert first_meta is None and second_meta is None
        assert first_reason is not None
        assert second_reason is not None, (
            "the cache hit dropped the failure reason — the note is now "
            "invisible with no explanation"
        )
        assert second_reason == first_reason

    @pytest.mark.parametrize(
        "name,content",
        [
            ("well-formed", "---\ntype: claude-session\nproject: p1\n---\n\nbody\n"),
            ("field-absent", "---\ntype: claude-session\n---\n\nbody\n"),
            ("empty-value", "---\ntype: claude-session\nproject:\n---\n\nbody\n"),
            ("no-closing-fence", "---\ntype: claude-session\nproject: p4\n# Title\n"),
            ("no-opening-fence", "# Title\n\nproject: p5\n"),
        ],
    )
    def test_single_and_multi_field_peek_agree(self, tmp_path, name, content):
        """_peek_frontmatter_field is a wrapper today; pin that it stays one.

        The reaper reads three fields per note through the multi-field call
        while every other call site reads one through the wrapper. If a future
        change re-implements either side, these two must not drift — a
        divergence would mean the SessionStart scan and the rest of the plugin
        disagree about what a note says.
        """
        note = tmp_path / f"{name}.md"
        note.write_text(content, encoding="utf-8")

        assert (
            obsidian_utils._peek_frontmatter_fields(note, ("project",))["project"]
            == obsidian_utils._peek_frontmatter_field(note, "project")
        )

    def test_peek_on_a_missing_file_agrees_across_both_entry_points(self, tmp_path):
        """Same parity, for the path that never opens: a file that isn't there."""
        missing = tmp_path / "gone.md"

        assert (
            obsidian_utils._peek_frontmatter_fields(missing, ("project",))["project"]
            == obsidian_utils._peek_frontmatter_field(missing, "project")
            is None
        )


def _install_read_spy(monkeypatch, target_path: str) -> dict:
    """Patch builtins.open so reads of ``target_path`` are counted.

    Every other path (session cache, config) passes through untouched, so
    this stays safe to install for the duration of one call.
    """
    import builtins

    stats = {"chars": 0, "calls": 0}
    real_open = builtins.open

    class _CountingReader:
        def __init__(self, fh):
            self._fh = fh

        def read(self, size=-1):
            data = self._fh.read(size)
            stats["calls"] += 1
            stats["chars"] += len(data)
            return data

        def __iter__(self):
            return iter(self._fh)

        def __enter__(self):
            self._fh.__enter__()
            return self

        def __exit__(self, *exc):
            return self._fh.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self._fh, name)

    def fake_open(file, *args, **kwargs):
        fh = real_open(file, *args, **kwargs)
        if str(file) == target_path:
            return _CountingReader(fh)
        return fh

    monkeypatch.setattr(builtins, "open", fake_open)
    return stats


# ===========================================================================
# Section 3: Message extraction
# ===========================================================================


class TestMessageExtraction:
    def test_extract_user_messages(self, sample_jsonl):
        """Extract user messages from JSONL transcript — expect 2."""
        entries = obsidian_utils.read_transcript(str(sample_jsonl))
        msgs = obsidian_utils.extract_user_messages(entries)
        assert len(msgs) == 2
        assert "Fix the login bug" in msgs[0]
        assert "deploy" in msgs[1].lower()

    def test_extract_assistant_messages(self, sample_jsonl):
        """Extract assistant messages — expect 2 including text from content blocks."""
        entries = obsidian_utils.read_transcript(str(sample_jsonl))
        msgs = obsidian_utils.extract_assistant_messages(entries)
        assert len(msgs) == 2
        assert "login handler" in msgs[0].lower()
        assert "deployed" in msgs[1].lower() or "done" in msgs[1].lower()

    def test_extract_user_messages_empty(self):
        """Empty list returns []."""
        assert obsidian_utils.extract_user_messages([]) == []


# ===========================================================================
# Section 4: Slug & filename
# ===========================================================================


class TestSlugAndFilename:
    def test_slugify(self):
        """Lowercases, replaces spaces/special chars, truncates at 40, empty returns 'session'."""
        assert obsidian_utils.slugify("Hello World") == "hello-world"
        assert obsidian_utils.slugify("Fix: AUTH bug #42!") == "fix-auth-bug-42"
        # Truncates at 40
        long_text = "a" * 50
        result = obsidian_utils.slugify(long_text)
        assert len(result) <= 40
        # Empty string returns "session"
        assert obsidian_utils.slugify("") == "session"
        # Only special chars → "session"
        assert obsidian_utils.slugify("---") == "session"

    def test_make_filename(self):
        """Verify format YYYY-MM-DD-slug-hash.md with sha256[:4]; test suffix parameter."""
        session_id = "test-session-abc"
        expected_hash = hashlib.sha256(session_id.encode()).hexdigest()[:4]

        filename = obsidian_utils.make_filename("2026-04-10", "my-slug", session_id)
        assert filename == f"2026-04-10-my-slug-{expected_hash}.md"

        # With suffix
        filename_suffixed = obsidian_utils.make_filename(
            "2026-04-10", "my-slug", session_id, suffix="-snapshot"
        )
        assert filename_suffixed == f"2026-04-10-my-slug-{expected_hash}-snapshot.md"


# ===========================================================================
# Section 5: Session skip logic
# ===========================================================================


class TestShouldSkipSession:
    def test_should_skip_session_short(self):
        """Below message threshold → True."""
        assert obsidian_utils.should_skip_session(["hello", "world"], 10.0) is True

    def test_should_skip_session_long(self):
        """Meets thresholds → False."""
        msgs = ["msg1", "msg2", "msg3", "msg4"]
        assert obsidian_utils.should_skip_session(msgs, 5.0) is False

    def test_should_skip_session_short_duration(self):
        """Known short duration (>0, <min_duration) → True."""
        msgs = ["msg1", "msg2", "msg3", "msg4"]
        assert obsidian_utils.should_skip_session(msgs, 1.0, min_duration=2.0) is True

    def test_should_skip_session_zero_duration(self):
        """Zero (unknown) duration bypasses duration check → False."""
        msgs = ["msg1", "msg2", "msg3", "msg4"]
        # zero duration means unknown — do not skip based on duration
        assert obsidian_utils.should_skip_session(msgs, 0.0, min_duration=2.0) is False


# ===========================================================================
# Section 6: Transcript parsing
# ===========================================================================


class TestReadTranscript:
    def test_read_transcript_valid_jsonl(self, sample_jsonl):
        """Read valid JSONL — expect 4 entries."""
        entries = obsidian_utils.read_transcript(str(sample_jsonl))
        assert len(entries) == 4

    def test_read_transcript_empty(self, tmp_path):
        """Empty file → []."""
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        result = obsidian_utils.read_transcript(str(empty))
        assert result == []

    def test_read_transcript_nonexistent(self, tmp_path):
        """Missing file → []."""
        result = obsidian_utils.read_transcript(str(tmp_path / "no-such.jsonl"))
        assert result == []


# ===========================================================================
# Section 7: Matching
# ===========================================================================


class TestMatchItemsAgainstEvidence:
    def test_match_items_against_evidence_match(self, tmp_path):
        """Evidence with distinctive tokens matches the open item."""
        # Create a fake file for the item reference
        fake_file = str(tmp_path / "session-note.md")
        item_text = "implement login authentication handler"
        evidence = (
            "Implemented the login authentication handler for user sessions. "
            "The feature is complete and deployed."
        )
        open_items = [(fake_file, 10, item_text)]

        results = obsidian_utils.match_items_against_evidence(evidence, open_items)
        assert len(results) >= 1
        assert results[0]["confidence"] >= 3

    def test_match_items_against_evidence_no_match(self, tmp_path):
        """Completely dissimilar evidence → []."""
        fake_file = str(tmp_path / "session-note.md")
        item_text = "refactor the database migration scripts"
        evidence = "The UI was redesigned with a new color palette."
        open_items = [(fake_file, 5, item_text)]

        results = obsidian_utils.match_items_against_evidence(evidence, open_items)
        assert results == []

    def test_match_items_against_evidence_empty_evidence(self, tmp_path):
        """Empty/whitespace evidence → []."""
        fake_file = str(tmp_path / "session-note.md")
        open_items = [(fake_file, 1, "add unit tests for authentication")]

        assert obsidian_utils.match_items_against_evidence("", open_items) == []
        assert obsidian_utils.match_items_against_evidence("   ", open_items) == []


# ===========================================================================
# Section 8: File I/O
# ===========================================================================


class TestWriteVaultNote:
    def test_write_vault_note_creates_file(self, tmp_vault):
        """Write succeeds and content is correct."""
        content = "# Test Note\n\nHello, vault!\n"
        result = obsidian_utils.write_vault_note(
            str(tmp_vault), "claude-sessions", "test-note.md", content
        )
        assert result is None, f"expected None on success, got {result!r}"
        written = (tmp_vault / "claude-sessions" / "test-note.md").read_text(encoding="utf-8")
        assert written == content

    def test_write_vault_note_creates_dirs(self, tmp_vault):
        """Creates missing directories."""
        content = "# New Folder Note\n"
        result = obsidian_utils.write_vault_note(
            str(tmp_vault), "new-folder/sub-folder", "note.md", content
        )
        assert result is None, f"expected None on success, got {result!r}"
        assert (tmp_vault / "new-folder" / "sub-folder" / "note.md").exists()

    def test_write_vault_note_permissions(self, tmp_vault):
        """Written file has 0o600 permissions."""
        obsidian_utils.write_vault_note(
            str(tmp_vault), "claude-sessions", "perm-test.md", "content\n"
        )
        note_path = tmp_vault / "claude-sessions" / "perm-test.md"
        mode = oct(note_path.stat().st_mode & 0o777)
        assert mode == oct(0o600)

    def test_write_vault_note_returns_none_on_success(self, tmp_vault):
        """F2 contract: successful write returns None (not True)."""
        result = obsidian_utils.write_vault_note(
            str(tmp_vault), "claude-sessions", "f2-success.md", "# F2\n"
        )
        assert result is None, f"expected None on success, got {result!r}"

    def test_write_vault_note_returns_error_string_on_failure(self, tmp_vault):
        """F2 contract: path-traversal failure returns a non-empty error string (not False)."""
        result = obsidian_utils.write_vault_note(
            str(tmp_vault), "../../etc", "evil.md", "payload"
        )
        assert isinstance(result, str), (
            f"expected str error on failure, got {type(result).__name__}: {result!r}"
        )
        assert result, "error string must be non-empty"

    def test_write_vault_note_error_string_contains_details(self, tmp_vault):
        """F2 contract: error string for write failure includes the destination path."""
        # Make the sessions dir read-only so the write itself fails.
        sessions_dir = tmp_vault / "f2-fail-folder"
        sessions_dir.mkdir(parents=True)
        sessions_dir.chmod(0o500)
        try:
            result = obsidian_utils.write_vault_note(
                str(tmp_vault), "f2-fail-folder", "note.md", "content"
            )
            assert isinstance(result, str), (
                f"expected str error on write failure, got {type(result).__name__}: {result!r}"
            )
            assert result, "error string must be non-empty"
            # Verify the error string contains diagnostic details (folder or filename)
            assert "f2-fail-folder" in result or "note.md" in result, (
                f"error string must contain destination path details, got: {result!r}"
            )
        finally:
            sessions_dir.chmod(0o700)


# ===========================================================================
# Section 9: Upgrade pipeline
# ===========================================================================


class TestUpgradeNoteWithSummary:
    def test_upgrade_note_with_summary_valid(self, sample_unsummarized_note, tmp_vault, monkeypatch):
        """Valid summary with all sections — status flipped and content inserted."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        summary = (
            "## Summary\n"
            "Fixed the login bug and deployed to production.\n\n"
            "## Key Decisions\n"
            "- Used JWT for session management.\n\n"
            "## Changes Made\n"
            "- `src/auth.py` — new authentication handler\n\n"
            "## Errors Encountered\n"
            "None.\n\n"
            "## Open Questions / Next Steps\n"
            "- [ ] Add integration tests\n"
        )

        result = obsidian_utils.upgrade_note_with_summary(
            str(sample_unsummarized_note),
            summary,
            str(tmp_vault),
            "claude-sessions",
            "test-project",
        )

        assert result.startswith("Upgraded")
        content = sample_unsummarized_note.read_text(encoding="utf-8")
        assert "status: summarized" in content
        assert "## Summary" in content
        assert "Fixed the login bug" in content

    def test_upgrade_note_with_summary_malformed(self, sample_unsummarized_note, tmp_vault):
        """Summary without '## Summary' → starts with 'Failed:'."""
        bad_summary = "This summary has no proper sections.\n\nJust random text."

        result = obsidian_utils.upgrade_note_with_summary(
            str(sample_unsummarized_note),
            bad_summary,
            str(tmp_vault),
            "claude-sessions",
            "test-project",
        )
        assert result.startswith("Failed:")

    def test_upgrade_note_with_summary_no_frontmatter(self, tmp_vault, monkeypatch):
        """Note without --- frontmatter → starts with 'Failed:'."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        note = tmp_vault / "claude-sessions" / "no-frontmatter.md"
        note.write_text("# No frontmatter here\n\nJust content.\n", encoding="utf-8")

        valid_summary = (
            "## Summary\nSomething happened.\n\n"
            "## Key Decisions\nNone noted.\n\n"
            "## Changes Made\nNone noted.\n\n"
            "## Errors Encountered\nNone.\n\n"
            "## Open Questions / Next Steps\nNone.\n"
        )

        result = obsidian_utils.upgrade_note_with_summary(
            str(note),
            valid_summary,
            str(tmp_vault),
            "claude-sessions",
            "test-project",
        )
        assert result.startswith("Failed:")

    def test_upgrade_note_with_summary_post_write_detects_status_not_flipped(
        self, sample_unsummarized_note, tmp_vault, monkeypatch
    ):
        """Clobber with original (auto-logged) content — status-flip branch fires."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        summary = (
            "## Summary\n"
            "SIGNATURE_MARKER_FOR_STATUS_BRANCH landed successfully.\n\n"
            "## Key Decisions\nNone noted.\n\n"
            "## Changes Made\nNone noted.\n\n"
            "## Errors Encountered\nNone.\n\n"
            "## Open Questions / Next Steps\nNone.\n"
        )

        real_replace = os.replace
        note_path_str = str(sample_unsummarized_note)
        stale_content = sample_unsummarized_note.read_text(encoding="utf-8")
        assert "status: auto-logged" in stale_content  # sanity check fixture

        def clobbering_replace(src, dst, *args, **kwargs):
            real_replace(src, dst, *args, **kwargs)
            if str(dst) == note_path_str:
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(stale_content)

        monkeypatch.setattr(os, "replace", clobbering_replace)

        result = obsidian_utils.upgrade_note_with_summary(
            note_path_str, summary, str(tmp_vault), "claude-sessions", "test-project"
        )

        assert result.startswith("Failed:"), f"expected Failed:, got {result!r}"
        assert "status not flipped" in result, (
            f"expected status-branch message, got {result!r}"
        )

    def test_upgrade_note_with_summary_post_write_detects_body_missing(
        self, sample_unsummarized_note, tmp_vault, monkeypatch
    ):
        """Clobber preserves status: summarized but strips the body signature —
        signature branch MUST fire (regression guard for deleted body check)."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        summary = (
            "## Summary\n"
            "BODY_BRANCH_SIGNATURE_LINE that must appear on disk.\n\n"
            "## Key Decisions\nNone noted.\n\n"
            "## Changes Made\nNone noted.\n\n"
            "## Errors Encountered\nNone.\n\n"
            "## Open Questions / Next Steps\nNone.\n"
        )

        real_replace = os.replace
        note_path_str = str(sample_unsummarized_note)

        # Craft stale content that passes status check but lacks the signature.
        fake_summarized = (
            "---\n"
            "type: claude-session\n"
            "date: 2026-04-10\n"
            "project: test-project\n"
            "session_id: stale-session\n"
            "status: summarized\n"
            "---\n"
            "\n# Stale content\n\n## Summary\nDifferent prior summary body.\n"
        )

        def clobbering_replace(src, dst, *args, **kwargs):
            real_replace(src, dst, *args, **kwargs)
            if str(dst) == note_path_str:
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(fake_summarized)

        monkeypatch.setattr(os, "replace", clobbering_replace)

        result = obsidian_utils.upgrade_note_with_summary(
            note_path_str, summary, str(tmp_vault), "claude-sessions", "test-project"
        )

        assert result.startswith("Failed:"), f"expected Failed:, got {result!r}"
        assert "summary body missing" in result, (
            f"expected body-branch message, got {result!r}"
        )

    def test_upgrade_note_with_summary_body_check_scoped_to_summary_section(
        self, sample_unsummarized_note, tmp_vault, monkeypatch
    ):
        """Signature check must be scoped to ## Summary — if the signature
        appears only in a preserved audit trail (not in Summary body), the
        function must still return Failed. Regression guard for Copilot #1."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        summary = (
            "## Summary\n"
            "AUDIT_TRAIL_FALSE_POSITIVE_SIGNATURE test line.\n\n"
            "## Key Decisions\nNone noted.\n\n"
            "## Changes Made\nNone noted.\n\n"
            "## Errors Encountered\nNone.\n\n"
            "## Open Questions / Next Steps\nNone.\n"
        )

        real_replace = os.replace
        note_path_str = str(sample_unsummarized_note)

        # Craft stale content that:
        #   - passes the frontmatter status check (status: summarized)
        #   - has a ## Summary section with a DIFFERENT body
        #   - leaks the signature into an audit trail section
        # If the signature check were whole-file, it would pass here. It
        # must only pass when the signature is in the Summary block.
        fake_content = (
            "---\n"
            "type: claude-session\n"
            "date: 2026-04-10\n"
            "project: test-project\n"
            "session_id: stale-session\n"
            "status: summarized\n"
            "---\n"
            "\n# Stale content\n\n"
            "## Summary\nThis is a different body that does not contain the signature.\n\n"
            "## Tool Usage\n"
            "- Message: AUDIT_TRAIL_FALSE_POSITIVE_SIGNATURE test line.\n"
        )

        def clobbering_replace(src, dst, *args, **kwargs):
            real_replace(src, dst, *args, **kwargs)
            if str(dst) == note_path_str:
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(fake_content)

        monkeypatch.setattr(os, "replace", clobbering_replace)

        result = obsidian_utils.upgrade_note_with_summary(
            note_path_str, summary, str(tmp_vault), "claude-sessions", "test-project"
        )

        assert result.startswith("Failed:"), f"expected Failed:, got {result!r}"
        assert "summary body missing" in result, (
            f"expected body-branch message, got {result!r}"
        )

    def test_upgrade_note_with_summary_tab_separated_next_section_breaks_cleanly(
        self, sample_unsummarized_note, tmp_vault, monkeypatch
    ):
        """Pre-write: an empty Summary body followed by `##\\tKey Decisions`
        (tab separator, not space) must still be recognized as the next
        top-level section so the loop breaks and the malformed-body check
        fires. Regression guard for Copilot #6 round 7 (level-2 heading
        must match any whitespace, not just space)."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        # Empty Summary body, next section uses a TAB after `##`.
        tab_summary = (
            "## Summary\n\n"
            "##\tKey Decisions\nNone noted.\n\n"
            "## Changes Made\nNone noted.\n\n"
            "## Errors Encountered\nNone.\n\n"
            "## Open Questions / Next Steps\nNone.\n"
        )

        result = obsidian_utils.upgrade_note_with_summary(
            str(sample_unsummarized_note),
            tab_summary,
            str(tmp_vault),
            "claude-sessions",
            "test-project",
        )

        # Must fail malformed — if the break condition missed the tab,
        # the loop would pick up "Key Decisions" text as the signature
        # and proceed to an Upgraded state with an empty real Summary.
        assert result.startswith("Failed:"), f"expected Failed:, got {result!r}"
        assert "malformed summary" in result.lower()
        assert "empty or heading-only" in result.lower()

    def test_upgrade_note_with_summary_post_write_tab_separated_section_boundary(
        self, sample_unsummarized_note, tmp_vault, monkeypatch
    ):
        """Post-write: the Summary block extraction regex must terminate
        at `##\\tKey Decisions` (tab separator), not swallow the next
        section. If it failed to terminate, a signature that lives ONLY
        in the next tabbed section would false-positive the body check.

        The clobber moves the signature out of the Summary body and into
        the tabbed Key Decisions section — so:
          - correct regex termination → Summary block is empty of signature
            → Failed: summary body missing
          - buggy regex (swallows the tabbed section) → Summary block
            contains the signature → phantom Upgraded

        Regression guard for Copilot round 7 #2 (lookahead must allow any
        whitespace after the `##` delimiter) AND round 8 (the round-7 test
        was branch-confused because it only checked the happy path)."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        summary = (
            "## Summary\n"
            "TAB_BOUNDARY_SIGNATURE content line.\n\n"
            "## Key Decisions\nNone noted.\n\n"
            "## Changes Made\nNone noted.\n\n"
            "## Errors Encountered\nNone.\n\n"
            "## Open Questions / Next Steps\nNone.\n"
        )

        real_replace = os.replace
        note_path_str = str(sample_unsummarized_note)

        # Stale content: valid YAML frontmatter with status: summarized,
        # an empty Summary body, and the signature hiding in a tab-
        # separated next section. If the extraction regex terminates
        # properly at `##\t`, the Summary block will be empty (no signature)
        # and the function must return Failed. If it fails to terminate,
        # it swallows the Key Decisions section and false-positives.
        clobber_content = (
            "---\n"
            "type: claude-session\n"
            "date: 2026-04-10\n"
            "project: test-project\n"
            "session_id: stale-session\n"
            "status: summarized\n"
            "---\n"
            "\n# Clobbered\n\n"
            "## Summary\n\n"
            "##\tKey Decisions\n"
            "- TAB_BOUNDARY_SIGNATURE content line.\n"
        )

        def clobbering_replace(src, dst, *args, **kwargs):
            real_replace(src, dst, *args, **kwargs)
            if str(dst) == note_path_str:
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(clobber_content)

        monkeypatch.setattr(os, "replace", clobbering_replace)

        result = obsidian_utils.upgrade_note_with_summary(
            note_path_str, summary, str(tmp_vault), "claude-sessions", "test-project"
        )

        assert result.startswith("Failed:"), f"expected Failed:, got {result!r}"
        assert "summary body missing" in result, (
            f"expected body-branch message, got {result!r}"
        )

    def test_upgrade_note_with_summary_hash_prefixed_content_is_not_a_heading(
        self, sample_unsummarized_note, tmp_vault, monkeypatch
    ):
        """A line starting with `#` followed by non-whitespace (e.g. `#1234`
        or `#hashtag`) is legitimate Markdown content, not an ATX heading,
        and must be accepted as the signature. Regression guard for
        Copilot #6 (strict ATX heading detection)."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        # First real content line starts with `#` but is not an ATX heading.
        summary = (
            "## Summary\n"
            "#1234 issue reference — fixed the auth bug.\n\n"
            "## Key Decisions\nNone noted.\n\n"
            "## Changes Made\nNone noted.\n\n"
            "## Errors Encountered\nNone.\n\n"
            "## Open Questions / Next Steps\nNone.\n"
        )

        result = obsidian_utils.upgrade_note_with_summary(
            str(sample_unsummarized_note),
            summary,
            str(tmp_vault),
            "claude-sessions",
            "test-project",
        )

        assert result.startswith("Upgraded"), (
            f"expected Upgraded, got {result!r} — hash-prefixed content line "
            f"was incorrectly classified as a heading"
        )
        disk_content = sample_unsummarized_note.read_text(encoding="utf-8")
        assert "status: summarized" in disk_content
        assert "#1234 issue reference — fixed the auth bug." in disk_content

    def test_upgrade_note_with_summary_skips_sub_headings_inside_summary(
        self, sample_unsummarized_note, tmp_vault, monkeypatch
    ):
        """A legitimate ATX sub-heading like `### Context` inside the Summary
        block should be skipped, and the next real content line becomes the
        signature. Pins the "sub-heading != break" branch of the new logic."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        summary = (
            "## Summary\n"
            "### Context\n"
            "The real signature is on this line.\n\n"
            "## Key Decisions\nNone noted.\n\n"
            "## Changes Made\nNone noted.\n\n"
            "## Errors Encountered\nNone.\n\n"
            "## Open Questions / Next Steps\nNone.\n"
        )

        result = obsidian_utils.upgrade_note_with_summary(
            str(sample_unsummarized_note),
            summary,
            str(tmp_vault),
            "claude-sessions",
            "test-project",
        )

        assert result.startswith("Upgraded"), f"expected Upgraded, got {result!r}"
        disk_content = sample_unsummarized_note.read_text(encoding="utf-8")
        assert "The real signature is on this line." in disk_content

    def test_upgrade_note_with_summary_body_check_is_line_granular(
        self, sample_unsummarized_note, tmp_vault, monkeypatch
    ):
        """Signature must match as a full stripped line in the Summary block,
        not as a substring of another line. Regression guard for Copilot #4."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        # Signature is "Short sig." — could false-positive as a substring
        # of "Before: Short sig. After: also changed." if the check used
        # a substring match instead of line equality.
        summary = (
            "## Summary\n"
            "Short sig.\n\n"
            "## Key Decisions\nNone noted.\n\n"
            "## Changes Made\nNone noted.\n\n"
            "## Errors Encountered\nNone.\n\n"
            "## Open Questions / Next Steps\nNone.\n"
        )

        real_replace = os.replace
        note_path_str = str(sample_unsummarized_note)

        # Stale content has `## Summary` with a line that CONTAINS "Short sig."
        # as a substring but is not equal to it. Under a substring check
        # this false-positives; under line-granularity it must fail.
        fake_content = (
            "---\n"
            "type: claude-session\n"
            "date: 2026-04-10\n"
            "project: test-project\n"
            "session_id: stale-session\n"
            "status: summarized\n"
            "---\n"
            "\n# Stale content\n\n"
            "## Summary\nBefore: Short sig. After: something else entirely.\n"
        )

        def clobbering_replace(src, dst, *args, **kwargs):
            real_replace(src, dst, *args, **kwargs)
            if str(dst) == note_path_str:
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(fake_content)

        monkeypatch.setattr(os, "replace", clobbering_replace)

        result = obsidian_utils.upgrade_note_with_summary(
            note_path_str, summary, str(tmp_vault), "claude-sessions", "test-project"
        )

        assert result.startswith("Failed:"), f"expected Failed:, got {result!r}"
        assert "summary body missing" in result, (
            f"expected line-granular miss, got {result!r}"
        )

    def test_upgrade_note_with_summary_accepts_bare_filename(
        self, sample_unsummarized_note, tmp_vault, monkeypatch
    ):
        """Bare filename (no directory component) must not crash tempfile.mkstemp.
        Regression guard for Copilot #3 latent bug (low-confidence suppressed)."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())
        # cd into the note's parent so a bare filename resolves correctly.
        monkeypatch.chdir(sample_unsummarized_note.parent)

        summary = (
            "## Summary\n"
            "BARE_FILENAME_SIGNATURE content line.\n\n"
            "## Key Decisions\nNone noted.\n\n"
            "## Changes Made\nNone noted.\n\n"
            "## Errors Encountered\nNone.\n\n"
            "## Open Questions / Next Steps\nNone.\n"
        )

        result = obsidian_utils.upgrade_note_with_summary(
            sample_unsummarized_note.name,  # bare filename, no directory
            summary,
            str(tmp_vault),
            "claude-sessions",
            "test-project",
        )

        assert result.startswith("Upgraded"), f"expected Upgraded, got {result!r}"
        disk_content = sample_unsummarized_note.read_text(encoding="utf-8")
        assert "status: summarized" in disk_content
        assert "BARE_FILENAME_SIGNATURE content line." in disk_content

    def test_upgrade_note_with_summary_frontmatter_anchored_to_file_start(
        self, sample_unsummarized_note, tmp_vault, monkeypatch
    ):
        """Clobber writes content with no YAML frontmatter but with a Markdown
        horizontal rule `---` and `status: summarized` in the body. The
        frontmatter scope check must fail because the opening `---` is not
        anchored to the start of the file. Regression guard for Copilot #3."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        summary = (
            "## Summary\n"
            "FRONTMATTER_ANCHOR_SIGNATURE content line.\n\n"
            "## Key Decisions\nNone noted.\n\n"
            "## Changes Made\nNone noted.\n\n"
            "## Errors Encountered\nNone.\n\n"
            "## Open Questions / Next Steps\nNone.\n"
        )

        real_replace = os.replace
        note_path_str = str(sample_unsummarized_note)

        # No YAML frontmatter at start — just a title, a Markdown HR, and
        # a line that SAYS status: summarized in the body text. If the
        # frontmatter detector was not start-anchored, it would scoop the
        # HR as "frontmatter" and find the status line within it.
        fake_content = (
            "# Stale session with no frontmatter\n"
            "\n"
            "Some narrative text here.\n"
            "\n"
            "---\n"
            "status: summarized\n"
            "(this is a body line that LOOKS like frontmatter)\n"
            "---\n"
            "\n"
            "## Summary\nFRONTMATTER_ANCHOR_SIGNATURE content line.\n"
        )

        def clobbering_replace(src, dst, *args, **kwargs):
            real_replace(src, dst, *args, **kwargs)
            if str(dst) == note_path_str:
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(fake_content)

        monkeypatch.setattr(os, "replace", clobbering_replace)

        result = obsidian_utils.upgrade_note_with_summary(
            note_path_str, summary, str(tmp_vault), "claude-sessions", "test-project"
        )

        assert result.startswith("Failed:"), f"expected Failed:, got {result!r}"
        assert "YAML frontmatter not found at start" in result, (
            f"expected anchored-frontmatter message, got {result!r}"
        )

    def test_upgrade_note_with_summary_rejects_empty_body(
        self, sample_unsummarized_note, tmp_vault, monkeypatch
    ):
        """Summary with header but no body content fails loudly (not phantom OK)."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        empty_body_summary = (
            "## Summary\n\n"
            "## Key Decisions\nNone noted.\n\n"
            "## Changes Made\nNone noted.\n\n"
            "## Errors Encountered\nNone.\n\n"
            "## Open Questions / Next Steps\nNone.\n"
        )

        result = obsidian_utils.upgrade_note_with_summary(
            str(sample_unsummarized_note),
            empty_body_summary,
            str(tmp_vault),
            "claude-sessions",
            "test-project",
        )

        assert result.startswith("Failed:"), f"expected Failed:, got {result!r}"
        assert "malformed summary" in result.lower()
        assert "empty or heading-only" in result.lower()

    def test_upgrade_note_with_summary_post_write_read_failure(
        self, sample_unsummarized_note, tmp_vault, monkeypatch
    ):
        """Post-write re-read raising OSError → Failed (not phantom success)."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        summary = (
            "## Summary\n"
            "POST_READ_FAILURE_SIGNATURE content line.\n\n"
            "## Key Decisions\nNone noted.\n\n"
            "## Changes Made\nNone noted.\n\n"
            "## Errors Encountered\nNone.\n\n"
            "## Open Questions / Next Steps\nNone.\n"
        )

        import builtins
        real_open = builtins.open
        note_path_str = str(sample_unsummarized_note)
        replace_done = {"flag": False}
        real_replace = os.replace

        def flagging_replace(src, dst, *args, **kwargs):
            real_replace(src, dst, *args, **kwargs)
            if str(dst) == note_path_str:
                replace_done["flag"] = True

        def failing_open(path, mode="r", *args, **kwargs):
            if (
                replace_done["flag"]
                and str(path) == note_path_str
                and "r" in mode
                and "w" not in mode
            ):
                raise OSError("simulated post-write read failure")
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(os, "replace", flagging_replace)
        monkeypatch.setattr(builtins, "open", failing_open)

        result = obsidian_utils.upgrade_note_with_summary(
            note_path_str, summary, str(tmp_vault), "claude-sessions", "test-project"
        )

        assert result.startswith("Failed:"), f"expected Failed:, got {result!r}"
        assert "post-write read verification failed" in result

    def test_upgrade_note_with_summary_persists_to_disk(
        self, sample_unsummarized_note, tmp_vault, monkeypatch
    ):
        """Happy path: the summary signature is readable from disk after return."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        summary = (
            "## Summary\n"
            "UNIQUE_POST_WRITE_CHECK_PHRASE landed on disk successfully.\n\n"
            "## Key Decisions\nNone noted.\n\n"
            "## Changes Made\nNone noted.\n\n"
            "## Errors Encountered\nNone.\n\n"
            "## Open Questions / Next Steps\nNone.\n"
        )

        result = obsidian_utils.upgrade_note_with_summary(
            str(sample_unsummarized_note),
            summary,
            str(tmp_vault),
            "claude-sessions",
            "test-project",
        )

        assert result.startswith("Upgraded")
        # Re-read from disk (not cached text).
        disk_content = sample_unsummarized_note.read_text(encoding="utf-8")
        assert "status: summarized" in disk_content
        assert "UNIQUE_POST_WRITE_CHECK_PHRASE landed on disk successfully." in disk_content


# ===========================================================================
# Section 10: Prepare summary input
# ===========================================================================


class TestPrepareSummaryInput:
    def test_prepare_summary_input_no_session_id(self, tmp_path):
        """Note without session_id → 'NO_CONTENT:...'."""
        note = tmp_path / "no-session-id.md"
        note.write_text(
            "---\n"
            "type: claude-session\n"
            "date: 2026-04-10\n"
            "project: test-project\n"
            "status: auto-logged\n"
            "---\n\n"
            "# Session\n\n## Summary\nSomething.\n",
            encoding="utf-8",
        )
        result = obsidian_utils.prepare_summary_input(str(note))
        assert result.startswith("NO_CONTENT:")

    def test_prepare_summary_input_no_jsonl(self, tmp_path, monkeypatch):
        """Has session_id, JSONL not found → 'RAW_OK:...'."""
        note = tmp_path / "with-session-id.md"
        note.write_text(
            "---\n"
            "type: claude-session\n"
            "date: 2026-04-10\n"
            "session_id: fake-session-no-jsonl\n"
            "project: test-project\n"
            "status: auto-logged\n"
            "---\n\n"
            "# Session\n\n## Summary\nSomething.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda sid: None)
        result = obsidian_utils.prepare_summary_input(str(note))
        assert result.startswith("RAW_OK:")

    def test_prepare_summary_input_read_error(self, tmp_path):
        """Nonexistent file → 'NO_CONTENT:...'."""
        result = obsidian_utils.prepare_summary_input(str(tmp_path / "ghost.md"))
        assert result.startswith("NO_CONTENT:")


# ===========================================================================
# Section 11: Sampling logic (mock-based)
# ===========================================================================


class TestGenerateSummarySampling:
    """Test message sampling and truncation inside generate_summary()."""

    def _fake_run_factory(self, captured: dict):
        """Return a fake subprocess.run that captures its input."""
        def fake_run(cmd, **kwargs):
            captured["prompt"] = kwargs.get("input", "")
            return type(
                "Result",
                (),
                {"returncode": 0, "stdout": "## Summary\nDone.\n", "stderr": ""},
            )()
        return fake_run

    def test_generate_summary_sampling_under_20(self, monkeypatch):
        """15 messages — no '[... middle messages omitted ...]' marker."""
        captured: dict = {}
        monkeypatch.setattr("subprocess.run", self._fake_run_factory(captured))

        user_msgs = [f"user message {i}" for i in range(15)]
        assistant_msgs = [f"assistant response {i}" for i in range(15)]
        metadata = {"project": "test", "git_branch": "main", "duration_minutes": 5, "files_touched": []}

        obsidian_utils.generate_summary(user_msgs, assistant_msgs, metadata)

        assert captured.get("prompt") is not None
        assert "[... middle messages omitted ...]" not in captured["prompt"]
        # All messages should appear
        assert "user message 0" in captured["prompt"]
        assert "user message 14" in captured["prompt"]

    def test_generate_summary_sampling_over_20(self, monkeypatch):
        """30 messages — marker present, first/last present, middle absent."""
        captured: dict = {}
        monkeypatch.setattr("subprocess.run", self._fake_run_factory(captured))

        user_msgs = [f"user message {i}" for i in range(30)]
        assistant_msgs = [f"assistant response {i}" for i in range(30)]
        metadata = {"project": "test", "git_branch": "main", "duration_minutes": 10, "files_touched": []}

        obsidian_utils.generate_summary(user_msgs, assistant_msgs, metadata)

        prompt = captured.get("prompt", "")
        assert "[... middle messages omitted ...]" in prompt
        # First and last 10 present
        assert "user message 0" in prompt
        assert "user message 29" in prompt
        # Middle absent
        assert "user message 15" not in prompt

    def test_generate_summary_truncation_12k(self, monkeypatch):
        """15 messages of 1000 chars each — total prompt stays bounded."""
        captured: dict = {}
        monkeypatch.setattr("subprocess.run", self._fake_run_factory(captured))

        user_msgs = ["u" * 1000 for _ in range(15)]
        assistant_msgs = ["a" * 1000 for _ in range(15)]
        metadata = {"project": "test", "git_branch": "main", "duration_minutes": 5, "files_touched": []}

        obsidian_utils.generate_summary(user_msgs, assistant_msgs, metadata)

        prompt = captured.get("prompt", "")
        # 15 msgs × 1000 chars + separators ≤ 12000 for user + 12000 for assistant + overhead
        # The join is truncated at 12000 each, so total user+assistant ≤ 24000
        assert len(prompt) < 30000  # generous upper bound; key check is it's bounded


class TestSummarizerTimeoutBudget:
    """#84 — claude -p first-attempt timeout raised 30s→120s for slow-start CC builds."""

    def _capture_timeout_run(self, captured: dict):
        def fake_run(cmd, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return type("Result", (), {"returncode": 0, "stdout": "## Summary\nDone.\n", "stderr": ""})()
        return fake_run

    def test_generate_summary_first_attempt_timeout_is_120(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(obsidian_utils.subprocess, "run", self._capture_timeout_run(captured))
        obsidian_utils.generate_summary(["u"], ["a"], {"project": "t", "files_touched": []})
        assert captured.get("timeout") == 120

    def test_generate_snapshot_summary_first_attempt_timeout_is_120(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(obsidian_utils.subprocess, "run", self._capture_timeout_run(captured))
        obsidian_utils.generate_snapshot_summary(["u"], ["a"], {"project": "t"})
        assert captured.get("timeout") == 120

    def test_generate_summary_retry_uses_double_timeout(self, monkeypatch):
        """Both attempts must time out; second attempt must use timeout * 2 == 240."""
        seen = []

        def fake_run(cmd, **kwargs):
            seen.append(kwargs.get("timeout"))
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

        monkeypatch.setattr(obsidian_utils.subprocess, "run", fake_run)
        result = obsidian_utils.generate_summary(["u"], ["a"], {"project": "t", "files_touched": []})
        assert seen == [120, 240]
        assert result == (None, "haiku_timeout")


def test_summary_pipeline_default_is_auto():
    assert obsidian_utils._DEFAULTS.get("summary_pipeline") == "auto"


# ===========================================================================
# Section 7: build_context_brief — sort order and duration
# ===========================================================================

def _make_session_note(path, project, date, branch, duration, summary, mtime=None):
    """Helper: write a minimal session note and optionally set its mtime."""
    content = f"""---
type: claude-session
date: {date}
project: {project}
git_branch: {branch}
duration_minutes: {duration}
status: summarized
---

# Session: {project} ({branch})

## Summary
{summary}
"""
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class TestBuildContextBriefSort:
    """Verify hybrid sort (date desc, mtime desc within same date) and duration column."""

    def test_same_day_sorted_by_mtime(self, tmp_path, monkeypatch):
        """Sessions from the same day should sort by mtime descending, not filename."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        sessions = tmp_path / "sessions"
        insights = tmp_path / "insights"
        sessions.mkdir()
        insights.mkdir()

        # Create two notes from the same day — 'aaaa' is alphabetically first
        # but should sort SECOND because its mtime is older.
        _make_session_note(
            sessions / "2026-04-10-proj-aaaa.md",
            "proj", "2026-04-10", "main", 30, "First created session.", mtime=1000,
        )
        _make_session_note(
            sessions / "2026-04-10-proj-zzzz.md",
            "proj", "2026-04-10", "main", 60, "Second created session.", mtime=2000,
        )

        output = obsidian_utils.build_context_brief(
            str(tmp_path), "sessions", "insights", "proj",
        )

        # The more recent mtime (zzzz) should appear first in the table
        zzzz_pos = output.find("Second created session.")
        aaaa_pos = output.find("First created session.")
        assert zzzz_pos < aaaa_pos, "mtime-newer session should appear first"

    def test_different_days_sorted_by_date(self, tmp_path, monkeypatch):
        """Older-date session should not float up even if its mtime is newer."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        sessions = tmp_path / "sessions"
        insights = tmp_path / "insights"
        sessions.mkdir()
        insights.mkdir()

        # April 5 note has a NEWER mtime than April 10 note
        _make_session_note(
            sessions / "2026-04-05-proj-aaaa.md",
            "proj", "2026-04-05", "main", 10, "Older date session.", mtime=9999,
        )
        _make_session_note(
            sessions / "2026-04-10-proj-bbbb.md",
            "proj", "2026-04-10", "main", 20, "Newer date session.", mtime=1000,
        )

        output = obsidian_utils.build_context_brief(
            str(tmp_path), "sessions", "insights", "proj",
        )

        newer_pos = output.find("Newer date session.")
        older_pos = output.find("Older date session.")
        assert newer_pos < older_pos, "date descending should take priority over mtime"

    def test_duration_format_hours_minutes(self, tmp_path, monkeypatch):
        """Duration >= 60 min should display as Xh Ym."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        sessions = tmp_path / "sessions"
        insights = tmp_path / "insights"
        sessions.mkdir()
        insights.mkdir()

        _make_session_note(
            sessions / "2026-04-10-proj-aaaa.md",
            "proj", "2026-04-10", "main", 80.3, "Long session.",
        )

        output = obsidian_utils.build_context_brief(
            str(tmp_path), "sessions", "insights", "proj",
        )

        assert "| 1h 20m |" in output

    def test_duration_format_minutes_only(self, tmp_path, monkeypatch):
        """Duration < 60 min should display as Xm."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        sessions = tmp_path / "sessions"
        insights = tmp_path / "insights"
        sessions.mkdir()
        insights.mkdir()

        _make_session_note(
            sessions / "2026-04-10-proj-aaaa.md",
            "proj", "2026-04-10", "main", 27, "Short session.",
        )

        output = obsidian_utils.build_context_brief(
            str(tmp_path), "sessions", "insights", "proj",
        )

        assert "| 27m |" in output

    def test_duration_format_zero(self, tmp_path, monkeypatch):
        """Duration 0 should produce empty string in column."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        sessions = tmp_path / "sessions"
        insights = tmp_path / "insights"
        sessions.mkdir()
        insights.mkdir()

        _make_session_note(
            sessions / "2026-04-10-proj-aaaa.md",
            "proj", "2026-04-10", "main", 0, "No duration.",
        )

        output = obsidian_utils.build_context_brief(
            str(tmp_path), "sessions", "insights", "proj",
        )

        assert "|  |" in output or "| |" in output

    def test_session_number_column(self, tmp_path, monkeypatch):
        """Each row should have a sequential number in the first column."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        sessions = tmp_path / "sessions"
        insights = tmp_path / "insights"
        sessions.mkdir()
        insights.mkdir()

        _make_session_note(
            sessions / "2026-04-10-proj-aaaa.md",
            "proj", "2026-04-10", "main", 10, "First.", mtime=2000,
        )
        _make_session_note(
            sessions / "2026-04-10-proj-bbbb.md",
            "proj", "2026-04-10", "main", 20, "Second.", mtime=1000,
        )

        output = obsidian_utils.build_context_brief(
            str(tmp_path), "sessions", "insights", "proj",
        )

        rows = [l for l in output.split("\n") if l.startswith("| ") and l[2:3].isdigit()]
        assert len(rows) == 2
        assert rows[0].startswith("| 1 |")
        assert rows[1].startswith("| 2 |")

    def test_stat_failure_does_not_crash(self, tmp_path, monkeypatch):
        """A broken symlink in the sessions dir should not crash build_context_brief."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        sessions = tmp_path / "sessions"
        insights = tmp_path / "insights"
        sessions.mkdir()
        insights.mkdir()

        # Create a valid note
        _make_session_note(
            sessions / "2026-04-10-proj-aaaa.md",
            "proj", "2026-04-10", "main", 15, "Valid session.",
        )
        # Create a broken symlink (.md suffix so it passes the filter)
        broken = sessions / "2026-04-10-proj-broken.md"
        broken.symlink_to(tmp_path / "nonexistent-target.md")

        output = obsidian_utils.build_context_brief(
            str(tmp_path), "sessions", "insights", "proj",
        )

        # Should still produce output with the valid session
        assert "Valid session." in output

    def test_duration_boundary_60_minutes(self, tmp_path, monkeypatch):
        """Duration of exactly 60 min should display as 1h 0m."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        sessions = tmp_path / "sessions"
        insights = tmp_path / "insights"
        sessions.mkdir()
        insights.mkdir()

        _make_session_note(
            sessions / "2026-04-10-proj-aaaa.md",
            "proj", "2026-04-10", "main", 60, "Boundary session.",
        )

        output = obsidian_utils.build_context_brief(
            str(tmp_path), "sessions", "insights", "proj",
        )

        assert "| 1h 0m |" in output


def test_get_session_id_fast_rejects_stale_bootstrap(tmp_path, monkeypatch):
    """Fast path must fall through to slow path when a newer JSONL exists."""
    import obsidian_utils
    import os
    import time

    # Fake ~/.claude/projects/<project>/ with two JSONL files
    project_basename = "fake-proj-abc"
    cc_projects = tmp_path / ".claude" / "projects" / f"-foo-{project_basename}"
    cc_projects.mkdir(parents=True)

    old_jsonl = cc_projects / "old-sid-0000.jsonl"
    new_jsonl = cc_projects / "new-sid-9999.jsonl"
    old_jsonl.write_text("{}", encoding="utf-8")
    new_jsonl.write_text("{}", encoding="utf-8")
    os.utime(old_jsonl, (time.time() - 7200, time.time() - 7200))
    os.utime(new_jsonl, (time.time() - 60, time.time() - 60))

    proj_dir = tmp_path / project_basename
    proj_dir.mkdir()
    monkeypatch.chdir(proj_dir)
    monkeypatch.setenv("HOME", str(tmp_path))

    bootstrap = tmp_path / f".obsidian-brain-sid-{project_basename}"
    bootstrap.write_text("old-sid-0000", encoding="utf-8")
    os.utime(bootstrap, (time.time() - 3600, time.time() - 3600))

    monkeypatch.setattr(obsidian_utils, "_BOOTSTRAP_PREFIX", str(tmp_path / ".obsidian-brain-sid-"))

    result = obsidian_utils._get_session_id_fast()
    assert result == "new-sid-9999", f"expected newest sid, got {result}"


def test_get_session_id_fast_trusts_fresh_bootstrap(tmp_path, monkeypatch):
    """Fast path must return bootstrap sid when bootstrap is newer than all JSONLs."""
    import obsidian_utils
    import os
    import time

    project_basename = "fresh-proj-xyz"
    cc_projects = tmp_path / ".claude" / "projects" / f"-foo-{project_basename}"
    cc_projects.mkdir(parents=True)

    jsonl = cc_projects / "fresh-sid-1234.jsonl"
    jsonl.write_text("{}", encoding="utf-8")
    os.utime(jsonl, (time.time() - 3600, time.time() - 3600))

    proj_dir = tmp_path / project_basename
    proj_dir.mkdir()
    monkeypatch.chdir(proj_dir)
    monkeypatch.setenv("HOME", str(tmp_path))

    bootstrap = tmp_path / f".obsidian-brain-sid-{project_basename}"
    bootstrap.write_text("fresh-sid-1234", encoding="utf-8")
    os.utime(bootstrap, (time.time() - 60, time.time() - 60))

    monkeypatch.setattr(obsidian_utils, "_BOOTSTRAP_PREFIX", str(tmp_path / ".obsidian-brain-sid-"))

    result = obsidian_utils._get_session_id_fast()
    assert result == "fresh-sid-1234"


def test_get_session_id_fast_invalidates_when_cached_jsonl_deleted(tmp_path, monkeypatch):
    """Bootstrap points at a sid whose JSONL has been removed — slow path picks newest survivor."""
    import obsidian_utils
    import os
    import time

    project_basename = "deleted-proj"
    cc_projects = tmp_path / ".claude" / "projects" / f"-foo-{project_basename}"
    cc_projects.mkdir(parents=True)

    # Create only the "surviving" JSONL; the cached one in the bootstrap doesn't exist on disk
    survivor = cc_projects / "survivor-sid-ffff.jsonl"
    survivor.write_text("{}", encoding="utf-8")
    os.utime(survivor, (time.time() - 60, time.time() - 60))

    proj_dir = tmp_path / project_basename
    proj_dir.mkdir()
    monkeypatch.chdir(proj_dir)
    monkeypatch.setenv("HOME", str(tmp_path))

    bootstrap = tmp_path / f".obsidian-brain-sid-{project_basename}"
    bootstrap.write_text("deleted-sid-0000", encoding="utf-8")

    monkeypatch.setattr(
        obsidian_utils, "_BOOTSTRAP_PREFIX", str(tmp_path / ".obsidian-brain-sid-")
    )

    result = obsidian_utils._get_session_id_fast()
    assert result == "survivor-sid-ffff", (
        f"expected fast path to fall through and return newest survivor, got {result}"
    )


def test_check_hook_status_matches(tmp_path, monkeypatch):
    """check_hook_status returns ok=True when bootstrap matches current sid."""
    import obsidian_utils
    import os

    project_basename = "stat-proj"
    cc_projects = tmp_path / ".claude" / "projects" / f"-foo-{project_basename}"
    cc_projects.mkdir(parents=True)
    jsonl = cc_projects / "live-sid-1111.jsonl"
    jsonl.write_text("{}", encoding="utf-8")

    proj_dir = tmp_path / project_basename
    proj_dir.mkdir()
    monkeypatch.chdir(proj_dir)
    monkeypatch.setenv("HOME", str(tmp_path))

    bootstrap_prefix = str(tmp_path / ".obsidian-brain-sid-")
    monkeypatch.setattr(obsidian_utils, "_BOOTSTRAP_PREFIX", bootstrap_prefix)
    bootstrap = tmp_path / f".obsidian-brain-sid-{project_basename}"
    bootstrap.write_text("live-sid-1111", encoding="utf-8")
    # Make bootstrap newer than the JSONL so the fast path trusts it
    import time
    os.utime(jsonl, (time.time() - 3600, time.time() - 3600))
    os.utime(bootstrap, (time.time() - 60, time.time() - 60))

    status = obsidian_utils.check_hook_status()
    assert status["ok"] is True
    assert status["bootstrap_sid"] == "live-sid-1111"
    assert status["current_sid"] == "live-sid-1111"


def test_check_hook_status_sid_mismatch_is_ok(tmp_path, monkeypatch):
    """SID mismatch (e.g. after reconnect) is ok=True when bootstrap exists."""
    import obsidian_utils
    import os, time

    project_basename = "mismatch-proj"
    cc_projects = tmp_path / ".claude" / "projects" / f"-foo-{project_basename}"
    cc_projects.mkdir(parents=True)
    # Two JSONLs: old one matching bootstrap, newer one for current session
    old_jsonl = cc_projects / "old-sid-aaaa.jsonl"
    old_jsonl.write_text("{}", encoding="utf-8")
    new_jsonl = cc_projects / "new-sid-bbbb.jsonl"
    new_jsonl.write_text("{}", encoding="utf-8")
    os.utime(old_jsonl, (time.time() - 3600, time.time() - 3600))
    os.utime(new_jsonl, (time.time() - 10, time.time() - 10))

    proj_dir = tmp_path / project_basename
    proj_dir.mkdir()
    monkeypatch.chdir(proj_dir)
    monkeypatch.setenv("HOME", str(tmp_path))

    bootstrap_prefix = str(tmp_path / ".obsidian-brain-sid-")
    monkeypatch.setattr(obsidian_utils, "_BOOTSTRAP_PREFIX", bootstrap_prefix)
    bootstrap = tmp_path / f".obsidian-brain-sid-{project_basename}"
    bootstrap.write_text("old-sid-aaaa", encoding="utf-8")

    status = obsidian_utils.check_hook_status()
    assert status["ok"] is True
    assert status["current_sid"] == "new-sid-bbbb"
    assert status["bootstrap_sid"] == "old-sid-aaaa"
    assert "resumed session" in status["message"]


def test_check_hook_status_no_session_files(tmp_path, monkeypatch):
    """check_hook_status returns ok=False when bootstrap exists but no JSONLs."""
    import obsidian_utils

    project_basename = "no-sessions-proj"
    # CC projects dir exists but has NO .jsonl files
    cc_projects = tmp_path / ".claude" / "projects" / f"-foo-{project_basename}"
    cc_projects.mkdir(parents=True)

    proj_dir = tmp_path / project_basename
    proj_dir.mkdir()
    monkeypatch.chdir(proj_dir)
    monkeypatch.setenv("HOME", str(tmp_path))

    bootstrap_prefix = str(tmp_path / ".obsidian-brain-sid-")
    monkeypatch.setattr(obsidian_utils, "_BOOTSTRAP_PREFIX", bootstrap_prefix)
    bootstrap = tmp_path / f".obsidian-brain-sid-{project_basename}"
    bootstrap.write_text("some-old-sid", encoding="utf-8")

    status = obsidian_utils.check_hook_status()
    assert status["ok"] is False
    assert "No session files found" in status["message"] or "not be active" in status["message"]
    assert status["bootstrap_sid"] == "some-old-sid"
    assert status["current_sid"] == "unknown"


def test_slow_path_underscore_to_hyphen_fallback(tmp_path, monkeypatch):
    """_slow_path_newest_sid matches when cwd has underscores but CC dir has hyphens."""
    import obsidian_utils

    # cwd basename has underscores
    proj_dir = tmp_path / "personal_ws"
    proj_dir.mkdir()
    monkeypatch.chdir(proj_dir)
    monkeypatch.setenv("HOME", str(tmp_path))

    # Claude Code normalizes underscores to hyphens in project dir names
    cc_projects = tmp_path / ".claude" / "projects" / "-Users-foo-personal-ws"
    cc_projects.mkdir(parents=True)
    jsonl = cc_projects / "abc123.jsonl"
    jsonl.write_text("{}", encoding="utf-8")

    sid = obsidian_utils._slow_path_newest_sid()
    assert sid == "abc123", f"Expected 'abc123' but got '{sid}' — hyphen fallback failed"


def test_fast_path_underscore_to_hyphen_fallback(tmp_path, monkeypatch):
    """_get_session_id_fast matches when cwd has underscores but CC dir has hyphens."""
    import obsidian_utils

    proj_dir = tmp_path / "my_project"
    proj_dir.mkdir()
    monkeypatch.chdir(proj_dir)
    monkeypatch.setenv("HOME", str(tmp_path))

    # CC dir uses hyphens
    cc_projects = tmp_path / ".claude" / "projects" / "-Users-foo-my-project"
    cc_projects.mkdir(parents=True)
    jsonl = cc_projects / "sess-fast-123.jsonl"
    jsonl.write_text("{}", encoding="utf-8")

    # Bootstrap file points to the correct sid
    bootstrap_prefix = str(tmp_path / ".obsidian-brain-sid-")
    monkeypatch.setattr(obsidian_utils, "_BOOTSTRAP_PREFIX", bootstrap_prefix)
    bootstrap = tmp_path / ".obsidian-brain-sid-my_project"
    bootstrap.write_text("sess-fast-123", encoding="utf-8")

    sid = obsidian_utils._get_session_id_fast()
    assert sid == "sess-fast-123", f"Expected 'sess-fast-123' but got '{sid}'"


def test_get_session_context_normalizes_underscores(tmp_path, monkeypatch):
    """get_session_context normalizes underscores to hyphens in project name."""
    import obsidian_utils

    proj_dir = tmp_path / "personal_ws"
    proj_dir.mkdir()
    monkeypatch.chdir(proj_dir)
    monkeypatch.setenv("HOME", str(tmp_path))

    # Stub _get_session_id_fast to return unknown (avoids bootstrap setup)
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: "unknown")

    ctx = obsidian_utils.get_session_context()
    assert ctx["project"] == "personal-ws", (
        f"Expected 'personal-ws' but got '{ctx['project']}' — underscores not normalized"
    )


def test_extract_session_metadata_normalizes_underscores():
    """extract_session_metadata normalizes underscores to hyphens in project name."""
    import obsidian_utils

    meta = obsidian_utils.extract_session_metadata([], "/Users/foo/personal_ws")
    assert meta["project"] == "personal-ws", (
        f"Expected 'personal-ws' but got '{meta['project']}' — underscores not normalized"
    )


def test_extract_session_metadata_normalizes_case_and_spaces():
    """extract_session_metadata lowercases and normalizes spaces like get_session_context."""
    import obsidian_utils

    meta = obsidian_utils.extract_session_metadata([], "/Users/foo/My Project")
    assert meta["project"] == "my-project", (
        f"Expected 'my-project' but got '{meta['project']}' — case/space normalization failed"
    )


def test_check_hook_status_missing_bootstrap(tmp_path, monkeypatch):
    """check_hook_status returns ok=False when bootstrap file is absent."""
    import obsidian_utils

    project_basename = "missing-proj"
    cc_projects = tmp_path / ".claude" / "projects" / f"-foo-{project_basename}"
    cc_projects.mkdir(parents=True)
    (cc_projects / "sid-xxxx.jsonl").write_text("{}", encoding="utf-8")

    proj_dir = tmp_path / project_basename
    proj_dir.mkdir()
    monkeypatch.chdir(proj_dir)
    monkeypatch.setenv("HOME", str(tmp_path))

    monkeypatch.setattr(
        obsidian_utils, "_BOOTSTRAP_PREFIX", str(tmp_path / ".obsidian-brain-sid-")
    )

    status = obsidian_utils.check_hook_status()
    assert status["ok"] is False
    assert "not be active" in status["message"]


def test_build_context_brief_prepends_hook_status(tmp_path):
    """build_context_brief prepends the hook_status_line when provided."""
    import obsidian_utils

    vault = tmp_path / "vault"
    (vault / "sessions").mkdir(parents=True)
    (vault / "insights").mkdir(parents=True)

    status_line = "[OK] Session logging active"
    output = obsidian_utils.build_context_brief(
        str(vault),
        "sessions",
        "insights",
        "nonexistent-project",
        hook_status_line=status_line,
    )

    # Extract the CONTEXT_BRIEF section and verify the first content line
    # is the status line, appearing before the "## Project Context" header.
    assert "<<<OB_CONTEXT_BRIEF>>>" in output
    brief_section = output.split("<<<OB_CONTEXT_BRIEF>>>", 1)[1].split("<<<OB_LOAD_MANIFEST>>>", 1)[0]
    brief_lines = [ln for ln in brief_section.split("\n") if ln.strip()]
    assert brief_lines[0] == status_line
    # Header should still exist after the status line
    assert any(ln.startswith("## Project Context") for ln in brief_lines)
    # Ensure the status line appears BEFORE the header
    status_idx = brief_lines.index(status_line)
    header_idx = next(i for i, ln in enumerate(brief_lines) if ln.startswith("## Project Context"))
    assert status_idx < header_idx


def test_build_context_brief_without_hook_status(tmp_path):
    """build_context_brief omits the status line when not provided (default)."""
    import obsidian_utils

    vault = tmp_path / "vault"
    (vault / "sessions").mkdir(parents=True)
    (vault / "insights").mkdir(parents=True)

    output = obsidian_utils.build_context_brief(
        str(vault),
        "sessions",
        "insights",
        "nonexistent-project",
    )

    brief_section = output.split("<<<OB_CONTEXT_BRIEF>>>", 1)[1].split("<<<OB_LOAD_MANIFEST>>>", 1)[0]
    brief_lines = [ln for ln in brief_section.split("\n") if ln.strip()]
    # First non-empty line should be the Project Context header, not a status line
    assert brief_lines[0].startswith("## Project Context")


def test_get_session_id_fast_same_second_tiebreaker(tmp_path, monkeypatch):
    """Same-second mtime ties: cached sid wins when its JSONL is tied for newest."""
    import obsidian_utils
    import os
    import time

    project_basename = "tie-proj"
    cc_projects = tmp_path / ".claude" / "projects" / f"-foo-{project_basename}"
    cc_projects.mkdir(parents=True)

    # Two JSONLs with IDENTICAL mtimes
    now = time.time()
    old_jsonl = cc_projects / "aaa-previous.jsonl"
    current_jsonl = cc_projects / "zzz-current.jsonl"
    old_jsonl.write_text("{}", encoding="utf-8")
    current_jsonl.write_text("{}", encoding="utf-8")
    os.utime(old_jsonl, (now, now))
    os.utime(current_jsonl, (now, now))

    proj_dir = tmp_path / project_basename
    proj_dir.mkdir()
    monkeypatch.chdir(proj_dir)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(obsidian_utils, "_BOOTSTRAP_PREFIX", str(tmp_path / ".obsidian-brain-sid-"))

    # Bootstrap claims "aaa-previous" is current. Because path-sort tiebreak
    # would otherwise pick "zzz-current" (lexicographically larger) as the
    # newest, the cached sid must win via the same-mtime tie-breaker.
    bootstrap = tmp_path / f".obsidian-brain-sid-{project_basename}"
    bootstrap.write_text("aaa-previous", encoding="utf-8")

    result = obsidian_utils._get_session_id_fast()
    assert result == "aaa-previous", (
        f"expected cached sid to win same-mtime tie, got {result}"
    )


def test_get_session_id_fast_multiple_cached_matches_tiebreak(tmp_path, monkeypatch):
    """When multiple project dirs contain the cached sid's JSONL, at least one
    must tie the newest mtime for the cache to be trusted."""
    import obsidian_utils
    import os
    import time

    project_basename = "multi-proj"
    # Two project-dir variants (like worktrees)
    dir_a = tmp_path / ".claude" / "projects" / f"-alpha-{project_basename}"
    dir_b = tmp_path / ".claude" / "projects" / f"-beta-{project_basename}"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    cached_sid = "shared-sid-1234"
    # Put the cached sid's jsonl in BOTH project dirs
    a_jsonl = dir_a / f"{cached_sid}.jsonl"
    b_jsonl = dir_b / f"{cached_sid}.jsonl"
    other_jsonl = dir_b / "other-sid-5678.jsonl"
    a_jsonl.write_text("{}", encoding="utf-8")
    b_jsonl.write_text("{}", encoding="utf-8")
    other_jsonl.write_text("{}", encoding="utf-8")

    # Scenario: a_jsonl is OLDER, b_jsonl matches newest mtime, other_jsonl
    # is also at newest mtime. Tiebreaker MUST trust the cache because
    # at least one cached match (b_jsonl) ties newest mtime.
    now = time.time()
    os.utime(a_jsonl, (now - 3600, now - 3600))  # old
    os.utime(b_jsonl, (now, now))  # tied with other
    os.utime(other_jsonl, (now, now))  # tied with b

    proj_dir = tmp_path / project_basename
    proj_dir.mkdir()
    monkeypatch.chdir(proj_dir)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(obsidian_utils, "_BOOTSTRAP_PREFIX", str(tmp_path / ".obsidian-brain-sid-"))

    bootstrap = tmp_path / f".obsidian-brain-sid-{project_basename}"
    bootstrap.write_text(cached_sid, encoding="utf-8")

    result = obsidian_utils._get_session_id_fast()
    assert result == cached_sid, (
        f"expected cached sid to win via multi-match tiebreaker, got {result}"
    )


def test_get_session_id_fast_slow_path_is_readonly(tmp_path, monkeypatch):
    """Slow path must NOT write to the bootstrap file.

    Regression test for the SessionStart-hook race: the hook writes the
    authoritative sid, then downstream hook code can trigger
    _get_session_id_fast() before CC has flushed the new session's JSONL.
    In that window, the cached_pattern glob misses and the slow path fires.
    If the slow path writes back to the bootstrap, it clobbers the hook's
    authoritative write with a stale result.
    """
    import obsidian_utils
    import os
    import time

    project_basename = "readonly-proj"
    cc_projects = tmp_path / ".claude" / "projects" / f"-foo-{project_basename}"
    cc_projects.mkdir(parents=True)

    # Previous session's JSONL exists (what the hook race would find as 'newest')
    old_jsonl = cc_projects / "old-sid-0000.jsonl"
    old_jsonl.write_text("{}", encoding="utf-8")
    os.utime(old_jsonl, (time.time() - 600, time.time() - 600))

    # New session's JSONL does NOT exist yet — this is the race window
    # (CC hasn't flushed it yet when the hook fires)

    proj_dir = tmp_path / project_basename
    proj_dir.mkdir()
    monkeypatch.chdir(proj_dir)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(obsidian_utils, "_BOOTSTRAP_PREFIX", str(tmp_path / ".obsidian-brain-sid-"))

    # Bootstrap contains the NEW authoritative sid (just written by the hook)
    bootstrap = tmp_path / f".obsidian-brain-sid-{project_basename}"
    bootstrap.write_text("new-sid-9999", encoding="utf-8")
    bootstrap_mtime_before = os.path.getmtime(bootstrap)
    bootstrap_contents_before = bootstrap.read_text(encoding="utf-8").strip()

    # Trigger _get_session_id_fast: cached JSONL doesn't exist yet, fall
    # through to slow path which finds old-sid-0000 as newest.
    result = obsidian_utils._get_session_id_fast()

    # The function may return either value — the return value is not what
    # we're testing. What matters: the bootstrap file MUST NOT be clobbered.
    bootstrap_contents_after = bootstrap.read_text(encoding="utf-8").strip()
    assert bootstrap_contents_after == bootstrap_contents_before, (
        f"slow path clobbered the bootstrap: before={bootstrap_contents_before!r} "
        f"after={bootstrap_contents_after!r}"
    )
    # And the mtime must be unchanged
    assert os.path.getmtime(bootstrap) == bootstrap_mtime_before


def test_get_session_id_fast_slow_path_returns_without_writing(tmp_path, monkeypatch):
    """When no bootstrap exists, slow path returns newest sid but creates no bootstrap file."""
    import obsidian_utils
    import os
    import time

    project_basename = "nobootstrap-proj"
    cc_projects = tmp_path / ".claude" / "projects" / f"-foo-{project_basename}"
    cc_projects.mkdir(parents=True)

    only_jsonl = cc_projects / "only-sid-abcd.jsonl"
    only_jsonl.write_text("{}", encoding="utf-8")
    os.utime(only_jsonl, (time.time() - 60, time.time() - 60))

    proj_dir = tmp_path / project_basename
    proj_dir.mkdir()
    monkeypatch.chdir(proj_dir)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(obsidian_utils, "_BOOTSTRAP_PREFIX", str(tmp_path / ".obsidian-brain-sid-"))

    bootstrap = tmp_path / f".obsidian-brain-sid-{project_basename}"
    assert not bootstrap.exists()

    result = obsidian_utils._get_session_id_fast()
    assert result == "only-sid-abcd"

    # Slow path must NOT have created a bootstrap file
    assert not bootstrap.exists(), (
        "slow path should be read-only and not create the bootstrap file"
    )


# ===========================================================================
# Section: upgrade_batch — concurrent wrapper around upgrade_unsummarized_note
# ===========================================================================


class TestUpgradeBatch:
    """Tests for upgrade_batch() — GH #69, instrumentation GH #74.

    `upgrade_unsummarized_note` is monkeypatched with fast fakes so none of
    these tests touch the filesystem, load a vault config, or shell out to
    `claude -p`. That isolation also means the `vault_path`/`sessions_folder`
    args are inert placeholder strings.

    upgrade_batch() now returns list[dict] with keys: path, status, elapsed_s,
    model_used, fallback_reason.
    """

    def test_upgrade_batch_empty_list_returns_empty(self, monkeypatch, tmp_path):
        """Empty input: return [] immediately and never invoke the wrapped fn or the sink."""
        calls = []

        def fake_impl(*args, **kwargs):
            calls.append(args)
            return "ok"

        monkeypatch.setattr(obsidian_utils, "upgrade_unsummarized_note", fake_impl)

        metrics_path = tmp_path / "test-metrics.jsonl"
        import summarizer_metrics
        monkeypatch.setattr(summarizer_metrics, "METRICS_PATH", metrics_path)

        result = obsidian_utils.upgrade_batch(
            [], "/vault", "sessions", "proj",
        )
        assert result == []
        assert calls == []
        assert not metrics_path.exists(), "empty batch must not write a sink record"

    def test_upgrade_batch_n1(self, monkeypatch):
        """Single path: single dict with path and status returned."""
        def fake_impl(note_path, *args, **kwargs):
            return f"Summarized: {os.path.basename(note_path)}", 0.1, "haiku", None

        monkeypatch.setattr(obsidian_utils, "upgrade_unsummarized_note", fake_impl)

        result = obsidian_utils.upgrade_batch(
            ["/vault/sessions/one.md"], "/vault", "sessions", "proj",
            summary_batch_size=1,
        )
        assert len(result) == 1
        assert result[0]["path"] == "/vault/sessions/one.md"
        assert result[0]["status"] == "Summarized: one.md"

    def test_upgrade_batch_n5_preserves_input_order(self, monkeypatch):
        """5 paths with variable sleeps: return order matches input, not completion order."""
        import time as _time

        # Force inverted completion order: last input completes first.
        sleeps = {
            "a.md": 0.15,
            "b.md": 0.12,
            "c.md": 0.09,
            "d.md": 0.06,
            "e.md": 0.03,
        }

        def fake_impl(note_path, *args, **kwargs):
            _time.sleep(sleeps[os.path.basename(note_path)])
            return f"done:{os.path.basename(note_path)}", 0.1, "haiku", None

        monkeypatch.setattr(obsidian_utils, "upgrade_unsummarized_note", fake_impl)

        paths = [f"/vault/sessions/{name}" for name in ("a.md", "b.md", "c.md", "d.md", "e.md")]
        result = obsidian_utils.upgrade_batch(paths, "/vault", "sessions", "proj",
                                              summary_batch_size=1)

        assert len(result) == 5
        assert [r["path"] for r in result] == paths, "input order must be preserved"
        assert [r["status"] for r in result] == [
            "done:a.md", "done:b.md", "done:c.md", "done:d.md", "done:e.md",
        ]

    def test_upgrade_batch_n10_runs_in_parallel(self, monkeypatch):
        """N=10 workers must all enter fake_impl concurrently.

        Concurrency is verified via threading.Barrier rather than wall-time
        bounds to avoid CI flakes; the Barrier times out with
        BrokenBarrierError if the executor serializes.
        """
        import threading

        N = 10
        barrier = threading.Barrier(N, timeout=5.0)  # generous margin for CI

        def fake_impl(note_path, *args, **kwargs):
            barrier.wait()  # raises BrokenBarrierError if < N threads ever gather
            return f"Upgraded {os.path.basename(note_path)} (source: JSONL)", 0.1, "haiku-4.5", None

        monkeypatch.setattr(obsidian_utils, "upgrade_unsummarized_note", fake_impl)
        paths = [f"/vault/sessions/note{i}.md" for i in range(N)]
        results = obsidian_utils.upgrade_batch(
            paths, "/vault", "sessions", "proj", max_workers=N,
            summary_batch_size=1,
        )
        assert len(results) == N
        assert all(r["status"].startswith("Upgraded ") for r in results)

    def test_upgrade_batch_captures_exceptions_per_note(self, monkeypatch):
        """One raising impl does not kill the batch; failure becomes a status string."""
        def fake_impl(note_path, *args, **kwargs):
            if os.path.basename(note_path) == "three.md":
                raise RuntimeError("boom")
            return f"ok:{os.path.basename(note_path)}", 0.1, "haiku-4.5", None

        monkeypatch.setattr(obsidian_utils, "upgrade_unsummarized_note", fake_impl)

        paths = [f"/vault/sessions/{n}" for n in
                 ("one.md", "two.md", "three.md", "four.md", "five.md")]
        result = obsidian_utils.upgrade_batch(paths, "/vault", "sessions", "proj",
                                              summary_batch_size=1)

        assert len(result) == 5
        assert [r["path"] for r in result] == paths
        assert result[0]["status"] == "ok:one.md"
        assert result[1]["status"] == "ok:two.md"
        assert result[2]["path"] == "/vault/sessions/three.md"
        assert result[2]["status"] == "Failed: RuntimeError: boom"
        assert result[3]["status"] == "ok:four.md"
        assert result[4]["status"] == "ok:five.md"

    def test_upgrade_batch_max_workers_caps_at_input_size(self, monkeypatch):
        """N=3 < max_workers=10: min() guard still yields real parallelism.

        Concurrency is verified via threading.Barrier rather than wall-time
        bounds to avoid CI flakes; all 3 threads must gather at the barrier
        simultaneously or BrokenBarrierError fails the test.
        """
        import threading

        N = 3
        barrier = threading.Barrier(N, timeout=5.0)

        def fake_impl(note_path, *args, **kwargs):
            barrier.wait()
            return f"Upgraded {os.path.basename(note_path)} (source: JSONL)", 0.1, "haiku-4.5", None

        monkeypatch.setattr(obsidian_utils, "upgrade_unsummarized_note", fake_impl)
        paths = [f"/vault/sessions/a{i}.md" for i in range(N)]
        results = obsidian_utils.upgrade_batch(
            paths, "/vault", "sessions", "proj", max_workers=10,
            summary_batch_size=1,
        )
        assert len(results) == N
        assert all(r["status"].startswith("Upgraded ") for r in results)

    def test_upgrade_batch_forwards_model_and_timeout(self, monkeypatch):
        received = {}

        def fake_impl(note_path, vault_path, sessions_folder, project,
                      summary_model, summary_timeout):
            received["model"] = summary_model
            received["timeout"] = summary_timeout
            return f"ok:{os.path.basename(note_path)}", 0.1, "haiku-4.5", None

        monkeypatch.setattr(obsidian_utils, "upgrade_unsummarized_note", fake_impl)
        results = obsidian_utils.upgrade_batch(
            ["/vault/sessions/a.md"],
            "/vault",
            "sessions",
            "proj",
            summary_model="claude-3-5-haiku",
            summary_timeout=30,
            summary_batch_size=1,
        )
        assert len(results) == 1
        assert results[0]["path"] == "/vault/sessions/a.md"
        assert results[0]["status"] == "ok:a.md"
        assert received == {"model": "claude-3-5-haiku", "timeout": 30}

    def test_upgrade_batch_rejects_invalid_max_workers(self, monkeypatch):
        monkeypatch.setattr(
            obsidian_utils, "upgrade_unsummarized_note",
            lambda *a, **k: ("Upgraded test.md (source: ...)", 0.1, "haiku-4.5", None),
        )
        for bad in (0, -1, -100):
            with pytest.raises(ValueError, match="max_workers must be >= 1"):
                obsidian_utils.upgrade_batch(
                    ["/vault/sessions/a.md"], "/vault", "sessions", "proj",
                    max_workers=bad,
                    summary_batch_size=1,
                )

    def test_upgrade_batch_skill_md_json_round_trip(self, monkeypatch):
        """Verifies that path and status are accessible by dict key.

        The shape has migrated from (path, status) tuples to list[dict].
        Serialization using the new dict-key access pattern is verified here;
        SKILL.md Step 2 will be updated in Task 9 to match.
        """
        import json

        monkeypatch.setattr(
            obsidian_utils, "upgrade_unsummarized_note",
            lambda note_path, *a, **k: (f"Upgraded {os.path.basename(note_path)} (source: JSONL)", 0.1, "haiku-4.5", None),
        )
        paths = [
            "/vault/sessions/a.md",
            "/vault/sessions/b.md",
            "/vault/sessions/c.md",
        ]
        results = obsidian_utils.upgrade_batch(paths, "/vault", "sessions", "proj",
                                               summary_batch_size=1)

        # Serialize using dict-key access (new shape)
        serialized = json.dumps([{"path": r["path"], "status": r["status"]} for r in results])
        parsed = json.loads(serialized)
        assert parsed == [
            {"path": "/vault/sessions/a.md", "status": "Upgraded a.md (source: JSONL)"},
            {"path": "/vault/sessions/b.md", "status": "Upgraded b.md (source: JSONL)"},
            {"path": "/vault/sessions/c.md", "status": "Upgraded c.md (source: JSONL)"},
        ]

    def test_upgrade_batch_real_integration_catches_signature_drift(
        self, monkeypatch, tmp_path
    ):
        """Drive the real upgrade_unsummarized_note through upgrade_batch.

        Monkeypatches only generate_summary (the claude -p shell-out) and
        find_transcript_jsonl (forced to None so the raw-note fallback
        runs deterministically). The rest of the pipeline runs for real.
        If upgrade_unsummarized_note ever gains a required positional
        parameter that upgrade_batch doesn't forward, this test fails —
        unit tests using *args/**kwargs fakes would silently pass.
        """
        vault = tmp_path / "vault"
        sessions_dir = vault / "sessions"
        sessions_dir.mkdir(parents=True)

        note_path = sessions_dir / "2026-04-20-integration-test-abcd.md"
        note_path.write_text(
            "---\n"
            "type: claude-session\n"
            "date: 2026-04-20\n"
            "session_id: integration-test-session-id-0000-0000-000000000000\n"
            "project: test-project\n"
            "status: auto-logged\n"
            "tags:\n"
            "  - claude/session\n"
            "  - claude/project/test-project\n"
            "---\n\n"
            "# Session: test-project\n\n"
            "## Conversation (raw)\n"
            "**User:** hello\n"
            "**Assistant:** hi there\n"
        )

        def fake_generate_summary(*args, **kwargs):
            return (
                "## Summary\nDid a thing.\n\n"
                "## Key Decisions\n- None noted.\n\n"
                "## Changes Made\n- None noted.\n\n"
                "## Errors Encountered\n- None.\n\n"
                "## Open Questions / Next Steps\n- [ ] None.\n\n"
                "IMPORTANCE: 5\n"
            ), None

        monkeypatch.setattr(
            obsidian_utils, "generate_summary", fake_generate_summary
        )
        # Force raw-note fallback so we don't accidentally pick up a JSONL
        # from the developer's running CC environment.
        monkeypatch.setattr(
            obsidian_utils, "find_transcript_jsonl", lambda session_id: None
        )

        results = obsidian_utils.upgrade_batch(
            [str(note_path)], str(vault), "sessions", "test-project",
            summary_batch_size=1,
        )

        assert len(results) == 1
        assert set(results[0].keys()) == {"path", "status", "elapsed_s", "model_used", "fallback_reason"}
        assert results[0]["path"] == str(note_path)
        assert results[0]["status"].startswith("Upgraded "), f"expected success, got: {results[0]['status']}"

        # Verify the note was actually rewritten with a summary
        updated = note_path.read_text()
        assert "status: summarized" in updated
        assert "## Summary" in updated
        assert "Did a thing." in updated

    def test_upgrade_unsummarized_note_default_timeout_reaches_generate_summary(
        self, monkeypatch, tmp_path
    ):
        """When summary_timeout=None (the default), generate_summary must be called WITHOUT
        a timeout kwarg so its own default of 120 applies. This pins the propagation
        path upgrade_unsummarized_note(summary_timeout=None) → gen_kwargs has no
        'timeout' key → generate_summary uses its default parameter (120).

        Approach: monkeypatch generate_summary to capture kwargs; drive one real
        note through upgrade_unsummarized_note without passing summary_timeout.
        Assert 'timeout' is absent from captured kwargs (so the 120 default applies).
        """
        vault = tmp_path / "vault"
        sessions_dir = vault / "sessions"
        sessions_dir.mkdir(parents=True)

        note_path = sessions_dir / "2026-04-20-default-timeout-test-abcd.md"
        note_path.write_text(
            "---\n"
            "type: claude-session\n"
            "date: 2026-04-20\n"
            "session_id: default-timeout-session-id-0000-0000-000000000000\n"
            "project: test-project\n"
            "status: auto-logged\n"
            "tags:\n"
            "  - claude/session\n"
            "---\n\n"
            "## Conversation (raw)\n"
            "**User:** hello\n"
            "**Assistant:** hi there\n",
            encoding="utf-8",
        )

        captured_gen_kwargs: dict = {}

        def fake_generate_summary(*args, **kwargs):
            captured_gen_kwargs.update(kwargs)
            return (
                "## Summary\nDid a thing.\n\n"
                "## Key Decisions\n- None noted.\n\n"
                "## Changes Made\n- None noted.\n\n"
                "## Errors Encountered\n- None.\n\n"
                "## Open Questions / Next Steps\n- [ ] None.\n\n"
                "IMPORTANCE: 5\n"
            ), None

        monkeypatch.setattr(obsidian_utils, "generate_summary", fake_generate_summary)
        monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda session_id: None)

        # Call without summary_timeout — default None path
        status, elapsed_s, model_used, fallback_reason = obsidian_utils.upgrade_unsummarized_note(
            str(note_path),
            str(vault),
            "sessions",
            "test-project",
        )

        assert status.startswith("Upgraded "), f"expected Upgraded, got: {status!r}"
        # 'timeout' must NOT be in kwargs: the default (120) should come from
        # generate_summary's own parameter default, not a forwarded value.
        assert "timeout" not in captured_gen_kwargs, (
            f"expected no 'timeout' kwarg when summary_timeout=None, "
            f"but got kwargs={captured_gen_kwargs!r}"
        )


# ===========================================================================
# Section N-1: TestUpgradeBatchBatching — GH #166 batched upgrade_batch path
# ===========================================================================


class TestUpgradeBatchBatching:
    """Tests for the batched (summary_batch_size >= 2) path in upgrade_batch().

    Exercises generate_summaries_batch integration, fallthrough to the solo
    path, snapshot routing, and output ordering. Monkeypatches the subprocess
    so no ``claude -p`` calls are made.
    """

    # ---- helpers ----------------------------------------------------------------

    def _write_session_note(self, sessions_dir: Path, name: str, session_id: str | None = None) -> Path:
        """Write a minimal real session note to ``sessions_dir``."""
        sid = session_id or f"test-batch-session-{name.replace('.md', '')}-0000-0000-000000000000"
        note = sessions_dir / name
        note.write_text(
            "---\n"
            "type: claude-session\n"
            "date: 2026-04-20\n"
            f"session_id: {sid}\n"
            "project: test-project\n"
            "status: auto-logged\n"
            "tags:\n"
            "  - claude/session\n"
            "  - claude/project/test-project\n"
            "---\n\n"
            "# Session: test-project\n\n"
            "## Conversation (raw)\n"
            "**User:** describe what you did\n"
            "**Assistant:** I implemented the feature.\n",
            encoding="utf-8",
        )
        return note

    def _write_snapshot_note(self, sessions_dir: Path, name: str) -> Path:
        """Write a minimal snapshot note to ``sessions_dir``."""
        sid = f"test-snap-session-{name.replace('.md', '')}-0000-0000-000000000000"
        note = sessions_dir / name
        note.write_text(
            "---\n"
            "type: claude-snapshot\n"
            "date: 2026-04-20\n"
            f"session_id: {sid}\n"
            "project: test-project\n"
            "status: auto-logged\n"
            "tags:\n"
            "  - claude/snapshot\n"
            "---\n\n"
            "## Last messages (raw)\n"
            "**User:** context\n"
            "**Assistant:** snapshot content\n",
            encoding="utf-8",
        )
        return note

    def _fake_good_summary(self) -> str:
        return (
            "## Summary\nDid a thing.\n\n"
            "## Key Decisions\n- None noted.\n\n"
            "## Changes Made\n- None noted.\n\n"
            "## Errors Encountered\n- None.\n\n"
            "## Open Questions / Next Steps\n- [ ] None.\n\n"
            "## Importance\n5\n"
        )

    # ---- tests ------------------------------------------------------------------

    def test_batch_of_5_one_malformed_routes_to_fallback(self, monkeypatch, tmp_path):
        """Mandated fixture: 4 valid summaries + 1 missing_section → malformed goes to solo fallback."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        note_names = [f"note{i}.md" for i in range(5)]
        paths = [str(self._write_session_note(sessions_dir, name)) for name in note_names]

        monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda sid: None)

        batch_calls: list[list[dict]] = []
        # Return good summaries for notes 0-3, missing_section for note 4.
        def fake_generate_summaries_batch(prepared_notes, **kwargs):
            batch_calls.append(prepared_notes)
            results = []
            for i, _ in enumerate(prepared_notes):
                if i == len(prepared_notes) - 1:
                    results.append((None, "missing_section"))
                else:
                    results.append((self._fake_good_summary(), None))
            return results

        monkeypatch.setattr(obsidian_utils, "generate_summaries_batch", fake_generate_summaries_batch)

        # upgrade_note_with_summary returns success for all batch-written notes
        def fake_upgrade_note_with_summary(path, summary, *args, **kwargs):
            return f"Upgraded {os.path.basename(path)} (source: batch)"

        monkeypatch.setattr(obsidian_utils, "upgrade_note_with_summary", fake_upgrade_note_with_summary)

        # Solo fallback records which paths it was called with
        solo_fallback_called: list[str] = []
        def fake_upgrade_unsummarized_note(path, *args, **kwargs):
            solo_fallback_called.append(path)
            return f"Upgraded {os.path.basename(path)} (source: solo-fallback)", 0.1, "haiku", None

        monkeypatch.setattr(obsidian_utils, "upgrade_unsummarized_note", fake_upgrade_unsummarized_note)

        results = obsidian_utils.upgrade_batch(
            paths, str(tmp_path), "sessions", "test-project",
            summary_batch_size=5,
        )

        # 5 results in input order
        assert len(results) == 5
        assert [r["path"] for r in results] == paths

        # All 5 have the expected keys
        for r in results:
            assert set(r.keys()) == {"path", "status", "elapsed_s", "model_used", "fallback_reason"}

        # First 4 upgraded via batch path
        for i in range(4):
            assert results[i]["status"].startswith("Upgraded "), (
                f"note {i} should be Upgraded, got: {results[i]['status']!r}"
            )
            assert results[i]["model_used"] == "haiku"

        # Note 4 (malformed) routed to solo fallback
        assert paths[4] in solo_fallback_called, (
            f"malformed note {paths[4]!r} should have gone to solo fallback; "
            f"solo_fallback_called={solo_fallback_called!r}"
        )
        assert results[4]["status"].startswith("Upgraded "), (
            f"solo fallback note should still succeed, got: {results[4]['status']!r}"
        )

    def test_batch_size_1_uses_solo_path(self, monkeypatch, tmp_path):
        """batch_size=1: legacy fan-out fires for every note; generate_summaries_batch never called."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        paths = [str(self._write_session_note(sessions_dir, f"note{i}.md")) for i in range(3)]

        monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda sid: None)

        def batch_must_not_be_called(*args, **kwargs):
            raise AssertionError("generate_summaries_batch must NOT be called with batch_size=1")

        monkeypatch.setattr(obsidian_utils, "generate_summaries_batch", batch_must_not_be_called)

        solo_calls: list[str] = []
        def fake_solo(path, *args, **kwargs):
            solo_calls.append(path)
            return f"Upgraded {os.path.basename(path)} (source: solo)", 0.1, "haiku", None

        monkeypatch.setattr(obsidian_utils, "upgrade_unsummarized_note", fake_solo)

        results = obsidian_utils.upgrade_batch(
            paths, str(tmp_path), "sessions", "test-project",
            summary_batch_size=1,
        )

        assert len(results) == 3
        # Each path was processed by solo
        assert sorted(solo_calls) == sorted(paths)

    def test_whole_spawn_timeout_falls_back_to_solo(self, monkeypatch, tmp_path):
        """Whole-spawn timeout: every note routed to solo fallback."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        n = 3
        paths = [str(self._write_session_note(sessions_dir, f"t{i}.md")) for i in range(n)]

        monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda sid: None)

        def fake_batch_timeout(prepared_notes, **kwargs):
            return [(None, "haiku_timeout")] * len(prepared_notes)

        monkeypatch.setattr(obsidian_utils, "generate_summaries_batch", fake_batch_timeout)

        solo_calls: list[str] = []
        def fake_solo(path, *args, **kwargs):
            solo_calls.append(path)
            return f"Upgraded {os.path.basename(path)} (source: solo-fallback)", 0.2, "haiku", None

        monkeypatch.setattr(obsidian_utils, "upgrade_unsummarized_note", fake_solo)

        results = obsidian_utils.upgrade_batch(
            paths, str(tmp_path), "sessions", "test-project",
            summary_batch_size=5,
        )

        assert len(results) == n
        assert [r["path"] for r in results] == paths
        assert sorted(solo_calls) == sorted(paths), (
            f"all notes should go to solo fallback on timeout; got: {solo_calls!r}"
        )
        for r in results:
            assert r["status"].startswith("Upgraded "), (
                f"solo fallback should produce Upgraded, got: {r['status']!r}"
            )

    def test_results_in_input_order_mixed_session_and_snapshot(self, monkeypatch, tmp_path):
        """Mixed session + snapshot notes: snapshot goes to solo, order preserved."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        session_a = self._write_session_note(sessions_dir, "session-a.md")
        snapshot_b = self._write_snapshot_note(sessions_dir, "snapshot-b.md")
        session_c = self._write_session_note(sessions_dir, "session-c.md")
        paths = [str(session_a), str(snapshot_b), str(session_c)]

        monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda sid: None)

        batch_paths_seen: list[str] = []
        def fake_batch(prepared_notes, **kwargs):
            for p in prepared_notes:
                # We record via metadata to know which note was batched.
                batch_paths_seen.append(p.get("metadata", {}).get("project", "?"))
            return [(self._fake_good_summary(), None)] * len(prepared_notes)

        monkeypatch.setattr(obsidian_utils, "generate_summaries_batch", fake_batch)

        def fake_upgrade_note_with_summary(path, summary, *args, **kwargs):
            return f"Upgraded {os.path.basename(path)} (source: batch)"

        monkeypatch.setattr(obsidian_utils, "upgrade_note_with_summary", fake_upgrade_note_with_summary)

        solo_calls: list[str] = []
        def fake_solo(path, *args, **kwargs):
            solo_calls.append(path)
            return f"Upgraded {os.path.basename(path)} (source: solo)", 0.1, "haiku", None

        monkeypatch.setattr(obsidian_utils, "upgrade_unsummarized_note", fake_solo)

        results = obsidian_utils.upgrade_batch(
            paths, str(tmp_path), "sessions", "test-project",
            summary_batch_size=5,
        )

        # Correct count and order preserved
        assert len(results) == 3
        assert [r["path"] for r in results] == paths

        # Snapshot went to solo
        assert str(snapshot_b) in solo_calls, (
            f"snapshot note should route to solo path; solo_calls={solo_calls!r}"
        )
        # Session notes NOT in solo (they went batch)
        assert str(session_a) not in solo_calls
        assert str(session_c) not in solo_calls

        # All have Upgraded status
        for r in results:
            assert r["status"].startswith("Upgraded "), (
                f"all notes should be Upgraded, got: {r['status']!r}"
            )

    def test_generate_summaries_batch_parses_delimited_output(self, monkeypatch, tmp_path):
        """Unit-test the parser: well-formed block 1, missing ## Summary block 2."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda sid: None)

        # Two minimal prep dicts (simulate ok=True preps with messages).
        prep1 = {
            "ok": True,
            "user_msgs": ["hello"],
            "assistant_msgs": ["hi"],
            "metadata": {"project": "test", "vault_path": "", "sessions_folder": ""},
            "note_type": "claude-session",
            "source": "raw note",
            "warnings": [],
        }
        prep2 = dict(prep1)  # same shape

        good_block = (
            "## Summary\nDid a thing.\n\n"
            "## Key Decisions\n- None.\n\n"
            "## Changes Made\n- None.\n\n"
            "## Errors Encountered\n- None.\n\n"
            "## Open Questions / Next Steps\n- [ ] None.\n\n"
            "## Importance\n5\n"
        )
        bad_block = "Just some text without the required sections.\n"

        stdout_text = (
            f"===== SUMMARY 1 =====\n{good_block}"
            f"===== SUMMARY 2 =====\n{bad_block}"
        )

        def fake_subprocess_run(cmd, input=None, capture_output=False, text=False, timeout=None):
            class _Res:
                returncode = 0
                stdout = stdout_text
                stderr = ""
            return _Res()

        monkeypatch.setattr(obsidian_utils.subprocess, "run", fake_subprocess_run)

        results = obsidian_utils.generate_summaries_batch(
            [prep1, prep2],
            model="haiku",
            timeout=30,
            project="test",
            vault_path="",
            sessions_folder="",
        )

        assert len(results) == 2
        text1, reason1 = results[0]
        text2, reason2 = results[1]

        assert reason1 is None, f"block 1 should be accepted; reason={reason1!r}"
        assert text1 is not None
        assert "## Summary" in text1

        assert text2 is None, f"block 2 should be rejected (missing ## Summary)"
        assert reason2 == "missing_section"

    def test_duplicate_summary_delimiter_first_wins(self, monkeypatch, tmp_path):
        """Fix 1: first-occurrence-wins — a repeated ===== SUMMARY k ===== delimiter
        in model output must NOT overwrite the valid first block with the junk repeat."""
        monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda sid: None)

        prep1 = {
            "ok": True,
            "user_msgs": ["hello"],
            "assistant_msgs": ["hi"],
            "metadata": {"project": "test", "vault_path": "", "sessions_folder": ""},
            "note_type": "claude-session",
            "source": "raw note",
            "warnings": [],
        }
        prep2 = dict(prep1)

        valid_block_1 = (
            "## Summary\nDid a thing.\n\n"
            "## Key Decisions\n- None noted.\n\n"
            "## Changes Made\n- None noted.\n\n"
            "## Errors Encountered\n- None.\n\n"
            "## Open Questions / Next Steps\n- [ ] None.\n\n"
            "## Importance\n5\n"
        )
        junk_fragment_1 = "Oops this is extra repeated output without Summary section.\n"
        valid_block_2 = (
            "## Summary\nSecond note done.\n\n"
            "## Key Decisions\n- None noted.\n\n"
            "## Changes Made\n- None noted.\n\n"
            "## Errors Encountered\n- None.\n\n"
            "## Open Questions / Next Steps\n- [ ] None.\n\n"
            "## Importance\n4\n"
        )

        # Model repeats the SUMMARY 1 delimiter — the junk block lacks ## Summary.
        stdout_text = (
            f"===== SUMMARY 1 =====\n{valid_block_1}"
            f"===== SUMMARY 1 =====\n{junk_fragment_1}"
            f"===== SUMMARY 2 =====\n{valid_block_2}"
        )

        def fake_subprocess_run(cmd, input=None, capture_output=False, text=False, timeout=None):
            class _Res:
                returncode = 0
                stdout = stdout_text
                stderr = ""
            return _Res()

        monkeypatch.setattr(obsidian_utils.subprocess, "run", fake_subprocess_run)

        results = obsidian_utils.generate_summaries_batch(
            [prep1, prep2],
            model="haiku",
            timeout=30,
            project="test",
            vault_path="",
            sessions_folder="",
        )

        assert len(results) == 2
        text1, reason1 = results[0]
        text2, reason2 = results[1]

        # Block 1: first occurrence (valid) must win; the junk repeat must be ignored.
        assert reason1 is None, (
            f"block 1 should be accepted (first-occurrence-wins); reason={reason1!r}"
        )
        assert text1 is not None, "block 1 text should not be None"
        assert "## Summary" in text1, "block 1 should contain the valid ## Summary section"
        assert "Oops" not in (text1 or ""), (
            "junk fragment must not appear in block 1 result"
        )

        # Block 2 should also be valid.
        assert reason2 is None, f"block 2 should be accepted; reason={reason2!r}"
        assert text2 is not None

    def test_writeback_exception_routes_to_solo_fallback(self, monkeypatch, tmp_path):
        """Fix 3: if upgrade_note_with_summary raises for one note, that note is
        routed to solo fallback and NO exception propagates out of upgrade_batch."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        note_names = [f"note{i}.md" for i in range(3)]
        paths = [str(self._write_session_note(sessions_dir, name)) for name in note_names]
        bad_path = paths[1]  # index 1 will raise on write-back

        monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda sid: None)

        def fake_generate_summaries_batch(prepared_notes, **kwargs):
            return [(self._fake_good_summary(), None)] * len(prepared_notes)

        monkeypatch.setattr(obsidian_utils, "generate_summaries_batch", fake_generate_summaries_batch)

        def fake_upgrade_note_with_summary(path, summary, *args, **kwargs):
            if path == bad_path:
                raise RuntimeError("simulated write-back failure")
            return f"Upgraded {os.path.basename(path)} (source: batch)"

        monkeypatch.setattr(obsidian_utils, "upgrade_note_with_summary", fake_upgrade_note_with_summary)

        solo_fallback_called: list[str] = []
        def fake_upgrade_unsummarized_note(path, *args, **kwargs):
            solo_fallback_called.append(path)
            return f"Upgraded {os.path.basename(path)} (source: solo-fallback)", 0.1, "haiku", None

        monkeypatch.setattr(obsidian_utils, "upgrade_unsummarized_note", fake_upgrade_unsummarized_note)

        # Must not raise even though write-back raises for bad_path.
        results = obsidian_utils.upgrade_batch(
            paths, str(tmp_path), "sessions", "test-project",
            summary_batch_size=5,
        )

        # All 3 results present in input order, no KeyError.
        assert len(results) == 3
        assert [r["path"] for r in results] == paths

        # bad_path was routed to solo fallback.
        assert bad_path in solo_fallback_called, (
            f"raising path should have gone to solo fallback; solo_fallback_called={solo_fallback_called!r}"
        )

        # All results have Upgraded status (solo succeeded for the bad path).
        for r in results:
            assert r["status"].startswith("Upgraded "), (
                f"all notes should be Upgraded, got: {r['status']!r}"
            )

    def test_dedup_failure_does_not_fail_note(self, monkeypatch, tmp_path):
        """Fix 2: if _dedup_summary_open_items raises, the note is still accepted
        with the undeduped block_text — result is (text, None), NOT (None, 'missing_section')."""
        monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda sid: None)

        prep1 = {
            "ok": True,
            "user_msgs": ["hello"],
            "assistant_msgs": ["hi"],
            "metadata": {"project": "test", "vault_path": "", "sessions_folder": ""},
            "note_type": "claude-session",
            "source": "raw note",
            "warnings": [],
        }

        valid_block = (
            "## Summary\nDedup failure test.\n\n"
            "## Key Decisions\n- None noted.\n\n"
            "## Changes Made\n- None noted.\n\n"
            "## Errors Encountered\n- None.\n\n"
            "## Open Questions / Next Steps\n- [ ] None.\n\n"
            "## Importance\n3\n"
        )
        stdout_text = f"===== SUMMARY 1 =====\n{valid_block}"

        def fake_subprocess_run(cmd, input=None, capture_output=False, text=False, timeout=None):
            class _Res:
                returncode = 0
                stdout = stdout_text
                stderr = ""
            return _Res()

        monkeypatch.setattr(obsidian_utils.subprocess, "run", fake_subprocess_run)

        # Make _dedup_summary_open_items raise.
        monkeypatch.setattr(
            obsidian_utils,
            "_dedup_summary_open_items",
            lambda block_text, existing_items: (_ for _ in ()).throw(RuntimeError("dedup boom")),
        )

        # Patch the import inside generate_summaries_batch so existing_items is non-empty.
        # Save and restore the real module (if already loaded) to avoid contaminating
        # other tests that use open_item_dedup (test-ordering safety).
        import types
        import sys as _sys

        fake_oid_module = types.ModuleType("open_item_dedup")
        fake_oid_module.collect_open_items = lambda *a, **kw: [("s", "t", "existing item 1")]

        _orig_oid = _sys.modules.get("open_item_dedup")
        _sys.modules["open_item_dedup"] = fake_oid_module

        try:
            results = obsidian_utils.generate_summaries_batch(
                [prep1],
                model="haiku",
                timeout=30,
                project="test",
                vault_path="",
                sessions_folder="",
            )
        finally:
            # Restore the original module (or remove if it wasn't present before).
            if _orig_oid is not None:
                _sys.modules["open_item_dedup"] = _orig_oid
            else:
                _sys.modules.pop("open_item_dedup", None)

        assert len(results) == 1
        text1, reason1 = results[0]

        # Must be accepted (not failed) even though dedup raised.
        assert reason1 is None, (
            f"dedup failure should not fail the note; reason={reason1!r}"
        )
        assert text1 is not None, "text should not be None when dedup fails"
        assert "## Summary" in text1


# ===========================================================================
# Section N: canonical_project_name — worktree-aware project name derivation
# ===========================================================================


def _init_git_repo(path: Path) -> None:
    """Create a minimal git repo at `path` for testing.

    Uses `git init` without `-b <branch>` so the helper works on older git
    versions that predate that flag (Copilot R5). The default branch name
    isn't asserted on by any caller — only the repo basename matters.
    """
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    (path / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


@_REQUIRES_GIT
def test_canonical_project_name_main_repo(tmp_path, monkeypatch):
    """In the main repo, canonical name = repo basename."""
    repo = tmp_path / "my-project"
    repo.mkdir()
    _init_git_repo(repo)
    monkeypatch.chdir(repo)
    assert obsidian_utils.canonical_project_name() == "my-project"


@_REQUIRES_GIT
def test_canonical_project_name_in_worktree(tmp_path, monkeypatch):
    """In a worktree, canonical name = main repo basename, not worktree basename."""
    repo = tmp_path / "my-project"
    repo.mkdir()
    _init_git_repo(repo)
    worktree = tmp_path / "my-project--feature-branch"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/x", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    monkeypatch.chdir(worktree)
    # Despite the worktree's directory basename being "my-project--feature-branch",
    # canonical_project_name returns the main-repo's basename.
    assert obsidian_utils.canonical_project_name() == "my-project"


@_REQUIRES_GIT
def test_canonical_project_name_falls_back_outside_git(tmp_path, monkeypatch):
    """Outside a git repo, fall back to cwd basename."""
    nongit = tmp_path / "Plain Folder"
    nongit.mkdir()
    monkeypatch.chdir(nongit)
    # The fallback applies normalization (spaces → hyphens, lowercase).
    assert obsidian_utils.canonical_project_name() == "plain-folder"


@_REQUIRES_GIT
def test_canonical_project_name_explicit_cwd_arg(tmp_path):
    """Explicit cwd= arg used instead of process cwd."""
    repo = tmp_path / "sample-repo"
    repo.mkdir()
    _init_git_repo(repo)
    # Note: NOT chdir-ing — using cwd= arg explicitly.
    assert obsidian_utils.canonical_project_name(cwd=str(repo)) == "sample-repo"


@_REQUIRES_GIT
def test_canonical_project_name_normalizes_underscores(tmp_path, monkeypatch):
    """Underscores and spaces normalize to hyphens, lowercase."""
    repo = tmp_path / "Snake_Case_Repo"
    repo.mkdir()
    _init_git_repo(repo)
    monkeypatch.chdir(repo)
    assert obsidian_utils.canonical_project_name() == "snake-case-repo"


def test_canonical_project_name_handles_deleted_cwd(monkeypatch):
    """When the current working directory has been deleted (e.g., a worktree
    removed mid-session via `gh pr merge --delete-branch`), os.getcwd() raises
    FileNotFoundError. Hooks must exit 0, so canonical_project_name returns
    'unknown' rather than propagating the exception (review C4)."""
    def _raise_fnf(*args, **kwargs):
        raise FileNotFoundError("cwd deleted")
    monkeypatch.setattr(obsidian_utils.os, "getcwd", _raise_fnf)
    monkeypatch.setattr(
        obsidian_utils,
        "_git_canonical_project_name_with_reason",
        lambda cwd=None: (None, "not-a-repo"),
    )
    assert obsidian_utils.canonical_project_name(None) == "unknown"


@_REQUIRES_GIT
def test_canonical_project_name_no_warn_when_not_a_repo(tmp_path, monkeypatch, capsys):
    """Copilot R2: the SF7 warning must NOT fire for the normal 'cwd is not
    inside a git repo' case (returncode != 0). That's an expected operating
    condition; warning would be noise.
    """
    monkeypatch.setattr(obsidian_utils, "_git_fallback_warned", False)
    nongit = tmp_path / "Plain Folder"
    nongit.mkdir()
    monkeypatch.chdir(nongit)
    capsys.readouterr()  # drain
    assert obsidian_utils.canonical_project_name() == "plain-folder"
    captured = capsys.readouterr()
    assert "[obsidian_utils]" not in captured.err, (
        f"warning should not fire for not-a-repo case, got stderr: {captured.err!r}"
    )


def test_canonical_project_name_warns_once_when_git_unavailable(monkeypatch, capsys):
    """Copilot R2: the SF7 warning fires for genuine git errors
    (git-unavailable / empty-output / resolve-failed) and is one-shot per
    process. Multiple calls produce exactly one stderr line.
    """
    monkeypatch.setattr(obsidian_utils, "_git_fallback_warned", False)
    monkeypatch.setattr(
        obsidian_utils,
        "_git_canonical_project_name_with_reason",
        lambda cwd=None: (None, "git-unavailable"),
    )
    monkeypatch.setattr(obsidian_utils.os, "getcwd", lambda: "/tmp/some-dir")
    capsys.readouterr()  # drain

    obsidian_utils.canonical_project_name()
    obsidian_utils.canonical_project_name()
    obsidian_utils.canonical_project_name()

    captured = capsys.readouterr()
    occurrences = captured.err.count(
        "[obsidian_utils] canonical_project_name: git error"
    )
    assert occurrences == 1, (
        f"warning should fire exactly once across 3 calls, got {occurrences} "
        f"in stderr: {captured.err!r}"
    )
    assert "git-unavailable" in captured.err


@_REQUIRES_GIT
def test_git_canonical_project_name_with_reason_distinguishes_failure_modes(
    tmp_path, monkeypatch
):
    """The new (name, reason) helper distinguishes: ok / not-a-repo /
    git-unavailable / empty-output / resolve-failed. Used by canonical_project_name
    to suppress noise for the common 'not-a-repo' case (Copilot R2).
    """
    # not-a-repo: real fs, no .git
    nongit = tmp_path / "no-repo"
    nongit.mkdir()
    name, reason = obsidian_utils._git_canonical_project_name_with_reason(str(nongit))
    assert name is None
    assert reason == "not-a-repo"

    # git-unavailable: subprocess.run raises OSError
    def _raise(*args, **kwargs):
        raise OSError("git binary missing")
    monkeypatch.setattr(obsidian_utils.subprocess, "run", _raise)
    name, reason = obsidian_utils._git_canonical_project_name_with_reason(str(nongit))
    assert name is None
    assert reason == "git-unavailable"


@_REQUIRES_GIT
def test_get_session_context_returns_canonical_project(tmp_path, monkeypatch):
    """get_session_context's `project` field uses canonical naming."""
    repo = tmp_path / "obsidian-brain"
    repo.mkdir()
    _init_git_repo(repo)
    worktree = tmp_path / "obsidian-brain--issue-93"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/issue-93", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    monkeypatch.chdir(worktree)
    # session_id may be 'unknown' in test env — that's fine; we only check project.
    ctx = obsidian_utils.get_session_context()
    assert ctx["project"] == "obsidian-brain"


@_REQUIRES_GIT
def test_extract_session_metadata_returns_canonical_project(tmp_path):
    """extract_session_metadata's `project` field uses canonical naming."""
    repo = tmp_path / "obsidian-brain"
    repo.mkdir()
    _init_git_repo(repo)
    worktree = tmp_path / "obsidian-brain--issue-93"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/issue-93", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    meta = obsidian_utils.extract_session_metadata([], cwd=str(worktree))
    assert meta["project"] == "obsidian-brain"
    # project_path should still be the WORKTREE path, since that's where
    # the session actually ran.
    assert meta["project_path"] == str(worktree)


@_REQUIRES_GIT
def test_get_session_context_cache_key_isolates_distinct_worktrees(tmp_path, monkeypatch):
    """T7: get_session_context's cache must isolate distinct worktrees of
    the same repo so a call from one worktree doesn't return cached state
    from a sibling worktree.

    Distinct worktrees have distinct CC session_ids (each owns its own
    JSONL), so the per-session cache file (~/.claude/obsidian-brain/cache-<sid>.json)
    is naturally partitioned by session. The cache_key within that file
    includes (vault_path, sessions_folder) — anything else that varies
    across worktrees (e.g., note basenames in vault_path) would still
    collide because vault_path is shared across worktrees.

    This test verifies the realistic case: two sessions with different
    SIDs from two worktrees produce different cache files, so call 2
    cannot inherit call 1's value. If a future change moves the cache
    file to be SID-independent without adding a worktree-discriminator
    to cache_key, this test will fail.
    """
    monkeypatch.setattr(obsidian_utils, "_CACHE_PREFIX", str(tmp_path / "cache-"))

    repo = tmp_path / "obsidian-brain"
    repo.mkdir()
    _init_git_repo(repo)
    worktree = tmp_path / "obsidian-brain--issue-93"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/issue-93", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    vault = tmp_path / "vault"
    sessions_dir = vault / "claude-sessions"
    sessions_dir.mkdir(parents=True)

    # Simulate two distinct CC sessions: SID1 (from main repo) and SID2 (from worktree)
    sid1 = "11111111-1111-1111-1111-111111111111"
    sid2 = "22222222-2222-2222-2222-222222222222"

    # Place a session note matching SID1's hash so the basename lookup
    # produces a non-default value worth caching/comparing.
    h1 = hashlib.sha256(sid1.encode()).hexdigest()[:4]
    h2 = hashlib.sha256(sid2.encode()).hexdigest()[:4]
    (sessions_dir / f"2026-04-25-obsidian-brain-{h1}.md").write_text(
        "---\ntype: claude-session\nsession_id: " + sid1 + "\n---\n", encoding="utf-8"
    )
    (sessions_dir / f"2026-04-25-obsidian-brain-{h2}.md").write_text(
        "---\ntype: claude-session\nsession_id: " + sid2 + "\n---\n", encoding="utf-8"
    )

    # Call 1: from main repo with SID1
    monkeypatch.chdir(repo)
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: sid1)
    ctx1 = obsidian_utils.get_session_context(str(vault), "claude-sessions")
    assert ctx1["session_id"] == sid1
    assert ctx1["hash"] == h1

    # Call 2: from worktree with SID2 — must NOT return ctx1's hash
    monkeypatch.chdir(worktree)
    monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: sid2)
    ctx2 = obsidian_utils.get_session_context(str(vault), "claude-sessions")
    assert ctx2["session_id"] == sid2, (
        f"cache leaked across worktrees: expected SID2 ({sid2}), got "
        f"{ctx2['session_id']}"
    )
    assert ctx2["hash"] == h2, (
        f"cache leaked across worktrees: expected hash {h2}, got {ctx2['hash']}"
    )
    # And canonical project naming must agree on both calls
    assert ctx1["project"] == ctx2["project"] == "obsidian-brain"


# ===========================================================================
# Task 4: generate_summary returns (text, fallback_reason)
# ===========================================================================


class TestGenerateSummaryReturnsFallbackReason:
    """generate_summary returns (summary, fallback_reason); reason populated only on failure."""

    def test_success_returns_summary_and_none_reason(self, monkeypatch):
        import obsidian_utils

        class FakeResult:
            returncode = 0
            stdout = "## Summary\nOK\n## Importance\n5\n"
            stderr = ""

        monkeypatch.setattr(obsidian_utils.subprocess, "run", lambda *a, **kw: FakeResult())

        summary, reason = obsidian_utils.generate_summary(
            ["hello"], ["hi"], {"project": "t"}, model="haiku", timeout=30,
        )
        assert summary.startswith("## Summary")
        assert reason is None

    def test_timeout_returns_none_and_haiku_timeout(self, monkeypatch):
        import obsidian_utils

        def fake_run(*a, **kw):
            raise obsidian_utils.subprocess.TimeoutExpired(cmd=a, timeout=kw["timeout"])

        monkeypatch.setattr(obsidian_utils.subprocess, "run", fake_run)
        summary, reason = obsidian_utils.generate_summary(
            ["hello"], ["hi"], {"project": "t"}, model="haiku", timeout=1,
        )
        assert summary is None
        assert reason == "haiku_timeout"

    def test_nonzero_rc_returns_none_and_subprocess_error(self, monkeypatch):
        import obsidian_utils

        class FakeResult:
            returncode = 2
            stdout = ""
            stderr = "boom"

        monkeypatch.setattr(obsidian_utils.subprocess, "run", lambda *a, **kw: FakeResult())
        summary, reason = obsidian_utils.generate_summary(
            ["hello"], ["hi"], {"project": "t"}, model="haiku", timeout=30,
        )
        assert summary is None
        assert reason == "haiku_subprocess_error"

    def test_empty_stdout_returns_none_and_empty_output(self, monkeypatch):
        import obsidian_utils

        class FakeResult:
            returncode = 0
            stdout = "   "
            stderr = ""

        monkeypatch.setattr(obsidian_utils.subprocess, "run", lambda *a, **kw: FakeResult())
        summary, reason = obsidian_utils.generate_summary(
            ["hello"], ["hi"], {"project": "t"}, model="haiku", timeout=30,
        )
        assert summary is None
        assert reason == "empty_output"


# ===========================================================================
# Task 5: generate_snapshot_summary returns (text, fallback_reason)
# ===========================================================================


class TestGenerateSnapshotSummaryReturnsFallbackReason:
    def test_success_returns_summary_and_none_reason(self, monkeypatch):
        import obsidian_utils

        class FakeResult:
            returncode = 0
            stdout = "snapshot OK"
            stderr = ""

        monkeypatch.setattr(obsidian_utils.subprocess, "run", lambda *a, **kw: FakeResult())
        summary, reason = obsidian_utils.generate_snapshot_summary(
            ["u"], ["a"], {"project": "t"}, model="haiku", timeout=30,
        )
        assert summary == "snapshot OK"
        assert reason is None

    def test_timeout_returns_none_and_haiku_timeout(self, monkeypatch):
        import obsidian_utils

        def fake_run(*a, **kw):
            raise obsidian_utils.subprocess.TimeoutExpired(cmd=a, timeout=kw["timeout"])

        monkeypatch.setattr(obsidian_utils.subprocess, "run", fake_run)
        summary, reason = obsidian_utils.generate_snapshot_summary(
            ["u"], ["a"], {"project": "t"}, model="haiku", timeout=1,
        )
        assert summary is None
        assert reason == "haiku_timeout"


# ===========================================================================
# Section: upgrade_unsummarized_note — 4-tuple return contract
# ===========================================================================


class TestUpgradeUnsummarizedNoteReturnsTuple:
    """upgrade_unsummarized_note returns (status, elapsed_s, model_used, fallback_reason)."""

    def test_success_returns_full_tuple(self, monkeypatch, tmp_path):
        import obsidian_utils

        # Stub the heavy lifting — we're testing wiring, not generation
        monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda sid: None)
        monkeypatch.setattr(
            obsidian_utils, "generate_summary",
            lambda *a, **kw: ("## Summary\nOK", None),
        )
        monkeypatch.setattr(
            obsidian_utils, "upgrade_note_with_summary",
            lambda *a, **kw: "Upgraded test.md (source: raw note)",
        )

        # Minimal valid note
        note = tmp_path / "test.md"
        note.write_text(
            "---\nsession_id: abc123\n---\n"
            "## Conversation (raw)\n**User:** hi\n**Assistant:** hello\n"
        )

        result = obsidian_utils.upgrade_unsummarized_note(
            str(note), str(tmp_path), "claude-sessions", "obsidian-brain",
        )
        assert isinstance(result, tuple)
        assert len(result) == 4
        status, elapsed_s, model_used, fallback_reason = result
        assert status.startswith("Upgraded ")
        assert elapsed_s >= 0
        assert model_used == "haiku"
        assert fallback_reason is None


# ===========================================================================
# Section: model-escalation chain (#165) — in-process Haiku→Sonnet→Opus
# ===========================================================================


class TestModelEscalationChain:
    """upgrade_unsummarized_note escalates through Haiku→Sonnet→Opus in-process.

    Fixture strategy: monkeypatch generate_summary (module-attribute form) to a
    scripted fake that inspects the ``model`` kwarg and returns a canned
    (text, reason) pair per model. Also monkeypatch find_transcript_jsonl to
    None (raw-note fallback) and upgrade_note_with_summary to avoid real I/O
    on the vault write. This matches the pattern used by
    TestUpgradeUnsummarizedNoteReturnsTuple, which is the lightest complete
    driver for the escalation loop.
    """

    # ------------------------------------------------------------------
    # Shared note factory
    # ------------------------------------------------------------------

    @staticmethod
    def _write_session_note(tmp_path: Path) -> Path:
        """Write a minimal valid session note to tmp_path and return its path."""
        note = tmp_path / "2026-06-01-escalation-test-abcd.md"
        note.write_text(
            "---\n"
            "type: claude-session\n"
            "date: 2026-06-01\n"
            "session_id: escalation-test-session-id-0000-0000-000000000000\n"
            "project: test-project\n"
            "status: auto-logged\n"
            "---\n\n"
            "## Conversation (raw)\n"
            "**User:** hello\n"
            "**Assistant:** hi there\n",
            encoding="utf-8",
        )
        return note

    # ------------------------------------------------------------------
    # Helpers for common monkeypatching
    # ------------------------------------------------------------------

    @staticmethod
    def _patch_common(monkeypatch, tmp_path: Path):
        """Patch pieces that are the same across all escalation tests."""
        import obsidian_utils

        monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda sid: None)
        monkeypatch.setattr(
            obsidian_utils,
            "upgrade_note_with_summary",
            lambda note_path, summary_text, *a, **kw: f"Upgraded {os.path.basename(note_path)} (source: raw note)",
        )

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_escalates_to_sonnet_on_empty_output(self, monkeypatch, tmp_path):
        """When haiku returns (None, 'empty_output'), escalate to sonnet."""
        import obsidian_utils

        self._patch_common(monkeypatch, tmp_path)
        note = self._write_session_note(tmp_path)

        def fake_generate_summary(*args, **kwargs):
            model = kwargs.get("model", "haiku")
            if model == "haiku":
                return None, "empty_output"
            if model == "sonnet":
                return (
                    "## Summary\nDid a thing.\n\n"
                    "## Key Decisions\n- None noted.\n\n"
                    "## Changes Made\n- None noted.\n\n"
                    "## Errors Encountered\n- None.\n\n"
                    "## Open Questions / Next Steps\n- [ ] None.\n\n"
                    "IMPORTANCE: 5\n"
                ), None
            return None, "empty_output"

        monkeypatch.setattr(obsidian_utils, "generate_summary", fake_generate_summary)

        status, elapsed_s, model_used, fallback_reason = obsidian_utils.upgrade_unsummarized_note(
            str(note), str(tmp_path), "claude-sessions", "test-project",
        )

        assert status.startswith("Upgraded "), f"expected success, got: {status}"
        assert model_used == "sonnet"
        assert fallback_reason is None

    def test_escalates_haiku_to_opus_when_sonnet_also_empty(self, monkeypatch, tmp_path):
        """When haiku and sonnet both return (None, 'empty_output'), opus is tried."""
        import obsidian_utils

        self._patch_common(monkeypatch, tmp_path)
        note = self._write_session_note(tmp_path)

        def fake_generate_summary(*args, **kwargs):
            model = kwargs.get("model", "haiku")
            if model == "opus":
                return (
                    "## Summary\nOpus summary.\n\n"
                    "## Key Decisions\n- None noted.\n\n"
                    "## Changes Made\n- None noted.\n\n"
                    "## Errors Encountered\n- None.\n\n"
                    "## Open Questions / Next Steps\n- [ ] None.\n\n"
                    "IMPORTANCE: 7\n"
                ), None
            # haiku and sonnet both fail with empty_output
            return None, "empty_output"

        monkeypatch.setattr(obsidian_utils, "generate_summary", fake_generate_summary)

        status, elapsed_s, model_used, fallback_reason = obsidian_utils.upgrade_unsummarized_note(
            str(note), str(tmp_path), "claude-sessions", "test-project",
        )

        assert status.startswith("Upgraded "), f"expected success, got: {status}"
        assert model_used == "opus"
        assert fallback_reason is None

    def test_no_escalation_on_timeout(self, monkeypatch, tmp_path):
        """When haiku returns (None, 'haiku_timeout'), do NOT escalate — timeout
        means the CLI is slow; a bigger model would only make it slower (#84).
        """
        import obsidian_utils

        self._patch_common(monkeypatch, tmp_path)
        note = self._write_session_note(tmp_path)

        models_attempted: list[str] = []

        def fake_generate_summary(*args, **kwargs):
            model = kwargs.get("model", "haiku")
            models_attempted.append(model)
            return None, "haiku_timeout"

        monkeypatch.setattr(obsidian_utils, "generate_summary", fake_generate_summary)

        status, elapsed_s, model_used, fallback_reason = obsidian_utils.upgrade_unsummarized_note(
            str(note), str(tmp_path), "claude-sessions", "test-project",
        )

        # Should have tried haiku once and stopped immediately
        assert models_attempted == ["haiku"], (
            f"expected only haiku to be attempted, got: {models_attempted}"
        )
        assert not status.startswith("Upgraded "), f"expected failure, got: {status}"
        assert model_used is None
        assert fallback_reason == "haiku_timeout"

    def test_happy_path_uses_primary_model_only(self, monkeypatch, tmp_path):
        """When haiku succeeds on the first call, no escalation occurs."""
        import obsidian_utils

        self._patch_common(monkeypatch, tmp_path)
        note = self._write_session_note(tmp_path)

        models_attempted: list[str] = []

        def fake_generate_summary(*args, **kwargs):
            model = kwargs.get("model", "haiku")
            models_attempted.append(model)
            return (
                "## Summary\nHaiku success.\n\n"
                "## Key Decisions\n- None noted.\n\n"
                "## Changes Made\n- None noted.\n\n"
                "## Errors Encountered\n- None.\n\n"
                "## Open Questions / Next Steps\n- [ ] None.\n\n"
                "IMPORTANCE: 4\n"
            ), None

        monkeypatch.setattr(obsidian_utils, "generate_summary", fake_generate_summary)

        status, elapsed_s, model_used, fallback_reason = obsidian_utils.upgrade_unsummarized_note(
            str(note), str(tmp_path), "claude-sessions", "test-project",
        )

        assert models_attempted == ["haiku"], (
            f"expected only haiku to be attempted, got: {models_attempted}"
        )
        assert status.startswith("Upgraded "), f"expected success, got: {status}"
        assert model_used == "haiku"
        assert fallback_reason is None


# ===========================================================================
# TestEscalationModels — unit tests for _escalation_models helper (Fix 4)
# ===========================================================================


class TestEscalationModels:
    """Unit tests for the _escalation_models() capability-ranked helper (#165 Fix 4).

    Ensures the escalation list only goes UP in capability, never backwards, so
    that passing summary_model="sonnet" does not produce ["sonnet", "haiku", "opus"].
    """

    def test_haiku_escalates_through_full_chain(self):
        """haiku -> [haiku, sonnet, opus]: all models tried in capability order."""
        import obsidian_utils
        result = obsidian_utils._escalation_models("haiku")
        assert result == ["haiku", "sonnet", "opus"], (
            f"haiku should escalate through full chain; got: {result!r}"
        )

    def test_sonnet_escalates_only_to_opus(self):
        """sonnet -> [sonnet, opus]: haiku (less capable) is excluded."""
        import obsidian_utils
        result = obsidian_utils._escalation_models("sonnet")
        assert result == ["sonnet", "opus"], (
            f"sonnet should only escalate to opus; got: {result!r}"
        )

    def test_opus_no_escalation(self):
        """opus -> [opus]: already at max capability, nothing to escalate to."""
        import obsidian_utils
        result = obsidian_utils._escalation_models("opus")
        assert result == ["opus"], (
            f"opus should have no escalation targets; got: {result!r}"
        )


# ===========================================================================
# TestNormalizeSummary — unit tests for _normalize_summary (#167)
# ===========================================================================


class TestNormalizeSummary:
    """Unit tests for the _normalize_summary() post-processing helper (#167).

    Exercises heading normalization, section synthesis, default importance,
    idempotency, and the no-op path when no recognisable Summary heading exists.
    """

    def test_normalizes_heading_variants(self):
        """Variant headings for any canonical section are rewritten to ## form."""
        loose = (
            "# Summary\n"
            "Did a thing.\n\n"
            "**Key Decisions**\n"
            "- Used pattern X.\n\n"
            "Changes Made:\n"
            "- file.py\n\n"
            "## Errors Encountered\n"
            "None.\n\n"
            "## Open Questions / Next Steps\n"
            "None.\n\n"
            "## Importance\n"
            "6\n"
        )
        out, notes = obsidian_utils._normalize_summary(loose)
        assert re.search(r"^## Summary\s*$", out, re.MULTILINE), (
            "# Summary should be rewritten to ## Summary"
        )
        assert re.search(r"^## Key Decisions\s*$", out, re.MULTILINE), (
            "**Key Decisions** should be rewritten to ## Key Decisions"
        )
        assert re.search(r"^## Changes Made\s*$", out, re.MULTILINE), (
            "Changes Made: should be rewritten to ## Changes Made"
        )
        assert "normalized heading: Summary" in notes
        assert "normalized heading: Key Decisions" in notes
        assert "normalized heading: Changes Made" in notes

    def test_no_summary_heading_returns_unchanged(self):
        """Prose with no recognisable Summary heading is returned verbatim."""
        prose = (
            "This is just some text about the session.\n"
            "No headings at all — not even a malformed one.\n"
        )
        out, notes = obsidian_utils._normalize_summary(prose)
        assert out == prose, "text without any Summary heading must be returned unchanged"
        assert notes == [], "no recovery notes should be produced when nothing was recovered"
        # Confirm the downstream check would still reject it.
        assert not re.search(r"^## Summary\s*$", out, re.MULTILINE)

    def test_synthesizes_missing_sections_and_default_importance(self):
        """A summary with only ## Summary gets all other sections synthesized."""
        minimal = "## Summary\nDid X.\n"
        out, notes = obsidian_utils._normalize_summary(minimal)

        for sec in ["Key Decisions", "Changes Made", "Errors Encountered",
                    "Open Questions / Next Steps"]:
            assert re.search(r"^## " + re.escape(sec) + r"\s*$", out, re.MULTILINE), (
                f"section '## {sec}' should be synthesized"
            )
            # Synthesized body must be "None."
            # Find the section and check next non-blank line.
            sec_match = re.search(
                r"^## " + re.escape(sec) + r"\s*\n(.*?)(?=\n## |\Z)",
                out, re.MULTILINE | re.DOTALL,
            )
            assert sec_match and "None." in sec_match.group(1), (
                f"synthesized body for '{sec}' should contain 'None.'"
            )

        assert re.search(r"^## Importance\s*$", out, re.MULTILINE), (
            "## Importance should be appended when absent"
        )
        assert re.search(r"^## Importance\s*\n5", out, re.MULTILINE), (
            "defaulted Importance must be 5"
        )
        assert "synthesized section: Key Decisions" in notes
        assert "synthesized section: Changes Made" in notes
        assert "synthesized section: Errors Encountered" in notes
        assert "synthesized section: Open Questions / Next Steps" in notes
        assert "defaulted importance" in notes

    def test_idempotent(self):
        """A well-formed summary is unchanged; double application yields same result."""
        full = (
            "## Summary\n"
            "Implemented feature Y.\n\n"
            "## Key Decisions\n"
            "- Chose approach A.\n\n"
            "## Changes Made\n"
            "- src/y.py\n\n"
            "## Errors Encountered\n"
            "None.\n\n"
            "## Open Questions / Next Steps\n"
            "None.\n\n"
            "## Importance\n"
            "7\n"
        )
        out1, notes1 = obsidian_utils._normalize_summary(full)
        out2, notes2 = obsidian_utils._normalize_summary(out1)
        assert out1 == full, "a well-formed summary should pass through unchanged"
        assert notes1 == [], "no recovery notes for an already-correct summary"
        assert out2 == out1, "double application must be idempotent"
        assert notes2 == []

    def test_existing_importance_line_preserved(self):
        """An existing ## Importance or IMPORTANCE: N line is not overwritten."""
        # Case 1: ## Importance section already present with score 8
        with_heading = (
            "## Summary\nDid Z.\n\n"
            "## Importance\n8\n"
        )
        out, notes = obsidian_utils._normalize_summary(with_heading)
        # Score must still be 8, not 5
        imp_match = re.search(r"^## Importance\s*\n(\d+)", out, re.MULTILINE)
        assert imp_match and imp_match.group(1) == "8", (
            f"existing ## Importance score should be preserved; got: {out!r}"
        )
        assert "defaulted importance" not in notes

        # Case 2: IMPORTANCE: N line present (sub-agent style)
        with_legacy = (
            "## Summary\nDid W.\n\n"
            "IMPORTANCE: 9\n"
        )
        out2, notes2 = obsidian_utils._normalize_summary(with_legacy)
        assert "defaulted importance" not in notes2, (
            "IMPORTANCE: N line should prevent default-importance injection"
        )
        assert "9" in out2, "existing IMPORTANCE score must be preserved"

    def test_idempotent_with_code_block(self):
        """A well-formed summary containing a fenced code block with a '# Summary'
        line inside the fence is returned UNCHANGED — idempotency fix (#167 Fix 1)."""
        text = (
            "## Summary\n"
            "Implemented feature Z.\n\n"
            "## Changes Made\n"
            "Added helper:\n\n"
            "```python\n"
            "# Summary\n"
            "def summarize(text):\n"
            "    return text[:100]\n"
            "```\n\n"
            "## Key Decisions\n"
            "- Kept it simple.\n\n"
            "## Errors Encountered\n"
            "None.\n\n"
            "## Open Questions / Next Steps\n"
            "None.\n\n"
            "## Importance\n"
            "6\n"
        )
        out, notes = obsidian_utils._normalize_summary(text)
        assert out == text, (
            "well-formed summary with code block containing '# Summary' must be "
            f"returned unchanged; diff: {set(out.splitlines()) ^ set(text.splitlines())}"
        )
        assert notes == [], (
            f"no recovery notes expected for already-correct summary; got: {notes!r}"
        )

    def test_normalizes_open_questions_variant(self):
        """'# Open Questions / Next Steps' (single-hash variant) is normalized to
        '## Open Questions / Next Steps'."""
        text = (
            "## Summary\n"
            "Did a thing.\n\n"
            "## Key Decisions\n"
            "None.\n\n"
            "## Changes Made\n"
            "None.\n\n"
            "## Errors Encountered\n"
            "None.\n\n"
            "# Open Questions / Next Steps\n"
            "- Check later.\n\n"
            "## Importance\n"
            "5\n"
        )
        out, notes = obsidian_utils._normalize_summary(text)
        assert re.search(r"^## Open Questions / Next Steps\s*$", out, re.MULTILINE), (
            "# Open Questions / Next Steps should be rewritten to "
            "## Open Questions / Next Steps"
        )
        assert "normalized heading: Open Questions / Next Steps" in notes


# ===========================================================================
# TestRecoveryIntegration — solo-path integration for _normalize_summary (#167)
# ===========================================================================


class TestRecoveryIntegration:
    """Integration tests confirming _normalize_summary is wired into the
    production paths (upgrade_unsummarized_note solo path, generate_summaries_batch
    batch path).

    Approach: because the existing fixtures monkeypatch generate_summary to return
    a well-formed summary, we test recovery via two complementary angles:

      (a) Direct: verify that _normalize_summary turns a # Summary input into text
          that passes re.search(r"^## Summary\\s*$", ..., re.M), and that
          upgrade_note_with_summary accepts the normalized form but rejects the raw form.
      (b) Solo path: drive upgrade_unsummarized_note end-to-end with a
          monkeypatched generate_summary that returns a # Summary (single-hash)
          summary.  With recovery enabled the note is Upgraded; with recovery
          disabled (summary_recovery=False) the malformed text reaches
          upgrade_note_with_summary and the note fails.
    """

    # ---- helpers ----------------------------------------------------------------

    @staticmethod
    def _loose_summary_text() -> str:
        """A structurally-loose summary: # Summary (single-hash), missing sections."""
        return (
            "# Summary\n"
            "Implemented the new feature.\n\n"
            "## Key Decisions\n"
            "- Used pattern X.\n"
        )

    @staticmethod
    def _write_session_note(sessions_dir: Path, note_name: str) -> Path:
        sid = f"recovery-integ-session-{note_name.replace('.md', '')}-0000"
        note = sessions_dir / note_name
        note.write_text(
            "---\n"
            "type: claude-session\n"
            "date: 2026-06-01\n"
            f"session_id: {sid}\n"
            "project: test-project\n"
            "status: auto-logged\n"
            "tags:\n"
            "  - claude/session\n"
            "---\n\n"
            "## Conversation (raw)\n"
            "**User:** hello\n"
            "**Assistant:** hi there\n",
            encoding="utf-8",
        )
        return note

    # ---- (a) Direct angle -------------------------------------------------------

    def test_normalize_turns_single_hash_into_passing_text(self):
        """_normalize_summary on # Summary input produces text that passes
        upgrade_note_with_summary's ## Summary gate."""
        raw = self._loose_summary_text()
        # Raw form fails the gate.
        assert not re.search(r"^## Summary\s*$", raw, re.MULTILINE), (
            "raw fixture should NOT have ## Summary (it uses # Summary)"
        )
        normalized, notes = obsidian_utils._normalize_summary(raw)
        assert re.search(r"^## Summary\s*$", normalized, re.MULTILINE), (
            "normalized text must contain ^## Summary$ for upgrade_note_with_summary"
        )
        assert "normalized heading: Summary" in notes

    def test_upgrade_note_with_summary_accepts_normalized_rejects_raw(
        self, tmp_vault, monkeypatch
    ):
        """upgrade_note_with_summary accepts normalized text, rejects raw # Summary."""
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        sessions_dir = tmp_vault / "claude-sessions"
        note = self._write_session_note(sessions_dir, "recovery-accept-reject-test.md")

        raw = self._loose_summary_text()
        normalized, _ = obsidian_utils._normalize_summary(raw)

        # Raw form must fail.
        result_raw = obsidian_utils.upgrade_note_with_summary(
            str(note), raw, str(tmp_vault), "claude-sessions", "test-project",
        )
        assert result_raw.startswith("Failed:"), (
            f"raw # Summary should fail upgrade_note_with_summary; got: {result_raw!r}"
        )

        # Reset note to auto-logged status for the accept test.
        note.write_text(
            "---\n"
            "type: claude-session\n"
            "date: 2026-06-01\n"
            "session_id: recovery-integ-session-recovery-accept-reject-test-0000\n"
            "project: test-project\n"
            "status: auto-logged\n"
            "tags:\n"
            "  - claude/session\n"
            "---\n\n"
            "## Conversation (raw)\n"
            "**User:** hello\n"
            "**Assistant:** hi there\n",
            encoding="utf-8",
        )

        # Normalized form must succeed.
        result_norm = obsidian_utils.upgrade_note_with_summary(
            str(note), normalized, str(tmp_vault), "claude-sessions", "test-project",
        )
        assert result_norm.startswith("Upgraded "), (
            f"normalized summary should be accepted; got: {result_norm!r}"
        )

    # ---- (b) Solo path end-to-end -----------------------------------------------

    def test_solo_path_recovers_loose_summary(self, tmp_path, monkeypatch):
        """upgrade_unsummarized_note with a # Summary output (loose Haiku):
        recovery enabled -> note is Upgraded; disabled -> note fails write-back."""
        vault = tmp_path / "vault"
        sessions_dir = vault / "claude-sessions"
        sessions_dir.mkdir(parents=True)

        # Unique session IDs so load_config cache doesn't interfere.
        monkeypatch.setattr(obsidian_utils, "_get_session_id_fast", lambda: _unique_sid())

        def _make_note(name: str) -> Path:
            return self._write_session_note(sessions_dir, name)

        # generate_summary returns a structurally-loose # Summary text.
        def fake_generate_summary(*args, **kwargs):
            return self._loose_summary_text(), None

        monkeypatch.setattr(obsidian_utils, "generate_summary", fake_generate_summary)
        monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda session_id: None)

        # --- recovery ENABLED (default) ---
        note_on = _make_note("recovery-on.md")
        status, _, model_used, fallback_reason = obsidian_utils.upgrade_unsummarized_note(
            str(note_on), str(vault), "claude-sessions", "test-project",
        )
        assert status.startswith("Upgraded "), (
            f"recovery enabled: expected Upgraded, got: {status!r}"
        )
        assert fallback_reason is None

        # --- recovery DISABLED (summary_recovery=False in config) ---
        note_off = _make_note("recovery-off.md")

        def fake_load_config_no_recovery():
            cfg = dict(obsidian_utils._DEFAULTS)
            cfg["summary_recovery"] = False
            return cfg

        # Patch load_config for the disabled check; _summary_recovery_enabled
        # calls load_config() directly.
        monkeypatch.setattr(obsidian_utils, "load_config", fake_load_config_no_recovery)

        status_off, _, _, _ = obsidian_utils.upgrade_unsummarized_note(
            str(note_off), str(vault), "claude-sessions", "test-project",
        )
        assert not status_off.startswith("Upgraded "), (
            f"recovery disabled: expected failure (malformed summary), got: {status_off!r}"
        )


# ===========================================================================
# TestBatchRecovery — batch-path recovery tests (#167 Fix 4, tests 3 & 4)
# ===========================================================================


class TestBatchRecovery:
    """Tests for _normalize_summary wired into generate_summaries_batch (#167)."""

    # Minimal prep dict used by batch tests (no JSONL path needed).
    _BASE_PREP: dict = {
        "ok": True,
        "user_msgs": ["hello"],
        "assistant_msgs": ["hi"],
        "metadata": {"project": "test", "vault_path": "", "sessions_folder": ""},
        "note_type": "claude-session",
        "source": "raw note",
        "warnings": [],
    }

    @staticmethod
    def _make_stdout(loose_block: str, good_block: str) -> str:
        """Build ===== SUMMARY k ===== delimited stdout with 2 blocks."""
        return (
            f"===== SUMMARY 1 =====\n{loose_block}"
            f"===== SUMMARY 2 =====\n{good_block}"
        )

    @staticmethod
    def _good_block() -> str:
        return (
            "## Summary\nDid something.\n\n"
            "## Key Decisions\n- None.\n\n"
            "## Changes Made\n- None.\n\n"
            "## Errors Encountered\n- None.\n\n"
            "## Open Questions / Next Steps\n- None.\n\n"
            "## Importance\n5\n"
        )

    @staticmethod
    def _loose_block() -> str:
        """Block with # Summary (single-hash) — recoverable by _normalize_summary."""
        return (
            "# Summary\n"
            "Implemented the widget.\n\n"
            "## Key Decisions\n"
            "- Used pattern X.\n"
        )

    def test_batch_recovers_loose_block(self, monkeypatch):
        """generate_summaries_batch: loose # Summary block is recovered (default enabled).

        With recovery enabled (default), the first block's '# Summary' heading is
        normalized to '## Summary' and the result is (text_with_##Summary, None),
        NOT (None, 'missing_section').
        """
        monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda sid: None)

        stdout_text = self._make_stdout(self._loose_block(), self._good_block())

        def fake_run(cmd, input=None, capture_output=False, text=False, timeout=None):
            class _R:
                returncode = 0
                stdout = stdout_text
                stderr = ""
            return _R()

        monkeypatch.setattr(obsidian_utils.subprocess, "run", fake_run)

        prep1 = dict(self._BASE_PREP)
        prep2 = dict(self._BASE_PREP)

        results = obsidian_utils.generate_summaries_batch(
            [prep1, prep2], model="haiku", timeout=30,
            project="test", vault_path="", sessions_folder="",
        )

        assert len(results) == 2
        text1, reason1 = results[0]
        assert reason1 is None, (
            f"recovery enabled: block 1 should be accepted; reason={reason1!r}"
        )
        assert text1 is not None, "recovery enabled: block 1 text must not be None"
        assert re.search(r"^## Summary\s*$", text1, re.MULTILINE), (
            f"recovered block must contain ^## Summary$; got:\n{text1!r}"
        )
        # Block 2 was already well-formed — must still pass.
        _, reason2 = results[1]
        assert reason2 is None, f"block 2 (well-formed) should also be accepted; {reason2!r}"

    def test_batch_recovery_disabled_yields_missing_section(self, monkeypatch):
        """generate_summaries_batch: with summary_recovery=False, a loose # Summary
        block returns (None, 'missing_section') instead of being recovered.

        Monkeypatches obsidian_utils.load_config (the function _summary_recovery_enabled
        calls directly) so the flag flip is exercised through the real wire-up path,
        not by stubbing the helper itself.
        """
        monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda sid: None)

        stdout_text = self._make_stdout(self._loose_block(), self._good_block())

        def fake_run(cmd, input=None, capture_output=False, text=False, timeout=None):
            class _R:
                returncode = 0
                stdout = stdout_text
                stderr = ""
            return _R()

        monkeypatch.setattr(obsidian_utils.subprocess, "run", fake_run)

        # Disable recovery via load_config — _summary_recovery_enabled() reads
        # load_config().get("summary_recovery", True), so patching here exercises
        # the real wire-up without bypassing _summary_recovery_enabled itself.
        def fake_load_config():
            cfg = dict(obsidian_utils._DEFAULTS)
            cfg["summary_recovery"] = False
            return cfg

        monkeypatch.setattr(obsidian_utils, "load_config", fake_load_config)

        prep1 = dict(self._BASE_PREP)
        prep2 = dict(self._BASE_PREP)

        results = obsidian_utils.generate_summaries_batch(
            [prep1, prep2], model="haiku", timeout=30,
            project="test", vault_path="", sessions_folder="",
        )

        assert len(results) == 2
        text1, reason1 = results[0]
        assert text1 is None, (
            f"recovery disabled: loose block should be rejected; text={text1!r}"
        )
        assert reason1 == "missing_section", (
            f"recovery disabled: expected 'missing_section', got {reason1!r}"
        )
