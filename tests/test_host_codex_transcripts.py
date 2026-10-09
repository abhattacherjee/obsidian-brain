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
    assert parsed.blocked_offset is None

    assert parsed.deferred_rows[0].offset == 10


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
    assert result.blocked_offset is None
    assert not result.records

    assert result.deferred_rows[0].offset == 10


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
    if content == [{'type': 'future', 'text': 'unknown'}]:
        assert result.blocked_offset is None and result.deferred_rows[0].offset == 10
    else:
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


def test_unknown_source_row_advances_with_private_deferred_reference(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    data = [_header().data, _message(), {'type': 'future_shape', 'payload': {}}]
    encoded = [json.dumps(row) + '\n' for row in data]
    path.write_text(''.join(encoded))
    context = SimpleNamespace(host='codex', native_session_id='child', transcript_path=path)
    result = read_records(context, SourceCursor(historical=True), time.monotonic() + 2)
    assert result.status == 'partial'
    assert not result.source_complete
    assert result.consumed_offset == path.stat().st_size
    ref = result.parser_state['_deferred_source_rows'][0]
    assert ref['offset'] == len(''.join(encoded[:2]).encode())
    assert ref['end_offset'] == path.stat().st_size
    assert ref['reason'] == 'unknown_schema:future_shape'
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


pytestmark = pytest.mark.host_only("codex", reason="codex-record-format", capability="codex_native_format")


def _current_user(text='Current request'):
    return {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': text,
        'local_images': [], 'local_audio': [], 'text_elements': []}}


def test_current_user_event_mirror_and_repeated_human_turns():
    mirror = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
        'content': [{'type': 'input_text', 'text': 'Current request'}]}}
    parsed = parse_rows(CTX, _rows(_current_user(), mirror, _current_user(), mirror), _header(), {})
    assert parsed.status == 'ok'
    assert [record.text for record in parsed.records] == ['Current request', 'Current request']
    assert len({record.source_id for record in parsed.records}) == 2


def test_current_user_event_mirror_across_batches():
    first = parse_rows(CTX, _rows(_current_user()), _header(), {})
    mirror = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
        'content': [{'type': 'input_text', 'text': 'Current request'}]}}
    second = parse_rows(CTX, (RawRecord(20, 30, mirror),), _header(), first.parser_state)
    assert first.status == second.status == 'ok'
    assert len(first.records) == 1 and not second.records


@pytest.mark.parametrize('provenance', ['additional_content.codex_apps_open_page', 'plugins.recommendations'])
def test_verified_current_injected_provenance_is_private(provenance):
    value = _message(text='Private context')
    value['payload']['internal_chat_message_metadata_passthrough']['content_item_kinds'] = [provenance]
    parsed = parse_rows(CTX, _rows(value, _current_user()), _header(), {})
    assert parsed.status == 'ok'
    assert [record.text for record in parsed.records] == ['Current request']


def test_encrypted_response_compaction_is_boundary_without_content():
    row = {'type': 'response_item', 'payload': {'type': 'compaction', 'id': 'compact1',
        'encrypted_content': 'Private encrypted bytes', 'internal_chat_message_metadata_passthrough': {'turn_id': 'turn'}}}
    parsed = parse_rows(CTX, _rows(row, _current_user()), _header(), {})
    assert parsed.status == 'ok'
    assert [record.kind for record in parsed.records] == ['compaction', 'message']
    assert 'Private' not in json.dumps(parsed.parser_state)
    assert not any('Private' in record.text for record in parsed.records)


@pytest.mark.parametrize('damage', ['extra', 'missing', 'wrong'])
def test_current_user_event_schema_drift_blocks(damage):
    row = _current_user()
    if damage == 'extra': row['payload']['future_content'] = 'private'
    elif damage == 'missing': row['payload'].pop('local_audio')
    else: row['payload']['message'] = ['private']
    parsed = parse_rows(CTX, _rows(row), _header(), {})
    assert parsed.status == 'partial' and parsed.blocked_offset == 10 and not parsed.records


CURRENT_CODEX_ROWS = [json.loads(line) for line in (Path(__file__).parent / 'fixtures/hosts/codex-0.159.0-verified-records.jsonl').read_text().splitlines()]

@pytest.mark.parametrize('value', [row for row in CURRENT_CODEX_ROWS if row.get('payload', {}).get('type') == 'item_completed'], ids=lambda row: row['payload']['item']['type'])
def test_current_native_completed_activity_summary(value):
    parsed = parse_rows(CTX, _rows(value), _header(), {})
    assert parsed.status == 'ok' and len(parsed.records) == 1
    assert parsed.records[0].role == 'tool'
    assert '<text>' not in parsed.records[0].text
    assert 'agent_path' not in json.dumps(parsed.parser_state)

