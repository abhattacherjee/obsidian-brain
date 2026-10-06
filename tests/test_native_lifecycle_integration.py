"""Native lifecycle wiring preserves deadlines and fail-open policy behavior."""
from pathlib import Path
from types import MappingProxyType
import json
import sqlite3
import time

import pytest
from runtime_context import RuntimeContext, using_runtime_context
from test_runtime_context import runtime_case


@pytest.fixture
def context(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    return RuntimeContext("codex", "codex-cli", "native-id", project, project, None, vault,
                          tmp_path / "config.json", MappingProxyType({}), project,
                          tmp_path / "index.sqlite3", tmp_path / "state")


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
    coordination = context.index_path.parent / ("." + context.index_path.name + ".coordination")
    coordination.mkdir()
    with sqlite3.connect(coordination / "state.sqlite3") as conn:
        conn.execute("CREATE TABLE identity (vault TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO identity VALUES (?)", (str(context.vault_path.resolve()),))
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


def test_native_wrapper_replays_real_codex_fixture_in_disposable_vault(runtime_case):
    import subprocess
    import sys
    case = runtime_case
    root = Path(__file__).resolve().parents[1]
    fixture = root / "tests/fixtures/hosts/codex-cli-0.159.0-alpha.12.1-original.jsonl"
    native_id = json.loads(fixture.read_text().splitlines()[0])["payload"]["id"]
    source = case["codex_home"] / "sessions" / "fixture.jsonl"
    source.parent.mkdir()
    source.write_bytes(fixture.read_bytes())
    case["config_path"].write_text(json.dumps({"vault_path": str(case["vault"]), "min_messages": 1, "min_duration_minutes": 0}))
    command = [sys.executable, str(root / ".codex/hooks/lifecycle.py"),
               "--host", "codex", "--client", "codex-cli", "--event", "session_end",
               "--config", str(case["config_path"]), "--resource-root", str(case["resource_root"]),
               "--index", str(case["home"] / "index.sqlite3"), "--state", str(case["home"] / "state")]
    result = subprocess.run(command, input=json.dumps({"session_id": native_id,
                             "cwd": str(case["worktree"]), "transcript_path": str(source)}),
                            text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert not result.stdout
    notes = list(case["vault"].rglob("*.md"))
    assert len(notes) == 1, result.stderr
    text = notes[0].read_text()
    assert 'agent_provider: "codex"' in text
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
    assert "--client" in result.stderr


@pytest.mark.parametrize("status,pending,loss", [("pending", 1, False), ("complete", 0, True)])
def test_doctor_reports_pending_recovery_and_keeps_scanning(context, capture_calls, monkeypatch, capsys,
                                                           status, pending, loss):
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
    monkeypatch.setattr(sys, "argv", ["vault_doctor", "--json"])
    with using_runtime_context(context):
        assert vault_doctor.main() == 2
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


def test_same_native_id_in_two_hosts_has_independent_retro_decisions(context, capture_calls):
    from dataclasses import replace
    from native_lifecycle import dispatch
    import obsidian_utils
    other = replace(context, host="claude", client="claude-code")
    for current in (context, other):
        with using_runtime_context(current):
            obsidian_utils.mark_retro_classification_pending(current.native_session_id, "retro.md")
        assert dispatch(current, "stop", {"turn_id": "same-turn"}, time.monotonic())["decision"] == "block"


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


def test_recover_cli_serializes_capture_result_and_uses_active_fact_recovery(runtime_case, capture_calls):
    import io
    import brain_cli
    output = io.StringIO()
    case = runtime_case
    code = brain_cli.main(["--host", "codex", "--client", "codex-cli", "--session-id", "native",
                           "--cwd", str(case["worktree"]), "--config", str(case["config_path"]),
                           "--resource-root", str(case["resource_root"]), "recover"],
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
