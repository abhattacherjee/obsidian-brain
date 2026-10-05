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
