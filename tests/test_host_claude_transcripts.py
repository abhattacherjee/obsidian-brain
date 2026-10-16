"""Claude native transcript layouts and privacy boundaries."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.host_only("claude", reason="claude-record-format", capability="claude_native_format")

from transcripts import SourceCursor


@pytest.fixture
def native_case(tmp_path, monkeypatch):
    home = tmp_path / "claude home"
    path = home / "projects" / "repo" / "session.jsonl"
    path.parent.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return SimpleNamespace(host="claude", native_session_id="opaque-native-id", transcript_path=path), path


def row(kind, uuid, content, **extra):
    return dict(type=kind, uuid=uuid, sessionId="opaque-native-id", timestamp="2026-10-05T12:00:00Z",
                message={"role": kind, "content": content}, **extra)


def read(case, rows, cursor=SourceCursor()):
    from transcripts.claude import read_records
    context, path = case
    path.write_text("".join(json.dumps(value) + "\n" for value in rows))
    return read_records(context, cursor, time.monotonic() + 2)


def test_visible_messages_tools_and_unknown_native_names(native_case):
    batch = read(native_case, [
        row("user", "u", "Question"),
        row("assistant", "a", [{"type": "thinking", "thinking": "hidden"},
                               {"type": "text", "text": "Visible answer"},
                               {"type": "tool_use", "id": "call-1", "name": "Edit", "input": {"file_path": "a.py", "old_string": "old", "new_string": "new"}},
                               {"type": "tool_use", "id": "call-2", "name": "FutureNativeTool", "input": {"untrusted": "private-tool-input"}}]),
        row("user", "r", [{"type": "tool_result", "tool_use_id": "call-1", "content": "Updated a.py"}]),
    ])
    assert batch.status == "ok" and batch.source_complete
    assert [record.text for record in batch.records if record.kind == "message"] == ["Question", "Visible answer"]
    tools = [record for record in batch.records if record.kind == "tool_call"]
    assert [(record.tool_name, record.tool_category) for record in tools] == [("Edit", "edit"), ("FutureNativeTool", "unknown")]
    result = next(record for record in batch.records if record.kind == "tool_result")
    assert result.tool_name == "Edit" and result.tool_category == "edit"
    assert len({record.source_id for record in batch.records}) == len(batch.records)
    assert all("hidden" not in record.text for record in batch.records)
    assert "private-tool-input" not in json.dumps(batch.parser_state)


def test_developer_system_environment_and_hidden_payloads_not_visible(native_case):
    batch = read(native_case, [
        row("user", "environment", "<environment_context>private runtime dump</environment_context>"),
        row("developer", "dev", "private instructions"),
        row("system", "sys", "private instructions"),
        row("assistant", "a", [{"type": "redacted_thinking", "data": "encrypted"}]),
        row("user", "visible", "Real question"),
    ])
    assert batch.status == "ok"
    assert [record.text for record in batch.records] == ["Real question"]
    assert "private" not in json.dumps(batch.metadata)
    assert "encrypted" not in json.dumps(batch.parser_state)


def test_metadata_only_file_has_no_session_facts(native_case):
    batch = read(native_case, [{"type": "progress", "sessionId": "opaque-native-id", "data": {"private": "transport"}}])
    assert batch.status == "ok" and batch.source_complete and not batch.records
    assert batch.metadata["native_session_id"] == "opaque-native-id"


def test_identity_mismatch_never_consumes_visible_records(native_case):
    value = row("user", "u", "Different session")
    value["sessionId"] = "foreign-session"
    batch = read(native_case, [value])
    assert batch.status == "unsupported" and not batch.records
    assert batch.consumed_offset == 0


def test_unknown_substantive_row_and_block_are_not_successful_empty(native_case):
    batch = read(native_case, [row("user", "u", "visible"), {"type": "future_visible_record", "sessionId": "opaque-native-id", "text": "unknown"}])
    assert batch.status == "partial" and not batch.source_complete
    assert len(batch.records) == 1
    blocked = read(native_case, [row("user", "u", [{"type": "new_visible_block", "value": "unknown"}])])
    assert blocked.status == "partial" and not blocked.source_complete
    assert blocked.consumed_offset == native_case[1].stat().st_size
    assert blocked.parser_state["_deferred_source_rows"][0]["reason"] == "unknown_schema:new_visible_block"


def test_sidechain_has_explicit_parent_provenance(native_case):
    batch = read(native_case, [row("user", "u", "Child activity", isSidechain=True,
                                  parentSessionId="parent-session", parentUuid="parent-message")])
    assert batch.metadata["sidechain"] is True
    assert batch.metadata["parent_session_id"] == "parent-session"
    assert batch.metadata["parent_message_id"] == "parent-message"


def test_compaction_boundary_is_metadata_not_fake_message(native_case):
    batch = read(native_case, [row("user", "u", "question"),
                               {"type": "system", "subtype": "compact_boundary", "uuid": "compact-1",
                                "sessionId": "opaque-native-id", "compactMetadata": {"trigger": "manual"}}])
    boundary = next(record for record in batch.records if record.kind == "compaction")
    assert boundary.source_id == "compact-1" and boundary.text == ""


def test_flat_layout_and_stable_tool_ids(native_case):
    batch = read(native_case, [{"role": "user", "uuid": "flat", "sessionId": "opaque-native-id", "content": "Flat message"}])
    assert batch.records[0].source_id == "flat:0:message"
    assert batch.records[0].role == "user" and batch.records[0].text == "Flat message"


def test_checked_in_native_claude_fixture_preserves_visible_layouts(tmp_path):
    from transcripts.claude import read_records
    fixture = Path(__file__).parent / "fixtures" / "dropped-sessions" / "d2cc7e46-long-617min-full.jsonl"
    rows = [json.loads(line) for line in fixture.read_text().splitlines()]
    sid = next(value["sessionId"] for value in rows if value.get("sessionId"))
    context = SimpleNamespace(host="claude", native_session_id=sid, transcript_path=fixture)
    batch = read_records(context, SourceCursor(historical=True), time.monotonic() + 2)
    assert batch.status == "ok" and batch.source_complete
    assert {record.role for record in batch.records} >= {"user", "assistant"}
    assert any(record.kind == "tool_call" for record in batch.records)
    assert batch.metadata["native_session_id"] == sid


def test_offset_only_ids_change_with_generation_after_rotation(native_case):
    from transcripts.claude import read_records
    context, path = native_case
    first_row = {"type": "user", "sessionId": "opaque-native-id", "message": {"content": "first message"}}
    first = read(native_case, [first_row])
    cursor = SourceCursor(first.source_generation, first.consumed_offset, first.source_identity,
                          first.anchor_digest, first.parser_state, known_size=first.source_size)
    path.rename(path.with_suffix(".old"))
    new_row = {"type": "user", "sessionId": "opaque-native-id", "message": {"content": "second message"}}
    path.write_text(json.dumps(new_row) + "\n")
    second = read_records(context, cursor, time.monotonic() + 2)
    assert first.records[0].sequence == second.records[0].sequence == 0
    assert first.records[0].source_id != second.records[0].source_id
    assert first.records[0].source_id.startswith("offset:")
    assert first.source_generation in first.records[0].source_id
    assert second.source_generation in second.records[0].source_id


def test_uuid_and_call_ids_remain_stable_across_source_generations(native_case):
    from transcripts.claude import read_records
    context, path = native_case
    values = [row("user", "stable-user-uuid", "Question"),
              row("assistant", "stable-assistant-uuid", [{"type": "tool_use", "id": "stable-call-id", "name": "Read", "input": {"file_path": "a.py"}}])]
    first = read(native_case, values)
    cursor = SourceCursor(first.source_generation, first.consumed_offset, first.source_identity,
                          first.anchor_digest, first.parser_state, known_size=first.source_size)
    path.rename(path.with_suffix(".old"))
    path.write_text("".join(json.dumps(value) + "\n" for value in values))
    second = read_records(context, cursor, time.monotonic() + 2)
    assert second.source_generation != first.source_generation
    assert [record.source_id for record in second.records] == [record.source_id for record in first.records]


def test_verified_current_metadata_does_not_block_visible_messages(native_case):
    import hashlib
    fixture = Path(__file__).parent / "fixtures" / "hosts" / "claude-2.1.291-metadata.jsonl"
    provenance = json.loads(fixture.with_suffix(".provenance.json").read_text())
    assert provenance["fixture_sha256"] == hashlib.sha256(fixture.read_bytes()).hexdigest()
    assert provenance["native_capture_verified"] is False
    values = [json.loads(line) for line in fixture.read_text().splitlines()]
    assert len(values) == 5
    batch = read(native_case, values)
    assert batch.status == "ok" and batch.source_complete
    assert [record.text for record in batch.records] == ["Synthetic question", "Synthetic answer"]
    assert batch.consumed_offset == native_case[1].stat().st_size
    assert "Synthetic title" not in json.dumps(batch.parser_state)


@pytest.mark.parametrize("kind,field,value", [("mode", "mode", "normal"),
                         ("ai-title", "aiTitle", "Synthetic title"),
                         ("atis-latch", "atis", "")])
@pytest.mark.parametrize("mutation", ["extra-content", "missing-field", "wrong-value-type", "wrong-session-type", "unknown-kind"])
def test_metadata_shape_changes_defer_without_discarding_input(native_case, kind, field, value, mutation):
    data = {"type": kind, field: value, "sessionId": "opaque-native-id"}
    if mutation == "extra-content":
        data["content"] = "Must not disappear"
    elif mutation == "missing-field":
        del data[field]
    elif mutation == "wrong-value-type":
        data[field] = {}
    elif mutation == "wrong-session-type":
        data["sessionId"] = []
    else:
        data["type"] = "future-metadata"
    before = row("user", "before", "Question")
    batch = read(native_case, [before, data, row("assistant", "after", "Answer")])
    assert batch.status == "partial" and not batch.source_complete
    assert [record.text for record in batch.records] == ["Question", "Answer"]
    assert batch.consumed_offset == native_case[1].stat().st_size
    assert batch.parser_state["_deferred_source_rows"][0]["offset"] == len((json.dumps(before) + "\n").encode())


@pytest.mark.parametrize("kind,field,value", [("mode", "mode", "normal"),
                         ("ai-title", "aiTitle", "Synthetic title"),
                         ("atis-latch", "atis", "")])
def test_verified_metadata_cannot_hide_conflicting_session_identity(native_case, kind, field, value):
    batch = read(native_case, [row("user", "before", "Question"),
                              {"type": kind, field: value, "sessionId": "foreign-session"}])
    assert batch.status == "unsupported" and batch.consumed_offset == 0
    assert not batch.records and not batch.source_complete


@pytest.mark.parametrize("kind,label", [
    ("future-event", "future-event"),
    ("future_event2", "future_event2"),
    ("future\nprivate content", "unrecognized"),
    ("future private content", "unrecognized"),
    ("a" * 49, "unrecognized"),
    ("sk-secret-token", "unrecognized"),
    ("sk_secret_token", "unrecognized"),
    ("ghp_secret_token", "unrecognized"),
    ("github_pat_secret_token", "unrecognized"),
    ("xoxb-secret-token", "unrecognized"),
    ("eyj-secret-token", "unrecognized"),
])
def test_unknown_row_diagnostic_reveals_only_bounded_type_label(native_case, kind, label):
    before = row("user", "before", "Question")
    batch = read(native_case, [before, {"type": kind, "sessionId": "opaque-native-id",
                                      "content": "Private content must not be emitted"}])
    assert batch.status == "partial" and not batch.source_complete
    assert batch.consumed_offset == native_case[1].stat().st_size
    assert batch.parser_state["_deferred_source_rows"][0]["reason"] == "unknown_schema:" + label
    assert batch.warnings == ("Unknown substantive Claude transcript record (type=" + label + ")", "Deferred transcript rows remain pending")
    assert "Private content" not in json.dumps(batch.warnings)
    if label == "unrecognized":
        assert kind not in json.dumps(batch.warnings)


VERIFIED_METADATA = [json.loads(line) for line in (Path(__file__).parent / 'fixtures/hosts/claude-2.1.291-verified-metadata.jsonl').read_text().splitlines()]

@pytest.mark.parametrize('metadata', VERIFIED_METADATA, ids=lambda value: value['type'])
def test_current_metadata_census_preserves_visible_messages(native_case, metadata):
    before = row('user', 'before', 'Before metadata')
    after = row('assistant', 'after', 'After metadata')
    batch = read(native_case, [before, metadata, after])
    assert batch.status == 'ok' and batch.source_complete
    assert [record.text for record in batch.records] == ['Before metadata', 'After metadata']
    assert set(batch.metadata) <= {'host', 'native_session_id', 'relocated_cwd', 'continued_in_session_id'}
    assert batch.metadata['native_session_id'] == 'opaque-native-id'
    assert 'ownerAccountUuid' not in json.dumps(batch.parser_state)
    assert 'modelUsage' not in json.dumps(batch.parser_state)

@pytest.mark.parametrize('metadata', VERIFIED_METADATA, ids=lambda value: value['type'])
@pytest.mark.parametrize('damage', ['extra-content', 'missing-field', 'wrong-field-type'])
def test_current_metadata_census_rejects_schema_drift(native_case, metadata, damage):
    import copy
    value = copy.deepcopy(metadata)
    field = next(key for key in value if key not in {'type', 'sessionId'})
    if damage == 'extra-content': value['content'] = 'Future substantive content'
    elif damage == 'missing-field': value.pop(field)
    else: value[field] = ['Unverified shape']
    before = row('user', 'before', 'Before metadata')
    batch = read(native_case, [before, value, row('assistant', 'after', 'After metadata')])
    assert batch.status == 'partial' and not batch.source_complete
    assert [record.text for record in batch.records] == ['Before metadata', 'After metadata']
    assert batch.consumed_offset == native_case[1].stat().st_size
    ref = batch.parser_state['_deferred_source_rows'][0]
    assert ref['offset'] == len((json.dumps(before) + '\n').encode())
    assert ref['reason'] == 'unknown_schema:' + metadata['type']
    assert 'Future substantive content' not in json.dumps(batch.parser_state)


@pytest.mark.parametrize('variant', ['W2', 'W3', 'W4'])
def test_verified_worktree_variants_do_not_change_selected_binding(native_case, variant):
    context, _ = native_case
    core = dict(originalCwd='/synthetic/original', preEnterOriginalCwd='/synthetic/original',
        sessionId='opaque-native-id', worktreeName='synthetic-worktree', worktreePath='/synthetic/worktree')
    if variant in {'W2', 'W3'}: core['enteredExisting'] = True
    if variant in {'W2', 'W4'}: core['worktreeBranch'] = 'synthetic-branch'
    if variant == 'W4': core.update(originalBranch='develop', originalHeadCommit='0'*40)
    metadata = dict(type='worktree-state', sessionId='opaque-native-id', worktreeSession=core)
    batch = read(native_case, [row('user', 'before', 'Visible'), metadata])
    assert batch.status == 'ok' and batch.metadata['native_session_id'] == context.native_session_id
    assert 'worktree' not in batch.metadata
    core['future_content'] = 'Substantive field'
    batch = read(native_case, [row('user', 'before', 'Visible'), metadata])
    assert batch.status == 'partial' and not batch.source_complete


def test_nested_cost_drift_and_nonfinite_numbers_are_not_ignored(native_case):
    import copy
    original = next(value for value in VERIFIED_METADATA if value['type'] == 'cost-state')
    for mutate in ('extra', 'bool', 'nonfinite'):
        value = copy.deepcopy(original)
        usage = next(iter(value['modelUsage'].values()))
        if mutate == 'extra': usage['content'] = 'Substantive'
        elif mutate == 'bool': usage['inputTokens'] = True
        else: usage['costUSD'] = float('inf')
        batch = read(native_case, [row('user', 'before', 'Visible'), value])
        assert batch.status == 'partial' and not batch.source_complete



def test_current_claude_census_fixture_is_synthetic_and_byte_bound():
    import hashlib
    path = Path(__file__).parent / 'fixtures/hosts/claude-2.1.291-verified-metadata.jsonl'
    provenance = json.loads(path.with_suffix('.provenance.json').read_text())
    assert provenance['native_capture_verified'] is False
    assert provenance['fixture_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_unknown_claude_control_keeps_owned_before_after_and_defers_untagged_row():
    from transcripts import RawRecord
    from transcripts.claude import parse_rows
    before=row('user','before','Own before')
    unknown={'type':'future_control','private':'NOT FOR NOTES'}
    untagged={'type':'assistant','uuid':'untagged','message':{'role':'assistant','content':'Unproven inherited answer'}}
    after=row('assistant','after','Own after')
    raw=tuple(RawRecord(i*10,(i+1)*10,value) for i,value in enumerate([before,unknown,untagged,after]))
    parsed=parse_rows(SimpleNamespace(native_session_id='opaque-native-id'),raw,raw[0],{})
    assert parsed.status=='partial' and parsed.blocked_offset is None
    assert [r.text for r in parsed.records]==['Own before','Own after']
    assert [(r.offset,r.reason) for r in parsed.deferred_rows]==[(10,'unknown_schema:future_control'),(20,'ambiguous_ownership')]
    assert 'NOT FOR NOTES' not in json.dumps(parsed.parser_state)



def test_unknown_metadata_does_not_block_explicit_native_dialogue(native_case):
    import hashlib
    unknown={'type':'foo-state','sessionId':'opaque-native-id',
             'accountUuid':'PRIVATE-ACCOUNT','privateFields':{'token':'PRIVATE-TOKEN'}}
    values=[row('user','before','Before unknown'),unknown,
            row('user','after-user','After question'),row('assistant','after-assistant','After answer')]
    batch=read(native_case,values)
    assert batch.status=='partial' and not batch.source_complete
    assert [record.text for record in batch.records]==['Before unknown','After question','After answer']
    assert batch.consumed_offset==native_case[1].stat().st_size
    assert batch.warnings==('Unknown substantive Claude transcript record (type=foo-state)','Deferred transcript rows remain pending')
    refs=batch.parser_state['_deferred_source_rows']
    assert len(refs)==1 and refs[0]['reason']=='unknown_schema:foo-state'
    encoded=(json.dumps(unknown)+'\n').encode()
    assert refs[0]['row_sha256']==hashlib.sha256(encoded).hexdigest()
    for private in ('PRIVATE-ACCOUNT','PRIVATE-TOKEN','accountUuid','privateFields'):
        assert private not in json.dumps(batch.parser_state)
        assert private not in json.dumps(batch.metadata)
        assert private not in repr(batch.records)



def test_unknown_metadata_fields_cannot_poison_native_identity_or_provenance(native_case):
    unknown={'type':'foo-state','sessionId':'PRIVATE-FOREIGN-ID','cwd':'PRIVATE-CWD',
             'version':'PRIVATE-VERSION','parentSessionId':'PRIVATE-PARENT','parentUuid':'PRIVATE-UUID',
             'isSidechain':True,'message':{'id':'PRIVATE-MESSAGE','content':'PRIVATE-CONTENT'}}
    batch=read(native_case,[row('user','before','Own before'),unknown,row('assistant','after','Own after')])
    assert batch.status=='partial' and not batch.source_complete
    assert [r.text for r in batch.records]==['Own before','Own after']
    assert batch.metadata['native_session_id']=='opaque-native-id'
    assert set(batch.metadata)=={'host','native_session_id'}
    assert batch.parser_state['_deferred_source_rows'][0]['reason']=='unknown_schema:foo-state'
    for private in ('PRIVATE-FOREIGN-ID','PRIVATE-CWD','PRIVATE-VERSION','PRIVATE-PARENT','PRIVATE-UUID',
                    'PRIVATE-MESSAGE','PRIVATE-CONTENT'):
        assert private not in json.dumps(batch.metadata)
        assert private not in json.dumps(batch.parser_state)
        assert private not in repr(batch.records)
        assert private not in repr(batch.warnings)


@pytest.mark.parametrize('malformation',['message-nondict','content-nondict-block'])
def test_known_malformed_messages_keep_blocked_offset(native_case,malformation):
    value = row('user','malformed','Visible original')
    if malformation == 'message-nondict':
        value['message'] = 'not an envelope'
    else:
        value['message']['content'] = ['not a content block']
    batch = read(native_case,[value])
    assert batch.status == 'unsupported' and batch.consumed_offset == 0
    assert not batch.records and not batch.parser_state.get('_deferred_source_rows')
