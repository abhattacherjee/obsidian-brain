"""Native lifecycle wiring preserves deadlines and fail-open policy behavior."""
from pathlib import Path
from types import MappingProxyType
import json
import sqlite3
import time

import pytest
from runtime_context import RuntimeContext, using_runtime_context
from test_runtime_context import runtime_case, runtime_case_data


def test_native_publication_failure_retains_checkpoint_and_reports_deferral(selected_host_context, monkeypatch, capsys):
    from dataclasses import replace
    from types import MappingProxyType
    import capture
    import native_lifecycle
    import note_transactions
    import transcripts
    selected = replace(selected_host_context, config=MappingProxyType(
        dict(selected_host_context.config, min_messages=1, min_duration_minutes=0)))
    records = (transcripts.SourceRecord('synthetic-fact', 'user', 'Retained synthetic fact.', 0),)
    monkeypatch.setattr(transcripts, 'read_records', lambda ctx, cursor, deadline:
        transcripts.TranscriptBatch('ok', records, 'synthetic-generation', 100,
            metadata={'native_session_id': ctx.native_session_id},
            source_identity='synthetic-source', source_complete=True, source_size=100))
    def disk_full(*args, **kwargs):
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(capture, 'apply_mutations', disk_full)
    assert native_lifecycle.dispatch(selected, 'session_end', {}, time.monotonic()) is None
    assert capsys.readouterr().err == '[obsidian-brain] capture failed: OSError\n'
    connection = note_transactions.connect_coordination(selected)
    try:
        assert connection.execute('SELECT COUNT(*) FROM capture_events WHERE scope=?',
                                  (selected.session_key,)).fetchone()[0] == 1
        assert connection.execute('SELECT phase FROM checkpoints WHERE scope=?',
                                  (selected.session_key,)).fetchone()[0] == 'ready'
        assert connection.execute('SELECT COUNT(*) FROM cursors WHERE scope=?',
                                  (selected.session_key,)).fetchone()[0] == 0
    finally:
        connection.close()
    assert list(selected.vault_path.rglob('*.md')) == []


@pytest.fixture
def context(selected_host_context):
    selected_host_context.index_path.parent.mkdir(parents=True, exist_ok=True)
    yield selected_host_context


@pytest.fixture
def capture_calls(monkeypatch):
    import capture
    from types import SimpleNamespace
    calls = []
    result = SimpleNamespace(status="complete", applied_revision="revision", pending_sources=0,
                             loss_of_input=False, warnings=())
    monkeypatch.setattr(capture, "CaptureEvent", lambda **kwargs: SimpleNamespace(**kwargs), raising=False)

    def checkpoint(ctx, event, deadline):
        calls.append(("capture", event.kind, event.turn_id, deadline))
        return result

    def recover(ctx, max_sources, deadline, include_active=False):
        calls.append(("recover", max_sources, include_active, deadline))
        return result

    monkeypatch.setattr(capture, "capture_checkpoint", checkpoint, raising=False)
    monkeypatch.setattr(capture, "recover_registered", recover, raising=False)
    return calls


def test_start_recovery_and_capture_share_entry_deadline(context, capture_calls):
    from native_lifecycle import dispatch
    started = time.monotonic()
    dispatch(context, "session_start", {"source": "resume"}, started)
    assert capture_calls[0] == ("recover", 8, True, started + 1)
    assert capture_calls[1] == ("capture", "resume", None, started + 1)


def test_end_deadline_uses_wrapper_start(context, capture_calls):
    from native_lifecycle import dispatch
    started = time.monotonic() - 1
    dispatch(context, "session_end", {}, started)
    assert capture_calls == [("capture", "session_end", None, started + 2.5)]


def test_compaction_dispatches_snapshot_event(context, capture_calls):
    from native_lifecycle import dispatch
    dispatch(context, "pre_compact", {"turn_id": "verified-turn"}, time.monotonic())
    assert capture_calls[0][1:3] == ("pre_compact", "verified-turn")


