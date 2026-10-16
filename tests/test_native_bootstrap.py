"""Import diagnostics retain the explicitly selected home and native identity."""
import importlib
import io
import json
from test_runtime_context import runtime_case, runtime_case_data, selected_host_context


def test_import_failure_uses_recorded_selected_home(runtime_case, selected_host_context, monkeypatch):
    bootstrap = importlib.import_module("hook_bootstrap")
    context = selected_host_context
    foreign = runtime_case['home'] / 'foreign native home'
    monkeypatch.setenv('CODEX_HOME', str(foreign))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(foreign))
    monkeypatch.setattr(bootstrap.sys, "stdin", io.StringIO(json.dumps({
        "session_id": "foreign-payload-id", "cwd": str(foreign),
    })))
    bootstrap.log_import_failure("SessionStart", ImportError("synthetic missing dependency"), host=context.host)
    logfile = context.native_home / "obsidian-brain-hook.log"
    content = logfile.read_text()
    assert "outcome=IMPORT_FAILED" in content
    assert 'sid=' + context.native_session_id[:8] in content
    assert 'project=' + context.worktree.name.replace(' ', '_') in content
    assert 'foreign-payload-id' not in content
    assert not foreign.exists()
    notice = json.loads(bootstrap.session_start_notice("synthetic", host=context.host))
    assert str(logfile) in notice["hookSpecificOutput"]["additionalContext"]
