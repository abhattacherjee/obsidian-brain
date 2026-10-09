"""CLI input boundaries must fail before resolving or mutating runtime state."""

import importlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_runtime_context import runtime_case, runtime_case_data, selected_host_context


def invoke(case, input_text, *options):
    module = importlib.import_module("brain_cli")
    output = io.StringIO()
    errors = io.StringIO()
    code = module.main([
        "--host", case["host"], "--client", "codex-cli" if case["host"] == "codex" else "claude-code",
        "--config", str(case["config_path"]), "--resource-root", str(case["resource_root"]),
        *options, "context",
    ], stdin=io.StringIO(input_text), stdout=output, stderr=errors)
    return code, output.getvalue(), errors.getvalue()


def test_context_command_uses_native_payload_and_explicit_paths(runtime_case):
    case = runtime_case
    code, output, errors = invoke(case, json.dumps({
        "session_id": "native-cli-thread", "cwd": str(case["worktree"]),
    }))
    assert code == 0 and not errors
    resolved = json.loads(output)
    assert resolved["native_session_id"] == "native-cli-thread"
    assert resolved["host"] == case["host"]
    assert resolved["vault_path"] == str(case["vault"])


@pytest.mark.parametrize("payload", ["[]", "{invalid", '"string"', "x" * (1024 * 1024 + 1),
                                    "[" * 2000 + "]" * 2000],
                         ids=["array", "malformed", "string", "oversized", "nested"])
def test_invalid_or_oversized_input_fails_with_a_structured_error(runtime_case, payload):
    code, output, errors = invoke(runtime_case, payload)
    assert code == 2 and not output
    assert json.loads(errors)["code"] == "input_invalid"


def test_cli_session_override_wins_over_payload(runtime_case):
    code, output, errors = invoke(runtime_case, json.dumps({
        "session_id": "payload-thread", "cwd": str(runtime_case["worktree"]),
    }), "--session-id", "explicit-thread")
    assert code == 0 and not errors
    assert json.loads(output)["native_session_id"] == "explicit-thread"


def test_native_process_rejects_excessive_json_nesting(runtime_case):
    script = Path(__file__).resolve().parents[1] / "hooks" / "brain_cli.py"
    result = subprocess.run([sys.executable, str(script), "--host", runtime_case["host"], "--client", "codex-cli" if runtime_case["host"] == "codex" else "claude-code", "context"],
                            input="[" * 2000 + "]" * 2000, text=True, capture_output=True,
                            cwd=runtime_case["worktree"], timeout=5)
    assert result.returncode == 2
    assert json.loads(result.stderr)["code"] == "input_invalid"


def test_parser_recursion_error_is_structured_before_runtime_resolution(runtime_case, monkeypatch):
    module = importlib.import_module("brain_cli")
    decode = json.loads
    def recurse(*args, **kwargs):
        raise RecursionError("synthetic native JSON decoder recursion limit")
    def resolve(*args, **kwargs):
        pytest.fail("Invalid JSON must not resolve runtime state")
    monkeypatch.setattr(module.json, "loads", recurse)
    monkeypatch.setattr(module, "resolve_runtime_context", resolve)
    code, output, errors = invoke(runtime_case, '{"nested": []}')
    assert code == 2 and not output
    assert decode(errors)["code"] == "input_invalid"
    assert "Traceback" not in errors


@pytest.mark.parametrize("sid", [None, "", "   ", 7])
@pytest.mark.parametrize("event", ["session_start", "stop", "pre_compact", "session_end"])
def test_native_hook_missing_payload_id_never_dispatches_or_writes(runtime_case, monkeypatch, sid, event):
    case = runtime_case
    monkeypatch.setenv("CODEX_THREAD_ID", "valid-parent-codex")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "valid-parent-claude")
    monkeypatch.setitem(sys.modules, "native_lifecycle", type("ForbiddenDispatch", (), {
        "dispatch": staticmethod(lambda *args: pytest.fail("Missing hook identity reached dispatch"))}))
    before = {str(path): path.read_bytes() for path in case["home"].rglob("*") if path.is_file()}
    vault_before = list(case["vault"].rglob("*"))
    home_before = list(case["home"].rglob("*"))
    coordination = Path(os.environ["XDG_STATE_HOME"])
    coordination_before = list(coordination.rglob("*"))
    payload = {"cwd": str(case["worktree"])}
    if sid is not None:
        payload["session_id"] = sid
    output, errors = io.StringIO(), io.StringIO()
    code = importlib.import_module("brain_cli").main([
        "--host", case["host"], "--client", "codex-cli" if case["host"] == "codex" else "claude-code",
        "--config", str(case["config_path"]), "--resource-root", str(case["resource_root"]),
        "--session-id", "explicit-cli-override", "--event", event, "hook",
    ], stdin=io.StringIO(json.dumps(payload)), stdout=output, stderr=errors)
    assert code == 0 and output.getvalue() == ""
    assert json.loads(errors.getvalue())["code"] == "session_missing"
    assert {str(path): path.read_bytes() for path in case["home"].rglob("*") if path.is_file()} == before
    assert list(case["vault"].rglob("*")) == vault_before
    assert list(case["home"].rglob("*")) == home_before
    assert list(coordination.rglob("*")) == coordination_before


def test_native_hook_uses_valid_payload_id_over_inherited_and_cli_ids(runtime_case, monkeypatch):
    case = runtime_case
    monkeypatch.setenv("CODEX_THREAD_ID", "valid-parent-codex")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "valid-parent-claude")
    observed = []
    monkeypatch.setitem(sys.modules, "native_lifecycle", type("ObservedDispatch", (), {
        "dispatch": staticmethod(lambda context, *args: observed.append(context.native_session_id))}))
    output, errors = io.StringIO(), io.StringIO()
    code = importlib.import_module("brain_cli").main([
        "--host", case["host"], "--client", "codex-cli" if case["host"] == "codex" else "claude-code",
        "--config", str(case["config_path"]), "--resource-root", str(case["resource_root"]),
        "--session-id", "explicit-cli-override", "--event", "stop", "hook",
    ], stdin=io.StringIO(json.dumps({"session_id": "payload-current", "cwd": str(case["worktree"])})),
        stdout=output, stderr=errors)
    assert code == 0 and errors.getvalue() == "" and output.getvalue() == ""
    assert observed == ["payload-current"]


@pytest.mark.parametrize("explicit", [False, True])
def test_authored_cli_context_keeps_explicit_and_selected_host_session_fallback(runtime_case, monkeypatch, explicit):
    monkeypatch.setenv("CODEX_THREAD_ID", "selected-codex")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "selected-claude")
    options = ("--session-id", "explicit-context") if explicit else ()
    code, output, errors = invoke(runtime_case, json.dumps({"cwd": str(runtime_case["worktree"])}), *options)
    assert code == 0 and not errors
    expected = "explicit-context" if explicit else ("selected-codex" if runtime_case["host"] == "codex" else "selected-claude")
    assert json.loads(output)["native_session_id"] == expected
