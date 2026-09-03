# tests/conftest.py
"""Shared fixtures for obsidian-brain test suite."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Add hooks/ to sys.path so test modules can import obsidian_utils etc.
_HOOKS_DIR = os.path.join(os.path.dirname(__file__), "..", "hooks")
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_HOOKS_DIR))

# Add repo root to sys.path so tests can use the `hooks.<module>` package form
# (in addition to the bare `obsidian_utils` form used by older tests).
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, os.path.abspath(_REPO_ROOT))


@pytest.fixture(autouse=True)
def _reset_session_resolution_state():
    """Clear obsidian_utils' one-shot WARN registries and transcript memo (#260).

    They are module-level BY DESIGN — _get_session_id_fast() runs once per note
    inside read_note_metadata(), so a WARN on that path must be said once per
    process, not once per note. That same statefulness makes tests order-
    dependent: a test asserting "this WARN is emitted" would silently pass or
    fail depending on whether an earlier test already consumed the key.
    """
    import obsidian_utils

    for name in (
        "_ambiguous_project_dirs_warned",
        "_sole_match_not_cwd_warned",
        "_unknown_sid_warned",
        "_transcript_dir_arbitration",
        # #330 task 2: the env-layer's one-shot WARN registry and its
        # transcript-existence memo. Same statefulness hazard as the others
        # above — a test asserting the WARN fires would pass or fail
        # depending on whether an earlier test already consumed the
        # (project, sid) key.
        "_env_sid_no_transcript_warned",
        "_env_sid_transcript_checked",
        # #330 task 3: the ambiguous-concurrent-sessions one-shot WARN
        # registry. Same statefulness hazard — missing from this fixture
        # meant a test asserting the WARN fires could pass or fail depending
        # on whether an earlier test already consumed the sids-tuple key
        # (#330 review item 7).
        "_concurrent_sids_warned",
        # #330 review item 8: malformed CLAUDE_CODE_SESSION_ID one-shot WARN.
        "_env_sid_malformed_warned",
        # #330 review item 2: resolve_source_session_note's contradiction
        # one-shot WARN.
        "_crossed_source_session_warned",
        # Process-lifetime snapshot index (#70). Keyed by resolved sessions
        # folder, so cross-test collisions are unlikely — but pytest can hand
        # the same tmp_path prefix to a re-run and the memo would then answer
        # from the previous test's file set. Clearing is one dict op.
        "_snapshot_index_cache",
    ):
        getattr(obsidian_utils, name).clear()
    yield


@pytest.fixture
def tmp_vault(tmp_path):
    """Create a temp vault with sessions and insights directories."""
    sessions = tmp_path / "claude-sessions"
    insights = tmp_path / "claude-insights"
    sessions.mkdir()
    insights.mkdir()
    return tmp_path


@pytest.fixture
def mock_config(tmp_vault, monkeypatch):
    """Patch load_config() to return config pointing at tmp_vault."""
    import obsidian_utils

    config = {
        "vault_path": str(tmp_vault),
        "sessions_folder": "claude-sessions",
        "insights_folder": "claude-insights",
        "dashboards_folder": "claude-dashboards",
        "min_messages": 3,
        "min_duration_minutes": 2,
        "summary_model": "haiku",
        "auto_log_enabled": True,
        "snapshot_on_compact": True,
        "snapshot_on_clear": True,
    }
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: config)
    return config


@pytest.fixture
def sample_session_note(tmp_vault):
    """Create a session note with valid frontmatter + summary sections."""
    note_path = tmp_vault / "claude-sessions" / "2026-04-10-test-project-abcd.md"
    note_path.write_text(
        "---\n"
        "type: claude-session\n"
        "date: 2026-04-10\n"
        "session_id: test-session-id-1234\n"
        "project: test-project\n"
        'project_path: "/tmp/test-project"\n'
        'git_branch: "feature/test"\n'
        "duration_minutes: 30.5\n"
        "tags:\n"
        "  - claude/session\n"
        "  - claude/project/test-project\n"
        "  - claude/auto\n"
        "status: summarized\n"
        "---\n"
        "\n"
        "# Session: test-project (feature/test)\n"
        "\n"
        "## Summary\n"
        "Implemented the frobulator widget with TDD approach.\n"
        "\n"
        "## Key Decisions\n"
        "- Used factory pattern for widget creation.\n"
        "\n"
        "## Changes Made\n"
        "- `src/frobulator.py` — new widget implementation\n"
        "\n"
        "## Errors Encountered\n"
        "None.\n"
        "\n"
        "## Open Questions / Next Steps\n"
        "- [ ] Add integration tests for frobulator\n"
        "- [ ] Review PR #42\n",
        encoding="utf-8",
    )
    return note_path


@pytest.fixture
def sample_unsummarized_note(tmp_vault):
    """Create a note with status: auto-logged and placeholder summary."""
    note_path = tmp_vault / "claude-sessions" / "2026-04-10-test-project-ef01.md"
    note_path.write_text(
        "---\n"
        "type: claude-session\n"
        "date: 2026-04-10\n"
        "session_id: unsummarized-session-id\n"
        "project: test-project\n"
        'project_path: "/tmp/test-project"\n'
        'git_branch: "develop"\n'
        "duration_minutes: 15.0\n"
        "tags:\n"
        "  - claude/session\n"
        "  - claude/project/test-project\n"
        "  - claude/auto\n"
        "status: auto-logged\n"
        "---\n"
        "\n"
        "# Session: test-project (develop)\n"
        "\n"
        "## Summary\n"
        "Session in **test-project** (15.0 min). "
        "AI summary unavailable \u2014 raw extraction below.\n"
        "\n"
        "## Conversation (raw)\n"
        "**User:** hello\n"
        "**Assistant:** hi there\n",
        encoding="utf-8",
    )
    return note_path


@pytest.fixture
def sample_jsonl(tmp_path):
    """Create a minimal JSONL transcript with user/assistant messages."""
    jsonl_path = tmp_path / "transcript.jsonl"
    entries = [
        {
            "type": "user",
            "timestamp": "2026-04-10T10:00:00Z",
            "message": {"role": "user", "content": "Fix the login bug"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-04-10T10:01:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll look at the login handler."},
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/src/login.py"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-04-10T10:02:00Z",
            "message": {"role": "user", "content": "Great, now deploy it"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-04-10T10:05:00Z",
            "message": {
                "role": "assistant",
                "content": "Done. The fix is deployed.",
            },
        },
    ]
    jsonl_path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n",
        encoding="utf-8",
    )
    return jsonl_path


@pytest.fixture(autouse=True)
def _isolate_summarizer_sink_globally(tmp_path_factory, monkeypatch):
    """Belt-and-suspenders: redirect summarizer_metrics.METRICS_PATH to a tmp
    path for every test in the suite. Prevents accidental pollution of
    ~/.claude/obsidian-brain-summarizer-metrics.jsonl when a future test
    forgets the per-class fixture."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
    try:
        import summarizer_metrics
    except ImportError:
        return  # sink module not present in some test contexts
    safe_path = tmp_path_factory.mktemp("metrics") / "summarizer-metrics.jsonl"
    monkeypatch.setattr(summarizer_metrics, "METRICS_PATH", safe_path)


