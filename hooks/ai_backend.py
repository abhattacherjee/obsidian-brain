"""Run bounded analysis through the explicitly selected native host."""

import copy
import json
import math
import os
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional

MAX_INPUT_BYTES = 512_000
MAX_OUTPUT_BYTES = 1_000_000
OPERATIONS = frozenset({"snapshot_summary", "theme_names", "session_summary",
                        "session_summaries", "semantic_merge", "classify_items"})
ANALYSIS_INSTRUCTION = "\n\nReturn only JSON matching this schema. Analyze the supplied input without tools.\n"
VALIDATION_POLICY_REVISION = "strict-json-semantic-v1"


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return copy.deepcopy(value)


@dataclass(frozen=True)
class AIRequest:
    input: str
    input_revision: str = ""
    timeout: float = 120.0
    model: Optional[str] = None
    options: Mapping = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "options", _freeze(dict(self.options)))


@dataclass(frozen=True)
class AIResult:
    status: str
    data: object = None
    input_revision: str = ""
    diagnostic: str = ""
    backend: str = ""
    model: Optional[str] = None
    error_code: Optional[str] = None


def resolve_ai_selection(context, operation, requested_model=None):
    """Unknown native defaults stay unknown and cannot authorize cache reuse."""
    if context is None or context.host not in {"claude", "codex"}:
        return "", None
    if requested_model is not None and not isinstance(requested_model, str):
        return context.host, None
    if context.host == "claude":
        return "claude", requested_model or context.config.get("summary_model", "haiku")
    selected = context.config.get("codex_ai_model") or context.config.get("codex_summary_model")
    if requested_model and requested_model not in {"haiku", "sonnet", "opus"}:
        selected = requested_model
    return "codex", selected