@pytest.mark.parametrize('item_type', ['SubAgentActivity', 'FileChange'])
def test_current_completed_activity_extra_substantive_field_blocks(item_type):
    import copy
    value = copy.deepcopy(next(row for row in CURRENT_CODEX_ROWS if row.get('payload', {}).get('item', {}).get('type') == item_type))
    value['payload']['item']['future_content'] = 'private'
    parsed = parse_rows(CTX, _rows(value), _header(), {})
    assert parsed.status == 'partial' and parsed.blocked_offset == 10 and not parsed.records


def test_verified_inter_agent_metadata_is_private_but_future_fields_block():
    value = {'type': 'inter_agent_communication_metadata', 'payload': {'trigger_turn': True}}
    parsed = parse_rows(CTX, _rows(value, _current_user()), _header(), {})
    assert parsed.status == 'ok' and [record.text for record in parsed.records] == ['Current request']
    value['payload']['content'] = 'Future substantive field'
    parsed = parse_rows(CTX, _rows(value), _header(), {})
    assert parsed.status == 'partial' and parsed.blocked_offset == 10 and not parsed.records


@pytest.mark.parametrize('value', [row for row in CURRENT_CODEX_ROWS if row.get('type') == 'response_item' and row.get('payload', {}).get('type') == 'agent_message'])
def test_agent_message_has_explicit_coordination_provenance(value):
    parsed = parse_rows(CTX, _rows(value), _header(), {})
    assert parsed.status == 'ok' and len(parsed.records) == 1
    record = parsed.records[0]
    assert record.role == 'agent' and record.kind == 'inter_agent_message'
    assert record.source_actor == value['payload']['author']
    assert record.source_recipient == value['payload']['recipient']
    assert record.text == '<text>' and '<opaque>' not in record.text
    assert record.turn_id == value['payload']['internal_chat_message_metadata_passthrough']['turn_id']
    assert '<text>' not in json.dumps(parsed.parser_state)


def test_live_user_mirror_in_fork_is_not_ambiguous_inherited_history():
    mirror = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
        'content': [{'type': 'input_text', 'text': 'Current request'}]}}
    parsed = parse_rows(CTX, _rows(_current_user(), mirror), _header(forked_from_id='parent'), {})
    assert parsed.status == 'ok' and [record.text for record in parsed.records] == ['Current request']
    inherited = _message('inherited')
    inherited['metadata'] = {'inherited_user_message': True}
    parsed = parse_rows(CTX, _rows(inherited, _current_user(), mirror), _header(forked_from_id='parent'), {})
    assert parsed.status == 'ok' and [record.text for record in parsed.records] == ['Current request']


def test_current_compacted_context_is_boundary_not_replayed_conversation():
    value = next(row for row in CURRENT_CODEX_ROWS if row['type'] == 'compacted')
    parsed = parse_rows(CTX, _rows(value), _header(forked_from_id='parent'), {})
    assert parsed.status == 'ok'
    assert [record.kind for record in parsed.records] == ['compaction']
    assert '<text>' not in json.dumps(parsed.parser_state)


@pytest.mark.parametrize('damage', ['extra-field', 'address', 'content-order', 'turn-type', 'time-bool'])
def test_agent_message_schema_drift_retains_blocked_offset(damage):
    import copy
    value = copy.deepcopy(next(row for row in CURRENT_CODEX_ROWS if row.get('payload', {}).get('type') == 'agent_message'))
    payload = value['payload']
    if damage == 'extra-field': payload['future_content'] = 'private'
    elif damage == 'address': payload['author'] = '/private/user/path'
    elif damage == 'content-order': payload['content'] = [{'type':'encrypted_content','encrypted_content':'private'}, {'type':'input_text','text':'visible'}]
    elif damage == 'turn-type': payload['internal_chat_message_metadata_passthrough']['turn_id'] = []
    else: payload['internal_chat_message_metadata_passthrough']['create_time'] = True
    parsed = parse_rows(CTX, _rows(value), _header(), {})
    assert parsed.status == 'partial' and parsed.blocked_offset == 10 and not parsed.records


def test_current_user_mirror_stripped_text_does_not_duplicate_turn():
    mirror = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
        'content': [{'type': 'input_text', 'text': ' Current request \n'}]}}
    parsed = parse_rows(CTX, _rows(_current_user(), mirror), _header(), {})
    assert parsed.status == 'ok' and [record.text for record in parsed.records] == ['Current request']


