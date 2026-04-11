# tests/test_obsidian_utils.py
"""Tests for obsidian_utils.py — config, metadata, messages, I/O, upgrade, sampling."""

import hashlib
import json
import os
import uuid

import pytest

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
        assert result is True
        written = (tmp_vault / "claude-sessions" / "test-note.md").read_text(encoding="utf-8")
        assert written == content

    def test_write_vault_note_creates_dirs(self, tmp_vault):
        """Creates missing directories."""
        content = "# New Folder Note\n"
        result = obsidian_utils.write_vault_note(
            str(tmp_vault), "new-folder/sub-folder", "note.md", content
        )
        assert result is True
        assert (tmp_vault / "new-folder" / "sub-folder" / "note.md").exists()

    def test_write_vault_note_permissions(self, tmp_vault):
        """Written file has 0o644 permissions."""
        obsidian_utils.write_vault_note(
            str(tmp_vault), "claude-sessions", "perm-test.md", "content\n"
        )
        note_path = tmp_vault / "claude-sessions" / "perm-test.md"
        mode = oct(note_path.stat().st_mode & 0o777)
        assert mode == oct(0o644)


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
    assert "missing" in status["message"]


def test_build_context_brief_prepends_hook_status(tmp_path):
    """build_context_brief prepends the hook_status_line when provided."""
    import obsidian_utils

    vault = tmp_path / "vault"
    (vault / "sessions").mkdir(parents=True)
    (vault / "insights").mkdir(parents=True)

    status_line = "[OK] SessionStart hook fired; bootstrap matches current session"
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
