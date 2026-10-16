"""Operation receipts prove bounded transport outcomes without retaining content."""
import hashlib
import io
import json
import os
import stat
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import ai_backend as backend
from session_auxiliary_state import directory

CONTENT = 'private-output-canary'
PROMPT = 'private-input-canary'
SHAPES = {
    'snapshot_summary': ({'text': CONTENT}, {}, CONTENT),
    'session_summary': ({'text': CONTENT}, {}, CONTENT),
    'theme_names': ({'themes': [{'name': 'Fixture', 'summary': CONTENT}]},
                    {'expected_count': 1}, [{'name': 'Fixture', 'summary': CONTENT}]),
    'session_summaries': ({'summaries': [{'index': 1, 'text': CONTENT}]},
                          {'expected_count': 1}, {1: CONTENT}),
    'semantic_merge': ({'merges': [], 'total_groups_before': 1, 'total_groups_after': 1},
                       {'expected_ids': ['item'], 'project_by_id': {'item': 'project'}},
                       {'merges': [], 'total_groups_before': 1, 'total_groups_after': 1}),
    'classify_items': ({'items': [{'group_id': 'item', 'classification': 'ACTIVE',
                                  'confidence': 'HIGH', 'canonical_text': CONTENT,
                                  'evidence_citation': None, 'action_required': None}]},
                       {'expected_ids': ['item']},
                       [{'group_id': 'item', 'classification': 'ACTIVE', 'confidence': 'HIGH',
                         'canonical_text': CONTENT, 'evidence_citation': None, 'action_required': None}]),
}


def transport(monkeypatch, context, output, status='ok', discovery=False):
    """Use real private child processes; never start a native model or service."""
    from ai_adapters import claude, codex
    def execute(selected, prompt, schema, model, deadline, env, *extra):
        if status == 'unavailable':
            raise backend._BackendFailure('unavailable', 'fixture_unavailable')
        if discovery:
            rpc = codex.NativeRpc(sys.executable, ['-c', 'import time; time.sleep(30)'],
                                  selected, env, deadline)
            rpc.close()
        script = 'import os,sys; sys.stdin.read(); print(os.getpid()); '
        if status == 'timeout':
            script += 'import time; time.sleep(30)'
            deadline = min(deadline, time.monotonic() + 0.1)
        else:
            script += 'print(' + repr(json.dumps(output)) + '); sys.exit(' + ('3' if status == 'auth_error' else '0') + ')'
        code, raw, errors = backend._run_bounded([sys.executable, '-c', script], prompt,
                                                cwd=selected.worktree, env=env, deadline=deadline)
        if code:
            raise backend._BackendFailure('auth_error', 'native_auth_error')
        pid, encoded = raw.decode().splitlines()
        # Child-reported PID is an independent control for the process receipt.
        assert int(pid) > 0
        observed_pids.append(int(pid))
        return json.loads(encoded), 'fixture-native-model'
    observed_pids = []
    monkeypatch.setattr(claude, 'execute', execute)
    monkeypatch.setattr(codex, 'execute', execute)
    return observed_pids


def receipts(context):
    path = directory(context, 'logs') / 'ai-operations.jsonl'
    return path, [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize('operation', sorted(SHAPES))
@pytest.mark.parametrize('status', ['ok', 'unavailable', 'timeout', 'invalid_output', 'auth_error'])
def test_operation_outcomes_have_private_content_free_receipts(selected_host_context, monkeypatch, operation, status):
    output, options, expected = SHAPES[operation]
    pids = transport(monkeypatch, selected_host_context, {} if status == 'invalid_output' else output, status)
    revision = hashlib.sha256(operation.encode()).hexdigest()
    result = backend.execute_ai(selected_host_context, operation,
                               backend.AIRequest(PROMPT, input_revision=revision, options=options))
    path, rows = receipts(selected_host_context)
    assert len(rows) == 1
    row = rows[0]
    assert row['status'] == result.status == status
    assert (row['host'], row['client'], row['native_session_id']) == (
        selected_host_context.host, selected_host_context.client, selected_host_context.native_session_id)
    assert row['operation'] == operation
    assert row['backend'] == selected_host_context.host
    assert row['input_revision'] == revision
    assert row['validation_policy_revision'] == backend.VALIDATION_POLICY_REVISION
    assert row['caller_skill'] == 'unknown'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert selected_host_context.state_path in path.parents
    assert selected_host_context.vault_path not in path.parents
    assert PROMPT not in path.read_text() and CONTENT not in path.read_text()
    if status == 'ok':
        assert result.data == expected
        digest = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':'),
                                          ensure_ascii=False).encode()).hexdigest()
        assert row['output_sha256'] == digest
        assert row['model'] == 'fixture-native-model'
    else:
        assert row['output_sha256'] is None
        assert row['code'] == result.error_code
    if status == 'unavailable':
        assert row['children'] == []
    else:
        child, = row['children']
        assert child['kind'] == 'analysis'
        assert child['argv0'] == os.path.basename(sys.executable)
        assert child['argv0_sha256'] == hashlib.sha256(os.fsencode(sys.executable)).hexdigest()
        assert isinstance(child['pid'], int) and child['exit'] is not None
        if pids:
            assert child['pid'] == pids[0]
        assert child['exit'] == (0 if status in {'ok', 'invalid_output'} else 3 if status == 'auth_error' else -15)
    assert backend._CALL_CHILDREN.get() is None


