"""Visible Codex rollout records, with bounded mirror and ownership state."""
import hashlib
import re
import math
from . import DeferredRow, ParsedRecords, RawRecord, SourceRecord, read_source

MAX_SEEN_IDS = 4096
MAX_ASSOCIATIONS = 256
MAX_IDENTIFIER_LENGTH = 512


class UnknownSchema(ValueError):
    """A well-formed source row whose substantive schema is not supported."""


def safe_record_type_label(value):
    if (isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", value)
            and not value.startswith(('sk-', 'sk_', 'ghp_', 'github_pat_', 'xox', 'eyj'))):
        return value
    return 'unrecognized'


def _unknown_label(row, payload):
    if row.data.get('type') not in {'event_msg', 'response_item'}:
        return safe_record_type_label(row.data.get('type'))
    item = payload.get('item')
    if isinstance(item, dict):
        return safe_record_type_label(item.get('type'))
    if payload.get('type') == 'message' and isinstance(payload.get('content'), list):
        for block in payload['content']:
            if isinstance(block, dict) and block.get('type') not in {'input_text', 'output_text', 'text', 'input_image', 'image', 'input_audio', 'audio', 'output_audio'}:
                return safe_record_type_label(block.get('type'))
    return safe_record_type_label(payload.get('type'))


def _verified_unattributed_assistant(payload):
    base = {'type', 'role', 'content'}
    content = payload.get('content')
    if (payload.get('type') != 'message' or payload.get('role') != 'assistant'
            or not isinstance(content, list) or not content
            or not all(isinstance(block, dict) and set(block) == {'type', 'text'}
                       and block['type'] == 'output_text' and isinstance(block['text'], str) for block in content)):
        return False
    if set(payload) == base:
        return True
    metadata = payload.get('internal_chat_message_metadata_passthrough')
    return (set(payload) == base | {'id', 'phase', 'internal_chat_message_metadata_passthrough'}
            and _identifier(payload['id']) and payload['phase'] in {'final_answer', 'commentary'}
            and isinstance(metadata, dict)
            and set(metadata) in ({'content_item_kinds', 'turn_id'}, {'content_item_kinds', 'turn_id', 'create_time'})
            and metadata['content_item_kinds'] == ['unknown'] and _identifier(metadata['turn_id'])
            and ('create_time' not in metadata or (type(metadata['create_time']) in {int, float} and math.isfinite(metadata['create_time']))))

def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_IDENTIFIER_LENGTH


def read_records(context, cursor, deadline):
    return read_source(context, cursor, deadline, parse_rows)


def _turn(payload):
    metadata = payload.get('internal_chat_message_metadata_passthrough') or {}
    if not isinstance(metadata, dict):
        return None
    item = payload.get('item') or {}
    return payload.get('turn_id') or metadata.get('turn_id') or (item.get('turn_id') if isinstance(item, dict) else None)


def _text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError('Visible content must be a string or list')
    parts = []
    for item in content:
        if not isinstance(item, dict):
            raise ValueError('Visible content item must be an object')
        kind = item.get('type', '').lower()
        if kind in {'input_text', 'output_text', 'text'}:
            if not isinstance(item.get('text'), str):
                raise ValueError('Visible text must be a string')
            parts.append(item['text'])
        elif kind not in {'input_image', 'image', 'input_audio', 'audio', 'output_audio'}:
            raise UnknownSchema('Unknown visible content kind')
    return '\n'.join(parts)


def _agent_address(value):
    return isinstance(value, str) and re.fullmatch(r"/root(?:/[A-Za-z0-9_-]{1,64})?", value) is not None


