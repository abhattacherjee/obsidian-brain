"""Legacy summary shapes still dispatch through the invoking native host."""
from types import MappingProxyType

import pytest
import ai_backend
import obsidian_utils
from runtime_context import RuntimeContext, current_runtime_context, using_runtime_context

@pytest.fixture
def selected_host_context(selected_host_context, host):
    from dataclasses import replace
    selected = replace(selected_host_context, config=MappingProxyType(dict(selected_host_context.config, codex_summary_model='native-model')))
    with using_runtime_context(selected):
        yield selected

@pytest.fixture
def context(selected_host_context):
    return selected_host_context

def test_helpers_dispatch_only_bound_native_model(context, monkeypatch):
    seen = []
    def execute(ctx, operation, request):
        seen.append((ctx, operation, request))
        data = [{'name': 'Theme', 'summary': 'Theme body'}] if operation == 'theme_names' else '## Summary\nFact.\n'
        return ai_backend.AIResult('ok', data, request.input_revision, backend=context.host, model='actual-native-model')
    monkeypatch.setattr(ai_backend, 'execute_ai', execute)
    with using_runtime_context(context):
        assert obsidian_utils.generate_snapshot_summary(['ask'], ['answer'], {'input_revision': 'capture-sha'}, model='haiku')[1] is None
        assert obsidian_utils.generate_theme_names([{'top_terms': ['x'], 'sample_titles': ['title']}], model='sonnet')[1] is None
        assert obsidian_utils.generate_summary(['ask'], ['answer'], {'input_revision': 'capture-sha'}, model='opus')[1] is None
        assert obsidian_utils._escalation_models('haiku') == (['haiku'] if context.host == 'codex' else ['haiku', 'sonnet', 'opus'])
    assert [item[1] for item in seen] == ['snapshot_summary', 'theme_names', 'session_summary']
    assert all(item[0] is context for item in seen)
    assert [item[2].model for item in seen] == (['native-model'] * 3 if context.host == 'codex' else ['haiku', 'sonnet', 'opus'])
    assert seen[0][2].input_revision == seen[2][2].input_revision == 'capture-sha'
    assert seen[1][2].input_revision

def test_no_context_cannot_invoke_backend(selected_host_context, monkeypatch):
    monkeypatch.setattr(ai_backend, 'execute_ai', lambda *args: pytest.fail('backend must not run'))
    with using_runtime_context(None):
        assert obsidian_utils.generate_theme_names([{'top_terms': ['x'], 'sample_titles': []}])[0] is None

def test_parallel_upgrades_copy_native_context_per_worker(context, monkeypatch):
    seen = []
    def upgrade(path, *args):
        seen.append(current_runtime_context())
        return 'Upgraded ' + path, 0.01, 'actual-native-model', None
    monkeypatch.setattr(obsidian_utils, 'upgrade_unsummarized_note', upgrade)
    with using_runtime_context(context):
        results = obsidian_utils.upgrade_batch(['a', 'b', 'c'], str(context.vault_path),
                     'sessions', 'project', max_workers=3, summary_batch_size=1)
    assert len(results) == 3
    assert len(seen) == 3 and all(item is context for item in seen)
    assert all(item['model_used'] == 'actual-native-model' for item in results)

@pytest.mark.parametrize('requested', ['haiku', 'opus'])
def test_observed_model_does_not_become_requested_alias(context, monkeypatch, requested):
    host = context.host
    bound = context
    selected = 'native-model' if host == 'codex' else requested
    requests = []
    def execute(ctx, operation, request):
        requests.append(request)
        return ai_backend.AIResult('ok', 'Useful result.', request.input_revision,
                                   backend=host, model='observed-full-model-id')
    monkeypatch.setattr(ai_backend, 'execute_ai', execute)
    with using_runtime_context(bound):
        result = obsidian_utils._execute_summary_ai('Input', 'snapshot_summary', requested, 12, 'source-revision')
        assert obsidian_utils._SUMMARY_NATIVE_MODEL.get() == 'observed-full-model-id'
    assert requests[0].model == selected
    assert result.model == 'observed-full-model-id'
    assert requests[0].input_revision == 'source-revision'
    assert requests[0].timeout == 12


@pytest.mark.parametrize('status,error,reason', [
    ('invalid_output', 'parse_error', 'parse_error'),
    ('invalid_output', 'missing_section', 'missing_section'),
    ('invalid_output', 'count_mismatch', 'count_mismatch'),
    ('unavailable', 'unknown_private_diagnostic', 'haiku_subprocess_error'),
])
def test_failed_summary_clears_previous_model_and_does_not_return_partial_output(context, monkeypatch, status, error, reason):
    monkeypatch.setattr(ai_backend, 'execute_ai', lambda *args: ai_backend.AIResult(
        status, 'Partial untrusted output', diagnostic='private detail', error_code=error))
    obsidian_utils._SUMMARY_NATIVE_MODEL.set('previous-model')
    with using_runtime_context(context):
        result = obsidian_utils._execute_summary_ai('Input', 'session_summary', 'haiku', 9)
        assert obsidian_utils._SUMMARY_NATIVE_MODEL.get() is None
    assert result.returncode == 1
    assert result.stdout == ''
    assert result.stderr == status
    assert result.failure_reason == reason
    assert 'private detail' not in result.stderr


