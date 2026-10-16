"""Contract evidence must distinguish observation from runtime acceptance."""
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "dev-test" / "validate-host-contracts.py"


def run_validator(tmp_path, observation):
    assert VALIDATOR.is_file(), "Installed-host contract validator is missing"
    evidence = tmp_path / "observation.json"
    evidence.write_text(json.dumps(observation), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(VALIDATOR), str(evidence)],
        capture_output=True, text=True, timeout=10,
    )


def observation():
    return {
        "client": "codex-cli",
        "version": "0.155.1",
        "method": "hooks/list",
        "response": {"data": [{
            "cwd": "<sandbox>", "warnings": [], "errors": [],
            "hooks": [{
                "eventName": "sessionEnd", "sourcePath": "<plugin>/hooks/hooks.json",
                "pluginId": "obsidian-brain@obsidian-brain-repo",
                "enabled": True, "timeoutSec": 1, "trustStatus": "untrusted",
            }],
        }]},
    }


def test_untrusted_selected_handler_is_observed_without_claiming_acceptance(tmp_path):
    result = run_validator(tmp_path, observation())
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["runtime_verified"] is False
    assert report["hooks"][0]["timeout_seconds"] == 1
    assert report["hooks"][0]["trusted_and_enabled"] is False
    assert report["hooks"][0]["source"] == "<plugin>/hooks/hooks.json"


def test_trusted_metadata_still_does_not_prove_execution(tmp_path):
    evidence = observation()
    evidence["response"]["data"][0]["hooks"][0]["trustStatus"] = "trusted"
    result = run_validator(tmp_path, evidence)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["hooks"][0]["trusted_and_enabled"] is True
    assert report["runtime_verified"] is False


@pytest.mark.parametrize("mutation", ["no_version", "rpc_error", "discovery_error", "no_hooks", "bad_timeout", "unknown_event", "bad_trust"])
def test_invalid_or_empty_evidence_is_not_success(tmp_path, mutation):
    evidence = observation()
    group = evidence["response"]["data"][0]
    if mutation == "no_version":
        del evidence["version"]
    elif mutation == "rpc_error":
        evidence["response"] = {"error": {"message": "Native request failed"}}
    elif mutation == "discovery_error":
        group["errors"] = ["Invalid installed manifest"]
    elif mutation == "no_hooks":
        group["hooks"] = []
    elif mutation == "bad_timeout":
        group["hooks"][0]["timeoutSec"] = True
    elif mutation == "unknown_event":
        group["hooks"][0]["eventName"] = "futureEvent"
    else:
        group["hooks"][0]["trustStatus"] = "assumed"
    result = run_validator(tmp_path, evidence)
    assert result.returncode == 1
    assert result.stderr.strip()
    assert not result.stdout.strip()


def test_checked_in_observations_have_pinned_versions_and_no_home_paths(tmp_path):
    fixtures = ROOT / "tests" / "fixtures" / "hosts"
    for name, version in (
        ("codex-cli-0.159.0-alpha.12.1-hooks.json", "0.159.0-alpha.12.1"),
        ("codex-desktop-0.159.0-alpha.12.1-hooks.json", "0.159.0-alpha.12.1"),
    ):
        path = fixtures / name
        assert path.is_file(), "Native hook observation fixture is missing"
        raw = path.read_text(encoding="utf-8")
        assert "/Users/" not in raw
        assert "/private/tmp/" not in raw
        evidence = json.loads(raw)
        assert evidence["version"] == version
        result = run_validator(tmp_path, evidence)
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert {hook["event"] for hook in report["hooks"]} == {"sessionStart", "preCompact", "sessionEnd", "stop"}
        assert report["runtime_verified"] is False


