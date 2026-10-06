"""Claude native transcript layouts and privacy boundaries."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    assert blocked.status == "unsupported" and blocked.consumed_offset == 0


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
