"""Native entry binding checks; these do not certify Desktop dispatch."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent


def entry():
    spec = importlib.util.spec_from_file_location("brain_native_entry_test", ROOT / "hooks" / "native_entry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("client", ["codex-cli", "codex-desktop"])
def test_explicit_current_client_reaches_adjacent_runtime(monkeypatch, client):
    module = entry()
    observed = []
    monkeypatch.setitem(sys.modules, "brain_cli", SimpleNamespace(main=lambda args, **kwargs: observed.append((args, kwargs))))
    assert module.run(["--host", "codex", "--client", client, "--event", "stop"], started_at=123.0) == 0
    args, kwargs = observed[0]
    assert args == ["--resource-root", str(ROOT), "--host", "codex", "--client", client, "--event", "stop", "hook"]
    assert kwargs == {"started_at": 123.0}


def test_unknown_codex_frontend_does_not_guess_from_origin_or_environment(monkeypatch, capsys):
    module = entry()
    monkeypatch.setenv("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "Codex Desktop")
    monkeypatch.setenv("CODEX_THREAD_ID", "native-session")
    monkeypatch.setitem(sys.modules, "brain_cli", SimpleNamespace(main=lambda *args, **kwargs: pytest.fail("Unresolved client reached runtime")))
    assert module.run(["--host", "codex", "--event", "session_start"]) == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert "client identity is unavailable" in output.err


@pytest.mark.parametrize("override", [["--resource-root", "/unrelated"], ["--resource-root=/unrelated"]])
def test_native_entry_refuses_another_resource_tree(monkeypatch, capsys, override):
    module = entry()
    monkeypatch.setitem(sys.modules, "brain_cli", SimpleNamespace(main=lambda *args, **kwargs: pytest.fail("Foreign tree reached runtime")))
    assert module.run(["--host", "claude", "--client", "claude-code", *override]) == 0
    assert "resource root must be adjacent" in capsys.readouterr().err


def test_import_or_runtime_failure_remains_fail_open(monkeypatch, capsys):
    module = entry()
    def fail(*args, **kwargs):
        raise RuntimeError("Sensitive diagnostic must not be echoed")
    monkeypatch.setitem(sys.modules, "brain_cli", SimpleNamespace(main=fail))
    assert module.run(["--host", "claude", "--client", "claude-code", "--event", "stop"]) == 0
    error = capsys.readouterr().err
    assert "capture is deferred" in error
    assert "Sensitive" not in error


@pytest.mark.parametrize("arguments", [["--host"], ["--client"]])
def test_malformed_binding_fails_open_without_entering_runtime(monkeypatch, capsys, arguments):
    module = entry()
    monkeypatch.setitem(sys.modules, "brain_cli", SimpleNamespace(main=lambda *args, **kwargs: pytest.fail("Malformed binding reached runtime")))
    assert module.run(arguments) == 0
    assert "arguments are invalid" in capsys.readouterr().err