def test_current_file_add_is_native_summary_not_full_file_content():
    import copy
    value = copy.deepcopy(next(row for row in CURRENT_CODEX_ROWS if row.get('payload', {}).get('item', {}).get('type') == 'FileChange'))
    value['payload']['item']['changes'] = {'synthetic.txt': {'type': 'add', 'content': 'Private full file contents'}}
    parsed = parse_rows(CTX, _rows(value), _header(), {})
    assert parsed.status == 'ok' and parsed.records[0].text == 'Native file changes: 1'
    assert 'Private' not in json.dumps(parsed.parser_state)


def test_unknown_private_provenance_is_not_hidden_by_named_known_prefixes():
    value = _message(text='Unknown injected content')
    value['payload']['internal_chat_message_metadata_passthrough']['content_item_kinds'] = ['additional_content.future_substantive']
    parsed = parse_rows(CTX, _rows(value), _header(), {})
    assert parsed.status == 'partial' and parsed.blocked_offset is None

    assert parsed.deferred_rows[0].offset == 10


def test_current_codex_fixture_provenance_is_synthetic_and_byte_bound():
    import hashlib
    path = Path(__file__).parent / 'fixtures/hosts/codex-0.159.0-verified-records.jsonl'
    provenance = json.loads(path.with_suffix('.provenance.json').read_text())
    assert provenance['native_capture_verified'] is False
    assert provenance['fixture_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()



def _assistant_event(text='Synthetic assistant answer'):
    return {'type':'event_msg','payload':{'type':'agent_message','message':text,'phase':None,'memory_citation':None}}


def test_assistant_event_mirror_keeps_distinct_repeated_answers():
    mirror = {'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':'Synthetic assistant answer'}]}}
    parsed = parse_rows(CTX, _rows(_assistant_event(),mirror,_assistant_event(),mirror), _header(), {})
    assert parsed.status == 'ok'
    assert [record.text for record in parsed.records] == ['Synthetic assistant answer']*2
    assert all(record.role == 'assistant' for record in parsed.records)


@pytest.mark.parametrize('damage', ['extra', 'phase', 'memory'])
def test_assistant_event_unverified_fields_block(damage):
    value = _assistant_event()
    if damage == 'extra': value['payload']['content'] = 'Future substantive field'
    elif damage == 'phase': value['payload']['phase'] = 'analysis'
    else: value['payload']['memory_citation'] = {'private':'citation'}
    parsed = parse_rows(CTX, _rows(value), _header(), {})
    assert parsed.status == 'partial' and parsed.blocked_offset == 10 and not parsed.records


@pytest.mark.parametrize('kind', ['CollabAgentToolCall','McpToolCall'])
@pytest.mark.parametrize('damage', ['extra', 'status', 'malformed'])
def test_named_native_tool_envelope_drift_blocks(kind,damage):
    import copy
    value = copy.deepcopy(next(row for row in CURRENT_CODEX_ROWS if row.get('payload',{}).get('item',{}).get('type')==kind))
    item=value['payload']['item']
    if damage=='extra': item['future_content']='Substantive'
    elif damage=='status': item['status']='future_status'
    elif kind=='McpToolCall': item['duration']['secs']=True
    else: item['receiver_agents']=['Unverified receiver']
    parsed=parse_rows(CTX,_rows(value),_header(),{})
    assert parsed.status=='partial' and parsed.blocked_offset==10 and not parsed.records


def test_verified_failed_mcp_records_failure_without_private_result():
    import copy
    value = copy.deepcopy(next(row for row in CURRENT_CODEX_ROWS if row.get('payload', {}).get('item', {}).get('type') == 'McpToolCall'))
    value['payload']['item']['status'] = 'failed'
    value['payload']['item']['result'] = {'private': 'SECRET'}
    parsed = parse_rows(CTX, _rows(value), _header(), {})
    assert parsed.status == 'ok'
    assert parsed.records[0].text == 'Native MCP tool failed'
    assert 'SECRET' not in json.dumps(parsed.parser_state)


def test_verified_clock_extension_has_no_private_tool_output():
    value = {'type':'event_msg','payload':{'type':'item_completed','thread_id':'child','turn_id':'turn',
             'started_at_ms':1,'completed_at_ms':2,
             'item':{'type':'Extension','id':'clock-id','kind':'clock.sleep','durationMs':1}}}
    parsed = parse_rows(CTX, _rows(value), _header(), {})
    assert parsed.status == 'ok'
    assert [(record.tool_name, record.text) for record in parsed.records] == [('clock.sleep', 'Native clock sleep completed')]
    value['payload']['item']['durationMs'] = True
    parsed = parse_rows(CTX, _rows(value), _header(), {})
    assert parsed.status == 'partial' and parsed.blocked_offset == 10


