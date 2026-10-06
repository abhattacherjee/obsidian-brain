"""Claude's print-mode response contract, with native permissions preserved."""

import json
import re


def execute(context, prompt, schema, model, deadline, env):
    from ai_backend import _BackendFailure, _failure_status, _json, _run_bounded
    binary = context.config.get("claude_executable", "claude")
    if not isinstance(binary, str) or not binary:
        raise _BackendFailure("unavailable", "native_executable_invalid")
    command = [binary, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
               "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--no-session-persistence", "--no-chrome", "--permission-prompts", "none"]
    if model:
        command.extend(["--model", model])
    code, raw, errors = _run_bounded(command, prompt, cwd=context.worktree, env=env, deadline=deadline)
    try:
        envelope = _json(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        if not code:
            raise
        envelope = None
    if code or (isinstance(envelope, dict) and envelope.get("is_error") is True):
        details = envelope.get("errors", []) if isinstance(envelope, dict) else []
        details = [item for item in details if isinstance(item, str)] if isinstance(details, list) else []
        status, error = _failure_status(errors + "\n".join(details).encode())
        raise _BackendFailure(status, error)
    if not isinstance(envelope, dict):
        raise _BackendFailure("invalid_output", "native_result_invalid")
    output = envelope.get("structured_output")
    if output is None:
        output = _json(envelope.get("result", ""))
    usage = envelope.get("modelUsage")
    observed = None
    if isinstance(usage, dict) and len(usage) == 1:
        name = next(iter(usage))
        if isinstance(name, str) and re.fullmatch(r"claude-[a-z0-9][a-z0-9-]{0,127}", name):
            observed = name
    return output, observed
