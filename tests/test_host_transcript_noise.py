"""Native instruction envelopes and notifications stay out of dialogue."""
import json
import time
from dataclasses import replace

import pytest
from runtime_context import using_runtime_context
from transcripts import SourceCursor, read_records


@pytest.mark.parametrize('noise', ['is-meta', 'local-command', 'task-notification', 'skill-directory'])
def test_native_authored_instructions_are_not_user_dialogue(selected_host_context, noise):
    context = selected_host_context
    path = context.native_home / ('projects' if context.host == 'claude' else 'sessions') / 'noise.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    native = context.native_session_id
    texts = {'is-meta': 'Authored skill body: private instructions',
             'local-command': '<local-command-stdout>private terminal notice</local-command-stdout>',
             'task-notification': '<task-notification>private worker notice</task-notification>',
             'skill-directory': 'Base directory for this skill: /private/authored-skill'}
    if context.host == 'claude':
        def message(role, text, identity, **extra):
            return dict(type=role, uuid=identity, sessionId=native,
                        message={'role': role, 'content': text}, **extra)
        rows = [message('user', 'Real question', 'human'),
                message('user', texts[noise], 'noise', **({'isMeta': True} if noise == 'is-meta' else {})),
                message('assistant', 'Real answer', 'answer')]
    else:
        def message(role, text, identity, provenance=None):
            payload = dict(type='message', id=identity, role=role,
                content=[{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}])
            if provenance:
                payload['internal_chat_message_metadata_passthrough'] = {'content_item_kinds': [provenance]}
            return {'type': 'response_item', 'payload': payload}
        rows = [{'type': 'session_meta', 'payload': {'id': native}},
                message('user', 'Real question', 'human'),
                message('user', texts[noise], 'noise', 'skills.authored' if noise in {'is-meta','skill-directory'} else 'hooks.notification'),
                message('assistant', 'Real answer', 'answer')]
    path.write_bytes(b''.join(json.dumps(row).encode()+b'\n' for row in rows))
    context = replace(context, transcript_path=path)
    with using_runtime_context(context):
        batch = read_records(context, SourceCursor(), time.monotonic()+2)
    assert batch.status == 'ok' and batch.source_complete
    assert [record.text for record in batch.records] == ['Real question', 'Real answer']
    assert texts[noise] not in json.dumps(batch.parser_state)


def test_real_dialogue_with_notification_words_is_preserved(selected_host_context):
    context = selected_host_context
    path = context.native_home / ('projects' if context.host == 'claude' else 'sessions') / 'real.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    native = context.native_session_id
    human = 'Please explain Base directory for this skill and <task-notification>.'
    answer = 'Base directory for this skill: is a user-facing description here.'
    if context.host == 'claude':
        rows = [dict(type=role, uuid=role, sessionId=native, isMeta=False,
                     message={'role': role, 'content': text}) for role, text in [('user',human),('assistant',answer)]]
    else:
        rows = [{'type':'session_meta','payload':{'id':native}}]+[
            {'type':'response_item','payload':{'type':'message','id':role,'role':role,
             'content':[{'type':'input_text' if role=='user' else 'output_text','text':text}]}}
            for role,text in [('user',human),('assistant',answer)]]
    path.write_bytes(b''.join(json.dumps(row).encode()+b'\n' for row in rows))
    context = replace(context, transcript_path=path)
    with using_runtime_context(context):
        batch = read_records(context, SourceCursor(), time.monotonic()+2)
    assert batch.status == 'ok'
    assert [record.text for record in batch.records] == [human, answer]
