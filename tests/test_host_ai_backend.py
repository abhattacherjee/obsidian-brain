"""Native analysis cannot fall back to another provider or write model output."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import MappingProxyType

import pytest

import ai_backend as backend
from runtime_context import RuntimeContext


def context(tmp_path, host="claude", config=None):
    return RuntimeContext(host, "cli", "native-session", tmp_path, tmp_path, None,
                          tmp_path / "vault", tmp_path / "config.json",
                          MappingProxyType(config or {}), tmp_path,
                          tmp_path / "index.db", tmp_path / "state")


def native_result(monkeypatch, output, code=0, stderr=b""):
    calls = []
    def run(command, prompt, **kwargs):
        calls.append((command, prompt, kwargs))
        envelope = {"structured_output": output, "is_error": False}
        return code, json.dumps(envelope).encode(), stderr
    monkeypatch.setattr(backend, "_run_bounded", run)
    return calls


def test_selected_claude_is_bounded_inline_and_has_no_tools(tmp_path, monkeypatch):
    calls = native_result(monkeypatch, {"text": "A summary."})
    request = backend.AIRequest("Analyze this input", input_revision="capture-sha", model="sonnet")
    result = backend.execute_ai(context(tmp_path), "session_summary", request)
    assert (result.status, result.data, result.input_revision) == ("ok", "A summary.", "capture-sha")
    command, prompt, options = calls[0]
    assert command[0] == "claude"
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--strict-mcp-config" in command
    assert "--no-session-persistence" in command
    assert "--dangerously-skip-permissions" not in command
    assert options["env"]["OBSIDIAN_BRAIN_NESTED_AI"] == "1"
    assert options["cwd"] == tmp_path
    assert "Analyze this input" in prompt
    assert str(tmp_path / "vault") not in prompt


@pytest.mark.parametrize("code,stderr,status,error", [
    (1, b"401: confidential credential", "auth_error", "native_auth_error"),
    (1, b"permission denied confidential credential", "unavailable", "policy_denied"),
    (1, b"other failure confidential credential", "unavailable", "native_execution_failed"),
])
def test_failures_do_not_expose_native_errors_or_switch_host(tmp_path, monkeypatch, code, stderr, status, error):
    calls = native_result(monkeypatch, {}, code, stderr)
    result = backend.execute_ai(context(tmp_path), "snapshot_summary", backend.AIRequest("Input"))
    assert result.status == status
    assert result.diagnostic == error
    assert "confidential" not in repr(result)
    assert len(calls) == 1
    assert calls[0][0][0] == "claude"


def test_no_context_never_guesses_from_mixed_host_environment(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "codex")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude")
    calls = native_result(monkeypatch, {"text": "Unwanted"})
    result = backend.execute_ai(None, "session_summary", backend.AIRequest("Input"))
    assert result.status == "unavailable"
    assert result.error_code == "context_required"
    assert calls == []


def test_codex_never_dispatches_claude_aliases(tmp_path):
    ctx = context(tmp_path, "codex")
    assert backend.resolve_ai_selection(ctx, "session_summary", "haiku") == ("codex", None)
    configured = context(tmp_path, "codex", {"codex_ai_model": "native-model"})
    assert backend.resolve_ai_selection(configured, "session_summary", "opus") == ("codex", "native-model")


def test_codex_dispatches_only_native_adapter_and_reports_known_model(tmp_path, monkeypatch):
    from ai_adapters import codex
    calls = []
    def execute(*args):
        calls.append(args)
        return {"text": "Native summary"}, "observed-native-model"
    monkeypatch.setattr(codex, "execute", execute)
    result = backend.execute_ai(context(tmp_path, "codex"), "session_summary", backend.AIRequest("Input", model="haiku"))
    assert result.status == "ok"
    assert result.backend == "codex"
    assert result.model == "observed-native-model"
    assert result.data == "Native summary"
    assert len(calls) == 1


def test_request_freezes_nested_options():
    source = {"expected_ids": ["g1"], "project_by_id": {"g1": "project"}}
    request = backend.AIRequest("Input", options=source)
    source["expected_ids"].append("g2")
    assert request.options["expected_ids"] == ("g1",)
    with pytest.raises(TypeError):
        request.options["project_by_id"]["g1"] = "other"


@pytest.mark.parametrize("host", ["claude", "codex"])
@pytest.mark.parametrize("model", ["", [], {}, True, 1, "  ", "x" * 257])
def test_invalid_explicit_model_cannot_start_native_analysis(tmp_path, monkeypatch, host, model):
    from ai_adapters import codex
    calls = native_result(monkeypatch, {"text": "Must not run"})
    def native(*args):
        calls.append(args)
        return {"text": "Must not run"}, "native-model"
    monkeypatch.setattr(codex, "execute", native)
    result = backend.execute_ai(context(tmp_path, host), "session_summary", backend.AIRequest("Input", model=model))
    assert result.status == "unavailable"
    assert result.error_code == "model_invalid"
    assert result.model is None
    assert calls == []


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf"), 3601])
def test_invalid_timeout_cannot_start_native_process(tmp_path, monkeypatch, timeout):
    calls = native_result(monkeypatch, {"text": "Unwanted"})
    result = backend.execute_ai(context(tmp_path), "session_summary", backend.AIRequest("Input", timeout=timeout))
    assert result.status == "unavailable"
    assert calls == []


def test_secrets_scrubbed_before_native_transport(tmp_path, monkeypatch):
    token = "ghp_" + "a" * 36
    calls = native_result(monkeypatch, {"text": "Summary"})
    assert backend.execute_ai(context(tmp_path), "session_summary", backend.AIRequest(token)).status == "ok"
    assert token not in calls[0][1]


def test_duplicate_json_fields_are_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "_run_bounded", lambda *args, **kwargs:
                        (0, b'{"structured_output":{"text":"one","text":"two"}}', b""))
    assert backend.execute_ai(context(tmp_path), "session_summary", backend.AIRequest("Input")).status == "invalid_output"


@pytest.mark.parametrize("usage,model", [
    ({"claude-haiku-4-5-test": {}}, "claude-haiku-4-5-test"),
    ({"claude-haiku-4-5-test": {}, "claude-sonnet-4-5-test": {}}, None),
    ({"haiku": {}}, None), ({}, None), (None, None),
])
def test_claude_reports_observed_model_without_guessing_alias(tmp_path, monkeypatch, usage, model):
    envelope = {"structured_output": {"text": "Summary"}, "modelUsage": usage}
    monkeypatch.setattr(backend, "_run_bounded", lambda *args, **kwargs: (0, json.dumps(envelope).encode(), b""))
    result = backend.execute_ai(context(tmp_path), "session_summary", backend.AIRequest("Input", model="haiku"))
    assert result.status == "ok"
    assert result.model == model


def test_claude_auth_error_in_native_envelope_is_private(tmp_path, monkeypatch):
    envelope = {"is_error": True, "errors": ["401 unauthorized confidential credential"]}
    monkeypatch.setattr(backend, "_run_bounded", lambda *args, **kwargs: (1, json.dumps(envelope).encode(), b""))
    result = backend.execute_ai(context(tmp_path), "session_summary", backend.AIRequest("Input"))
    assert result.status == "auth_error"
    assert "confidential" not in repr(result)


def item(group_id):
    return {"group_id": group_id, "classification": "REVIEW", "confidence": "MED",
            "canonical_text": "An item", "evidence_citation": None, "action_required": None}


@pytest.mark.parametrize("items", [[item("g1"), item("g1")], [item("g2")], [],
                                  [{**item("g1"), "confidence": "certain"}],
                                  [{**item("g1"), "canonical_text": None}]])
def test_classifier_rejects_missing_duplicate_unknown_or_malformed_items(items):
    with pytest.raises(ValueError):
        backend.validate_ai_output("classify_items", {"items": items}, {"expected_ids": ["g1"]})


def test_cross_project_merge_is_rejected():
    output = {"merges": [{"canonical_group_id": "g1", "absorbed_group_ids": ["g2"], "reasoning": "Same"}],
              "total_groups_before": 2, "total_groups_after": 1}
    with pytest.raises(ValueError):
        backend.validate_ai_output("semantic_merge", output,
                                   {"expected_ids": ["g1", "g2"], "project_by_id": {"g1": "one", "g2": "two"}})


def test_batch_retains_only_valid_unique_note_indices():
    assert backend.validate_ai_output("session_summaries", {"summaries": [{"index": 2, "text": "Second"}]},
                                      {"expected_count": 2}) == {2: "Second"}
    with pytest.raises(ValueError):
        backend.validate_ai_output("session_summaries", {"summaries": [{"index": True, "text": "First"}]},
                                   {"expected_count": 2})


def test_native_output_limit_stops_process(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "MAX_OUTPUT_BYTES", 2048)
    with pytest.raises(backend._BackendFailure) as failure:
        backend._run_bounded([sys.executable, "-c", "import sys,time;sys.stdout.write('x'*20000);sys.stdout.flush();time.sleep(10)"],
                             "Input", cwd=tmp_path, env=dict(os.environ), deadline=time.monotonic() + 2)
    assert failure.value.code == "output_limit"


def test_native_deadline_kills_child(tmp_path, monkeypatch):
    real_popen = subprocess.Popen
    processes = []
    def spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(backend.subprocess, "Popen", spawn)
    with pytest.raises(backend._BackendFailure) as failure:
        backend._run_bounded([sys.executable, "-c", "import time;time.sleep(10)"], "Input",
                             cwd=tmp_path, env=dict(os.environ), deadline=time.monotonic() + 0.1)
    assert failure.value.status == "timeout"
    assert len(processes) == 1
    assert processes[0].poll() is not None
