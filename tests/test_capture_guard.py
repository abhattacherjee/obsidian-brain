"""Tests for the auto-capture config defaults and the worker recursion guard (#425)."""

import ast
import io
import json
import sys
from pathlib import Path

import pytest

import obsidian_utils

HOOKS = Path(__file__).resolve().parent.parent / "hooks"


def test_defaults_include_auto_capture(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    monkeypatch.setattr(obsidian_utils, "_CONFIG_PATH", cfg)
    config = obsidian_utils.load_config(fresh=True)
    assert config["auto_capture_enabled"] is True
    assert config["auto_capture_interval_minutes"] == 10


def test_is_capture_worker(monkeypatch):
    monkeypatch.delenv("OBSIDIAN_BRAIN_WORKER", raising=False)
    assert obsidian_utils.is_capture_worker() is False
    monkeypatch.setenv("OBSIDIAN_BRAIN_WORKER", "1")
    assert obsidian_utils.is_capture_worker() is True
    monkeypatch.setenv("OBSIDIAN_BRAIN_WORKER", "0")
    assert obsidian_utils.is_capture_worker() is False


def test_worker_env_sets_guard_and_keeps_environment(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "kept")
    monkeypatch.delenv("OBSIDIAN_BRAIN_WORKER", raising=False)
    env = obsidian_utils.worker_env()
    assert env["OBSIDIAN_BRAIN_WORKER"] == "1"
    assert env["SOME_VAR"] == "kept"
    assert "OBSIDIAN_BRAIN_WORKER" not in __import__("os").environ  # a copy, not a mutation


def test_check_items_worker_env_matches_utils(monkeypatch):
    import check_items_cli

    monkeypatch.delenv(obsidian_utils.WORKER_ENV_VAR, raising=False)
    assert check_items_cli.worker_env()[obsidian_utils.WORKER_ENV_VAR] == "1"


def _claude_p_calls(path: Path):
    """Yield (lineno, has_env) for every subprocess.run call that runs `claude -p`."""
    tree = ast.parse(path.read_text())
    source = path.read_text()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "run" or getattr(node.func.value, "id", None) != "subprocess":
            continue
        first = ast.get_source_segment(source, node.args[0]) if node.args else ""
        if '"claude"' in first or first == "cmd":
            yield node.lineno, any(k.arg == "env" for k in node.keywords)


@pytest.mark.parametrize("name", ["obsidian_utils.py", "check_items_cli.py"])
def test_every_claude_p_call_passes_worker_env(name):
    calls = list(_claude_p_calls(HOOKS / name))
    assert calls, f"expected claude -p calls in {name}"
    missing = [line for line, has_env in calls if not has_env]
    assert missing == [], f"{name}: claude -p calls without env= at lines {missing}"


class _CountingStdin(io.StringIO):
    reads = 0

    def read(self, *a):
        type(self).reads += 1
        return super().read(*a)


@pytest.mark.parametrize(
    "module,entry",
    [
        ("obsidian_session_log", "_run"),
        ("obsidian_session_hint", "_run"),
        ("obsidian_context_snapshot", "_run"),
        ("obsidian_retro_gate", "main"),
    ],
)
def test_hooks_noop_under_guard(module, entry, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_BRAIN_WORKER", "1")
    _CountingStdin.reads = 0
    monkeypatch.setattr(sys, "stdin", _CountingStdin(json.dumps({"session_id": "abc"})))
    mod = __import__(module)
    try:
        getattr(mod, entry)()
    except SystemExit:
        pass
    assert _CountingStdin.reads == 0, f"{module}.{entry} read stdin under the worker guard"
