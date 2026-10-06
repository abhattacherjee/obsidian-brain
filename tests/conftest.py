# tests/conftest.py
"""Shared fixtures for obsidian-brain test suite."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import pwd
_ACCOUNT_COORDINATION_ROOT = (Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local' / 'state').resolve()
_SYSTEM_COORDINATION_ROOT = (Path('/var/tmp') / ('obsidian-brain-state-' + str(os.getuid()))).resolve()


def _assert_test_coordination_root(value):
    """Refuse actual account and UID fallback state before a test can write."""
    if not isinstance(value, (str, Path)) or not Path(value).is_absolute():
        raise AssertionError('Test coordination requires an explicit private XDG_STATE_HOME')
    path = Path(value).resolve()
    if any(path == live or path.is_relative_to(live)
           for live in (_ACCOUNT_COORDINATION_ROOT, _SYSTEM_COORDINATION_ROOT)):
        raise AssertionError('Tests cannot use actual account or UID fallback coordination state')

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
def _private_coordination_state(tmp_path_factory, monkeypatch):
    """Never let transaction tests use the account's real durable state."""
    import shutil
    root = tmp_path_factory.mktemp('private-coordination-state')
    root.chmod(0o700)
    monkeypatch.setenv('XDG_STATE_HOME', str(root))
    import importlib
    for name in ('note_transactions', 'hooks.note_transactions'):
        module = importlib.import_module(name)
        original = module.coordination_path
        def guarded(context, original=original):
            _assert_test_coordination_root(context.coordination_root)
            return original(context)
        monkeypatch.setattr(module, 'coordination_path', guarded)
    yield root
    shutil.rmtree(root)


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
        # #330 review item 8: malformed CLAUDE_CODE_SESSION_ID one-shot WARN.
        "_env_sid_malformed_warned",
        # #362: foreign-host (Codex marker) one-shot WARN.
        "_foreign_host_warned",
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
    import hooks.obsidian_utils as qualified_utils
    secure = tmp_path_factory.mktemp("ob-secure")
    for module in (obsidian_utils, qualified_utils):
        monkeypatch.setattr(module, "_SECURE_DIR", str(secure))
        monkeypatch.setattr(module, "_LOCK_DIR", str(secure / "locks"))
        monkeypatch.setattr(module, "_CACHE_PREFIX", str(secure / "cache-"))
        monkeypatch.setattr(module, "_BOOTSTRAP_PREFIX", str(secure / "sid-"))

    # Classifier scratch files have an independent state helper. Keep it in
    # the same sandbox, while preserving tests that explicitly select HOME.
    import importlib
    from pathlib import Path
    original_home = Path.home()
    for name, helper in (
        ("check_items_cli", "_safe_workdir"),
        ("hooks.check_items_cli", "_safe_workdir"),
        ("open_item_dedup", "_check_items_workdir"),
        ("hooks.open_item_dedup", "_check_items_workdir"),
    ):
        module = importlib.import_module(name)
        original_workdir = getattr(module, helper)

        def isolated_workdir(original=original_workdir):
            if Path.home() != original_home:
                return original()
            secure.mkdir(mode=0o700, parents=True, exist_ok=True)
            secure.chmod(0o700)
            return secure

        monkeypatch.setattr(module, helper, isolated_workdir)


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
    # Same for the Codex host markers (#362): a suite run from a Codex shell
    # would otherwise resolve every session id to 'unknown'.
    import obsidian_utils
    for name in obsidian_utils._CODEX_HOST_MARKERS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _doctor_tests_do_not_read_live_config(request, monkeypatch):
    """Legacy synthetic doctor issues use test folders, never user config."""
    module_name = getattr(request.module, "__name__", "")
    if "vault_doctor" not in module_name:
        return
    import obsidian_utils
    import hooks.obsidian_utils as qualified_utils
    isolated_config = lambda context=None: dict(context.config) if context is not None else {}
    monkeypatch.setattr(obsidian_utils, "load_config", isolated_config)
    monkeypatch.setattr(qualified_utils, "load_config", isolated_config)


@pytest.fixture(autouse=True)
def _block_unmocked_native_ai_processes(monkeypatch, _private_coordination_state):
    """Tests must replace native AI transport before dispatching model jobs."""
    import shlex
    import subprocess
    original = subprocess.Popen

    def guarded(args, *positional, **kwargs):
        environment = kwargs.get('env')
        environment = os.environ if environment is None else environment
        _assert_test_coordination_root(environment.get('XDG_STATE_HOME'))
        values = shlex.split(args) if isinstance(args, str) else list(args)
        commands = [Path(str(value)).name for value in values]
        if commands and (commands[0] in {"claude", "codex"} or
                         commands[0] == "env" and any(value in {"claude", "codex"} for value in commands[1:])):
            pytest.fail("Native AI subprocess transport must be mocked in tests", pytrace=False)
        return original(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded)


@pytest.fixture
def native_ai_frontend(selected_host_context, monkeypatch):
    """Frontend tests use the selected invoking host and replace AI transport."""
    import ai_backend
    import native_ai_test_adapter
    from runtime_context import using_runtime_context
    context = selected_host_context
    monkeypatch.setattr(ai_backend, "execute_ai", native_ai_test_adapter.execute_ai)
    with using_runtime_context(context):
        yield context


# Invoking-host conformance uses an explicit active context. Individual modules
# may specialize its vault/config fixture while keeping this host contract.
from parity_test_helpers import host, selected_host_context, host_identity_scenario  # noqa: E402, F401