def _completed_summary(item):
    """Validate native activity schemas and expose no tool payload contents."""
    kind = item.get('type')
    if kind == 'SubAgentActivity':
        if (set(item) != {'type', 'id', 'agent_path', 'agent_thread_id', 'kind'}
                or not all(_identifier(item[key]) for key in ('id', 'agent_path', 'agent_thread_id'))
                or item['kind'] not in {'interacted', 'completed', 'started', 'interrupted'}):
            raise ValueError('Unknown subagent activity schema')
        return 'Sub-agent activity: ' + item['kind'], 'subagent'
    if kind == 'FileChange':
        if (set(item) != {'type', 'id', 'status', 'stdout', 'stderr', 'changes'}
                or not _identifier(item['id']) or item['status'] != 'completed'
                or not all(isinstance(item[key], str) for key in ('stdout', 'stderr'))
                or not isinstance(item['changes'], dict)):
            raise ValueError('Unknown native file change schema')
        for path, change in item['changes'].items():
            if not isinstance(path, str) or not isinstance(change, dict):
                raise ValueError('Unknown native file change')
            if change.get('type') == 'update':
                valid = (set(change) == {'type', 'move_path', 'unified_diff'}
                         and change['move_path'] is None and isinstance(change['unified_diff'], str))
            elif change.get('type') == 'add':
                valid = set(change) == {'type', 'content'} and isinstance(change['content'], str)
            else:
                valid = False
            if not valid:
                raise ValueError('Unknown native change kind')
        return 'Native file changes: ' + str(len(item['changes'])), 'file_change'
    if kind == 'CollabAgentToolCall':
        if (set(item) != {'type', 'id', 'tool', 'status', 'sender_thread_id', 'receiver_agents', 'receiver_thread_ids', 'agents_states'}
                or not all(_identifier(item[key]) for key in ('id', 'tool', 'sender_thread_id'))
                or item['status'] != 'completed' or item['receiver_agents'] != []
                or item['receiver_thread_ids'] != [] or item['agents_states'] != {}):
            raise ValueError('Unknown native collaboration tool schema')
        return 'Native collaboration tool completed', item['tool']
    if kind == 'McpToolCall':
        fields = {'type', 'id', 'tool', 'server', 'pluginId', 'status', 'arguments', 'result', 'duration'}
        if (set(item) not in (fields, fields | {'readOnlyHint'})
                or not all(_identifier(item[key]) for key in ('id', 'tool', 'server', 'pluginId'))
                or item['status'] not in {'completed', 'failed'}
                or not isinstance(item['arguments'], dict) or not isinstance(item['result'], dict)
                or ('readOnlyHint' in item and type(item['readOnlyHint']) is not bool)
                or not isinstance(item['duration'], dict) or set(item['duration']) != {'secs', 'nanos'}
                or any(type(value) is not int or value < 0 for value in item['duration'].values())):
            raise ValueError('Unknown native MCP tool schema')
        return 'Native MCP tool ' + item['status'], item['tool']
    if kind == 'Extension':
        if (set(item) == {'type', 'id', 'kind', 'durationMs'}
                and _identifier(item['id']) and item['kind'] == 'clock.sleep'
                and type(item['durationMs']) is int and item['durationMs'] >= 0):
            return 'Native clock sleep completed', 'clock.sleep'
        if (set(item) != {'type', 'id', 'kind', 'query', 'action', 'results'}
                or not _identifier(item['id']) or item['kind'] != 'web.search'
                or not isinstance(item['query'], str) or not isinstance(item['action'], dict)
                or not isinstance(item['results'], list)):
            raise ValueError('Unknown native extension schema')
        action = item['action']
        tag = action.get('type')
        valid = False
        if tag == 'openPage':
            valid = set(action) == {'type', 'url'} and isinstance(action['url'], str)
        elif tag == 'findInPage':
            valid = (set(action) == {'type', 'url', 'pattern'} and isinstance(action['pattern'], str)
                     and (action['url'] is None or isinstance(action['url'], str)))
        elif tag == 'other':
            valid = set(action) == {'type'}
        elif tag == 'search' and set(action) == {'type', 'query', 'queries'}:
            valid = ((isinstance(action['query'], str) and action['queries'] is None)
                     or (action['query'] is None and isinstance(action['queries'], list)
                         and all(isinstance(query, str) for query in action['queries'])))
        if not valid:
            raise ValueError('Unknown native web action schema')
        base = {'type', 'ref_id', 'snippet'}
        result_shapes = (base | {'title'}, base | {'title', 'domain', 'url'},
                         base | {'title', 'domain', 'url', 'thumbnail_url'}, base | {'domain', 'url'})
        for result in item['results']:
            if (not isinstance(result, dict) or set(result) not in result_shapes
                    or result['type'] != 'text_result'
                    or not all(isinstance(value, str) for value in result.values())):
                raise ValueError('Unknown native web result schema')
        return 'Native web action completed', 'web.search'
    raise ValueError('Unknown completed activity')



