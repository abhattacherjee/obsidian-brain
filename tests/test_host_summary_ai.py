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


@pytest.mark.parametrize("route,cap", [("snapshot", 8000), ("single", 12000), ("batch", 12000)])
@pytest.mark.parametrize("retained", [True, False], ids=["native-capture", "legacy-raw"])
@pytest.mark.parametrize("padding", [14900, 0], ids=["over-budget", "within-budget"])
def test_summary_prompt_keeps_completed_native_tail_without_changing_legacy_sampling(
        context, monkeypatch, route, cap, retained, padding):
    """Prefix clipping must not turn a completed retained capture into pending work."""
    from note_transactions import NoteMutation, apply_mutations, read_revision
    import re

    capture = "EARLY_PENDING|" + "x" * padding + "MIDDLE_ONLY|" + "y" * padding + "FINAL_COMPLETED"
    note = context.vault_path / "summary-input.md"
    kind = "claude-snapshot" if route == "snapshot" else "claude-session"
    note.write_text("---\ntype: " + kind + "\nsession_id: synthetic-sampling-id\n"
                    "status: auto-logged\nproject: project\n---\nUser prose must stay outside AI input.\n")
    if retained:
        apply_mutations(context, [NoteMutation(note, read_revision(context, note),
                         {"capture": capture}, "seed-native-sampling")])
    else:
        with note.open("a") as stream:
            stream.write("\n## Conversation (raw)\n**User:** " + capture + "\n")
    monkeypatch.setattr(obsidian_utils, "find_transcript_jsonl", lambda *a: None)
    prompts = []

    def execute(ctx, operation, request):
        prompts.append(request.input)
        text = "## Summary\nThe work completed.\n"
        data = {1: text} if operation == "session_summaries" else text
        return ai_backend.AIResult("ok", data, request.input_revision,
                                   backend=context.host, model="observed-model")

    monkeypatch.setattr(ai_backend, "execute_ai", execute)
    with using_runtime_context(context):
        prep = obsidian_utils._prepare_note_for_summary(
            str(note), str(context.vault_path), "sessions", "project")
        assert prep["ok"]
        if route == "snapshot":
            result, reason = obsidian_utils.generate_snapshot_summary(
                prep["user_msgs"], prep["assistant_msgs"], prep["metadata"])
            assert result and reason is None
            pattern = r"USER MESSAGES:\n(.*?)\n\n---\n\nASSISTANT MESSAGES:"
        elif route == "single":
            result, reason = obsidian_utils.generate_summary(
                prep["user_msgs"], prep["assistant_msgs"], prep["metadata"])
            assert result and reason is None
            pattern = r"TRANSCRIPT \(user and assistant messages\):\n(.*?)\n\n---\n\n"
        else:
            result = obsidian_utils.generate_summaries_batch(
                [prep], vault_path=str(context.vault_path), sessions_folder="sessions", project="project")
            assert result[0][0] and result[0][1] is None
            pattern = r"SESSION TRANSCRIPT:\n(.*?)\n\n---\n\n"
    assert len(prompts) == 1
    sample = re.search(pattern, prompts[0], re.DOTALL).group(1)
    assert len(sample) <= cap
    assert "User prose must stay outside AI input." not in prompts[0]
    if retained and len(capture) > cap:
        assert "FINAL_COMPLETED" in sample
        assert "omitted" in sample.lower()
        assert "MIDDLE_ONLY" not in sample
        assert ("EARLY_PENDING" in sample) == (route != "snapshot")
    elif retained:
        assert sample == capture + "\n"
    else:
        assert sample == capture[:cap]