def test_capture_failure_does_not_hide_retro_block(context, capture_calls, monkeypatch):
    from native_lifecycle import dispatch
    import capture
    import obsidian_utils
    with using_runtime_context(context):
        obsidian_utils.mark_retro_classification_pending(context.native_session_id, "retro.md")

    def fail(*args):
        raise OSError("capture unavailable")

    monkeypatch.setattr(capture, "capture_checkpoint", fail)
    out = dispatch(context, "stop", {"turn_id": "turn-one"}, time.monotonic())
    assert out["decision"] == "block"
    assert dispatch(context, "stop", {"turn_id": "turn-one"}, time.monotonic()) is None


def test_active_stop_guard_passes_and_clears_pending(context, capture_calls):
    from native_lifecycle import dispatch
    import obsidian_utils
    with using_runtime_context(context):
        obsidian_utils.mark_retro_classification_pending(context.native_session_id, "retro.md")
    assert dispatch(context, "stop", {"stop_hook_active": True}, time.monotonic()) is None
    with using_runtime_context(context):
        assert obsidian_utils.get_retro_classification_pending(context.native_session_id) is None


def test_native_start_never_scans_or_rebuilds_index(context, capture_calls, monkeypatch):
    from native_lifecycle import dispatch
    import obsidian_utils
    import vault_index
    monkeypatch.setattr(obsidian_utils, "find_latest_session", lambda *a: pytest.fail("vault scan"))
    monkeypatch.setattr(vault_index, "ensure_index", lambda *a, **k: pytest.fail("index rebuild"))
    assert dispatch(context, "session_start", {}, time.monotonic()) is None
    assert not context.index_path.exists()


def test_existing_verified_index_can_supply_bounded_context(context, capture_calls):
    from native_lifecycle import dispatch
    from contextlib import closing
    from note_transactions import connect_coordination
    with closing(connect_coordination(context)):
        pass
    note = context.vault_path / "claude-sessions" / "prior.md"
    with sqlite3.connect(context.index_path) as conn:
        conn.execute("CREATE TABLE notes(path TEXT, project TEXT, type TEXT, date TEXT, title TEXT, body TEXT)")
        conn.execute("INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?)",
                     (str(note), "project", "claude-session", "2026-10-05", "Prior session",
                      "## Summary\nStored context\n## Next steps\nContinue implementation"))
    out = dispatch(context, "session_start", {}, time.monotonic())
    assert "Stored context" in out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_native_reaper_uses_registered_sources_without_active_finalization(context, capture_calls):
    import obsidian_session_reaper
    out = obsidian_session_reaper.reap_registered_sessions(context, max_sources=3, deadline=time.monotonic()+1)
    assert capture_calls[0][0:3] == ("recover", 3, False)
    assert out.status == "complete"


