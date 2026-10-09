"""Selected memory discovery cannot borrow inherited foreign host storage."""
import importlib
from test_runtime_context import runtime_case, runtime_case_data, selected_host_context


def test_selected_memory_reports_supported_empty_or_explicit_unsupported(selected_host_context):
    module = importlib.import_module("memory_sources")
    errors = []
    assert module.detect_host() == ('claude-code' if selected_host_context.host == 'claude' else 'codex')
    assert module.memory_sources(module.detect_host(), errors=errors) == []
    if selected_host_context.host == 'codex':
        assert errors[0]["unsupported"] is True
        assert module.failed_scopes(errors)[0] is True
    else:
        assert errors == []
        assert module.failed_scopes(errors) == (False, set(), set())


def test_selected_context_ignores_inherited_foreign_memory_markers(selected_host_context, monkeypatch):
    module = importlib.import_module("memory_sources")
    monkeypatch.setenv("CODEX_THREAD_ID", "inherited-codex")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "inherited-claude")
    assert module.detect_host() == ('claude-code' if selected_host_context.host == 'claude' else 'codex')
