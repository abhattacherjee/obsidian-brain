"""Translate old frontend response fixtures into explicit fake native AI results."""
import json
import re
import subprocess


def run(*args, **kwargs):
    raise AssertionError("Frontend AI result fixture must be mocked")


def execute_ai(context, operation, request):
    from ai_backend import AIResult
    try:
        response = run(["claude", "-p", "--model", request.model],
                       input=request.input, capture_output=True, text=True,
                       timeout=request.timeout)
    except subprocess.TimeoutExpired:
        return AIResult("timeout", input_revision=request.input_revision, backend="claude", model=request.model)
    except FileNotFoundError:
        return AIResult("unavailable", input_revision=request.input_revision, backend="claude",
                        model=request.model, error_code="executable_missing")
    if response.returncode:
        return AIResult("unavailable", input_revision=request.input_revision, backend="claude", model=request.model)
    output = response.stdout.strip()
    if not output:
        return AIResult("invalid_output", input_revision=request.input_revision, backend="claude",
                        model=request.model, error_code="empty_output")
    if operation == "theme_names":
        if output.startswith("```"):
            output = output.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            output = json.loads(output)
        except ValueError:
            return AIResult("invalid_output", error_code="parse_error")
        if not isinstance(output, list) or len(output) != request.options["expected_count"]:
            return AIResult("invalid_output", error_code="count_mismatch")
    elif operation == "session_summaries":
        parts = re.compile(r"^=====\s*SUMMARY\s+(\d+)\s*=====\s*$", re.MULTILINE).split(output)
        blocks = {}
        for index in range(1, len(parts) - 1, 2):
            blocks.setdefault(int(parts[index]), parts[index + 1].strip())
        output = blocks
    return AIResult("ok", output, request.input_revision, backend="claude", model=request.model)