def test_same_turn_distant_opposite_flavors_are_not_text_deduplicated():
    start = {'type':'event_msg','payload':{'type':'task_started','turn_id':'turn'}}
    unrelated = {'type':'event_msg','payload':{'type':'token_count'}}
    response = _message('new-answer', 'Synthetic assistant answer', role='assistant')
    parsed = parse_rows(CTX, _rows(start, _assistant_event(), unrelated, response), _header(), {})
    assert parsed.status == 'ok'
    assert [r.text for r in parsed.records if r.role == 'assistant'] == ['Synthetic assistant answer'] * 2


def test_batch_turn_owner_proof_is_not_evicted_before_early_message():
    rows = [_message('parent-first', 'Parent facts', turn='parent-turn'),
            _completed('parent-first', 'Parent facts', turn='parent-turn', thread='parent')]
    rows.extend(_completed('child-' + str(i), 'Child ' + str(i), turn='turn-' + str(i)) for i in range(257))
    parsed = parse_rows(CTX, _rows(*rows), _header(forked_from_id='parent'), {})
    assert parsed.status == 'ok'
    assert 'Parent facts' not in [record.text for record in parsed.records]
    assert len(parsed.records) == 257
    assert len(parsed.parser_state['turn_owners']) <= 256


@pytest.mark.parametrize('action', [
    {'type':'openPage','url':'https://example.invalid'},
    {'type':'findInPage','url':None,'pattern':'synthetic'},
    {'type':'findInPage','url':'https://example.invalid','pattern':'synthetic'},
    {'type':'other'},
    {'type':'search','query':'synthetic','queries':None},
    {'type':'search','query':None,'queries':['synthetic']},
])
def test_verified_web_extension_hides_query_results_and_keeps_tool_name(action):
    item = {'type':'Extension','id':'web-id','kind':'web.search','query':'PRIVATE QUERY',
            'action':action,'results':[{'type':'text_result','ref_id':'private-ref','snippet':'SECRET','title':'private-title'}]}
    value = {'type':'event_msg','payload':{'type':'item_completed','thread_id':'child','turn_id':'turn',
             'started_at_ms':1,'completed_at_ms':2,'item':item}}
    parsed = parse_rows(CTX, _rows(value), _header(), {})
    assert parsed.status == 'ok'
    assert [(record.tool_name,record.text) for record in parsed.records] == [('web.search','Native web action completed')]
    assert 'SECRET' not in json.dumps(parsed.parser_state)
    item['action']['future_content'] = 'substantive'
    parsed = parse_rows(CTX, _rows(value), _header(), {})
    assert parsed.status == 'partial' and parsed.blocked_offset == 10


def _native_turn_start(turn='own-turn'):
    return {'type':'event_msg','payload':{'type':'task_started','turn_id':turn,'started_at':1,
            'collaboration_mode_kind':'default','model_context_window':None}}


def test_verified_own_task_turn_retains_subagent_input_before_user_event():
    value = _message('own-input', 'Subagent input', turn='own-turn')
    value['payload']['internal_chat_message_metadata_passthrough'].update(content_item_kinds=['user.text'],create_time=1)
    value['metadata'] = {'inherited_user_message':True}
    parsed = parse_rows(CTX, _rows(value,_native_turn_start()), _header(forked_from_id='parent'), {})
    assert parsed.status == 'ok'
    assert [r.text for r in parsed.records if r.role=='user'] == ['Subagent input']


def test_selected_task_started_cannot_override_explicit_parent_turn_proof():
    parsed = parse_rows(CTX, _rows(_message(turn='own-turn'),_native_turn_start(),
                        _completed(turn='own-turn',thread='parent')), _header(forked_from_id='parent'), {})
    assert parsed.status == 'ok'
    assert not [r for r in parsed.records if r.role=='user']


def test_verified_questions_preserve_unmatched_visible_prompt():
    value = _completed(id='question',text='Choose an option',type='AgentMessage')
    value['payload']['item'].update(phase='commentary',delivery='synthetic-native-delivery',
                                   questions=[{'title':'Preferred approach?','options':['First','Second']}])
    parsed = parse_rows(CTX,_rows(value),_header(),{})
    assert parsed.status == 'ok'
    assert parsed.records[0].text == 'Choose an option\nPreferred approach?\n- First\n- Second'
    assert 'synthetic-native-delivery' not in parsed.records[0].text
    value['payload']['item']['questions'][0]['future_content']='hidden'
    parsed = parse_rows(CTX,_rows(value),_header(),{})
    assert parsed.status=='partial' and parsed.blocked_offset==10


