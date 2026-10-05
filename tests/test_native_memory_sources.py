"""A selected host must not silently borrow another host's memory files."""

import importlib

from test_runtime_context import resolve, runtime_case


def test_unsupported_codex_memory_is_reported_and_cannot_trigger_pruning(runtime_case):
    module = importlib.import_module("memory_sources")
    runtime = importlib.import_module("runtime_context")
    context = resolve(runtime_case)
    errors = []
    with runtime.using_runtime_context(context):
        assert module.memory_sources(module.detect_host(), errors=errors) == []
        assert errors[0]["unsupported"] is True
        assert module.failed_scopes(errors)[0] is True


def test_explicit_claude_context_ignores_inherited_codex_memory_marker(runtime_case, monkeypatch):
    module = importlib.import_module("memory_sources")
    runtime = importlib.import_module("runtime_context")
    monkeypatch.setenv("CODEX_THREAD_ID", "inherited")
    context = resolve(runtime_case, host="claude", client="claude-code")
    with runtime.using_runtime_context(context):
        assert module.detect_host() == "claude-code"