def test_explicit_native_cli_replays_real_fixture_in_disposable_vault(selected_host_context):
    import subprocess
    import sys
    context = selected_host_context
    root = Path(__file__).resolve().parents[1]
    fixture = (root / "tests/fixtures/hosts/codex-cli-0.159.0-alpha.12.1-original.jsonl"
               if context.host == 'codex' else root / 'tests/fixtures/dropped-sessions/d2cc7e46-long-617min-full.jsonl')
    native_id = context.native_session_id
    rows = [json.loads(line) for line in fixture.read_text().splitlines()]
    for row in rows:
        if context.host == 'codex' and row.get('type') == 'session_meta':
            row['payload']['id'] = native_id
            if 'session_id' in row['payload']:
                row['payload']['session_id'] = native_id
        elif context.host == 'codex' and isinstance(row.get('payload'), dict):
            if 'thread_id' in row['payload']:
                row['payload']['thread_id'] = native_id
        elif context.host == 'claude' and 'sessionId' in row:
            row['sessionId'] = native_id
    source = context.native_home / ('sessions' if context.host == 'codex' else 'projects') / "fixture.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    context.config_path.write_text(json.dumps({"vault_path": str(context.vault_path), "min_messages": 1, "min_duration_minutes": 0}))
    command = [sys.executable, str(root / "hooks/brain_cli.py"),
               "--host", context.host, "--client", context.client, "--event", "session_end",
               "--config", str(context.config_path), "--resource-root", str(context.resource_root),
               "--index", str(context.index_path), "--state", str(context.state_path), 'hook']
    result = subprocess.run(command, input=json.dumps({"session_id": native_id,
                             "cwd": str(context.worktree), "transcript_path": str(source)}),
                            text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert not result.stdout
    notes = list(context.vault_path.rglob("*.md"))
    assert len(notes) == 1, result.stderr
    text = notes[0].read_text()
    assert 'agent_provider: "' + context.host + '"' in text
    assert 'capture_state: "ended"' in text
    assert native_id in text


def test_native_wrapper_argument_errors_fail_open():
    import subprocess
    import sys
    wrapper = Path(__file__).resolve().parents[1] / ".codex/hooks/lifecycle.py"
    result = subprocess.run([sys.executable, str(wrapper), "--host", "codex"],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert not result.stdout
    assert "native client identity is unavailable" in result.stderr


@pytest.mark.parametrize("status,pending,loss,expected_exit", [
    ("pending", 1, False, 1),
    ("complete", 0, True, 2),
    ("unavailable", 1, False, 2),
])
def test_doctor_reports_pending_recovery_and_keeps_scanning(context, capture_calls, monkeypatch, capsys,
                                                           status, pending, loss, expected_exit):
    from types import SimpleNamespace
    from scripts import vault_doctor
    import capture
    import sys
    scanned = []
    module = SimpleNamespace(NAME="independent", DEFAULT_WINDOW_DAYS=1,
                             scan=lambda *a, **k: scanned.append(True) or [])
    monkeypatch.setattr(vault_doctor, "_load_config", lambda args: {
        "vault": str(context.vault_path), "sessions_folder": "claude-sessions", "insights_folder": "claude-insights"})
    monkeypatch.setattr(vault_doctor.vault_doctor_checks, "all_checks", lambda: [module])
    monkeypatch.setattr(capture, "recover_registered", lambda *a, **k: SimpleNamespace(
        status=status, applied_revision=None, pending_sources=pending, loss_of_input=loss, warnings=("deadline",)))
    monkeypatch.setattr(sys, "argv", ["vault_doctor", "--json", "--apply"])
    with using_runtime_context(context):
        assert vault_doctor.main() == expected_exit
    output = capsys.readouterr()
    assert scanned
    assert json.loads(output.out)["capture_recovery"]["pending_sources"] == pending
    assert ("capture source input was lost" if loss else "capture recovery remains pending") in output.err


@pytest.mark.parametrize("module,event,entry", [
    ("obsidian_session_hint", "session_start", "_run"),
    ("obsidian_session_log", "session_end", "_run"),
    ("obsidian_context_snapshot", "pre_compact", "_run"),
    ("obsidian_retro_gate", "stop", "main"),
])
def test_existing_bound_hooks_use_shared_dispatch_and_bounded_payload(context, capture_calls, monkeypatch,
                                                                      module, event, entry):
    import importlib
    import io
    import sys
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"turn_id": "native-turn"})))
    getattr(importlib.import_module(module), entry)(context=context)
    captures = [call for call in capture_calls if call[0] == "capture"]
    assert captures[0][1:3] == (event, "native-turn")


def test_turn_specific_retro_sentinel_does_not_block_another_turn(context, capture_calls):
    from native_lifecycle import dispatch
    import obsidian_utils
    with using_runtime_context(context):
        obsidian_utils.mark_retro_classification_pending(context.native_session_id, "retro.md", turn_id="turn-one")
        assert obsidian_utils.get_retro_classification_pending(context.native_session_id)["turn_id"] == "turn-one"
    assert dispatch(context, "stop", {"turn_id": "turn-two"}, time.monotonic()) is None
    assert dispatch(context, "stop", {"turn_id": "turn-one"}, time.monotonic())["decision"] == "block"