def test_discovery_children_are_distinct_from_analysis(selected_host_context, monkeypatch):
    pids = transport(monkeypatch, selected_host_context, {'text': CONTENT}, discovery=True)
    assert backend.execute_ai(selected_host_context, 'session_summary', backend.AIRequest(PROMPT)).status == 'ok'
    _, rows = receipts(selected_host_context)
    children = rows[0]['children']
    assert [child['kind'] for child in children] == ['discovery', 'analysis']
    assert children[1]['pid'] == pids[0]
    assert children[0]['pid'] != children[1]['pid']
    assert all(child['exit'] is not None for child in children)


def test_concurrent_invocations_keep_child_metadata_separate(selected_host_context, monkeypatch):
    transport(monkeypatch, selected_host_context, {'text': CONTENT})
    revisions = [hashlib.sha256(str(index).encode()).hexdigest() for index in range(8)]
    def run(revision):
        return backend.execute_ai(selected_host_context, 'session_summary',
                                  backend.AIRequest(PROMPT, input_revision=revision))
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(result.status == 'ok' for result in pool.map(run, revisions))
    _, rows = receipts(selected_host_context)
    assert len(rows) == 8
    assert {row['input_revision'] for row in rows} == set(revisions)
    assert len({row['invocation_id'] for row in rows}) == 8
    assert all(len(row['children']) == 1 for row in rows)
    assert len({row['children'][0]['pid'] for row in rows}) == 8
    assert backend._CALL_CHILDREN.get() is None


def test_missing_context_has_no_storage_fallback(selected_host_context, monkeypatch):
    import session_auxiliary_state
    monkeypatch.setattr(session_auxiliary_state, 'directory', lambda *args: pytest.fail('storage fallback'))
    assert backend.execute_ai(None, 'session_summary', backend.AIRequest(PROMPT)).error_code == 'context_required'


@pytest.mark.parametrize('failure', ['write', 'symlink', 'hardlink'])
def test_receipt_failures_preserve_ai_result_and_protected_bytes(selected_host_context, monkeypatch, tmp_path, capsys, failure):
    transport(monkeypatch, selected_host_context, {'text': CONTENT})
    sentinel = tmp_path / 'sentinel'
    sentinel.write_text(PROMPT)
    before_mode = sentinel.stat().st_mode
    path = directory(selected_host_context, 'logs') / 'ai-operations.jsonl'
    if failure == 'write':
        def fail(*args):
            raise OSError(PROMPT)
        monkeypatch.setattr(backend, '_write_ai_receipt', fail)
    elif failure == 'symlink':
        path.symlink_to(sentinel)
    else:
        os.link(sentinel, path)
    result = backend.execute_ai(selected_host_context, 'session_summary', backend.AIRequest(PROMPT))
    assert result.status == 'ok' and result.data == CONTENT
    assert sentinel.read_text() == PROMPT and sentinel.stat().st_mode == before_mode
    warning = capsys.readouterr().err
    assert 'receipt unavailable' in warning and PROMPT not in warning and CONTENT not in warning


def test_receipt_records_actual_skill_context_and_redacts_nonhash_revision(selected_host_context, monkeypatch):
    import skill_procedures
    transport(monkeypatch, selected_host_context, {'text': CONTENT})
    def procedure(context, payload):
        assert backend.execute_ai(context, 'session_summary',
                                 backend.AIRequest(PROMPT, input_revision=PROMPT)).status == 'ok'
        return 0
    monkeypatch.setitem(skill_procedures.OPERATIONS['recall'], 'upgrade-batch', procedure)
    assert skill_procedures.run_operation(selected_host_context, 'recall', 'upgrade-batch', {},
                                         io.StringIO(), io.StringIO()) == 0
    _, rows = receipts(selected_host_context)
    assert rows[0]['caller_skill'] == 'recall' and rows[0]['input_revision'] is None
    assert PROMPT not in json.dumps(rows)
    assert skill_procedures.context_skill_name() is None
