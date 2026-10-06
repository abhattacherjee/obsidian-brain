"""CLI input boundaries must fail before resolving or mutating runtime state."""

import importlib
import io
import json
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
