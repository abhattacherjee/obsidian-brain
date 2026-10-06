"""Visible Codex records retain native identity and exclude internal messages."""
import json
import time
from types import SimpleNamespace
from pathlib import Path
import pytest
from transcripts import RawRecord, SourceCursor
from transcripts.codex import parse_rows, read_records


def _header(**extra):
    return RawRecord(0, 10, {'type': 'session_meta', 'payload': {'id': 'child', 'cwd': '/project', **extra}})


def _rows(*items):
    return tuple(RawRecord(10 + i * 10, 20 + i * 10, item) for i, item in enumerate(items))


def _message(id='message', text='Visible request', turn='turn', role='user', **extra):
    return {'type': 'response_item', 'payload': {'type': 'message', 'id': id, 'role': role,
            'content': [{'type': 'input_text', 'text': text}],
            'internal_chat_message_metadata_passthrough': {'turn_id': turn}, **extra}}


def _completed(id='message', text='Visible request', turn='turn', thread='child', type='UserMessage'):
    return {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': thread,
            'turn_id': turn, 'item': {'type': type, 'id': id, 'content': [{'type': 'text', 'text': text}]}}}


CTX = SimpleNamespace(native_session_id='child', host='codex')


def test_native_message_mirror_once_across_calls():
    first = parse_rows(CTX, _rows(_message()), _header(), {})
    second = parse_rows(CTX, _rows(_completed()), _header(), first.parser_state)
    assert len(first.records) == 1
    assert not second.records


def test_distinct_native_user_mirror_ids_pair_without_erasing_human_repeats():
    rows = _rows(_message('response1'), _completed('event1'),
                 _message('response2'), _completed('event2'))
    parsed = parse_rows(CTX, rows, _header(), {})
    assert len(parsed.records) == 2
    assert parsed.records[0].source_id != parsed.records[1].source_id


def test_user_environment_and_hook_messages_are_not_human():
    internal = _message('environment', '<private>')
    internal['payload']['internal_chat_message_metadata_passthrough']['content_item_kinds'] = ['environments.environment_context']
    hook = _message('hook', 'Internal instruction')
    event = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': 'child',
             'turn_id': 'turn', 'item': {'type': 'HookPrompt', 'id': 'hook', 'fragments': [{'text': 'Internal instruction'}]}}}
    parsed = parse_rows(CTX, _rows(internal, hook, event), _header(account='private'), {})
    assert not parsed.records
    assert set(parsed.metadata) <= {'native_session_id', 'cwd', 'fork_parent_id'}


def test_synthetic_inherited_prefix_uses_parent_turn_ownership():
    rows = _rows(_message('parent-message', 'Parent facts', turn='parent-turn'),
                 _completed('parent-message', 'Parent facts', turn='parent-turn', thread='parent'),
                 _message('child-message', 'Child facts', turn='child-turn'),
                 _completed('child-message', 'Child facts', turn='child-turn'))
    parsed = parse_rows(CTX, rows, _header(forked_from_id='parent'), {})
    assert parsed.status == 'ok'
    assert [r.text for r in parsed.records] == ['Child facts']


def test_ambiguous_fork_prefix_remains_pending():
    parsed = parse_rows(CTX, _rows(_message('ambiguous')), _header(forked_from_id='parent'), {})
    assert parsed.status == 'partial'
    assert parsed.blocked_offset == 10
    assert not parsed.records


def test_unknown_shape_retains_offset():
    parsed = parse_rows(CTX, _rows({'type': 'future_record', 'payload': {}}), _header(), {})
    assert parsed.status == 'partial'
    assert parsed.blocked_offset == 10


def test_tools_never_infer_file_changes_from_javascript():
    row = {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'id': 'tool1',
           'call_id': 'call1', 'name': 'exec', 'input': 'await tools.exec_command({cmd:"echo edit file.py"})'}}
    parsed = parse_rows(CTX, _rows(row), _header(), {})
    assert parsed.records[0].tool_name == 'exec'
    assert parsed.records[0].tool_category == 'unknown'
    assert parsed.records[0].kind == 'tool_call'
    assert not any('file' in key for key in parsed.metadata)