class _BackendFailure(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def _stop_process(process):
    # A parent may exit while its children still hold the output pipes open.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        if process.poll() is None:
            raise _BackendFailure("unavailable", "process_cleanup_denied")
    if process.poll() is None:
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        if process.poll() is None:
            raise _BackendFailure("unavailable", "process_cleanup_denied")


def _run_bounded(command, prompt, *, cwd, env, deadline):
    """Drain both pipes while sending input; cancel the complete process group."""
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=cwd, env=env,
                                   start_new_session=True, umask=0o077)
    except FileNotFoundError as exc:
        raise _BackendFailure("unavailable", "executable_missing") from exc
    except OSError as exc:
        raise _BackendFailure("unavailable", "process_start_failed") from exc
    payload = memoryview(prompt.encode("utf-8"))
    output, errors = bytearray(), bytearray()
    total = 0
    try:
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "input")
            selector.register(process.stdout, selectors.EVENT_READ, output)
            selector.register(process.stderr, selectors.EVENT_READ, errors)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _BackendFailure("timeout", "deadline_exceeded")
                for key, _ in selector.select(min(remaining, 0.1)):
                    pipe = key.fileobj
                    if key.data == "input":
                        try:
                            sent = os.write(pipe.fileno(), payload[:65536]) if payload else 0
                            payload = payload[sent:]
                        except BrokenPipeError:
                            payload = memoryview(b"")
                        except BlockingIOError:
                            continue
                        if not payload:
                            selector.unregister(pipe)
                            pipe.close()
                        continue
                    try:
                        chunk = os.read(pipe.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(pipe)
                        pipe.close()
                        continue
                    total += len(chunk)
                    if total > MAX_OUTPUT_BYTES:
                        raise _BackendFailure("invalid_output", "output_limit")
                    key.data.extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _BackendFailure("timeout", "deadline_exceeded")
            try:
                code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise _BackendFailure("timeout", "deadline_exceeded") from exc
        return code, bytes(output), bytes(errors)
    finally:
        _stop_process(process)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe and not pipe.closed:
                pipe.close()


def _object(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


def _schema(operation, options):
    text = {"type": "string", "minLength": 1}
    if operation in {"snapshot_summary", "session_summary"}:
        return _object({"text": text})
    if operation == "theme_names":
        return _object({"themes": {"type": "array", "items": _object({
            "name": text, "summary": {"type": "string"}})}})
    if operation == "session_summaries":
        return _object({"summaries": {"type": "array", "items": _object({
            "index": {"type": "integer", "minimum": 1}, "text": text})}})
    if operation == "semantic_merge":
        return _object({"merges": {"type": "array", "items": _object({
            "canonical_group_id": text, "absorbed_group_ids": {"type": "array", "items": text},
            "reasoning": text})}, "total_groups_before": {"type": "integer", "minimum": 0},
            "total_groups_after": {"type": "integer", "minimum": 0}})
    if operation == "classify_items":
        nullable = {"type": ["string", "null"]}
        return _object({"items": {"type": "array", "items": _object({
            "group_id": text,
            "classification": {"type": "string", "enum": ["DONE", "NEEDS-ACTION", "STALE", "ACTIVE", "REVIEW"]},
            "confidence": {"type": "string", "enum": ["HIGH", "MED", "LOW"]},
            "canonical_text": text, "evidence_citation": nullable, "action_required": nullable})}})
    raise _BackendFailure("unavailable", "operation_unknown")


def _json(raw):
    def unique(pairs):
        value = {}
        for name, item in pairs:
            if name in value:
                raise ValueError("duplicate key")
            value[name] = item
        return value
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def _validate_schema(value, schema):
    kind = schema["type"]
    if isinstance(kind, list):
        if value is None and "null" in kind:
            return
        kind = "string"
    if kind == "object":
        if not isinstance(value, dict) or set(value) != set(schema["properties"]):
            raise ValueError("object fields")
        for name, item in value.items():
            _validate_schema(item, schema["properties"][name])
    elif kind == "array":
        if not isinstance(value, list):
            raise ValueError("array")
        for item in value:
            _validate_schema(item, schema["items"])
    elif kind == "string":
        if not isinstance(value, str) or (schema.get("minLength") and not value.strip()):
            raise ValueError("string")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError("enum")
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int) or value < schema.get("minimum", 0):
            raise ValueError("integer")


def validate_ai_output(operation, output, options):
    _validate_schema(output, _schema(operation, options))
    if operation in {"snapshot_summary", "session_summary"}:
        return output["text"].strip()
    if operation == "theme_names":
        if len(output["themes"]) != options.get("expected_count"):
            raise ValueError("theme count")
        return output["themes"]
    if operation == "session_summaries":
        result = {}
        for item in output["summaries"]:
            index = item["index"]
            if index in result or index > options.get("expected_count", 0):
                raise ValueError("summary index")
            result[index] = item["text"]
        return result
    expected = options.get("expected_ids")
    if not isinstance(expected, (list, tuple)) or len(set(expected)) != len(expected):
        raise ValueError("request IDs")
    expected = set(expected)
    if operation == "classify_items":
        seen = [item["group_id"] for item in output["items"]]
        if set(seen) != expected or len(seen) != len(expected):
            raise ValueError("classification IDs")
        return output["items"]
    used = set()
    projects = options.get("project_by_id", {})
    removed = 0
    for merge in output["merges"]:
        canonical, absorbed = merge["canonical_group_id"], merge["absorbed_group_ids"]
        group = [canonical, *absorbed]
        if not absorbed or len(set(group)) != len(group) or not set(group).issubset(expected):
            raise ValueError("merge IDs")
        if used.intersection(group) or not projects or len({projects.get(key) for key in group}) != 1:
            raise ValueError("merge ownership")
        if any(key not in projects for key in group):
            raise ValueError("project missing")
        used.update(group)
        removed += len(absorbed)
    if output["total_groups_before"] != len(expected) or output["total_groups_after"] != len(expected) - removed:
        raise ValueError("merge totals")
    return output


def _failure_status(stderr):
    # Inspect bounded native errors locally; never return their contents.
    lower = stderr.lower()
    if any(word in lower for word in (b"unauthorized", b"not logged in", b"authentication", b"401")):
        return "auth_error", "native_auth_error"
    if any(word in lower for word in (b"permission denied", b"policy", b"forbidden", b"approval")):
        return "unavailable", "policy_denied"
    return "unavailable", "native_execution_failed"


def ai_contract_identity(operation, options=None):
    """Return the actual shared output contract for durable cache fingerprints."""
    return {"operation": operation, "schema": _schema(operation, options or {}),
            "analysis_instruction": ANALYSIS_INSTRUCTION,
            "validation_policy_revision": VALIDATION_POLICY_REVISION}


def execute_ai(context, operation, request):
    backend, selected_model = resolve_ai_selection(context, operation, request.model)
    model = None
    def result(status, data=None, code=""):
        return AIResult(status, data, request.input_revision, code, backend, model, code or None)
    if not backend:
        return result("unavailable", code="context_required")
    if (request.model is not None and (not isinstance(request.model, str)
            or not request.model.strip() or len(request.model) > 256)):
        return result("unavailable", code="model_invalid")
    if selected_model is not None and (not isinstance(selected_model, str) or not selected_model.strip() or len(selected_model) > 256):
        return result("unavailable", code="model_invalid")
    if (not isinstance(request.input, str) or isinstance(request.timeout, bool)
            or not isinstance(request.timeout, (int, float)) or not math.isfinite(request.timeout)
            or request.timeout <= 0 or request.timeout > 3600):
        return result("unavailable", code="request_invalid")
    try:
        schema = _schema(operation, request.options)
        from obsidian_utils import scrub_secrets
        prompt = scrub_secrets(request.input)
        prompt += ANALYSIS_INSTRUCTION + json.dumps(schema)
        if len(prompt.encode("utf-8")) > MAX_INPUT_BYTES:
            raise _BackendFailure("unavailable", "input_limit")
        deadline = time.monotonic() + request.timeout
        if context.native_home is None:
            raise _BackendFailure("unavailable", "native_home_missing")
        env = dict(os.environ)
        env["OBSIDIAN_BRAIN_NESTED_AI"] = "1"
        with tempfile.TemporaryDirectory(prefix="obsidian-ai-") as private:
            directory = Path(private)
            os.chmod(directory, 0o700)
            if backend == "claude":
                from ai_adapters import claude
                output, model = claude.execute(context, prompt, schema, selected_model, deadline, env)
            else:
                from ai_adapters import codex
                output, model = codex.execute(context, prompt, schema, selected_model, deadline, env, directory)
            data = validate_ai_output(operation, output, request.options)
            return result("ok", data)
    except _BackendFailure as exc:
        return result(exc.status, code=exc.code)
    except (ValueError, TypeError, UnicodeError, OSError):
        return result("invalid_output", code="output_invalid")