def test_same_native_id_in_two_hosts_has_independent_retro_decisions(context, capture_calls, host_identity_scenario):
    from dataclasses import replace
    from native_lifecycle import dispatch
    import obsidian_utils
    other_host = 'codex' if context.host == 'claude' else 'claude'
    other = replace(context, host=other_host,
                    client='codex-cli' if other_host == 'codex' else 'claude-code')
    host_identity_scenario.register(context, other)
    with using_runtime_context(other):
        obsidian_utils.mark_retro_classification_pending(other.native_session_id, "retro.md")
    assert dispatch(context, "stop", {"turn_id": "same-turn"}, time.monotonic()) is None
    obsidian_utils.mark_retro_classification_pending(context.native_session_id, "retro.md")
    assert dispatch(context, "stop", {"turn_id": "same-turn"}, time.monotonic())["decision"] == "block"
    with using_runtime_context(other):
        assert obsidian_utils.get_retro_classification_pending(other.native_session_id) is not None


@pytest.mark.parametrize("boundary", ["vault", "project"])
def test_same_session_and_turn_have_independent_retro_decisions_across_contexts(
        context, capture_calls, monkeypatch, tmp_path, boundary):
    from dataclasses import replace
    from native_lifecycle import dispatch
    import obsidian_utils
    # Identical sentinel timestamps ensure only the state-path boundary separates claims.
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    other_path = tmp_path / ("other-" + boundary)
    other_path.mkdir()
    other = (replace(context, vault_path=other_path) if boundary == "vault"
             else replace(context, canonical_project_root=other_path))
    for current in (context, other):
        with using_runtime_context(current):
            obsidian_utils.mark_retro_classification_pending(current.native_session_id, "retro.md")
        assert dispatch(current, "stop", {"turn_id": "same-turn"}, time.monotonic())["decision"] == "block"
        assert dispatch(current, "stop", {"turn_id": "same-turn"}, time.monotonic()) is None


@pytest.mark.parametrize("trigger", ["auto", "manual"])
def test_precompact_forwards_native_trigger(context, capture_calls, monkeypatch, trigger):
    import capture
    from native_lifecycle import dispatch
    seen = []
    real = capture.CaptureEvent

    def event(**options):
        seen.append(options)
        return real(**options)

    monkeypatch.setattr(capture, "CaptureEvent", event)
    dispatch(context, "pre_compact", {"trigger": trigger}, time.monotonic())
    assert seen[0]["trigger"] == trigger


def test_recover_cli_serializes_capture_result_and_uses_active_fact_recovery(context, capture_calls):
    import io
    import brain_cli
    output = io.StringIO()
    code = brain_cli.main(["--host", context.host, "--client", context.client,
                           "--session-id", context.native_session_id,
                           "--cwd", str(context.worktree), "--config", str(context.config_path),
                           "--resource-root", str(context.resource_root),
                           "--index", str(context.index_path), "--state", str(context.state_path), "recover"],
                          stdin=io.StringIO("{}"), stdout=output)
    assert code == 0
    result = json.loads(output.getvalue())
    assert result == {"status": "complete", "applied_revision": "revision", "pending_sources": 0,
                      "loss_of_input": False, "warnings": []}
    assert capture_calls[0][0:3] == ("recover", 8, True)


def test_hook_context_resolution_error_cannot_be_a_policy_block(runtime_case):
    import io
    import brain_cli
    output, error = io.StringIO(), io.StringIO()
    code = brain_cli.main(["--host", "codex", "--client", "codex-cli", "--event", "stop", "hook"],
                          stdin=io.StringIO("{}"), stdout=output, stderr=error)
    assert code == 0
    assert not output.getvalue()
    assert json.loads(error.getvalue())["code"] == "session_missing"


