"""Import failure diagnostics stay inside the explicitly selected host."""

import importlib
import io
import json

from test_runtime_context import runtime_case


def test_codex_import_failure_uses_custom_home(runtime_case, monkeypatch):
    bootstrap = importlib.import_module("hook_bootstrap")
    monkeypatch.setattr(bootstrap.sys, "stdin", io.StringIO(json.dumps({
        "session_id": "codex-native", "cwd": str(runtime_case["worktree"]),
    })))
    bootstrap.log_import_failure("SessionStart", ImportError("synthetic missing dependency"), host="codex")
    logfile = runtime_case["codex_home"] / "obsidian-brain-hook.log"
    assert "outcome=IMPORT_FAILED" in logfile.read_text()
    assert not (runtime_case["home"] / ".claude").exists()
    notice = json.loads(bootstrap.session_start_notice("synthetic", host="codex"))
    assert "~/.claude" not in notice["hookSpecificOutput"]["additionalContext"]
