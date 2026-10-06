"""Visible Codex rollout records, with bounded mirror and ownership state."""
import hashlib
from . import ParsedRecords, SourceRecord, read_source

MAX_SEEN_IDS = 4096
MAX_ASSOCIATIONS = 256
MAX_IDENTIFIER_LENGTH = 512


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_IDENTIFIER_LENGTH


def read_records(context, cursor, deadline):
    return read_source(context, cursor, deadline, parse_rows)


def _turn(payload):
    metadata = payload.get('internal_chat_message_metadata_passthrough') or {}
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
            raise ValueError('Unknown visible content kind')
    return '\n'.join(parts)


def parse_rows(context, rows, header, cursor_state):
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
    # Learn explicit ownership before emitting earlier response-item mirrors.
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
    owners = dict(list(owners.items())[-MAX_ASSOCIATIONS:])
    records = []
    warnings = []
    blocked = None
    active_turn = cursor_state.get('active_turn')
    def offset_identity(kind, offset):
        return 'offset:' + kind + ':' + str(offset)

    def state():
        return {'seen_ids': seen[-MAX_SEEN_IDS:], 'turn_owners': owners,
                'tool_names': dict(list(tools.items())[-MAX_ASSOCIATIONS:]),
                'mirror_pairs': pairs[-MAX_ASSOCIATIONS:],
                'last_message_end': last_end, 'hook_ids': sorted(hook_ids)[-MAX_ASSOCIATIONS:],
                'active_turn': active_turn, 'selected_metadata': metadata,
                'fork_cutoff': meta.get('forked_from_ordinal_exclusive')}

    def ownership(row, payload, turn):
        item = payload.get('item') or {}
        thread = payload.get('thread_id') or (item.get('thread_id') if isinstance(item, dict) else None) or owners.get(turn)
        if thread and thread != '<redacted>':
            return thread == native
        if not parent:
            return True
        ordinal = row.data.get('ordinal')
        cutoff = meta.get('forked_from_ordinal_exclusive')
        if isinstance(ordinal, int) and isinstance(cutoff, int):
            return ordinal >= cutoff
        return None

    def emit(row, payload, role, text, kind='message', name=None, turn=None, flavor=None):
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
            digest = hashlib.sha256((role + '\0' + str(turn or '') + '\0' + text).encode()).hexdigest()
            match = next((i for i, pair in enumerate(pairs)
                          if pair['digest'] == digest and pair['flavor'] != flavor
                          and (turn or last_end == row.offset)), None)
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
                                    'unknown' if role == 'tool' else None, turn))

    def control(row, kind, turn=None):
        identity = offset_identity(kind, row.offset)
        if identity not in seen:
            seen.append(identity)
            records.append(SourceRecord(identity, 'control', kind, row.offset,
                                        row.data.get('timestamp'), kind, turn_id=turn))

    for row in rows:
        top = row.data.get('type')
        payload = row.data.get('payload') or {}
        if not isinstance(payload, dict):
            warnings.append('Unknown Codex payload retained')
            blocked = row.offset
            break
        turn = _turn(payload) or active_turn
        try:
            if top == 'session_meta':
                if (payload.get('id') or payload.get('session_id')) == native:
                    active_turn = None
                continue
            if top in {'world_state', 'turn_context', 'token_usage_record'}:
                continue
            if top == 'compacted':
                owned = ownership(row, payload, turn)
                if owned is False:
                    continue
                if owned is None:
                    raise ValueError('Ambiguous inherited compaction')
                control(row, 'compaction', turn)
                continue
            if top == 'response_item':
                kind = payload.get('type')
                if kind == 'reasoning':
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
                        if all(isinstance(k, str) and (k.startswith(private_prefixes) or k == '<redacted>') for k in kinds):
                            continue
                        raise ValueError('Unknown user content provenance')
                    owned = ownership(row, payload, turn)
                    if owned is False:
                        continue
                    if owned is None:
                        raise ValueError('Ambiguous inherited Codex message')
                    text = _text(payload.get('content'))
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
                    raise ValueError('Unknown Codex response item')
            elif top == 'event_msg':
                kind = payload.get('type')
                if kind == 'task_started':
                    active_turn = payload.get('turn_id')
                    if ownership(row, payload, active_turn) is not False:
                        control(row, 'turn_start', active_turn)
                elif kind in {'task_complete', 'turn_aborted'}:
                    if ownership(row, payload, turn) is not False:
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
                             _text(item.get('content')), turn=turn, flavor='event')
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
                        raise ValueError('Unknown completed Codex item')
                else:
                    raise ValueError('Unknown Codex event')
            else:
                raise ValueError('Unknown Codex row')
        except (ValueError, TypeError, AttributeError):
            blocked = row.offset
            warnings.append('Unknown or ambiguous Codex record retained')
            break
    return ParsedRecords(tuple(records), metadata, 'partial' if blocked is not None else 'ok',
                         tuple(warnings), blocked, state())