def test_native_clear_reason_is_forwarded_as_explicit_snapshot_trigger(context, capture_calls, monkeypatch):
    import capture
    from native_lifecycle import dispatch
    seen = []
    real = capture.CaptureEvent

    def event(**fields):
        seen.append(fields)
        return real(**fields)

    monkeypatch.setattr(capture, "CaptureEvent", event)
    dispatch(context, "session_end", {"reason": "clear"}, time.monotonic())
    assert seen[0]["trigger"] == "clear"
    dispatch(context, "session_end", {"reason": "other"}, time.monotonic())
    assert "trigger" not in seen[1]


def test_native_reaper_reports_unavailable_without_crashing(context, monkeypatch):
    import capture
    import obsidian_session_reaper

    def failure(*args, **kwargs):
        raise OSError("retained source unavailable")

    monkeypatch.setattr(capture, "recover_registered", failure)
    result = obsidian_session_reaper.reap_registered_sessions(context)
    assert result.status == "unavailable"
    assert result.pending_sources == 1
    assert "retained source unavailable" in result.warnings


def test_unknown_source_retains_cursor_and_is_visible_without_index(selected_host_context):
    from dataclasses import replace
    import capture
    import native_lifecycle
    import note_transactions
    selected = selected_host_context
    secret = 'sk-test-secret-do-not-expose'
    if selected.host == 'claude':
        source = selected.native_home / 'projects' / 'synthetic' / 'source.jsonl'
        header = {'type': 'user', 'sessionId': selected.native_session_id,
                  'uuid': 'private-message-id', 'message': {'role': 'user', 'content': 'Visible first fact.'}}
    else:
        source = selected.native_home / 'sessions' / 'source.jsonl'
        header = {'type': 'session_meta', 'payload': {'id': selected.native_session_id,
                  'cwd': str(selected.canonical_project_root)}}
    source.parent.mkdir(parents=True, exist_ok=True)
    prefix = (json.dumps(header) + '\n').encode()
    unknown = {'type': 'future_record', 'sessionId': selected.native_session_id,
               'payload': {'content': secret, 'id': 'private-unknown-id'}}
    raw = prefix + (json.dumps(unknown) + '\n').encode()
    source.write_bytes(raw)
    selected = replace(selected, transcript_path=source)
    result = capture.capture_checkpoint(selected, capture.CaptureEvent('session_start'), time.monotonic()+1)
    assert result.status == 'pending'
    assert result.pending_sources == 1
    # The format is incomplete, but the unread bytes remain available: no input loss.
    assert result.loss_of_input is False
    output = native_lifecycle.dispatch(selected, 'session_start', {}, time.monotonic())
    hint = output['hookSpecificOutput']['additionalContext']
    assert 'Unknown substantive' in hint
    assert 'type=future_record' in hint
    assert 'at least 1 source(s)' in hint
    for private in (secret, str(source), selected.native_session_id, 'private-message-id', 'private-unknown-id'):
        assert private not in hint
    assert not selected.index_path.exists()
    assert not list(selected.vault_path.rglob('*.md'))
    assert source.read_bytes() == raw
    with note_transactions.connect_coordination(selected) as connection:
        row = connection.execute('SELECT cursor,completeness FROM source_sessions WHERE scope=?',
                                 (selected.session_key,)).fetchone()
    assert json.loads(row[0])['offset'] == len(raw)
    refs = json.loads(row[0])['parser_state']['_deferred_source_rows']
    assert len(refs) == 1 and refs[0]['offset'] == len(prefix)
    assert row[1] == 'partial'