def _known_unattributed_user():
    value = _message('unattributed-input', 'Private unattributed input', turn='unproven-turn')
    value['payload']['internal_chat_message_metadata_passthrough'].update(content_item_kinds=['user.text'], create_time=1.0)
    return value


def test_known_unattributed_user_does_not_stop_proven_own_messages():
    parsed = parse_rows(CTX, _rows(_completed('before','Own before',turn='before-turn'),
                        _known_unattributed_user(),_completed('after','Own after',turn='after-turn')),
                        _header(forked_from_id='parent'), {})
    assert parsed.status == 'partial' and parsed.blocked_offset is None
    assert [record.text for record in parsed.records] == ['Own before','Own after']
    assert [(row.offset,row.end_offset,row.reason) for row in parsed.deferred_rows] == [(20,30,'ambiguous_ownership')]
    assert 'Private unattributed input' not in json.dumps(parsed.parser_state)
    assert parsed.warnings == ('Ambiguous Codex user ownership retained',)


def test_proven_foreign_user_does_not_stop_own_before_after():
    value = _known_unattributed_user()
    value['payload']['thread_id'] = 'parent'
    parsed = parse_rows(CTX, _rows(_completed('before','Own before',turn='before-turn'),value,
                        _completed('after','Own after',turn='after-turn')), _header(forked_from_id='parent'), {})
    assert parsed.status == 'ok'
    assert [record.text for record in parsed.records] == ['Own before','Own after']
    assert not parsed.deferred_rows


@pytest.mark.parametrize('damage',['extra-payload','extra-metadata','bad-content','bad-time'])
def test_unattributed_user_schema_drift_still_blocks(damage):
    value = _known_unattributed_user()
    if damage=='extra-payload': value['payload']['future_content']='Substantive'
    elif damage=='extra-metadata': value['payload']['internal_chat_message_metadata_passthrough']['future_content']='Substantive'
    elif damage=='bad-content': value['payload']['content'][0]['type']='future_visible'
    else: value['payload']['internal_chat_message_metadata_passthrough']['create_time']=True
    parsed = parse_rows(CTX, _rows(_completed('before','Own before',turn='before-turn'),value,
                        _completed('after','Own after',turn='after-turn')), _header(forked_from_id='parent'), {})
    if damage == 'bad-content':
        assert parsed.status == 'partial' and parsed.blocked_offset is None
        assert [record.text for record in parsed.records] == ['Own before', 'Own after']
        assert parsed.deferred_rows[0].reason == 'unknown_schema:future_visible'
    else:
        assert parsed.status == 'partial' and parsed.blocked_offset == 20
        assert [record.text for record in parsed.records] == ['Own before']
        assert not parsed.deferred_rows


def test_inherited_flag_does_not_silently_drop_known_unproven_user():
    value = _known_unattributed_user()
    value['metadata'] = {'inherited_user_message':True}
    parsed = parse_rows(CTX, _rows(value,_completed('after','Own after',turn='after-turn')),
                        _header(forked_from_id='parent'), {})
    assert parsed.status=='partial' and parsed.blocked_offset is None
    assert [r.text for r in parsed.records]==['Own after']
    assert [(r.offset,r.end_offset) for r in parsed.deferred_rows]==[(10,20)]


def test_unknown_control_defers_exact_row_and_requires_renewed_owner_proof():
    unknown={'type':'future_control','payload':{'private':'NOT FOR NOTES'}}
    uncertain={'type':'response_item','payload':{'type':'message','role':'assistant',
               'content':[{'type':'output_text','text':'Unproven inherited answer'}]}}
    parsed=parse_rows(CTX,_rows(_completed('before','Own before'),unknown,uncertain,
                      _completed('after','Own after',turn='after-turn')),_header(),{})
    assert parsed.status=='partial' and parsed.blocked_offset is None
    assert [r.text for r in parsed.records]==['Own before','Own after']
    assert [(r.offset,r.reason) for r in parsed.deferred_rows]==[(20,'unknown_schema:future_control'),(30,'ambiguous_ownership')]
    assert 'NOT FOR NOTES' not in json.dumps(parsed.parser_state)


@pytest.mark.parametrize('kind,label',[('future_record','future_record'),('sk-secret','unrecognized'),
    ('xoxb-secret','unrecognized'),('bad\nlabel','unrecognized'),('space label','unrecognized'),('a'*49,'unrecognized')])
def test_codex_unknown_label_is_bounded_and_safe(kind,label):
    parsed=parse_rows(CTX,_rows({'type':kind,'payload':{'content':'private'}}),_header(),{})
    assert parsed.status=='partial' and parsed.blocked_offset is None
    assert parsed.warnings==('Unknown substantive Codex transcript record (type='+label+')',)
    assert parsed.deferred_rows[0].reason=='unknown_schema:'+label


