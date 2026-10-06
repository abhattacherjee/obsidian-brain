"""Codex analysis keeps native policy and disables discovered outbound tools."""

import json
import os
import re
import selectors
import stat
import subprocess
import time
from pathlib import Path

from ai_backend import (_BackendFailure, _failure_status, _json, _run_bounded,
                        _stop_process, MAX_OUTPUT_BYTES)

DISABLED_FEATURES = ("shell_tool", "unified_exec", "apps", "browser_use", "browser_use_external",
                     "browser_use_full_cdp_access", "computer_use", "image_generation", "in_app_browser",
                     "multi_agent", "remote_plugin", "view_image", "goals", "code_mode_host")
CODE_MODE_DISABLED_NOTICE = (
    "Code Mode is unavailable because code-mode host is disabled. "
    "Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`."
)


def _config_argument(path, value):
    return ["-c", path + "=" + json.dumps(value)]


def _segment(name):
    # Native -c splits paths on dots; quoted segments become literal quotes.
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_@-]+", name):
        raise _BackendFailure("unavailable", "native_override_key_unsupported")
    return name


def restrictions():
    arguments = []
    for feature in DISABLED_FEATURES:
        arguments.extend(_config_argument("features." + feature, False))
    arguments.extend(_config_argument("web_search", "disabled"))
    return arguments


class NativeRpc:
    """Read native config metadata without starting a model or answering approvals."""
    def __init__(self, binary, arguments, context, env, deadline):
        self.deadline = deadline
        try:
            self.process = subprocess.Popen([binary, *arguments, "app-server"], stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            cwd=context.worktree, env=env, start_new_session=True, umask=0o077)
        except (FileNotFoundError, OSError) as exc:
            raise _BackendFailure("unavailable", "native_inventory_start_failed") from exc
        self.selector = selectors.DefaultSelector()
        os.set_blocking(self.process.stdin.fileno(), False)
        for pipe in (self.process.stdout, self.process.stderr):
            os.set_blocking(pipe.fileno(), False)
            self.selector.register(pipe, selectors.EVENT_READ)
        self.buffer = bytearray()
        self.stderr = bytearray()
        self.bytes_read = 0
        self.number = 0

    def close(self):
        try:
            _stop_process(self.process)
        finally:
            self.selector.close()
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                if not pipe.closed:
                    pipe.close()

    def _send(self, message):
        payload = memoryview(message)
        if len(payload) > 2_000_000:
            raise _BackendFailure("unavailable", "native_inventory_limit")
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdin, selectors.EVENT_WRITE)
            while payload:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise _BackendFailure("timeout", "native_inventory_timeout")
                if not selector.select(min(remaining, 0.1)):
                    continue
                try:
                    written = os.write(self.process.stdin.fileno(), payload)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    raise _BackendFailure("unavailable", "native_inventory_closed") from exc
                payload = payload[written:]

    def request(self, method, params):
        self.number += 1
        number = self.number
        message = {"id": number, "method": method, "params": params}
        self._send((json.dumps(message) + "\n").encode())
        while True:
            while b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                if not line.strip():
                    continue
                reply = _json(line.decode("utf-8"))
                if not isinstance(reply, dict):
                    raise _BackendFailure("unavailable", "native_inventory_invalid")
                if "method" in reply:
                    if "id" in reply:
                        raise _BackendFailure("unavailable", "native_inventory_approval_required")
                    continue
                if reply.get("id") != number:
                    raise _BackendFailure("unavailable", "native_inventory_unexpected_reply")
                if "error" in reply or "result" not in reply:
                    raise _BackendFailure("unavailable", "native_inventory_error")
                return reply["result"]
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise _BackendFailure("timeout", "native_inventory_timeout")
            for key, _ in self.selector.select(min(remaining, 0.1)):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    if key.fileobj is self.process.stderr:
                        continue
                    raise _BackendFailure("unavailable", "native_inventory_closed")
                self.bytes_read += len(chunk)
                if self.bytes_read > 8_000_000:
                    raise _BackendFailure("unavailable", "native_inventory_limit")
                if key.fileobj is self.process.stdout:
                    self.buffer.extend(chunk)
                    if len(self.buffer) > 2_000_000:
                        raise _BackendFailure("unavailable", "native_inventory_limit")
                else:
                    self.stderr.extend(chunk[:max(0, 64000-len(self.stderr))])

    def initialize(self):
        self.request("initialize", {"clientInfo": {"name": "obsidian-native-analysis", "version": "1"},
                                    "capabilities": {"experimentalApi": True}})
        self._send(b'{"method":"initialized"}\n')


def _configuration(rpc, context):
    result = rpc.request("config/read", {"includeLayers": False, "cwd": str(context.worktree)})
    if not isinstance(result, dict) or not isinstance(result.get("config"), dict):
        raise _BackendFailure("unavailable", "native_config_invalid")
    return result["config"]