def test_start_diagnostic_deduplicates_safe_labels_and_uses_lower_bound(context, monkeypatch):
    import capture
    import native_lifecycle
    warning = 'Unknown substantive Claude transcript record (type=future_record)'
    monkeypatch.setattr(capture, 'recover_registered', lambda *a, **k: capture.CaptureResult(
        'pending', pending_sources=3, warnings=(warning, warning, 'raw secret sk-secret /private/path')
        + tuple('Unknown substantive Claude transcript record (type=future_' + label + ')'
                for label in ('one', 'two', 'three', 'four', 'five'))))
    monkeypatch.setattr(capture, 'capture_checkpoint', lambda *a, **k: capture.CaptureResult(
        'pending', pending_sources=1, warnings=(warning,
        'Unknown substantive Claude transcript record (type=sk-secret)',
        'Unknown substantive Claude transcript record (type=unsafe/path)')))
    monkeypatch.setattr(native_lifecycle, '_context_hint', lambda *a: {
        'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': 'Existing context'}})
    hint = native_lifecycle.dispatch(context, 'resume', {}, time.monotonic())['hookSpecificOutput']['additionalContext']
    assert hint.startswith('Existing context')
    assert 'at least 3 source(s)' in hint
    assert hint.count('future_record') == 1
    assert 'future_three' in hint
    assert 'future_four' not in hint and 'future_five' not in hint
    assert 'sk-secret' not in hint and '/private/path' not in hint and 'unsafe/path' not in hint


def test_oversized_source_record_remains_pending_with_visible_safe_hint(selected_host_context):
    from dataclasses import replace
    from contextlib import closing
    import capture, native_lifecycle, note_transactions
    selected = selected_host_context
    if selected.host == 'claude':
        source = selected.native_home / 'projects' / 'synthetic' / 'source.jsonl'
        header = {'type': 'user', 'sessionId': selected.native_session_id,
                  'uuid': 'private-message-id', 'message': {'role': 'user', 'content': 'Visible fact.'}}
    else:
        source = selected.native_home / 'sessions' / 'source.jsonl'
        header = {'type': 'session_meta', 'payload': {'id': selected.native_session_id,
                  'cwd': str(selected.canonical_project_root)}}
    source.parent.mkdir(parents=True, exist_ok=True)
    prefix = (json.dumps(header) + '\n').encode()
    secret = 'private-source-body-secret'
    raw = prefix + (json.dumps({'type': 'future_record', 'content': secret + 'x' * 1100000}) + '\n').encode()
    source.write_bytes(raw)
    selected = replace(selected, transcript_path=source)
    result = capture.capture_checkpoint(selected, capture.CaptureEvent('session_start'), time.monotonic() + 2)
    assert result.status == 'pending' and result.pending_sources == 1
    assert result.loss_of_input is False
    output = native_lifecycle.dispatch(selected, 'session_start', {}, time.monotonic())
    hint = output['hookSpecificOutput']['additionalContext']
    assert 'Oversized '+selected.host.capitalize()+' transcript record retained' in hint
    assert 'at least 1 source(s)' in hint
    for private in (secret, str(source), selected.native_session_id, 'private-message-id'):
        assert private not in hint
    with closing(note_transactions.connect_coordination(selected)) as connection:
        row = connection.execute('SELECT descriptor FROM source_sessions WHERE scope=?', (capture._scope(selected),)).fetchone()
        assert row is not None
        state = json.loads(connection.execute('SELECT cursor FROM source_sessions WHERE scope=?', (capture._scope(selected),)).fetchone()[0])
        assert state['offset'] == len(raw)
        refs = state['parser_state']['_deferred_source_rows']
        assert len(refs) == 1 and refs[0]['offset'] == len(prefix)
        assert refs[0]['reason'] == 'oversized:unrecognized'
        assert secret not in json.dumps(refs)
    assert source.read_bytes() == raw and not selected.index_path.exists()