@pytest.fixture(autouse=True)
def _isolate_secure_dir_globally(tmp_path_factory, monkeypatch):
    """Point obsidian-brain's secure dir (and its lock subdir) at a throwaway
    per-test location so no test writes to the real ~/.claude/obsidian-brain/
    (notably the cross-plugin dedup lock files written by claim_hook_run).

    Tests that already monkeypatch _SECURE_DIR/_LOCK_DIR themselves (e.g. the
    lock_dir fixture in test_hook_dedup_guard.py, test_security.py) are
    unaffected: their explicit setattr runs after autouse and simply re-points
    both attributes to their own tmp — last setattr wins, both are tmp.

    Subprocess tests (test_snapshot_e2e.py, two-process test in
    test_hook_dedup_guard.py) spawn child processes that re-import obsidian_utils
    fresh; they are already isolated via HOME redirection and are unaffected by
    this in-process monkeypatch.

    _CACHE_PREFIX and _BOOTSTRAP_PREFIX are also patched to consistent tmp-based
    paths so that tests checking `x.startswith(_SECURE_DIR)` still hold."""
    import obsidian_utils
    secure = tmp_path_factory.mktemp("ob-secure")
    monkeypatch.setattr(obsidian_utils, "_SECURE_DIR", str(secure))
    monkeypatch.setattr(obsidian_utils, "_LOCK_DIR", str(secure / "locks"))
    monkeypatch.setattr(obsidian_utils, "_CACHE_PREFIX", str(secure / "cache-"))
    monkeypatch.setattr(obsidian_utils, "_BOOTSTRAP_PREFIX", str(secure / "sid-"))


@pytest.fixture(autouse=True)
def _isolate_vault_index_db_globally(tmp_path_factory, monkeypatch):
    """Redirect the default index DB to a per-test tmp path so an un-isolated
    in-process call (e.g. deep_analysis_pipeline / obsidian_utils indexing with
    no db_path) cannot reach the production DB. The _connect() guard is the
    backstop; this fixture is the belt (#192).

    Tests that pass an explicit db_path are unaffected (they ignore the env).
    Subprocess tests inherit OBSIDIAN_BRAIN_DB when they copy the parent environment (os.environ.copy()), which the existing subprocess tests do.
    """
    db = tmp_path_factory.mktemp("vidx") / "test-vault.db"
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", str(db))


@pytest.fixture(autouse=True)
def _isolate_acted_items_path_globally(tmp_path_factory, monkeypatch):
    """Redirect deep_cli._ACTED_ITEMS_PATH to a per-test tmp file so tests that
    call run_batch_edit never read/write/remove the REAL
    ~/.claude/obsidian-brain/deep-acted-items.json. That real-state mutation
    caused a non-reproducible flake in
    test_deep_cli.py::test_guard_b_ambiguous_text_match_refuses (#201 round-3).

    deep_cli is only present once hooks/ is on sys.path (added at module import
    above); if it can't be imported in a given context, this is a no-op."""
    try:
        import deep_cli
    except ImportError:
        return  # module not present in some test contexts
    acted = tmp_path_factory.mktemp("acted") / "deep-acted-items.json"
    monkeypatch.setattr(deep_cli, "_ACTED_ITEMS_PATH", str(acted))


@pytest.fixture(autouse=True)
def _isolate_harness_session_id_globally(monkeypatch):
    """Clear CLAUDE_CODE_SESSION_ID for every test. The suite runs inside a
    live Claude Code session, so this is already set in pytest's own
    environment to the developer's real session id. Without this fixture, a
    resolver test would assert against that live value instead of the
    fixture it set up (#330).

    Subprocess tests that do os.environ.copy() simply inherit the absence,
    same as _isolate_vault_index_db_globally above; the resolver's layer 0
    now reads this var (#330 task 2), so without this fixture the developer's
    real session id would leak into resolution tests via the ambient
    environment rather than the fixture each test explicitly sets up."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