@pytest.mark.parametrize('event',[_current_user('Unproven user'),_assistant_event('Unproven assistant')])
def test_unknown_control_defers_unowned_native_event_before_own_item(event):
    parsed=parse_rows(CTX,_rows({'type':'future_control','payload':{}},event,
                      _completed('after','Own after',turn='after-turn')),_header(),{})
    assert parsed.status=='partial' and parsed.blocked_offset is None
    assert [r.text for r in parsed.records]==['Own after']
    assert [(r.offset,r.reason) for r in parsed.deferred_rows]==[(10,'unknown_schema:future_control'),(20,'ambiguous_ownership')]


def test_unknown_control_proof_requirement_survives_parser_calls():
    first=parse_rows(CTX,_rows({'type':'future_control','payload':{}}),_header(),{})
    second=parse_rows(CTX,_rows(_assistant_event('Unproven answer'),_completed('own','Owned answer')),
                      _header(),first.parser_state)
    assert [r.text for r in second.records]==['Owned answer']
    assert second.deferred_rows[0].reason=='ambiguous_ownership'


def test_parent_task_start_cannot_clear_unknown_control_ownership_requirement():
    parsed=parse_rows(CTX,_rows({'type':'future_control','payload':{}},_native_turn_start(),
                      _completed('parent','Parent answer',turn='own-turn',thread='parent'),
                      _assistant_event('Unproven answer'),_completed('own','Owned answer',turn='after')),
                      _header(forked_from_id='parent'),{})
    assert [r.text for r in parsed.records]==['Owned answer']
    assert parsed.blocked_offset is None
    assert [r.reason for r in parsed.deferred_rows]==['unknown_schema:future_control','ambiguous_ownership']


def test_proven_child_turn_survives_replayed_parent_metadata_prelude():
    parent_header=_header().data
    parent_header['payload']['id']='parent'
    parsed=parse_rows(CTX,_rows(parent_header,
        _completed('parent-input','Parent input',turn='parent-turn',thread='parent'),
        _native_turn_start('child-turn'),_current_user('Child question'),
        _assistant_event('Child answer'),
        _completed('child-proof','Child final proof',turn='child-turn',thread='child')),
        _header(forked_from_id='parent'),{})
    assert parsed.status=='ok'
    assert [r.text for r in parsed.records if r.role in {'user','assistant'}]==[
        'Child question','Child answer','Child final proof']


def test_unrelated_metadata_cannot_be_treated_as_replayed_fork_parent():
    unrelated=_header().data
    unrelated['payload']['id']='unrelated'
    parsed=parse_rows(CTX,_rows(_completed('before','Owned before'),unrelated,
                      _completed('after','Owned after')),_header(forked_from_id='parent'),{})
    assert parsed.status=='partial' and parsed.blocked_offset==20
    assert [r.text for r in parsed.records]==['Owned before']


def test_later_matching_header_needs_proven_fork_relation_to_physical_parent():
    physical=_header().data
    physical['payload']['id']='unrelated'
    parsed=parse_rows(CTX,_rows(_header().data,_completed('own','Owned message')),
                      RawRecord(0,10,physical),{})
    assert parsed.status=='partial' and parsed.blocked_offset==0
    assert not parsed.records


def _replay_header(boundary=4):
    return _header(session_id='parent',forked_from_id='parent',parent_thread_id='parent',
        subagent_history_start_ordinal=boundary,source={'subagent':{'thread_spawn':{
            'parent_thread_id':'parent','depth':1,'agent_path':'/root/child',
            'agent_nickname':'Synthetic child','agent_role':None}}})


def _ordinal_rows(*values,start=1):
    import copy
    rows=[]
    for ordinal,value in enumerate(values,start=start):
        value=copy.deepcopy(value);value['ordinal']=ordinal
        rows.append(RawRecord(ordinal*10,(ordinal+1)*10,value))
    return tuple(rows)


def _replay_rows():
    parent=_header().data;parent['payload']['id']='parent'
    return _ordinal_rows(parent,_native_turn_start('parent-turn'),
        _message('parent-question','Inherited parent question',turn='parent-turn'),
        {'type':'event_msg','payload':{'type':'thread_settings_applied','thread_id':'child','thread_settings':{}}},
        _native_turn_start('child-turn'),_current_user('Own child question'),
        _assistant_event('Own child answer'))