@pytest.mark.parametrize('unknown_type',['future_record','foo-state'])
def test_known_rows_after_unknown_publish_partial_and_doctor_names_type(selected_host_context, capsys, unknown_type):
    from dataclasses import replace
    import capture
    import native_lifecycle
    import note_transactions
    import vault_doctor
    selected = selected_host_context
    if selected.host == 'claude':
        source = selected.native_home/'projects'/'synthetic'/'around-unknown.jsonl'
        header = {'type':'user','sessionId':selected.native_session_id,'uuid':'before',
                  'message':{'role':'user','content':'Known before unknown.'}}
        later = {'type':'user','sessionId':selected.native_session_id,'uuid':'after',
                 'message':{'role':'user','content':'Known after unknown.'}}
    else:
        source = selected.native_home/'sessions'/'around-unknown.jsonl'
        header = {'type':'session_meta','payload':{'id':selected.native_session_id,
                  'cwd':str(selected.canonical_project_root)}}
        later = {'type':'event_msg','payload':{'type':'item_completed',
                 'thread_id':selected.native_session_id,'turn_id':'owned-later-turn',
                 'item':{'type':'UserMessage','id':'after',
                         'content':[{'type':'text','text':'Known after unknown.'}]}}}
    unknown = {'type':unknown_type,'version':'private-unknown-version','cwd':'/private/unknown-directory',
               'sessionId':'private-foreign-session-id',
               'payload':{'content':'private-unknown-content','account_id':'private-account-id'}}
    source.parent.mkdir(parents=True,exist_ok=True)
    if selected.host == 'claude':
        assistant = {'type':'assistant','sessionId':selected.native_session_id,'uuid':'answer',
                     'message':{'role':'assistant','content':'Known answer after unknown.'}}
    else:
        assistant = {'type':'event_msg','payload':{'type':'item_completed',
                     'thread_id':selected.native_session_id,'turn_id':'owned-later-turn',
                     'item':{'type':'AgentMessage','id':'answer','content':[{'type':'text','text':'Known answer after unknown.'}]}}}
    raw = ''.join(json.dumps(row)+'\n' for row in (header,unknown,later,assistant)).encode()
    source.write_bytes(raw)
    actor = replace(selected,transcript_path=source)
    note = selected.vault_path/'partial.md'
    result = capture.capture_checkpoint(actor,capture.CaptureEvent('stop',note_path=note,min_messages=1),time.monotonic()+2)
    assert result.status == 'pending' and result.pending_sources == 1 and not result.loss_of_input
    assert 'Known after unknown.' in note.read_text()
    assert 'Known answer after unknown.' in note.read_text()
    assert 'private-unknown-content' not in note.read_text()
    output = native_lifecycle.dispatch(actor,'session_start',{},time.monotonic())
    assert 'type='+unknown_type in output['hookSpecificOutput']['additionalContext']
    report = vault_doctor._recover_runtime_pending()
    assert report['status'] == 'pending' and report['pending_sources'] >= 1
    assert any('type='+unknown_type in warning for warning in report['warnings'])
    assert 'capture recovery pending' in capsys.readouterr().err
    assert source.read_bytes() == raw
    with note_transactions.connect_coordination(actor) as connection:
        retained = connection.execute('SELECT cursor FROM source_sessions WHERE scope=?',(actor.session_key,)).fetchone()[0]
    refs = json.loads(retained)['parser_state']['_deferred_source_rows']
    assert len(refs) == 1 and 'private-unknown-content' not in json.dumps(refs)
    with note_transactions.connect_coordination(actor) as connection:
        facts = repr(connection.execute('SELECT text FROM capture_events').fetchall())
        documents = repr(connection.execute('SELECT payload,document FROM operations').fetchall())
    for secret in ('private-unknown-content','private-account-id','private-unknown-version',
                   '/private/unknown-directory','private-foreign-session-id'):
        assert secret not in facts+documents+retained+note.read_text()