def test_compaction_and_interruption_control_kinds():
    parsed = parse_rows(CTX, _rows({'type': 'compacted', 'payload': {'replacement_history': 'private'}},
        {'type': 'event_msg', 'payload': {'type': 'turn_aborted', 'turn_id': 'turn'}},
        {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn', 'last_agent_message': 'mirror'}}), _header(), {})
    assert [r.kind for r in parsed.records] == ['compaction', 'interruption', 'turn_end']
    assert all(r.role == 'control' for r in parsed.records)
    assert 'private' not in repr(parsed)


@pytest.mark.parametrize('filename', [
    'codex-cli-0.159.0-alpha.12.1-original.jsonl',
    'codex-cli-0.159.0-alpha.12.1-fork.jsonl',
    'codex-desktop-0.159.0-alpha.12.1-session.jsonl',
])
def test_sanitized_native_rollouts(filename):
    path = Path(__file__).parent / 'fixtures' / 'hosts' / filename
    data = [json.loads(line) for line in path.read_text().splitlines()]
    context = SimpleNamespace(host='codex', native_session_id=data[0]['payload']['id'], transcript_path=path)
    result = read_records(context, SourceCursor(historical=True), time.monotonic() + 2)
    assert result.status == 'ok'
    assert result.source_complete
    assert len([r.source_id for r in result.records]) == len(set(r.source_id for r in result.records))
    assert not any('<redacted>' in r.text for r in result.records if r.kind == 'message')


def test_unknown_user_provenance_is_retained_not_assumed_human():
    row = _message()
    row['payload']['internal_chat_message_metadata_passthrough']['content_item_kinds'] = ['future.kind']
    result = parse_rows(CTX, _rows(row), _header(), {})
    assert result.status == 'partial'
    assert result.blocked_offset == 10
    assert not result.records


def test_same_text_across_turns_is_not_mirror_deduped():
    rows = _rows(_message('first', turn='first-turn'), _completed('second', turn='second-turn'))
    result = parse_rows(CTX, rows, _header(), {})
    assert len(result.records) == 2


def test_unknown_turn_distant_occurrences_are_not_paired():
    rows = _rows(_message('first', turn=None), {'type': 'world_state', 'payload': {}},
                 _completed('second', turn=None))
    result = parse_rows(CTX, rows, _header(), {})
    assert len(result.records) == 2


def test_stable_native_ids_make_replay_idempotent():
    rows = _rows(_message('first'), _completed('first'))
    first = parse_rows(CTX, rows, _header(), {})
    second = parse_rows(CTX, rows, _header(), first.parser_state)
    assert not second.records
    assert not second.parser_state['mirror_pairs']


def test_parser_state_is_bounded():
    from transcripts.codex import MAX_SEEN_IDS, MAX_ASSOCIATIONS
    rows = _rows(*[_message('id' + str(i), turn='turn' + str(i)) for i in range(MAX_SEEN_IDS + 10)])
    result = parse_rows(CTX, rows, _header(), {})
    assert len(result.records) == MAX_SEEN_IDS + 10
    assert len(result.parser_state['seen_ids']) == MAX_SEEN_IDS
    assert len(result.parser_state['mirror_pairs']) <= MAX_ASSOCIATIONS


def test_completed_item_thread_id_proves_parent_ownership():
    parent = _completed('parent', 'Inherited facts', thread=None)
    parent['payload']['item']['thread_id'] = 'parent'
    result = parse_rows(CTX, _rows(_message('parent', 'Inherited facts'), parent), _header(forked_from_id='parent'), {})
    assert result.status == 'ok'
    assert not result.records


def test_unbounded_native_id_is_retained_not_stored():
    result = parse_rows(CTX, _rows(_message('x' * 513)), _header(), {})
    assert result.status == 'partial'
    assert not result.parser_state['seen_ids']


@pytest.mark.parametrize('content', [None, ['bad block'], [{'type': 'text', 'text': 7}], [{'type': 'future', 'text': 'unknown'}]])
def test_malformed_visible_content_retains_source(content):
    row = _message()
    row['payload']['content'] = content
    result = parse_rows(CTX, _rows(row), _header(), {})
    assert result.status == 'partial'
    assert result.blocked_offset == 10


def test_reasoning_and_encrypted_payload_are_ignored():
    result = parse_rows(CTX, _rows({'type': 'response_item', 'payload': {
        'type': 'reasoning', 'summary': 'PRIVATE ANALYSIS', 'encrypted_content': 'PRIVATE ENCRYPTED',
    }}), _header(), {})
    assert result.status == 'ok'
    assert not result.records
    assert 'PRIVATE' not in repr(result)


def test_source_header_must_match_selected_native_identity():
    wrong = _header()
    wrong.data['payload']['id'] = 'other-session'
    assert parse_rows(CTX, (), wrong, {}).status == 'partial'
    assert parse_rows(CTX, (), RawRecord(0, 10, {'type': 'future'}), {}).status == 'unsupported'


def test_unknown_source_row_does_not_advance_durable_cursor(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    data = [_header().data, _message(), {'type': 'future_shape', 'payload': {}}]
    encoded = [json.dumps(row) + '\n' for row in data]
    path.write_text(''.join(encoded))
    context = SimpleNamespace(host='codex', native_session_id='child', transcript_path=path)
    result = read_records(context, SourceCursor(historical=True), time.monotonic() + 2)
    assert result.status == 'partial'
    assert not result.source_complete
    assert result.consumed_offset == len(''.join(encoded[:2]).encode())
    assert len(result.records) == 1


def test_full_fork_selects_later_child_metadata_and_excludes_parent_prefix():
    parent_header = RawRecord(0, 10, {'type': 'session_meta', 'payload': {'id': 'parent', 'cwd': '/parent', 'instructions': 'private'}})
    rows = _rows(_message('parent-message', 'Parent facts', turn='parent-turn'),
                 _completed('parent-message', 'Parent facts', turn='parent-turn', thread='parent'),
                 _header(forked_from_id='parent').data,
                 _message('child-message', 'Child facts', turn='child-turn'),
                 _completed('child-message', 'Child facts', turn='child-turn'))
    result = parse_rows(CTX, rows, parent_header, {})
    assert result.status == 'ok'
    assert [r.text for r in result.records] == ['Child facts']
    assert result.metadata == {'native_session_id': 'child', 'cwd': '/project', 'fork_parent_id': 'parent'}
    resumed = parse_rows(CTX, _rows(_message('next', 'Next child facts', turn='child-turn')),
                         parent_header, result.parser_state)
    assert resumed.status == 'ok'
    assert [r.text for r in resumed.records] == ['Next child facts']


def test_parent_prefix_without_child_metadata_is_retained():
    parent_header = RawRecord(0, 10, {'type': 'session_meta', 'payload': {'id': 'parent'}})
    result = parse_rows(CTX, _rows(_message('parent', 'Ambiguous facts')), parent_header, {})
    assert result.status == 'partial'
    assert result.blocked_offset == 0
    assert not result.records


def test_full_fork_ambiguous_prefix_remains_pending():
    parent_header = RawRecord(0, 10, {'type': 'session_meta', 'payload': {'id': 'parent'}})
    result = parse_rows(CTX, _rows(_message('ambiguous'), _header(forked_from_id='parent').data), parent_header, {})
    assert result.status == 'partial'
    assert result.blocked_offset == 10
    assert not result.records


def test_offset_fallback_rotation_requires_source_generation(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    context = SimpleNamespace(host='codex', native_session_id='child', transcript_path=path)
    row = _message(text='First source')
    row['payload'].pop('id')
    path.write_text(json.dumps(_header().data) + '\n' + json.dumps(row) + '\n')
    first = read_records(context, SourceCursor(historical=True), time.monotonic() + 2)
    assert first.records[0].source_id.startswith('offset:')
    replacement = tmp_path / 'replacement.jsonl'
    row['payload']['content'][0]['text'] = 'Other source'
    replacement.write_text(json.dumps(_header().data) + '\n' + json.dumps(row) + '\n')
    replacement.replace(path)
    cursor = SourceCursor(generation=first.source_generation, offset=first.consumed_offset,
                          source_identity=first.source_identity, anchor_digest=first.anchor_digest,
                          parser_state=first.parser_state, historical=True, known_size=first.source_size)
    second = read_records(context, cursor, time.monotonic() + 2)
    assert second.records[0].source_id != first.records[0].source_id
    assert first.records[0].source_id.startswith('offset:' + first.source_generation + ':')
    assert second.records[0].source_id.startswith('offset:' + second.source_generation + ':')
    assert second.source_generation != first.source_generation
    assert second.records[0].text == 'Other source'