def test_native_cli_lifecycle_preserves_thread_identity_and_stop_loop():
    path = ROOT / "tests" / "fixtures" / "hosts" / "codex-cli-0.159.0-alpha.12.1-lifecycle.jsonl"
    assert path.is_file(), "Native lifecycle evidence is missing"
    raw = path.read_text(encoding="utf-8")
    assert "/Users/" not in raw and "/private/tmp/" not in raw
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    starts = {event.get("source"): event for event in events if event["hook_event_name"] == "SessionStart"}
    assert {"startup", "resume", "fork"} <= set(starts)
    assert starts["startup"]["session_id"] != starts["fork"]["session_id"]
    assert starts["resume"]["session_id"] == starts["fork"]["session_id"]
    assert any(event["hook_event_name"] == "PreCompact" and event["trigger"] == "manual" for event in events)
    assert any(event["hook_event_name"] == "SessionEnd" for event in events)
    for event in events:
        assert event["session_id"] in event["transcript_path"]
        assert event["plugin_root_present"] and event["plugin_data_present"]
        assert event["native_thread_id"] is None
    stops = [event for event in events if event["hook_event_name"] == "Stop"]
    block = next(event for event in stops if event["hook_output"].get("decision") == "block")
    assert block["stop_hook_active"] is False
    continuation = [event for event in stops if event["turn_id"] == block["turn_id"] and event["stop_hook_active"]]
    assert len(continuation) == 1
    assert continuation[0]["session_id"] == block["session_id"]
    assert continuation[0]["hook_output"] == {}


@pytest.mark.parametrize("name,sid,required", [
    ("codex-cli-0.159.0-alpha.12.1-original.jsonl", "01a10c1b-7f42-7093-855a-2522456861b9", {"compacted"}),
    ("codex-cli-0.159.0-alpha.12.1-fork.jsonl", "01a10ce5-b3ce-79c3-9e67-827b41ab9a7a", {"turn_aborted"}),
    ("codex-desktop-0.159.0-alpha.12.1-session.jsonl", "01a10ce2-7898-7b01-889f-80c112ac9118", {"task_complete"}),
])
def test_native_transcript_layouts_keep_metadata_tools_and_mirrors(name, sid, required):
    path = ROOT / "tests" / "fixtures" / "hosts" / name
    assert path.is_file(), "Native transcript fixture is missing"
    raw = path.read_text(encoding="utf-8")
    assert "/Users/" not in raw and "/private/tmp/" not in raw
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert rows[0]["type"] == "session_meta"
    assert rows[0]["payload"]["id"] == sid
    assert rows[0]["payload"]["cli_version"] == "0.159.0-alpha.12.1"
    if "desktop" in name:
        assert rows[0]["payload"]["originator"] == "Codex Desktop"
        assert rows[0]["payload"]["source"] == "vscode"
    else:
        assert rows[0]["payload"]["source"] == "cli"
    if "fork" in name:
        assert rows[0]["payload"]["forked_from_id"] == "01a10c1b-7f42-7093-855a-2522456861b9"
    envelopes = {row["type"] for row in rows}
    payload_types = {row.get("payload", {}).get("type") for row in rows}
    assert {"response_item", "event_msg", "turn_context"} <= envelopes
    assert {"custom_tool_call", "custom_tool_call_output", "item_completed"} <= payload_types
    assert required <= envelopes | payload_types


@pytest.mark.parametrize("name", [
    "codex-cli-0.159.0-alpha.12.1-original.jsonl",
    "codex-cli-0.159.0-alpha.12.1-fork.jsonl",
    "codex-desktop-0.159.0-alpha.12.1-session.jsonl",
])
def test_native_fixtures_exclude_account_and_instruction_metadata(name):
    import hashlib
    fixtures = ROOT / "tests" / "fixtures" / "hosts"
    path = fixtures / name
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    forbidden = {"creator_user_id", "creator_account_id", "base_instructions", "rate_limits",
                 "plan_type", "credits", "usage", "turn_token_usage", "thread_token_usage",
                 "total_token_usage", "last_token_usage"}

    def check(value):
        if isinstance(value, dict):
            if forbidden.intersection(value):
                pytest.fail("Private native metadata remains", pytrace=False)
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    for row in rows:
        check(row)
        payload = row.get("payload", {})
        if row["type"] == "token_usage_record":
            assert payload == {}
        elif row["type"] == "event_msg" and payload.get("type") == "token_count":
            assert payload == {"type": "token_count"}
    provenance = json.loads((fixtures / "native-session-provenance.json").read_text())
    recorded = next(item for item in provenance["candidates"] if item["fixture_filename"] == name)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == recorded["fixture_sha256"]
    counts = {}
    for row in rows:
        counts[row["type"]] = counts.get(row["type"], 0) + 1
    assert counts == recorded["record_type_counts"]
