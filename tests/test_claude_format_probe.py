"""The manual format probe reads only disposable synthetic sources."""
import importlib.util
import json
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('claude_format_probe', Path(__file__).parents[1] / 'scripts/probe-claude-transcript-format.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def write(root, rows):
    root.mkdir(exist_ok=True)
    (root / 'synthetic.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))


def test_verified_metadata_and_visible_messages(tmp_path, capsys):
    rows = [{'type': kind, field: value, 'sessionId': 'private-id'} for kind, field, value in
            [('mode', 'mode', 'normal'), ('ai-title', 'aiTitle', 'Private title'), ('atis-latch', 'atis', '')]]
    rows += [{'type': 'user', 'sessionId': 'private-id', 'message': {'content': 'Private message'}},
             {'type': 'assistant', 'sessionId': 'private-id', 'message': {'content': [
                 {'type': 'text', 'text': 'Private answer'}, {'type': 'thinking', 'thinking': 'Private thought'}]}}]
    write(tmp_path, rows)
    assert probe.main(['--source-root', str(tmp_path)]) == 0
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data['known_records'] == {'mode': 1, 'ai-title': 1, 'atis-latch': 1, 'user': 1, 'assistant': 1}
    assert data['known_blocks'] == {'text': 1, 'thinking': 1}
    assert data['scan_complete']
    for private in ('private-id', 'Private', str(tmp_path)):
        assert private not in output


def test_unknown_scalar_and_content_are_not_metadata(tmp_path):
    write(tmp_path, [{'type': 'user-note', 'text': 'hidden private value'},
                     {'type': 'assistant', 'message': {'content': [{'type': 'new_block', 'text': 'private'}]}}])
    data = probe.scan(tmp_path)
    assert data['unknown_records'] == {'user-note': 1}
    assert data['unknown_blocks'] == {'new_block': 1}
    assert data['unsupported_schema'] == 2
    assert not data['scan_complete']


@pytest.mark.parametrize('row', [[], {'type': 'mode', 'mode': 'normal', 'sessionId': 's', 'content': 'private'},
                                    {'type': 'mode', 'mode': 3, 'sessionId': 's'},
                                    {'type': 'user', 'message': {'content': 3}},
                                    {'type': 'user', 'sessionId': []}])
def test_invalid_shapes_fail(tmp_path, row):
    write(tmp_path, [row])
    data = probe.scan(tmp_path)
    assert data['unsupported_schema']
    assert not data['scan_complete']


def test_invalid_json_and_incomplete_line(tmp_path):
    (tmp_path / 'synthetic.jsonl').write_bytes(b'{bad}\n{"type":"user"}')
    data = probe.scan(tmp_path)
    assert data['malformed'] == 2
    assert not data['scan_complete']


def test_symlink_file_directory_and_root_are_not_followed(tmp_path):
    private = tmp_path / 'private'
    write(private, [{'type': 'secret-type'}])
    selected = tmp_path / 'selected'
    selected.mkdir()
    (selected / 'linked.jsonl').symlink_to(private / 'synthetic.jsonl')
    (selected / 'linked-dir').symlink_to(private, target_is_directory=True)
    data = probe.scan(selected)
    assert data['scanned'] == 0 and data['skipped'] == 2
    assert not data['unknown_records'] and not data['scan_complete']
    linked = tmp_path / 'linked-root'
    linked.symlink_to(private, target_is_directory=True)
    assert probe.scan(linked)['read_failures'] == 1
    assert probe.scan(linked / 'child')['read_failures'] == 1


@pytest.mark.parametrize('bounds', [{'max_files': 1}, {'max_entries': 1}, {'max_bytes': 5}, {'max_line_bytes': 5}])
def test_scan_budgets_fail_without_claiming_clean(tmp_path, bounds):
    write(tmp_path, [{'type': 'user', 'message': {'content': 'synthetic'}}])
    (tmp_path / 'second.jsonl').write_text('{"type":"summary"}\n')
    data = probe.scan(tmp_path, **bounds)
    assert data['truncated'] and not data['scan_complete']


def test_deadline_and_safe_type_labels(tmp_path, monkeypatch):
    write(tmp_path, [{'type': 'sk-private-secret'}, {'type': 'not safe /private/path'}])
    data = probe.scan(tmp_path)
    assert data['unknown_records'] == {'unrecognized': 2}
    ticks = iter([0, 2])
    monkeypatch.setattr(probe.time, 'monotonic', lambda: next(ticks))
    assert probe.scan(tmp_path, seconds=1)['truncated'] == 1


def test_explicit_root_and_positive_bounds_required(tmp_path):
    with pytest.raises(SystemExit):
        probe.main([])
    with pytest.raises(SystemExit):
        probe.main(['--source-root', str(tmp_path), '--seconds', 'nan'])


def test_unreadable_sources_fail_without_path_disclosure(tmp_path, monkeypatch, capsys):
    write(tmp_path, [{'type': 'summary'}])
    original = probe.os.open
    def refused(path, *args, **kwargs):
        if path == 'synthetic.jsonl':
            raise PermissionError('private path must not be printed')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(probe.os, 'open', refused)
    assert probe.main(['--source-root', str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)['read_failures'] == 1
    assert 'private path' not in output and str(tmp_path) not in output


def test_diagnostic_type_cardinality_is_bounded(tmp_path):
    write(tmp_path, [{'type': 'new_type_' + str(i)} for i in range(160)])
    data = probe.scan(tmp_path)
    assert len(data['unknown_records']) <= 129
    assert sum(data['unknown_records'].values()) == 160
    assert not data['scan_complete']


def test_codex_probe_uses_native_header_and_reports_safe_nested_types(tmp_path, capsys):
    write(tmp_path, [
        {'type': 'session_meta', 'payload': {'id': 'private-native-id', 'cwd': '/private/worktree'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'id': 'u',
            'content': [{'type': 'input_text', 'text': 'Private question'}]}},
        {'type': 'event_msg', 'payload': {'type': 'future_event', 'content': 'Private unknown'}},
        {'type': 'event_msg', 'payload': {'type': 'sk-secret', 'content': 'Private secret'}},
    ])
    assert probe.main(['--host', 'codex', '--source-root', str(tmp_path)]) == 1
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data['known_records'] == {'session_meta': 1, 'response_item/message': 1}
    assert data['unknown_records'] == {'event_msg/future_event': 1, 'event_msg/unrecognized': 1}
    assert data['unsupported_schema'] == 2
    for private in ('private-native-id', 'Private', '/private/worktree', 'sk-secret', str(tmp_path)):
        assert private not in output


def test_codex_probe_missing_header_is_not_guessed(tmp_path):
    write(tmp_path, [{'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
        'content': [{'type': 'input_text', 'text': 'private'}]}}])
    data = probe.scan(tmp_path, host='codex')
    assert not data['scan_complete'] and data['unsupported_schema'] == 1
    assert data['unknown_records'] == {'response_item/message': 1}


def test_codex_probe_does_not_drop_malformed_verified_items(tmp_path):
    write(tmp_path, [{'type': 'session_meta', 'payload': {'id': 's'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': 42}},
        {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'type': 'FileChange', 'content': 'private'}}}])
    data = probe.scan(tmp_path, host='codex')
    assert not data['scan_complete'] and data['unsupported_schema'] == 2
    assert data['unknown_records'] == {'response_item/message': 1, 'event_msg/item_completed/filechange': 1}



def test_codex_probe_keeps_unknown_control_ownership_requirement(tmp_path):
    write(tmp_path,[{'type':'session_meta','payload':{'id':'synthetic-native-id'}},
        {'type':'future_control','payload':{'private':'NOT FOR OUTPUT'}},
        {'type':'event_msg','payload':{'type':'agent_message','message':'Unproven answer',
                                     'phase':None,'memory_citation':None}}])
    data=probe.scan(tmp_path,host='codex')
    assert data['unsupported_schema']==2
    assert data['unknown_records']=={'future_control':1,'event_msg/agent_message':1}
    assert not data['scan_complete']
    assert 'NOT FOR OUTPUT' not in json.dumps(data)
    assert 'Unproven answer' not in json.dumps(data)