def test_verified_native_subagent_boundary_excludes_parent_prefix_and_keeps_child():
    parsed=parse_rows(CTX,_replay_rows(),_replay_header(),{'_source_generation':'a'*64})
    assert parsed.status=='ok' and parsed.blocked_offset is None
    assert [r.text for r in parsed.records if r.role in {'user','assistant'}]==['Own child question','Own child answer']
    assert not parsed.deferred_rows


@pytest.mark.parametrize('damage',['wrong-session','wrong-parent','wrong-spawn-parent','wrong-boundary-thread',
    'negative-cutoff','bool-cutoff','text-cutoff','missing-boundary','wrong-cutoff'])
def test_subagent_boundary_requires_exact_parent_and_child_proof(damage):
    import copy
    header=copy.deepcopy(_replay_header());rows=list(_replay_rows())
    if damage=='wrong-session':header.data['payload']['session_id']='unrelated'
    elif damage=='wrong-parent':header.data['payload']['parent_thread_id']='unrelated'
    elif damage=='wrong-spawn-parent':header.data['payload']['source']['subagent']['thread_spawn']['parent_thread_id']='unrelated'
    elif damage=='wrong-boundary-thread':rows[3].data['payload']['thread_id']='parent'
    elif damage=='negative-cutoff':header.data['payload']['subagent_history_start_ordinal']=-1
    elif damage=='bool-cutoff':header.data['payload']['subagent_history_start_ordinal']=True
    elif damage=='text-cutoff':header.data['payload']['subagent_history_start_ordinal']='4'
    elif damage=='missing-boundary':rows.pop(3)
    else:header.data['payload']['subagent_history_start_ordinal']=5
    parsed=parse_rows(CTX,tuple(rows),header,{'_source_generation':'a'*64})
    assert parsed.status=='partial'
    assert not [r for r in parsed.records if r.role in {'user','assistant'}]


def test_subagent_boundary_can_be_proven_in_later_batch_same_generation():
    rows=_replay_rows();header=_replay_header()
    first=parse_rows(CTX,rows[:3],header,{'_source_generation':'a'*64})
    assert first.status=='partial' and not first.records
    assert first.blocked_offset is None
    second=parse_rows(CTX,rows[3:],header,{**first.parser_state,'_source_generation':'a'*64})
    assert second.status=='ok'
    assert [r.text for r in second.records if r.role in {'user','assistant'}]==['Own child question','Own child answer']
    third=parse_rows(CTX,_ordinal_rows(_assistant_event('Next own answer'),start=8),header,
                     {**second.parser_state,'_source_generation':'b'*64})
    assert third.status=='partial'
    assert not third.records



def test_single_header_subagent_history_marker_is_not_double_header_replay():
    header=_header(session_id='origin-session',parent_thread_id='origin-thread',
                   subagent_history_start_ordinal=10,source={'subagent':'review'})
    compaction={'type':'response_item','payload':{'type':'compaction','id':'boundary',
        'encrypted_content':'PRIVATE ENCRYPTED',
        'internal_chat_message_metadata_passthrough':{'turn_id':'own-turn'}}}
    parsed=parse_rows(CTX,_rows(compaction,_message('own','Own question',turn='own-turn')),
                      header,{'_source_generation':'a'*64})
    assert parsed.status=='ok'
    assert [r.text for r in parsed.records if r.role=='user']==['Own question']
    assert 'PRIVATE ENCRYPTED' not in repr(parsed)


@pytest.mark.parametrize('foreign_kind',['user-event','assistant-event','agent-response','compaction-response'])
def test_foreign_parent_section_messages_and_compaction_are_excluded(foreign_kind):
    if foreign_kind == 'user-event':
        foreign = _current_user()
    elif foreign_kind == 'assistant-event':
        foreign = _assistant_event('Foreign parent answer')
    elif foreign_kind == 'agent-response':
        foreign = next(row for row in CURRENT_CODEX_ROWS if row.get('type') == 'response_item' and row.get('payload',{}).get('type') == 'agent_message')
    else:
        foreign = {'type':'response_item','payload':{'type':'compaction','id':'parent-compact',
            'encrypted_content':'private encrypted parent',
            'internal_chat_message_metadata_passthrough':{'turn_id':'parent-turn'}}}
    parent = {'type':'session_meta','payload':{'id':'parent'}}
    child = _header(forked_from_id='parent').data
    own = _message('own-final','Owned final',thread_id='child')
    parsed = parse_rows(CTX,_rows(parent,foreign,child,own),_header(forked_from_id='parent'),{})
    assert parsed.status == 'ok'
    assert [(record.kind,record.text) for record in parsed.records] == [('message','Owned final')]


