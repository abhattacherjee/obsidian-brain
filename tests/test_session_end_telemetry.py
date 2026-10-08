"""Tests for SessionEnd telemetry: _append_sessionend_log helper + _Outcome enum wraps."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Make hooks/ importable for in-process tests.
_HOOKS_DIR = str(Path(__file__).parent.parent / "hooks")
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)


def _hook_script_path() -> str:
    return str(Path(__file__).parent.parent / "hooks" / "obsidian_session_log.py")


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


class TestAppendSessionEndLog:
    @pytest.fixture(autouse=True)
    def _restore_obsidian_utils(self):
        """Reload obsidian_utils after each test to undo HOME-monkeypatched module constants."""
        yield
        import importlib
        import obsidian_utils
        # Selected native context owns the sink; no module reload is needed.

    def test_appends_one_line_with_expected_fields(self, tmp_path, monkeypatch):
        """Helper appends exactly one line with timestamp, event tag, project, sid, outcome, msgs, dur, detail."""
        monkeypatch.setenv("HOME", str(tmp_path))
        # Re-import obsidian_utils so the HOME-derived log path is picked up if cached.
        import importlib
        import obsidian_utils
        # Selected native context owns the sink; no module reload is needed.

        obsidian_utils._append_sessionend_log(
            project="myproj",
            session_id="sid-deadbeef-1234",
            outcome="OK_RAW_NOTE_ONLY",
            msgs=42,
            dur_min=12.5,
            detail="",
        )

        log_path = _selected_log_dir() / "obsidian-brain-hook.log"
        assert log_path.exists(), "telemetry log file was not created"
        content = log_path.read_text(encoding="utf-8")
        lines = [ln for ln in content.splitlines() if ln.strip()]
        assert len(lines) == 1, f"expected 1 line, got {len(lines)}: {content!r}"

        line = lines[0]
        # Format: "<UTC ISO8601> SessionEnd project=<p> sid=<s8> outcome=<O> msgs=<N> dur=<F> detail=<str>"
        assert "SessionEnd" in line
        assert "project=myproj" in line
        # sid is short-form (8 chars)
        assert "sid=sid-dead" in line
        assert "outcome=OK_RAW_NOTE_ONLY" in line
        assert "msgs=42" in line
        # dur is formatted with one decimal
        assert "dur=12.5" in line
        # detail field is present even when empty
        assert "detail=" in line

    def test_rotates_when_log_exceeds_cap(self, tmp_path, monkeypatch):
        """When log exceeds 100KB, helper rotates to .1 and starts a new file."""
        monkeypatch.setenv("HOME", str(tmp_path))
        import importlib
        import obsidian_utils
        # Selected native context owns the sink; no module reload is needed.

        log_dir = _selected_log_dir()
        log_dir.mkdir(exist_ok=True)
        log_path = log_dir / "obsidian-brain-hook.log"
        # Pre-fill the log past the rotation threshold.
        log_path.write_text("x" * (150 * 1024), encoding="utf-8")

        obsidian_utils._append_sessionend_log(
            project="rot", session_id="sid-rotate-aa", outcome="OK_RAW_NOTE_ONLY"
        )

        rotated = log_dir / "obsidian-brain-hook.log.1"
        assert rotated.exists(), "rotated file was not created"
        # New log file contains only the one fresh line.
        new_content = log_path.read_text(encoding="utf-8")
        new_lines = [ln for ln in new_content.splitlines() if ln.strip()]
        assert len(new_lines) == 1, f"new log should have only the single fresh line, got: {new_content!r}"
        assert "outcome=OK_RAW_NOTE_ONLY" in new_lines[0]

    def test_handles_missing_fields_with_defaults(self, tmp_path, monkeypatch):
        """Helper accepts only the required args (project, session_id, outcome) and uses safe defaults."""
        monkeypatch.setenv("HOME", str(tmp_path))
        import importlib
        import obsidian_utils
        # Selected native context owns the sink; no module reload is needed.

        obsidian_utils._append_sessionend_log(
            project="", session_id="", outcome="EXCEPTION"
        )

        log_path = _selected_log_dir() / "obsidian-brain-hook.log"
        content = log_path.read_text(encoding="utf-8")
        # Empty project becomes "unknown"; empty sid becomes "unknown"[:8]
        assert "project=unknown" in content
        assert "sid=unknown" in content
        assert "outcome=EXCEPTION" in content
        assert "msgs=0" in content
        assert "dur=0.0" in content

    def test_sanitizes_carriage_returns_and_tabs(self, tmp_path, monkeypatch):
        """Project/outcome/sid/detail with \\r or \\t produce a single line, not a corrupted multi-line entry."""
        monkeypatch.setenv("HOME", str(tmp_path))
        import importlib
        import obsidian_utils
        # Selected native context owns the sink; no module reload is needed.

        obsidian_utils._append_sessionend_log(
            project="my\rproj\twith\nbad",
            session_id="sid\rbad\t12",
            outcome="OK\rBAD",
            detail="multi\rline\twith\nstuff",
        )

        log_path = _selected_log_dir() / "obsidian-brain-hook.log"
        content = log_path.read_text(encoding="utf-8")
        # Exactly one logical line (one \n at end)
        assert content.count("\n") == 1, f"expected 1 line, got: {content!r}"
        # No \r or \t survived
        assert "\r" not in content
        assert "\t" not in content


# ---------------------------------------------------------------------------
# Outcome wrapping — subprocess-driven (drives the real hook entry point)
# ---------------------------------------------------------------------------


def _read_log_lines(tmp_path):
    """Helper: return the SessionEnd lines from the hook log, or [] if missing."""
    log_path = _selected_log_dir() / "obsidian-brain-hook.log"
    if not log_path.exists():
        return []
    return [
        ln for ln in log_path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and "SessionEnd" in ln
    ]




def _projects_dir(tmp_path, project_slug):
    """Create a fake CC projects dir for a given project so transcript_path passes validation."""
    # Claude Code's path-encoded slug: leading dash, underscores->hyphens
    cc_slug = "-" + project_slug.replace("_", "-")
    d = tmp_path / ".claude" / "projects" / cc_slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_config(tmp_path, **overrides):
    cfg = {
        "vault_path": str(tmp_path / "vault"),
        "sessions_folder": "claude-sessions",
        "auto_log_enabled": True,
        "min_messages": 3,
        "min_duration_minutes": 2,
    }
    cfg.update(overrides)
    cfg_path = tmp_path / ".claude" / "obsidian-brain-config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    # Make sure the vault exists if vault_path is set so write attempts don't fail elsewhere.
    if cfg.get("vault_path"):
        (Path(cfg["vault_path"]) / cfg["sessions_folder"]).mkdir(parents=True, exist_ok=True)
    return cfg_path


def _make_jsonl(path, n_user_msgs, duration_sec):
    """Create a minimal JSONL with N user messages spanning duration_sec seconds."""
    import datetime as _dt
    start = _dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=_dt.timezone.utc)
    entries = []
    for i in range(n_user_msgs):
        ts = start + _dt.timedelta(seconds=i * (duration_sec / max(n_user_msgs, 1)))
        entries.append({
            "type": "user",
            "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "message": {"role": "user", "content": f"msg {i}"},
        })
    # Final assistant message at the end so duration metadata reflects duration_sec.
    end = start + _dt.timedelta(seconds=duration_sec)
    entries.append({
        "type": "assistant",
        "timestamp": end.isoformat().replace("+00:00", "Z"),
        "message": {"role": "assistant", "content": "ok"},
    })
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")








class TestWriteFailedOutcome:
    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses chmod restrictions")

    def test_sessionend_write_fail_logs_errno(self, selected_host_context):
        """The selected telemetry sink preserves errno text in a failure record."""
        import obsidian_utils
        from session_auxiliary_state import directory

        simulated_err = "OSError: [Errno 28] No space left on device: /tmp/foo.md.tmp"
        obsidian_utils._append_sessionend_log(
            project="synthetic-project", session_id=selected_host_context.native_session_id,
            outcome="WRITE_FAILED", detail=simulated_err,
        )
        log_path = directory(selected_host_context, "logs") / "obsidian-brain-hook.log"
        lines = [line for line in log_path.read_text().splitlines() if "SessionEnd" in line]
        assert len(lines) == 1
        assert "outcome=WRITE_FAILED" in lines[0]
        assert "Errno 28" in lines[0]
        assert "No space left on device" in lines[0]
        assert lines[0].rfind("detail=") > lines[0].rfind("dur=")




class TestExceptionOutcome:

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses chmod restrictions")

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses chmod restrictions")

    def test_main_logs_exception_when_run_raises(self, selected_host_context, monkeypatch):
        """In-process strict test: main() catches _run() exceptions and logs EXCEPTION
        with project/sid context if _run() updated _LAST_PROJECT/_LAST_SESSION_ID first."""
        import obsidian_session_log
        from session_auxiliary_state import directory

        # Pretend _run() got far enough to update _LAST_* before raising
        obsidian_session_log._LAST_PROJECT = "myproj"
        obsidian_session_log._LAST_SESSION_ID = "sid-explode-1234"

        # Make _run raise
        def _boom():
            raise RuntimeError("simulated mid-run failure")
        monkeypatch.setattr(obsidian_session_log, "_run", _boom)

        # Avoid sys.exit propagating out of pytest
        with pytest.raises(SystemExit) as exc_info:
            obsidian_session_log.main()
        assert exc_info.value.code == 0  # hook exits 0 even on exception

        log_path = directory(selected_host_context, "logs") / "obsidian-brain-hook.log"
        assert log_path.exists()
        content = log_path.read_text(encoding="utf-8")
        lines = [ln for ln in content.splitlines() if "SessionEnd" in ln]
        assert len(lines) == 1
        assert "outcome=EXCEPTION" in lines[0]
        assert "project=myproj" in lines[0]
        assert "sid=sid-expl" in lines[0]  # short_sid is first 8
        # detail should contain the exception repr (truncated to 200 chars)
        assert "RuntimeError" in lines[0] or "simulated" in lines[0]


# ---------------------------------------------------------------------------
# Unit tests for helper edge-cases (improve line coverage)
# ---------------------------------------------------------------------------


class TestProjectSlugForLog:
    """_project_slug_for_log: empty cwd → 'unknown' (line 73 coverage)."""

    def test_empty_cwd_returns_unknown(self):
        from obsidian_session_log import _project_slug_for_log
        assert _project_slug_for_log("") == "unknown"

    def test_nonempty_cwd_returns_slug(self):
        from obsidian_session_log import _project_slug_for_log
        assert _project_slug_for_log("/home/user/myproject") == "myproject"


class TestCleanupSessionCacheException:
    """_cleanup_session_cache: exception branch (lines 90-91 coverage)."""

    def test_exception_in_unlink_is_swallowed(self, tmp_path, monkeypatch):
        """If os.unlink raises, _cleanup_session_cache must not propagate."""
        import obsidian_session_log

        # Monkeypatch os.path.exists to return True (so unlink is attempted)
        # and os.unlink to raise an OSError.
        monkeypatch.setattr("obsidian_session_log.os.path.exists", lambda p: True)
        monkeypatch.setattr("obsidian_session_log.os.unlink", lambda p: (_ for _ in ()).throw(OSError("simulated")))

        # Must not raise, and must exit cleanly.
        obsidian_session_log._cleanup_session_cache("any-session-id")

from test_snapshot_e2e import selected_host_context


def _native_session_child(context, payload=None, *, script='obsidian_session_log.py', driver=True):
    root=Path(__file__).resolve().parents[1]
    payload = payload if payload is not None else {'session_id':context.native_session_id,
        'cwd':str(context.worktree),'transcript_path':str(context.transcript_path),'trigger':'manual'}
    if driver:
        command=[sys.executable,str(root/'tests/native_hook_test_driver.py'),
            '--host',context.host,'--client',context.client,'--session-id',context.native_session_id,
            '--cwd',str(context.worktree),'--vault',str(context.vault_path),
            '--config',str(context.config_path),'--resource-root',str(context.resource_root),
            '--index',str(context.index_path),'--state',str(context.state_path),
            '--transcript',str(context.transcript_path),script]
    else:
        command=[sys.executable,str(root/'hooks/brain_cli.py'),
            '--host',context.host,'--client',context.client,'--event','session_end',
            '--config',str(context.config_path),'--resource-root',str(context.resource_root),
            '--index',str(context.index_path),'--state',str(context.state_path),'hook']
    raw=payload if isinstance(payload,str) else json.dumps(payload)
    result=subprocess.run(command,input=raw,capture_output=True,text=True,
        cwd=context.worktree,env=dict(os.environ,CLAUDE_CODE_SESSION_ID='foreign-id',
                                     CODEX_THREAD_ID='foreign-thread'),timeout=5)
    assert result.returncode==0,result.stderr
    if driver:
        proof=json.loads(next(line.split(':',1)[1] for line in result.stderr.splitlines()
                              if line.startswith('NATIVE_CONTEXT_PROOF:')))
        for field in ('host','client','native_session_id','vault_path','index_path','state_path'):
            assert proof[field]==str(getattr(context,field))
        return result,proof
    return result,None


def _native_config(context, **changes):
    value=json.loads(context.config_path.read_text())
    value.update(changes)
    context.config_path.write_text(json.dumps(value))


@pytest.mark.parametrize('raw',['','{}','{"cwd":"/synthetic"}','{bad'])
def test_native_sessionend_invalid_identity_fails_open_without_capture(selected_host_context,raw):
    result,_=_native_session_child(selected_host_context,raw,driver=False)
    assert result.stdout==''
    assert result.stderr
    assert list(selected_host_context.vault_path.rglob('*.md'))==[]


def test_native_sessionend_rejects_outside_transcript(selected_host_context,tmp_path):
    outside=tmp_path/'unselected-source.jsonl';outside.write_text('{}\n')
    result,_=_native_session_child(selected_host_context,{'session_id':selected_host_context.native_session_id,
        'cwd':str(selected_host_context.worktree),'transcript_path':str(outside)},driver=False)
    assert json.loads(result.stderr)['code']=='transcript_outside_host'
    assert list(selected_host_context.vault_path.rglob('*.md'))==[]


def test_native_auto_log_disabled_does_not_capture_or_recover(selected_host_context):
    context=selected_host_context
    _native_config(context,auto_log_enabled=False)
    _,proof=_native_session_child(context)
    assert proof['mutation_contexts']==[]
    assert list(context.vault_path.rglob('*.md'))==[]
    assert not context.index_path.parent.joinpath('.'+context.index_path.name+'.coordination','state.sqlite3').exists()


def test_native_sessionend_refuses_missing_vault_configuration(selected_host_context):
    context=selected_host_context
    _native_config(context,vault_path='')
    result,_=_native_session_child(context,{'session_id':context.native_session_id,'cwd':str(context.worktree)},driver=False)
    assert json.loads(result.stderr)['code']=='vault_missing'
    assert list(context.vault_path.rglob('*.md'))==[]


def test_native_sessionend_empty_source_stays_unpublished(selected_host_context):
    context=selected_host_context
    context.transcript_path.write_text('')
    result,proof=_native_session_child(context)
    assert proof['mutation_contexts']==[]
    assert '[obsidian-brain] capture pending: Native transcript identity is unverified' in result.stderr
    assert list(context.vault_path.rglob('*.md'))==[]


@pytest.mark.parametrize('threshold',['messages','duration'])
def test_native_sessionend_threshold_retains_facts_without_publication(selected_host_context,threshold):
    context=selected_host_context
    if threshold=='messages':
        _native_config(context,min_messages=99)
    else:
        rows=[json.loads(line) for line in context.transcript_path.read_text().splitlines()]
        for i,row in enumerate(rows):row['timestamp']='2026-10-05T00:00:%02dZ'%i
        context.transcript_path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        _native_config(context,min_duration_minutes=2)
    _,proof=_native_session_child(context)
    assert proof['mutation_contexts']==[]
    assert list(context.vault_path.rglob('*.md'))==[]
    import note_transactions
    with note_transactions.connect_coordination(context) as connection:
        row=connection.execute('SELECT state,messages,note FROM source_sessions WHERE scope=?',
                               (context.session_key,)).fetchone()
        assert row==('ended',3,None)
        assert connection.execute('SELECT COUNT(*) FROM native_events WHERE scope=?',
                                  (context.session_key,)).fetchone()[0]==4


def test_native_sessionend_write_failure_keeps_input_for_retry(selected_host_context):
    context=selected_host_context
    folder=context.vault_path/'claude-sessions';folder.mkdir()
    folder.chmod(0o500)
    try:
        result,proof=_native_session_child(context)
        assert '[obsidian-brain] capture pending: Source input remains pending.' in result.stderr
        assert list(context.vault_path.rglob('*.md'))==[]
    finally:
        folder.chmod(0o700)
    _native_session_child(context)
    assert len(list(context.vault_path.rglob('*.md')))==1


def test_native_sessionend_updates_snapshot_parent_even_after_threshold_change(selected_host_context):
    context=selected_host_context
    _native_session_child(context,script='obsidian_context_snapshot.py')
    _native_config(context,min_messages=99)
    _native_session_child(context)
    paths=list(context.vault_path.rglob('*.md'))
    assert len(paths)==2
    parent=next(p for p in paths if 'snapshot' not in p.name)
    import obsidian_utils
    assert obsidian_utils.parse_frontmatter_field(parent.read_text(),'capture_state')=='ended'


def test_native_sessionend_success_records_selected_origin_and_retained_facts(selected_host_context):
    context=selected_host_context
    _,proof=_native_session_child(context)
    assert proof['mutation_contexts']
    [note]=list(context.vault_path.rglob('*.md'))
    import obsidian_utils
    fields=note.read_text()
    assert obsidian_utils.parse_frontmatter_field(fields,'agent_provider')==context.host
    assert obsidian_utils.parse_frontmatter_field(fields,'agent_session_id')==context.native_session_id
    assert obsidian_utils.parse_frontmatter_field(fields,'capture_state')=='ended'
    assert 'Keep all synthetic facts.' in fields
    assert 'foreign-thread' not in fields


def test_native_sessionend_bad_config_fails_open_without_mutation(selected_host_context):
    context=selected_host_context
    context.config_path.write_text('{bad')
    result,_=_native_session_child(context,driver=False)
    assert json.loads(result.stderr)['code']=='config_invalid'
    assert list(context.vault_path.rglob('*.md'))==[]


def test_native_sessionend_unreadable_source_does_not_bless_lost_input(selected_host_context):
    context=selected_host_context
    before=context.transcript_path.read_bytes()
    context.transcript_path.chmod(0)
    try:
        result,proof=_native_session_child(context)
        assert proof['mutation_contexts']==[]
        assert '[obsidian-brain] capture pending: Native transcript identity is unverified' in result.stderr
        assert list(context.vault_path.rglob('*.md'))==[]
    finally:
        context.transcript_path.chmod(0o600)
    assert context.transcript_path.read_bytes()==before

pytestmark = pytest.mark.usefixtures("selected_host_context")

def _selected_log_dir():
    from runtime_context import current_runtime_context
    from session_auxiliary_state import directory
    return directory(current_runtime_context(), "logs")