def test_batch_adapter_preserves_indexes_and_source_revision(context, monkeypatch):
    requests = []
    def execute(ctx, operation, request):
        requests.append((operation, request))
        return ai_backend.AIResult('ok', {2: 'Second.', 1: 'First.'}, request.input_revision,
                                   backend=context.host, model='observed-model')
    monkeypatch.setattr(ai_backend, 'execute_ai', execute)
    with using_runtime_context(context):
        result = obsidian_utils._execute_summary_ai('Batch input', 'session_summaries', 'haiku', 10,
                                                   'batch-revision', expected_count=2)
    assert result.stdout == '===== SUMMARY 1 =====\nFirst.\n\n===== SUMMARY 2 =====\nSecond.'
    assert requests[0][0] == 'session_summaries'
    assert requests[0][1].options['expected_count'] == 2
    assert requests[0][1].input_revision == 'batch-revision'


def test_timeout_clears_previous_model_and_retains_native_host(context, monkeypatch):
    import subprocess
    monkeypatch.setattr(ai_backend, 'execute_ai', lambda *args: ai_backend.AIResult('timeout'))
    obsidian_utils._SUMMARY_NATIVE_MODEL.set('previous-model')
    with using_runtime_context(context):
        with pytest.raises(subprocess.TimeoutExpired) as failure:
            obsidian_utils._execute_summary_ai('Input', 'session_summary', 'haiku', 7)
        assert obsidian_utils._SUMMARY_NATIVE_MODEL.get() is None
    assert failure.value.cmd == [context.host]
    assert failure.value.timeout == 7


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_structured_batch_headers_preserve_each_note_sections(context, monkeypatch, wrapped, newline):
    sections = [newline.join(["## Summary", "First fact.", ""]),
                newline.join(["## Summary", "Second fact.", ""])]
    output = {"summaries": [{"index": index, "text":
              ("===== SUMMARY %d =====" % index + newline if wrapped else "") + text}
              for index, text in enumerate(sections, 1)]}
    def execute(ctx, operation, request):
        assert operation == "session_summaries"
        data = ai_backend.validate_ai_output(operation, output, request.options)
        return ai_backend.AIResult("ok", data, request.input_revision,
                                   backend=context.host, model="observed-model")
    monkeypatch.setattr(ai_backend, "execute_ai", execute)
    import open_item_dedup
    monkeypatch.setattr(open_item_dedup, "collect_open_items", lambda *a, **k: [])
    monkeypatch.setattr(obsidian_utils, "_summary_recovery_enabled", lambda: False)
    prepared = [{"user_msgs": ["Synthetic fact"], "assistant_msgs": ["Recorded"],
                 "metadata": {}, "input_capture_revision": "a" * 64} for _ in sections]
    with using_runtime_context(context):
        rendered = obsidian_utils._execute_summary_ai("Batch", "session_summaries", "haiku", 10,
                                                     expected_count=2)
        parsed = obsidian_utils.generate_summaries_batch(prepared, vault_path=str(context.vault_path))
    assert rendered.stdout.count("===== SUMMARY 1 =====") == 1
    assert rendered.stdout.count("===== SUMMARY 2 =====") == 1
    assert parsed == [(text.strip(), None) for text in sections]


@pytest.mark.parametrize("indexes", [[1, 1], [1, 3]])
def test_structured_batch_still_rejects_duplicate_or_out_of_range_indexes(indexes):
    output = {"summaries": [{"index": index, "text": "## Summary\nFact."} for index in indexes]}
    with pytest.raises(ValueError):
        ai_backend.validate_ai_output("session_summaries", output, {"expected_count": 2})


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("lines", [
    ["===== SUMMARY 2 =====", "## Summary", "Wrong target."],
    ["## Summary", "First fact.", "===== SUMMARY 2 =====", "## Summary", "Wrong target."],
    ["===== SUMMARY 1 =====", "===== SUMMARY 1 =====", "## Summary", "Repeated boundary."],
])
def test_structured_batch_foreign_header_cannot_move_summary_between_notes(context, monkeypatch, newline, lines):
    monkeypatch.setattr(ai_backend, "execute_ai", lambda *a: ai_backend.AIResult(
        "ok", {1: newline.join(lines)}, backend=context.host))
    with using_runtime_context(context):
        result = obsidian_utils._execute_summary_ai("Batch", "session_summaries", "haiku", 10,
                                                   expected_count=2)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.failure_reason == "missing_section"