def _native_environment(context, env):
    """Bind discovery and execution to the selected native configuration."""
    if context.native_home is None:
        raise _BackendFailure("unavailable", "native_home_missing")
    return dict(env, CODEX_HOME=str(context.native_home))


def discover_restrictions(binary, context, env, deadline):
    """Enumerate configured and installed servers before a model can run."""
    env = _native_environment(context, env)
    arguments = restrictions()
    rpc = NativeRpc(binary, arguments, context, env, deadline)
    try:
        rpc.initialize()
        config = _configuration(rpc, context)
        servers = config.get("mcp_servers", {})
        plugins = config.get("plugins", {})
        if not isinstance(servers, dict) or not isinstance(plugins, dict):
            raise _BackendFailure("unavailable", "native_config_invalid")
        for name in servers:
            arguments.extend(_config_argument("mcp_servers." + _segment(name) + ".enabled", False))
        listing = rpc.request("plugin/list", {"cwds": [str(context.worktree)], "forceRefetch": False,
                                              "marketplaceKinds": ["local"]})
        if not isinstance(listing, dict) or not isinstance(listing.get("marketplaces"), list):
            raise _BackendFailure("unavailable", "native_plugin_inventory_invalid")
        if listing.get("marketplaceLoadErrors"):
            raise _BackendFailure("unavailable", "native_plugin_inventory_partial")
        installed = {}
        for marketplace in listing["marketplaces"]:
            if not isinstance(marketplace, dict) or marketplace.get("loadErrors"):
                raise _BackendFailure("unavailable", "native_plugin_inventory_partial")
            entries = marketplace.get("plugins")
            if not isinstance(entries, list):
                raise _BackendFailure("unavailable", "native_plugin_inventory_partial")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise _BackendFailure("unavailable", "native_plugin_inventory_invalid")
                if entry.get("enabled") is not True or entry.get("installed") is not True:
                    continue
                name, marketplace_path = entry.get("name"), marketplace.get("path")
                if not isinstance(name, str) or not isinstance(marketplace_path, str):
                    raise _BackendFailure("unavailable", "native_plugin_inventory_invalid")
                detail = rpc.request("plugin/read", {"pluginName": name, "marketplacePath": marketplace_path})
                if not isinstance(detail, dict) or not isinstance(detail.get("plugin"), dict):
                    raise _BackendFailure("unavailable", "native_plugin_inventory_partial")
                plugin = detail["plugin"]
                identity = entry.get("id")
                bundled = plugin.get("mcpServers")
                if (not isinstance(identity, str) or not isinstance(bundled, list)
                        or any(not isinstance(name, str) or not name for name in bundled)
                        or len(set(bundled)) != len(bundled) or identity in installed):
                    raise _BackendFailure("unavailable", "native_plugin_inventory_invalid")
                installed[identity] = bundled
                for server in bundled:
                    path = "plugins." + _segment(identity) + ".mcp_servers." + _segment(server) + ".enabled"
                    arguments.extend(_config_argument(path, False))
                if len(installed) > 256:
                    raise _BackendFailure("unavailable", "native_plugin_inventory_limit")
        for identity, settings in plugins.items():
            if isinstance(settings, dict) and settings.get("enabled") is True and identity not in installed:
                raise _BackendFailure("unavailable", "native_plugin_inventory_partial")
        model = config.get("model")
        model = model if isinstance(model, str) and model else None
    finally:
        rpc.close()
    # Re-read the effective override layer and all runtime pages before execution.
    rpc = NativeRpc(binary, arguments, context, env, deadline)
    try:
        rpc.initialize()
        restricted = _configuration(rpc, context)
        features = restricted.get("features")
        if (not isinstance(features, dict) or any(features.get(name) is not False for name in DISABLED_FEATURES)
                or restricted.get("web_search") != "disabled"):
            raise _BackendFailure("unavailable", "native_feature_restriction_failed")
        effective = restricted.get("mcp_servers", {})
        if not isinstance(effective, dict) or any(not isinstance(value, dict) or value.get("enabled") is not False
                                                 for value in effective.values()):
            raise _BackendFailure("unavailable", "native_mcp_restriction_failed")
        effective_plugins = restricted.get("plugins", {})
        for identity, bundled in installed.items():
            settings = effective_plugins.get(identity, {}).get("mcp_servers", {})
            if any(settings.get(name, {}).get("enabled") is not False for name in bundled):
                raise _BackendFailure("unavailable", "native_mcp_restriction_failed")
        cursor = None
        seen = set()
        for _ in range(32):
            params = {"limit": 100, "detail": "toolsAndAuthOnly"}
            if cursor is not None:
                params["cursor"] = cursor
            status = rpc.request("mcpServerStatus/list", params)
            if not isinstance(status, dict) or not isinstance(status.get("data"), list):
                raise _BackendFailure("unavailable", "native_mcp_status_invalid")
            for server in status["data"]:
                required = {"name", "pluginId", "tools", "resources", "resourceTemplates", "runtimeStatus"}
                if (not isinstance(server, dict) or not required.issubset(server)
                        or not isinstance(server.get("name"), str)
                        or not isinstance(server["tools"], dict)
                        or not isinstance(server["resources"], list)
                        or not isinstance(server["resourceTemplates"], list)
                        or server["runtimeStatus"] is not None):
                    raise _BackendFailure("unavailable", "native_mcp_status_invalid")
                name, owner = server["name"], server.get("pluginId")
                if owner is None:
                    disabled = effective.get(name, {}).get("enabled") is False
                else:
                    disabled = (owner in installed and name in installed[owner]
                                and effective_plugins.get(owner, {}).get("mcp_servers", {}).get(name, {}).get("enabled") is False)
                # Native status lists disabled definitions with null runtimeStatus.
                # Config proof plus an empty catalog distinguishes them from tools.
                if not disabled or server.get("tools") or server.get("resources") or server.get("resourceTemplates"):
                    raise _BackendFailure("unavailable", "native_mcp_still_advertised")
            cursor = status.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise _BackendFailure("unavailable", "native_mcp_status_partial")
            seen.add(cursor)
        else:
            raise _BackendFailure("unavailable", "native_mcp_status_partial")
    finally:
        rpc.close()
    return arguments, model


