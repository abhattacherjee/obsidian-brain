"""Analysis skips this plugin's capture hooks without changing host policy."""

import importlib
import time

import pytest


@pytest.mark.parametrize("module_name,entry", [
    ("obsidian_context_snapshot", "_run"),
    ("obsidian_session_hint", "_run"),
    ("obsidian_session_log", "_run"),
    ("obsidian_retro_gate", "main"),
])
def test_nested_analysis_returns_before_context_or_stdin(monkeypatch, module_name, entry):
    import runtime_context

    def forbidden():
        raise AssertionError("Nested analysis reached capture context")

    monkeypatch.setattr(runtime_context, "current_runtime_context", forbidden)
    monkeypatch.setenv("OBSIDIAN_BRAIN_NESTED_AI", "1")
    assert getattr(importlib.import_module(module_name), entry)() is None


@pytest.mark.parametrize("marker", ["", "0", "true"])
@pytest.mark.parametrize("module_name,entry", [
    ("obsidian_context_snapshot", "_run"),
    ("obsidian_session_hint", "_run"),
    ("obsidian_session_log", "_run"),
    ("obsidian_retro_gate", "main"),
])
def test_other_marker_values_do_not_skip_capture(monkeypatch, marker, module_name, entry):
    import runtime_context

    def reached():
        raise RuntimeError("context reached")

    monkeypatch.setattr(runtime_context, "current_runtime_context", reached)
    monkeypatch.setenv("OBSIDIAN_BRAIN_NESTED_AI", marker)
    with pytest.raises(RuntimeError, match="context reached"):
        getattr(importlib.import_module(module_name), entry)()


def test_native_nested_guard_runs_before_dispatch(monkeypatch):
    import native_lifecycle

    monkeypatch.setenv("OBSIDIAN_BRAIN_NESTED_AI", "1")
    assert native_lifecycle.dispatch(None, "unknown", {}, time.monotonic()) is None
    monkeypatch.delenv("OBSIDIAN_BRAIN_NESTED_AI")
    with pytest.raises(ValueError, match="Unknown lifecycle event"):
        native_lifecycle.dispatch(None, "unknown", {}, time.monotonic())
