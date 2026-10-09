#!/usr/bin/env python3
"""Validate native hook-discovery evidence without claiming runtime acceptance."""
import json
import math
import sys
from pathlib import Path


EVENTS = frozenset((
    "preToolUse", "permissionRequest", "postToolUse", "preCompact", "postCompact",
    "sessionStart", "sessionEnd", "userPromptSubmit", "subagentStart", "subagentStop",
    "stop", "interrupt",
))
TRUST = frozenset(("managed", "untrusted", "trusted", "modified"))


def validate_observation(evidence):
    if not isinstance(evidence, dict):
        raise ValueError("Observation must be an object")
    for field in ("client", "version"):
        if not isinstance(evidence.get(field), str) or not evidence[field].strip():
            raise ValueError("Missing recorded " + field)
    if evidence.get("method") != "hooks/list":
        raise ValueError("Unsupported observation method")
    response = evidence.get("response")
    if not isinstance(response, dict) or "error" in response:
        raise ValueError("Native discovery request did not succeed")
    groups = response.get("data")
    if not isinstance(groups, list) or not groups:
        raise ValueError("No native discovery groups")
    hooks = []
    warnings = []
    for group in groups:
        if not isinstance(group, dict) or group.get("errors"):
            raise ValueError("Native hook discovery reported errors")
        entries = group.get("hooks")
        if not isinstance(entries, list):
            raise ValueError("Invalid native hooks array")
        group_warnings = group.get("warnings", [])
        if not isinstance(group_warnings, list):
            raise ValueError("Invalid native discovery warnings")
        warnings.extend(group_warnings)
        for hook in entries:
            if not isinstance(hook, dict) or hook.get("eventName") not in EVENTS:
                raise ValueError("Unknown or invalid installed hook event")
            if hook.get("trustStatus") not in TRUST:
                raise ValueError("Unknown installed hook trust state")
            timeout = hook.get("timeoutSec")
            if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout) or timeout <= 0):
                raise ValueError("Invalid installed hook timeout")
            if not isinstance(hook.get("sourcePath"), str) or not hook["sourcePath"]:
                raise ValueError("Missing installed hook source")
            if not isinstance(hook.get("enabled"), bool):
                raise ValueError("Invalid installed hook enabled flag")
            hooks.append({
                "event": hook["eventName"],
                "source": hook["sourcePath"],
                "timeout_seconds": timeout,
                "trust": hook["trustStatus"],
                "trusted_and_enabled": hook["enabled"] and hook["trustStatus"] in ("trusted", "managed"),
            })
    if not hooks:
        raise ValueError("No installed hooks observed")
    return {
        "client": evidence["client"], "version": evidence["version"],
        "runtime_verified": False, "hooks": hooks, "warnings": warnings,
    }


def main(argv):
    if len(argv) != 1:
        print("usage: validate-host-contracts.py OBSERVATION.json", file=sys.stderr)
        return 1
    try:
        with Path(argv[0]).open("r", encoding="utf-8") as source:
            raw = source.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("Observation exceeds size limit")
        report = validate_observation(json.loads(raw))
    except (OSError, ValueError, TypeError, KeyError) as error:
        print("Invalid contract evidence: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