def test_ambiguous_task_start_keeps_exact_blocked_offset():
    row = {'type':'event_msg','payload':{'type':'task_started','turn_id':'unknown-turn'}}
    parsed = parse_rows(CTX,_rows(row),_header(forked_from_id='parent'),{})
    assert parsed.status == 'partial' and parsed.blocked_offset == 10
    assert not parsed.records


def test_unattributed_analysis_assistant_cannot_be_visible():
    value = {'type':'response_item','payload':{'type':'message','id':'analysis-native','phase':'analysis',
        'role':'assistant','content':[{'type':'output_text','text':'private analysis'}],
        'internal_chat_message_metadata_passthrough':{'content_item_kinds':['unknown'],'turn_id':'unknown-turn'}}}
    parsed = parse_rows(CTX,_rows(value),_header(forked_from_id='parent'),{})
    assert parsed.status == 'partial' and parsed.blocked_offset == 10
    assert not parsed.records and not parsed.deferred_rows




def test_child_boundary_exact_keyset_is_independently_required():
    from transcripts.codex import _child_boundary_row
    row=RawRecord(0,10,{'type':'event_msg','payload':{'type':'thread_settings_applied',
        'thread_id':'child','thread_settings':{},'future_content':'private'}})
    assert _child_boundary_row(row,'child') is False


@pytest.mark.parametrize('boundary',[True,-1,1.5,'4'])
def test_replay_header_boundary_type_is_independently_required(boundary):
    from transcripts.codex import _replay_history_boundary
    with pytest.raises(ValueError,match='Unverified native subagent replay header'):
        _replay_history_boundary(_replay_header(boundary).data['payload'],'child')


def test_wrong_boundary_names_exact_retry_offset():
    values=list(_replay_rows())
    values[3].data['payload']['thread_id']='foreign'
    parsed=parse_rows(CTX,tuple(values),_replay_header(),{'_source_generation':'a'*64})
    assert parsed.blocked_offset==values[3].offset
    assert parsed.warnings==('Codex inherited history boundary is unverified',)


@pytest.mark.parametrize('ordinal',[True,-1])
def test_malformed_replay_ordinal_names_exact_retry_offset(ordinal):
    values=list(_replay_rows())
    values[4].data['ordinal']=ordinal
    parsed=parse_rows(CTX,tuple(values),_replay_header(),{'_source_generation':'a'*64})
    assert parsed.blocked_offset==values[4].offset
    assert 'Unknown or ambiguous Codex record retained' in parsed.warnings
    assert not any(record.role in {'user','assistant'} for record in parsed.records)


def test_inherited_task_start_cannot_register_selected_turn_owner():
    values=list(_replay_rows())
    values[0].data['payload']['id']='child'
    parsed=parse_rows(CTX,tuple(values),_replay_header(),{'_source_generation':'a'*64})
    assert 'parent-turn' not in parsed.parser_state['turn_owners']
    assert parsed.parser_state['turn_owners']['child-turn']=='child'
    assert [record.text for record in parsed.records if record.role in {'user','assistant'}]==[
        'Own child question','Own child answer']


def test_codex_opaque_row_cannot_reuse_preceding_turn():
    from transcripts import RawRecord
    from transcripts.codex import parse_rows
    ctx=CTX
    header=RawRecord(0,1,{'type':'session_meta','payload':{'id':ctx.native_session_id}})
    start=RawRecord(1,2,{'type':'event_msg','payload':{'type':'task_started','turn_id':'old-turn',
        'started_at':1,'collaboration_mode_kind':'default','model_context_window':None}})
    opaque=RawRecord(2,3,{},'oversized')
    after=RawRecord(3,4,{'type':'event_msg','payload':{'type':'user_message','message':'unproven after opaque',
        'local_audio':[],'local_images':[],'text_elements':[]}})
    parsed=parse_rows(ctx,(start,opaque,after),header,{})
    assert not any(r.text=='unproven after opaque' for r in parsed.records)
    assert any(row.offset==after.offset and row.reason=='ambiguous_ownership' for row in parsed.deferred_rows)
    assert parsed.status=='partial'

    fresh=RawRecord(4,5,{'type':'event_msg','payload':{'type':'task_started','turn_id':'new-turn',
        'started_at':2,'collaboration_mode_kind':'default','model_context_window':None}})
    verified=RawRecord(5,6,{'type':'event_msg','payload':{'type':'user_message','message':'verified new turn',
        'local_audio':[],'local_images':[],'text_elements':[]}})
    recovered=parse_rows(ctx,(fresh,verified),header,parsed.parser_state)
    assert [(r.text,r.turn_id) for r in recovered.records if r.role=='user']==[('verified new turn','new-turn')]