def execute(context, prompt, schema, requested_model, deadline, env, directory):
    env = _native_environment(context, env)
    metadata = directory.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or directory.resolve().is_relative_to(context.vault_path.resolve())):
        raise _BackendFailure("unavailable", "native_private_directory_invalid")
    binary = context.config.get("codex_executable", "codex")
    if not isinstance(binary, str) or not binary:
        raise _BackendFailure("unavailable", "native_executable_invalid")
    arguments, native_model = discover_restrictions(binary, context, env, deadline)
    model = requested_model or native_model
    schema_path, output_path = directory / "schema.json", directory / "result.json"
    for path, text in ((schema_path, json.dumps(schema)), (output_path, "")):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
    command = [binary, *arguments, "exec", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check",
               "--cd", str(context.worktree), "--output-schema", str(schema_path), "--json",
               "--output-last-message", str(output_path)]
    if model:
        command.extend(["--model", model])
    command.append("-")
    code, raw, errors = _run_bounded(command, prompt, cwd=context.worktree, env=env, deadline=deadline)
    if code:
        status, error = _failure_status(errors)
        raise _BackendFailure(status, error)
    completed = False
    started = False
    thread_seen = False
    message_seen = False
    for line in raw.splitlines():
        event = _json(line.decode("utf-8"))
        if not isinstance(event, dict):
            raise _BackendFailure("invalid_output", "native_event_invalid")
        kind = event.get("type")
        if completed:
            raise _BackendFailure("invalid_output", "native_turn_order_invalid")
        if kind in {"error", "turn.failed"}:
            raise _BackendFailure("unavailable", "native_turn_failed")
        if kind == "turn.completed":
            if not thread_seen or not started or not message_seen:
                raise _BackendFailure("invalid_output", "native_turn_order_invalid")
            completed = True
        elif kind == "turn.started":
            if not thread_seen or started:
                raise _BackendFailure("invalid_output", "native_turn_order_invalid")
            started = True
        elif kind == "thread.started":
            if thread_seen or started or not isinstance(event.get("thread_id"), str) or not event["thread_id"]:
                raise _BackendFailure("invalid_output", "native_turn_order_invalid")
            thread_seen = True
        elif kind in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            if (thread_seen and not started and kind == "item.completed"
                    and isinstance(item, dict) and item.get("type") == "error"
                    and " ".join(str(item.get("message", "")).split()).casefold() == CODE_MODE_DISABLED_NOTICE.casefold()):
                continue
            if not isinstance(item, dict) or item.get("type") not in {"reasoning", "agent_message"}:
                raise _BackendFailure("unavailable", "native_tool_attempt")
            if not started:
                raise _BackendFailure("invalid_output", "native_turn_order_invalid")
            if kind == "item.completed" and item["type"] == "agent_message":
                message_seen = True
        else:
            raise _BackendFailure("invalid_output", "native_event_unsupported")
    if not completed:
        raise _BackendFailure("invalid_output", "native_turn_incomplete")
    fd = os.open(output_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_OUTPUT_BYTES
                or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1):
            raise _BackendFailure("invalid_output", "native_output_invalid")
        output = _json(stream.read(MAX_OUTPUT_BYTES + 1).decode("utf-8"))
    return output, model
