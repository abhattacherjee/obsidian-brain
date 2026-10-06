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


def context(selected, config=None):
    from dataclasses import replace
    return replace(selected, config=MappingProxyType(dict(selected.config, **(config or {}))))


@pytest.fixture
def claude_context(tmp_path):
    from runtime_context import using_runtime_context
    vault = tmp_path / 'vault'
    vault.mkdir()
    native_home = tmp_path / 'native-home'
    native_home.mkdir()
    (native_home / 'settings.json').write_text('{}')
    selected = RuntimeContext('claude', 'claude-code', 'native-envelope', tmp_path, tmp_path, None,
                              vault, tmp_path / 'config.json', MappingProxyType({}), tmp_path,
                              tmp_path / 'index.sqlite3', tmp_path / 'state', native_home=native_home)
    with using_runtime_context(selected):
        yield selected


def native_result(monkeypatch, output, code=0, stderr=b""):
    calls = []
    def run(command, prompt, **kwargs):
        calls.append((command, prompt, kwargs))
        envelope = {"structured_output": output, "is_error": False}
        return code, json.dumps(envelope).encode(), stderr
    monkeypatch.setattr(backend, "_run_bounded", run)
    from ai_adapters import codex
    monkeypatch.setattr(codex, 'discover_restrictions', lambda *args: (codex.restrictions(), 'observed-native-model'))
    def codex_run(command, prompt, **kwargs):
        calls.append((command, prompt, kwargs))
        output_path = Path(command[command.index('--output-last-message') + 1])
        output_path.write_text(json.dumps(output))
        output_path.chmod(0o600)
        events = [{'type': 'thread.started', 'thread_id': 'native-analysis'},
                  {'type': 'turn.started'},
                  {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Native output'}},
                  {'type': 'turn.completed'}]
        return code, b'\n'.join(json.dumps(row).encode() for row in events), stderr
    monkeypatch.setattr(codex, '_run_bounded', codex_run)
    return calls


def test_selected_host_is_bounded_inline_and_has_no_tools(selected_host_context, monkeypatch):
    calls = native_result(monkeypatch, {"text": "A summary."})
    request = backend.AIRequest("Analyze this input", input_revision="capture-sha", model="sonnet")
    result = backend.execute_ai(context(selected_host_context), "session_summary", request)
    assert (result.status, result.data, result.input_revision) == ("ok", "A summary.", "capture-sha")
    command, prompt, options = calls[0]
    assert command[0] == selected_host_context.host
    if selected_host_context.host == 'claude':
        assert command[command.index('--tools') + 1] == ''
        assert command[command.index('--mcp-config') + 1] == '{"mcpServers":{}}'
        assert '--strict-mcp-config' in command
        assert '--no-session-persistence' in command
    else:
        assert command[command.index('--sandbox') + 1] == 'read-only'
        assert '--ephemeral' in command
        assert 'web_search="disabled"' in command
        assert 'features.shell_tool=false' in command
    assert "--dangerously-skip-permissions" not in command
    assert options["env"]["OBSIDIAN_BRAIN_NESTED_AI"] == "1"
    assert options["cwd"] == selected_host_context.worktree
    assert "Analyze this input" in prompt
    assert str(selected_host_context.vault_path) not in prompt


@pytest.mark.parametrize("code,stderr,status,error", [
    (1, b"401: confidential credential", "auth_error", "native_auth_error"),
    (1, b"permission denied confidential credential", "unavailable", "policy_denied"),
    (1, b"other failure confidential credential", "unavailable", "native_execution_failed"),
])
def test_failures_do_not_expose_native_errors_or_switch_host(selected_host_context, monkeypatch, code, stderr, status, error):
    calls = native_result(monkeypatch, {}, code, stderr)
    result = backend.execute_ai(context(selected_host_context), "snapshot_summary", backend.AIRequest("Input"))
    assert result.status == status
    assert result.diagnostic == error
    assert "confidential" not in repr(result)
    assert len(calls) == 1
    assert calls[0][0][0] == selected_host_context.host


def test_no_context_never_guesses_from_mixed_host_environment(selected_host_context, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "codex")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude")
    calls = native_result(monkeypatch, {"text": "Unwanted"})
    result = backend.execute_ai(None, "session_summary", backend.AIRequest("Input"))
    assert result.status == "unavailable"
    assert result.error_code == "context_required"
    assert calls == []


def test_selected_host_keeps_native_model_selection(selected_host_context):
    ctx = context(selected_host_context)
    assert backend.resolve_ai_selection(ctx, 'session_summary', 'haiku') == (ctx.host, (ctx.config.get('codex_ai_model') or ctx.config.get('codex_summary_model')) if ctx.host == 'codex' else 'haiku')
    from dataclasses import replace
    native_defaults = dict(ctx.config)
    native_defaults.pop('codex_ai_model', None)
    native_defaults.pop('codex_summary_model', None)
    unconfigured = replace(ctx, config=MappingProxyType(native_defaults))
    assert backend.resolve_ai_selection(unconfigured, 'session_summary', 'haiku') == (ctx.host, None if ctx.host == 'codex' else 'haiku')
    configured = context(selected_host_context, {'codex_ai_model': 'native-model'})
    assert backend.resolve_ai_selection(configured, 'session_summary', 'opus') == (ctx.host, 'native-model' if ctx.host == 'codex' else 'opus')


def test_selected_host_dispatches_only_native_adapter(selected_host_context, monkeypatch):
    from ai_adapters import claude, codex
    calls = []
    def execute(ctx, *args):
        assert ctx.host == selected_host_context.host
        calls.append(ctx)
        return {'text': 'Native summary'}, 'observed-native-model'
    selected = codex if selected_host_context.host == 'codex' else claude
    other = claude if selected_host_context.host == 'codex' else codex
    monkeypatch.setattr(selected, 'execute', execute)
    monkeypatch.setattr(other, 'execute', lambda *args: pytest.fail('Cross-provider dispatch'))
    result = backend.execute_ai(selected_host_context, 'session_summary', backend.AIRequest('Input', model='haiku'))
    assert result.status == 'ok'
    assert result.backend == selected_host_context.host
    assert result.model == 'observed-native-model'
    assert result.data == 'Native summary'
    assert calls == [selected_host_context]


def test_request_freezes_nested_options():
    source = {"expected_ids": ["g1"], "project_by_id": {"g1": "project"}}
    request = backend.AIRequest("Input", options=source)
    source["expected_ids"].append("g2")
    assert request.options["expected_ids"] == ("g1",)
    with pytest.raises(TypeError):
        request.options["project_by_id"]["g1"] = "other"


@pytest.mark.parametrize("model", ["", [], {}, True, 1, "  ", "x" * 257])
def test_invalid_explicit_model_cannot_start_native_analysis(selected_host_context, monkeypatch, model):
    from ai_adapters import codex
    calls = native_result(monkeypatch, {"text": "Must not run"})
    def native(*args):
        calls.append(args)
        return {"text": "Must not run"}, "native-model"
    monkeypatch.setattr(codex, "execute", native)
    result = backend.execute_ai(context(selected_host_context), "session_summary", backend.AIRequest("Input", model=model))
    assert result.status == "unavailable"
    assert result.error_code == "model_invalid"
    assert result.model is None
    assert calls == []


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf"), 3601])
def test_invalid_timeout_cannot_start_native_process(selected_host_context, monkeypatch, timeout):
    calls = native_result(monkeypatch, {"text": "Unwanted"})
    result = backend.execute_ai(context(selected_host_context), "session_summary", backend.AIRequest("Input", timeout=timeout))
    assert result.status == "unavailable"
    assert calls == []


def test_secrets_scrubbed_before_native_transport(selected_host_context, monkeypatch):
    token = "ghp_" + "a" * 36
    calls = native_result(monkeypatch, {"text": "Summary"})
    assert backend.execute_ai(context(selected_host_context), "session_summary", backend.AIRequest(token)).status == "ok"
    assert token not in calls[0][1]


@pytest.mark.host_only('claude', reason='claude-record-format', capability='claude_native_format')
def test_duplicate_json_fields_are_invalid(claude_context, monkeypatch):
    from ai_adapters import claude
    monkeypatch.setattr(backend, '_run_bounded', lambda *args, **kwargs:
                        (0, b'{"structured_output":{"text":"one","text":"two"}}', b''))
    with pytest.raises(ValueError):
        claude.execute(claude_context, 'Input', {}, None, time.monotonic() + 5, {})


@pytest.mark.parametrize("usage,model", [
    ({"claude-haiku-4-5-test": {}}, "claude-haiku-4-5-test"),
    ({"claude-haiku-4-5-test": {}, "claude-sonnet-4-5-test": {}}, None),
    ({"haiku": {}}, None), ({}, None), (None, None),
])
@pytest.mark.host_only('claude', reason='claude-record-format', capability='claude_native_format')
def test_claude_reports_observed_model_without_guessing_alias(claude_context, monkeypatch, usage, model):
    envelope = {"structured_output": {"text": "Summary"}, "modelUsage": usage}
    monkeypatch.setattr(backend, "_run_bounded", lambda *args, **kwargs: (0, json.dumps(envelope).encode(), b""))
    from ai_adapters import claude
    output, observed = claude.execute(claude_context, 'Input', {}, 'haiku', time.monotonic() + 5, {})
    assert output == {'text': 'Summary'}
    assert observed == model


@pytest.mark.host_only('claude', reason='claude-record-format', capability='claude_native_format')
def test_claude_auth_error_in_native_envelope_is_private(claude_context, monkeypatch):
    envelope = {"is_error": True, "errors": ["401 unauthorized confidential credential"]}
    monkeypatch.setattr(backend, "_run_bounded", lambda *args, **kwargs: (1, json.dumps(envelope).encode(), b""))
    from ai_adapters import claude
    with pytest.raises(backend._BackendFailure) as failure:
        claude.execute(claude_context, 'Input', {}, None, time.monotonic() + 5, {})
    assert failure.value.status == 'auth_error'
    assert 'confidential' not in repr(failure.value)


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


def test_native_output_limit_stops_process(selected_host_context, monkeypatch):
    monkeypatch.setattr(backend, "MAX_OUTPUT_BYTES", 2048)
    with pytest.raises(backend._BackendFailure) as failure:
        backend._run_bounded([sys.executable, "-c", "import sys,time;sys.stdout.write('x'*20000);sys.stdout.flush();time.sleep(10)"],
                             "Input", cwd=selected_host_context.worktree, env=dict(os.environ), deadline=time.monotonic() + 2)
    assert failure.value.code == "output_limit"


def test_native_deadline_kills_child(selected_host_context, monkeypatch):
    real_popen = subprocess.Popen
    processes = []
    def spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(backend.subprocess, "Popen", spawn)
    with pytest.raises(backend._BackendFailure) as failure:
        backend._run_bounded([sys.executable, "-c", "import time;time.sleep(10)"], "Input",
                             cwd=selected_host_context.worktree, env=dict(os.environ), deadline=time.monotonic() + 0.1)
    assert failure.value.status == "timeout"
    assert len(processes) == 1
    assert processes[0].poll() is not None


def test_native_ai_retains_selected_home_after_environment_changes(selected_host_context, monkeypatch, tmp_path):
    calls = native_result(monkeypatch, {"text": "A summary."})
    key = "CODEX_HOME" if selected_host_context.host == "codex" else "CLAUDE_CONFIG_DIR"
    foreign = tmp_path / "foreign-native-home"
    foreign.mkdir()
    monkeypatch.setenv(key, str(foreign))
    result = backend.execute_ai(selected_host_context, "session_summary", backend.AIRequest("Input"))
    assert result.status == "ok"
    assert calls[0][2]["env"][key] == str(selected_host_context.native_home)
    assert calls[0][2]["env"][key] != str(foreign)


def test_native_ai_without_frozen_home_defers_before_transport(selected_host_context, monkeypatch):
    from dataclasses import replace
    calls = native_result(monkeypatch, {"text": "Must not run"})
    context_without_home = replace(selected_host_context, native_home=None)
    result = backend.execute_ai(context_without_home, "session_summary", backend.AIRequest("Input"))
    assert result.status == "unavailable"
    assert result.error_code == "native_home_missing"
    assert calls == []