def test_retired_review_notice_is_project_scoped_and_has_no_scheduled_count(selected_host_context, monkeypatch, capsys):
    import io
    from dataclasses import replace
    import capture
    import native_entry
    import note_transactions
    selected = selected_host_context
    source = selected.native_home / ('projects/synthetic/notice.jsonl' if selected.host == 'claude'
                                    else 'sessions/notice.jsonl')
    source.parent.mkdir(parents=True, exist_ok=True)
    if selected.host == 'claude':
        rows = [{'type':'user','sessionId':selected.native_session_id,'uuid':'before',
                 'message':{'role':'user','content':'Known owned fact.'}},
                {'type':'future_record','private':'not-a-visible-fact'},
                {'type':'assistant','sessionId':selected.native_session_id,'uuid':'answer',
                 'message':{'role':'assistant','content':'Known owned answer.'}}]
    else:
        rows = [{'type':'session_meta','payload':{'id':selected.native_session_id,
                 'cwd':str(selected.canonical_project_root)}},
                {'type':'future_record','private':'not-a-visible-fact'},
                {'type':'event_msg','payload':{'type':'item_completed','thread_id':selected.native_session_id,
                 'turn_id':'own-turn','item':{'type':'UserMessage','id':'before',
                 'content':[{'type':'text','text':'Known owned fact.'}]}}},
                {'type':'event_msg','payload':{'type':'item_completed','thread_id':selected.native_session_id,
                 'turn_id':'own-turn','item':{'type':'AgentMessage','id':'answer',
                 'content':[{'type':'text','text':'Known owned answer.'}]}}}]
    source.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    selected.config_path.write_text(json.dumps(dict(selected.config,min_messages=1,min_duration_minutes=0)))
    def entry(event, project=selected.canonical_project_root, native_id=selected.native_session_id, path=source):
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'session_id':native_id,
            'cwd':str(project),'transcript_path':str(path),'reason':'other'})))
        assert native_entry.run(['--host',selected.host,'--client',selected.client,
            '--event',event,'--config',str(selected.config_path),'--index',str(selected.index_path),
            '--state',str(selected.state_path)], started_at=time.monotonic()) == 0
        return capsys.readouterr()
    for _ in range(3):
        output = entry('session_end')
        assert 'retains unverified input for review' in output.err
        assert 'at least 1 source(s)' not in output.err
    actor = replace(selected,transcript_path=source)
    with note_transactions.connect_coordination(actor) as connection:
        assert connection.execute('SELECT COUNT(*) FROM retired_source_versions').fetchone()[0] == 1
        refs = json.loads(connection.execute('SELECT cursor FROM source_sessions').fetchone()[0])
        assert refs['parser_state']['_deferred_source_rows']
    same_project = entry('session_start', native_id='next-same-project',path=source.parent/'not-created.jsonl')
    assert 'retains unverified input for review' in same_project.out
    other_project = selected.worktree.parent/'separate-parent'/selected.canonical_project_root.name
    other_project.mkdir(parents=True)
    foreign = entry('session_start',project=other_project,native_id='next-foreign-project',path=source.parent/'not-created.jsonl')
    assert 'unverified input' not in foreign.out + foreign.err
    assert 'future_record' not in foreign.out + foreign.err

    # An incomplete appended row is real scheduled input, unlike retired refs.
    if selected.host == 'claude':
        late = {'type':'user','sessionId':selected.native_session_id,'uuid':'late',
                'message':{'role':'user','content':'New owned append.'}}
    else:
        late = {'type':'event_msg','payload':{'type':'item_completed','thread_id':selected.native_session_id,
                'turn_id':'late-turn','item':{'type':'UserMessage','id':'late',
                'content':[{'type':'text','text':'New owned append.'}]}}}
    with source.open('a') as stream:
        stream.write(json.dumps(late))
    append_notice = entry('session_end')
    assert 'at least 1 source(s)' in append_notice.err
    with note_transactions.connect_coordination(actor) as connection:
        assert connection.execute('SELECT COUNT(*) FROM retired_source_versions').fetchone()[0] == 0
    with source.open('a') as stream:
        stream.write('\n')
    finished = entry('session_end')
    assert 'retains unverified input for review' in finished.err
    assert 'at least 1 source(s)' not in finished.err