def _assistant_item_text(item):
    base = {'type', 'id', 'content'}
    if set(item) == base:
        return _text(item['content'])
    questions = base | {'phase', 'delivery', 'questions'}
    if set(item) not in (base | {'phase'}, questions) or item['phase'] not in {'final_answer', 'commentary'}:
        raise ValueError('Unknown native assistant item schema')
    text = _text(item['content'])
    if set(item) == questions:
        if not isinstance(item['delivery'], str) or not isinstance(item['questions'], list):
            raise ValueError('Unknown native assistant questions')
        parts = [text] if text else []
        for question in item['questions']:
            if (not isinstance(question, dict) or set(question) != {'title', 'options'}
                    or not isinstance(question['title'], str) or not isinstance(question['options'], list)
                    or not all(isinstance(option, str) for option in question['options'])):
                raise ValueError('Unknown native assistant question schema')
            parts.append(question['title'])
            parts.extend('- ' + option for option in question['options'])
        text = '\n'.join(parts)
    return text



def _verified_unattributed_user(payload):
    fields = {'type', 'id', 'role', 'content', 'internal_chat_message_metadata_passthrough'}
    metadata = payload.get('internal_chat_message_metadata_passthrough')
    content = payload.get('content')
    return (set(payload) == fields and payload['type'] == 'message' and payload['role'] == 'user'
            and _identifier(payload['id']) and isinstance(metadata, dict)
            and set(metadata) == {'content_item_kinds', 'create_time', 'turn_id'}
            and metadata['content_item_kinds'] == ['user.text'] and _identifier(metadata['turn_id'])
            and type(metadata['create_time']) in {int, float} and math.isfinite(metadata['create_time'])
            and isinstance(content, list) and len(content) == 1
            and isinstance(content[0], dict) and set(content[0]) == {'type', 'text'}
            and content[0]['type'] == 'input_text' and isinstance(content[0]['text'], str))

def _canonical_task_start(payload):
    fields = {'type', 'turn_id', 'started_at', 'collaboration_mode_kind', 'model_context_window'}
    return (set(payload) in (fields, fields | {'root_turn_id'}) and payload.get('type') == 'task_started'
            and _identifier(payload['turn_id']) and type(payload['started_at']) is int
            and isinstance(payload['collaboration_mode_kind'], str)
            and (payload['model_context_window'] is None or type(payload['model_context_window']) is int)
            and ('root_turn_id' not in payload or _identifier(payload['root_turn_id'])))

def _agent_text(payload):
    fields = {'type', 'id', 'author', 'recipient', 'content', 'internal_chat_message_metadata_passthrough'}
    metadata = payload.get('internal_chat_message_metadata_passthrough')
    content = payload.get('content')
    if (set(payload) != fields or not _identifier(payload['id'])
            or not _agent_address(payload['author']) or not _agent_address(payload['recipient'])
            or payload['author'] == payload['recipient']
            or not isinstance(metadata, dict) or set(metadata) != {'create_time', 'turn_id'}
            or type(metadata['create_time']) not in {float, int} or not math.isfinite(metadata['create_time'])
            or not _identifier(metadata['turn_id'])
            or not isinstance(content, list) or len(content) not in {1, 2}):
        raise ValueError('Unknown inter-agent message schema')
    first = content[0]
    if not isinstance(first, dict) or set(first) != {'type', 'text'} or first['type'] != 'input_text' or not isinstance(first['text'], str):
        raise ValueError('Unknown inter-agent text')
    if len(content) == 2:
        encrypted = content[1]
        if (not isinstance(encrypted, dict) or set(encrypted) != {'type', 'encrypted_content'}
                or encrypted['type'] != 'encrypted_content' or not isinstance(encrypted['encrypted_content'], str)):
            raise ValueError('Unknown encrypted inter-agent block')
    return first['text']


def _replay_history_boundary(meta, native):
    """Validate the observed subagent replay header without exporting settings."""
    if 'subagent_history_start_ordinal' not in meta or 'forked_from_id' not in meta:
        return None
    parent = meta.get('forked_from_id')
    boundary = meta['subagent_history_start_ordinal']
    source = meta.get('source')
    subagent = source.get('subagent') if isinstance(source, dict) else None
    spawn = subagent.get('thread_spawn') if isinstance(subagent, dict) else None
    if (meta.get('id') != native or not _identifier(parent) or parent == native
            or meta.get('session_id') != parent or meta.get('parent_thread_id') != parent
            or not isinstance(spawn, dict) or spawn.get('parent_thread_id') != parent
            or type(boundary) is not int or boundary < 0):
        raise ValueError('Unverified native subagent replay header')
    return boundary


def _child_boundary_row(row, native):
    payload = row.data.get('payload')
    return (row.data.get('type') == 'event_msg' and isinstance(payload, dict)
            and set(payload) == {'type', 'thread_id', 'thread_settings'}
            and payload['type'] == 'thread_settings_applied' and payload['thread_id'] == native
            and isinstance(payload['thread_settings'], dict))


def parse_rows(context, rows, header, cursor_state, *, selected_metadata=None):
    """Normalize one source batch without persisting private rollout metadata."""
    meta = header.data.get('payload')
    if header.data.get('type') != 'session_meta' or not isinstance(meta, dict):
        return ParsedRecords(status='unsupported', warnings=('Missing Codex session metadata',),
                             blocked_offset=header.offset)
    native = meta.get('id') or meta.get('session_id')
    if native != context.native_session_id:
        selected = next((row.data.get('payload') for row in rows
                         if row.data.get('type') == 'session_meta'
                         and isinstance(row.data.get('payload'), dict)
                         and (row.data['payload'].get('id') or row.data['payload'].get('session_id')) == context.native_session_id), None)
        if selected is None and isinstance(selected_metadata, RawRecord):
            candidate = selected_metadata.data.get('payload')
            if (selected_metadata.data.get('type') == 'session_meta' and isinstance(candidate, dict)
                    and (candidate.get('id') or candidate.get('session_id')) == context.native_session_id):
                selected = candidate
        if selected is None:
            prior = cursor_state.get('selected_metadata') or {}
            if prior.get('native_session_id') == context.native_session_id:
                selected = {'id': prior['native_session_id'], 'cwd': prior.get('cwd'),
                            'forked_from_id': prior.get('fork_parent_id'),
                            'forked_from_ordinal_exclusive': cursor_state.get('fork_cutoff')}
        if selected is None:
            return ParsedRecords(status='partial', warnings=('Selected Codex session metadata is pending',),
                                 blocked_offset=header.offset, parser_state=cursor_state)
        meta = selected
        native = context.native_session_id
    parent = meta.get('forked_from_id')
    try:
        replay_boundary = _replay_history_boundary(meta, native)
    except ValueError:
        return ParsedRecords(status='partial', warnings=('Codex inherited history header is unverified',),
                             blocked_offset=header.offset, parser_state=cursor_state)
    generation = cursor_state.get('_source_generation')
    prior_boundary = cursor_state.get('replay_boundary')
    prior_valid = (isinstance(prior_boundary, dict)
        and set(prior_boundary) == {'ordinal', 'generation', 'verified', 'fresh_turn_started'}
        and type(prior_boundary['ordinal']) is int and prior_boundary['ordinal'] == replay_boundary
        and isinstance(generation, str) and re.fullmatch(r'[0-9a-f]{64}', generation) is not None
        and prior_boundary['generation'] == generation
        and type(prior_boundary['verified']) is bool and type(prior_boundary['fresh_turn_started']) is bool)
    boundary_verified = bool(prior_valid and prior_boundary['verified'])
    fresh_turn_started = bool(boundary_verified and prior_boundary['fresh_turn_started'])
    if replay_boundary is not None:
        boundary_row = next((row for row in rows if type(row.data.get('ordinal')) is int
                             and row.data['ordinal'] == replay_boundary), None)
        if boundary_row is not None:
            if not _child_boundary_row(boundary_row, native):
                return ParsedRecords(status='partial', warnings=('Codex inherited history boundary is unverified',),
                                     blocked_offset=boundary_row.offset, parser_state=cursor_state)
            boundary_verified = True
    physical_native = header.data['payload'].get('id') or header.data['payload'].get('session_id')
    if physical_native != native and (not _identifier(parent) or physical_native != parent):
        return ParsedRecords(status='partial', warnings=('Codex parent metadata relation is unverified',),
                             blocked_offset=header.offset, parser_state=cursor_state)
    metadata = {'native_session_id': native}
    if isinstance(meta.get('cwd'), str) and len(meta['cwd']) <= 4096:
        metadata['cwd'] = meta['cwd']
    if _identifier(parent):
        metadata['fork_parent_id'] = parent
    seen = [id for id in cursor_state.get('seen_ids', ()) if isinstance(id, str) and len(id) <= 600][-MAX_SEEN_IDS:]
    owners = dict(cursor_state.get('turn_owners', {}))
    tools = dict(cursor_state.get('tool_names', {}))
    pairs = list(cursor_state.get('mirror_pairs', ()))[:MAX_ASSOCIATIONS]
    last_end = cursor_state.get('last_message_end')
    hook_ids = set(cursor_state.get('hook_ids', ()))
    # A verified start in the selected native section proves its own turn.
    section = cursor_state.get('section_owner', header.data['payload'].get('id') or header.data['payload'].get('session_id'))
    for row in rows:
        payload = row.data.get('payload')
        if not isinstance(payload, dict):
            continue
        if row.data.get('type') == 'session_meta':
            section = payload.get('id') or payload.get('session_id')
        elif (replay_boundary is not None and boundary_verified
              and type(row.data.get('ordinal')) is int and row.data['ordinal'] == replay_boundary
              and _child_boundary_row(row, native)):
            section = native
        elif (row.data.get('type') == 'event_msg' and section == native and _canonical_task_start(payload)
              and (replay_boundary is None or (boundary_verified and type(row.data.get('ordinal')) is int
                   and row.data['ordinal'] > replay_boundary))):
            owners.setdefault(payload['turn_id'], native)
    # Explicit thread ownership overrides section proof, including parent turns.
    for row in rows:
        payload = row.data.get('payload') or {}
        if not isinstance(payload, dict):
            continue
        item = payload.get('item') or {}
        thread = payload.get('thread_id') or (item.get('thread_id') if isinstance(item, dict) else None)
        turn = _turn(payload)
        if _identifier(thread) and thread != '<redacted>' and _identifier(turn) and turn != '<redacted>':
            owners[turn] = thread
        item = payload.get('item') or {}
        if isinstance(item, dict) and item.get('type') == 'HookPrompt' and _identifier(item.get('id')):
            hook_ids.add(item['id'])
    records = []
    deferred = []
    warnings = []
    blocked = None
    active_turn = cursor_state.get('active_turn')
    section_owner = cursor_state.get('section_owner', header.data['payload'].get('id') or header.data['payload'].get('session_id'))
    requires_proof = bool(cursor_state.get('requires_session_proof'))
    def offset_identity(kind, offset):
        return 'offset:' + kind + ':' + str(offset)

    def state():
        return {'seen_ids': seen[-MAX_SEEN_IDS:], 'turn_owners': dict(list(owners.items())[-MAX_ASSOCIATIONS:]),
                'tool_names': dict(list(tools.items())[-MAX_ASSOCIATIONS:]),
                'mirror_pairs': pairs[-MAX_ASSOCIATIONS:],
                'last_message_end': last_end, 'hook_ids': sorted(hook_ids)[-MAX_ASSOCIATIONS:],
                'active_turn': active_turn, 'section_owner': section_owner, 'selected_metadata': metadata,
                'fork_cutoff': meta.get('forked_from_ordinal_exclusive'), 'requires_session_proof': requires_proof,
                **({'replay_boundary': {'ordinal': replay_boundary, 'generation': generation,
                     'verified': boundary_verified, 'fresh_turn_started': fresh_turn_started}}
                   if replay_boundary is not None else {})}

    def ownership(row, payload, turn, starting=False):
        if replay_boundary is not None:
            ordinal = row.data.get('ordinal')
            if type(ordinal) is not int or ordinal < 0:
                return None
            if ordinal < replay_boundary:
                return False
            if not boundary_verified or (not fresh_turn_started and not starting):
                return None
        item = payload.get('item') or {}
        thread = payload.get('thread_id') or (item.get('thread_id') if isinstance(item, dict) else None) or owners.get(turn)
        if thread and thread != '<redacted>':
            return thread == native
        if requires_proof:
            return None
        if not parent:
            return True
        ordinal = row.data.get('ordinal')
        cutoff = meta.get('forked_from_ordinal_exclusive')
        if isinstance(ordinal, int) and isinstance(cutoff, int):
            return ordinal >= cutoff
        return None

    def emit(row, payload, role, text, kind='message', name=None, turn=None, flavor=None, actor=None, recipient=None):
        nonlocal last_end
        source = payload.get('id') or payload.get('call_id')
        if source is None or source == '':
            # The shared reader qualifies this byte offset by source generation.
            identity = offset_identity(kind, row.offset)
        elif _identifier(source):
            identity = kind + ':' + source
        else:
            raise ValueError('Codex native ID exceeds the identifier limit')
        if turn is not None and not _identifier(turn):
            raise ValueError('Codex turn ID exceeds the identifier limit')
        already_seen = identity in seen
        # Native user response IDs and UserMessage event IDs can differ. Pair
        # occurrences across mirror flavors; same-flavor human repeats survive.
        if flavor and kind == 'message':
            digest = hashlib.sha256((role + '\0' + str(turn or '') + '\0' + text.strip()).encode()).hexdigest()
            match = next((i for i, pair in enumerate(pairs)
                          if pair['digest'] == digest and pair['flavor'] != flavor
                          and last_end == row.offset), None)
            if match is not None:
                pairs.pop(match)
                seen.append(identity)
                last_end = row.end_offset
                return
            if not already_seen:
                pairs.append({'digest': digest, 'flavor': flavor})
        if already_seen:
            last_end = row.end_offset
            return
        seen.append(identity)
        last_end = row.end_offset
        records.append(SourceRecord(identity, role, text, row.offset,
                                    row.data.get('timestamp'), kind, name,
                                    'unknown' if role == 'tool' else None, turn, actor, recipient))

    def control(row, kind, turn=None):
        identity = offset_identity(kind, row.offset)
        if identity not in seen:
            seen.append(identity)
            records.append(SourceRecord(identity, 'control', kind, row.offset,
                                        row.data.get('timestamp'), kind, turn_id=turn))

    for row in rows:
        if row.unparsed_reason == "oversized":
            deferred.append(DeferredRow(row.offset, row.end_offset, "oversized:unrecognized"))
            warnings.append("Oversized Codex transcript record retained")
            requires_proof = True
            active_turn = None
            continue
        top = row.data.get('type')
        payload = row.data.get('payload') or {}
        if not isinstance(payload, dict):
            warnings.append('Unknown Codex payload retained')
            blocked = row.offset
            break
        turn = _turn(payload) or active_turn
        try:
            if replay_boundary is not None:
                ordinal = row.data.get('ordinal')
                if type(ordinal) is not int or ordinal < 0:
                    raise ValueError('Malformed Codex replay ordinal')
                if ordinal < replay_boundary and top != 'session_meta':
                    continue
                if ordinal == replay_boundary and _child_boundary_row(row, native):
                    section_owner = native
                    active_turn = None
                    continue
            if top == 'session_meta':
                section_owner = payload.get('id') or payload.get('session_id')
                if section_owner != native and (not _identifier(parent) or section_owner != parent):
                    raise ValueError('Unverified Codex session metadata relation')
                if section_owner == native:
                    requires_proof = False
                if (payload.get('id') or payload.get('session_id')) == native:
                    active_turn = None
                continue
            if top == 'inter_agent_communication_metadata':
                if set(payload) != {'trigger_turn'} or type(payload['trigger_turn']) is not bool:
                    raise ValueError('Unknown inter-agent header schema')
                continue
            if top in {'world_state', 'turn_context', 'token_usage_record'}:
                continue
            if top == 'compacted':
                hint = payload.get('latest_token_usage_record')
                proof = hint.get('thread_id') if isinstance(hint, dict) else None
                owned = ownership(row, payload, turn)
                if owned is False or (_identifier(proof) and proof != native):
                    continue
                if owned is None and proof != native:
                    raise ValueError('Ambiguous inherited compaction')
                control(row, 'compaction', turn)
                continue
            if top == 'response_item':
                kind = payload.get('type')
                if kind == 'reasoning':
                    continue
                if kind == 'compaction':
                    if (set(payload) != {'type', 'id', 'encrypted_content', 'internal_chat_message_metadata_passthrough'}
                            or not _identifier(payload['id']) or not isinstance(payload['encrypted_content'], str)
                            or not isinstance(payload['internal_chat_message_metadata_passthrough'], dict)
                            or set(payload['internal_chat_message_metadata_passthrough']) != {'turn_id'}
                            or not _identifier(payload['internal_chat_message_metadata_passthrough']['turn_id'])):
                        raise ValueError('Unknown encrypted compaction schema')
                    owned = ownership(row, payload, turn)
                    if owned is False or section_owner != native:
                        continue
                    control(row, 'compaction', turn)
                    continue
                if kind == 'agent_message':
                    text = _agent_text(payload)
                    owned = ownership(row, payload, turn)
                    if owned is False or section_owner != native:
                        continue
                    emit(row, payload, 'agent', text, 'inter_agent_message', turn=turn, actor=payload['author'], recipient=payload['recipient'])
                    continue
                if kind == 'message':
                    role = payload.get('role')
                    if role in {'system', 'developer'} or payload.get('id') in hook_ids:
                        continue
                    if role not in {'user', 'assistant'}:
                        raise ValueError('Unknown message role')
                    kinds = (payload.get('internal_chat_message_metadata_passthrough') or {}).get('content_item_kinds')
                    if role == 'user' and kinds and not any(k in {'user.text', 'user.image', 'user.audio'} for k in kinds):
                        private_prefixes = ('agents_md.', 'environments.', 'skills.', 'hooks.')
                        private_kinds = {'additional_content.codex_apps_open_page', 'plugins.recommendations'}
                        if all(isinstance(k, str) and (k.startswith(private_prefixes) or k in private_kinds or k == '<redacted>') for k in kinds):
                            continue
                        raise UnknownSchema('Unknown user content provenance')
                    inherited = row.data.get('metadata')
                    native_metadata = payload.get('internal_chat_message_metadata_passthrough')
                    if (role == 'user' and isinstance(inherited, dict)
                            and set(inherited) == {'inherited_user_message'}
                            and inherited['inherited_user_message'] is True and owners.get(turn) != native
                            and set(payload) == {'type', 'id', 'role', 'content', 'internal_chat_message_metadata_passthrough'}
                            and isinstance(native_metadata, dict) and set(native_metadata) == {'turn_id'}):
                        continue
                    text = _text(payload.get('content'))
                    owned = ownership(row, payload, turn)
                    digest = hashlib.sha256((role + '\0' + str(turn or '') + '\0' + text.strip()).encode()).hexdigest()
                    live_mirror = (role == 'user' and last_end == row.offset and any(pair['digest'] == digest and pair['flavor'] == 'event' for pair in pairs))
                    if owned is False:
                        continue
                    if owned is None and not live_mirror:
                        if ((role == 'user' and _verified_unattributed_user(payload))
                                or (role == 'assistant' and _verified_unattributed_assistant(payload))):
                            deferred.append(DeferredRow(row.offset, row.end_offset, 'ambiguous_ownership'))
                            warning = 'Ambiguous Codex ' + role + ' ownership retained'
                            if warning not in warnings:
                                warnings.append(warning)
                            continue
                        raise ValueError('Ambiguous inherited Codex message')
                    if text:
                        emit(row, payload, role, text, turn=turn, flavor='response')
                elif kind in {'custom_tool_call', 'function_call'}:
                    owned = ownership(row, payload, turn)
                    if owned is False:
                        continue
                    if owned is None:
                        raise ValueError('Ambiguous inherited tool call')
                    name = payload.get('name')
                    if not _identifier(name):
                        raise ValueError('Tool call has no name')
                    tools[payload.get('call_id') or payload.get('id')] = name
                    text = payload.get('input', payload.get('arguments', ''))
                    if not isinstance(text, str):
                        raise ValueError('Unknown tool input')
                    emit(row, payload, 'tool', text, 'tool_call', name, turn)
                elif kind in {'custom_tool_call_output', 'function_call_output'}:
                    owned = ownership(row, payload, turn)
                    if owned is False:
                        continue
                    if owned is None:
                        raise ValueError('Ambiguous inherited tool output')
                    text = _text(payload.get('output', ''))
                    emit(row, payload, 'tool', text, 'tool_output', tools.get(payload.get('call_id')), turn)
                else:
                    raise UnknownSchema('Unknown Codex response item')
            elif top == 'event_msg':
                kind = payload.get('type')
                if kind == 'agent_message':
                    if (set(payload) != {'type', 'message', 'phase', 'memory_citation'}
                            or not isinstance(payload['message'], str)
                            or payload['phase'] is not None or payload['memory_citation'] is not None):
                        raise ValueError('Unknown assistant event schema')
                    if section_owner != native and owners.get(turn) != native:
                        continue
                    if (requires_proof or replay_boundary is not None) and ownership(row, payload, turn) is not True:
                        deferred.append(DeferredRow(row.offset, row.end_offset, 'ambiguous_ownership'))
                        warnings.append('Ambiguous Codex assistant ownership retained')
                        continue
                    emit(row, payload, 'assistant', payload['message'], turn=turn, flavor='event')
                elif kind == 'user_message':
                    if (set(payload) != {'type', 'message', 'local_audio', 'local_images', 'text_elements'}
                            or not isinstance(payload['message'], str)
                            or any(payload[field] != [] for field in ('local_audio', 'local_images', 'text_elements'))):
                        raise ValueError('Unknown current user event schema')
                    if section_owner != native and owners.get(turn) != native:
                        continue
                    if (requires_proof or replay_boundary is not None) and ownership(row, payload, turn) is not True:
                        deferred.append(DeferredRow(row.offset, row.end_offset, 'ambiguous_ownership'))
                        warnings.append('Ambiguous Codex user ownership retained')
                        continue
                    emit(row, payload, 'user', payload['message'], turn=turn, flavor='event')
                elif kind == 'task_started':
                    active_turn = payload.get('turn_id')
                    owned = ownership(row, payload, active_turn, starting=_canonical_task_start(payload))
                    if replay_boundary is not None and boundary_verified and owned is True and _canonical_task_start(payload):
                        fresh_turn_started = True
                    if section_owner == native and _canonical_task_start(payload) and owned is True:
                        requires_proof = False
                    if owned is None:
                        raise ValueError('Ambiguous Codex turn start')
                    if owned is True:
                        control(row, 'turn_start', active_turn)
                elif kind in {'task_complete', 'turn_aborted'}:
                    owned = ownership(row, payload, turn)
                    if owned is None:
                        raise ValueError('Ambiguous Codex turn boundary')
                    if owned is True:
                        control(row, 'turn_end' if kind == 'task_complete' else 'interruption', turn)
                elif kind in {'token_count', 'thread_settings_applied', 'agent_reasoning', 'agent_reasoning_raw_content'}:
                    continue
                elif kind == 'item_completed':
                    item = payload.get('item')
                    if not isinstance(item, dict):
                        raise ValueError('Unknown completed item')
                    item_kind = item.get('type')
                    if item_kind in {'HookPrompt', 'Reasoning'}:
                        continue
                    owned = ownership(row, payload, turn)
                    if owned is False:
                        continue
                    if owned is None:
                        raise ValueError('Ambiguous inherited completed item')
                    if item_kind in {'UserMessage', 'AgentMessage'}:
                        emit(row, item, 'user' if item_kind == 'UserMessage' else 'assistant',
                             (_assistant_item_text(item) if item_kind == 'AgentMessage' else _text(item.get('content'))), turn=turn, flavor='event')
                    elif item_kind in {'SubAgentActivity', 'FileChange', 'CollabAgentToolCall', 'McpToolCall', 'Extension'}:
                        if (set(payload) != {'type', 'thread_id', 'turn_id', 'started_at_ms', 'completed_at_ms', 'item'}
                                or any(type(payload[key]) is not int for key in ('started_at_ms', 'completed_at_ms'))):
                            raise ValueError('Unknown completed activity envelope')
                        text, name = _completed_summary(item)
                        emit(row, item, 'tool', text, 'tool_result', name, turn)
                    elif item_kind == 'ContextCompaction':
                        # The compacted row is the source boundary. A completed
                        # notification carries no additional visible content.
                        continue
                    elif item_kind == 'CommandExecution':
                        command = item.get('command', [])
                        if not isinstance(command, list) or not all(isinstance(p, str) for p in command):
                            raise ValueError('Unknown command execution')
                        output = item.get('aggregated_output', item.get('stdout', ''))
                        if not isinstance(output, str):
                            raise ValueError('Unknown command output')
                        emit(row, item, 'tool', ' '.join(command) + '\n' + output,
                             'tool_output', 'exec', turn)
                    elif item_kind == 'FunctionCallOutput':
                        emit(row, item, 'tool', _text(item.get('output', '')), 'tool_output', item.get('name'), turn)
                    else:
                        raise UnknownSchema('Unknown completed Codex item')
                else:
                    raise UnknownSchema('Unknown Codex event')
            else:
                raise UnknownSchema('Unknown Codex row')
        except UnknownSchema:
            label = _unknown_label(row, payload)
            warnings.append('Unknown substantive Codex transcript record (type=' + label + ')')
            deferred.append(DeferredRow(row.offset, row.end_offset, 'unknown_schema:' + label))
            requires_proof = True
            active_turn = None
            continue
        except (ValueError, TypeError, AttributeError):
            blocked = row.offset
            warnings.append('Unknown or ambiguous Codex record retained')
            break
    replay_pending = replay_boundary is not None and (not boundary_verified or not fresh_turn_started)
    if replay_pending:
        warnings.append('Codex inherited history boundary is pending')
    return ParsedRecords(tuple(records), metadata, 'partial' if blocked is not None or deferred or replay_pending else 'ok',
                         tuple(warnings), blocked, state(), deferred_rows=tuple(deferred))
