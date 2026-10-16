"""Normalize visible Claude records without exposing runtime instructions."""
import json
import re

from . import DeferredRow, ParsedRecords, SourceRecord, read_source


def native_tool_categories():
    """Normalize native source names; these names never select invocation tools."""
    return {"Bash": "shell", "Read": "read", "Edit": "edit", "MultiEdit": "edit",
            "Write": "write", "Grep": "search", "Glob": "search", "Agent": "agent",
            "Task": "agent", "WebFetch": "web", "WebSearch": "web", "TodoWrite": "planning"}


_CATEGORIES = native_tool_categories()
_IGNORED_ROWS = {"progress", "file-history-snapshot", "summary", "queue-operation", "last-prompt",
                 "custom-title", "agent-name", "agent-color", "pr-link", "saved_hook_context",
                 "permission-mode", "attachment"}
_PRIVATE_PREFIXES = ("<environment_context>", "<permissions instructions>", "<system-reminder>",
                     "<INSTRUCTIONS>", "# AGENTS.md instructions for ")


def _shape(value, fields):
    """Match a named verified object, including every nested field."""
    return (isinstance(value, dict) and set(value) == set(fields)
            and all(check(value[key]) for key, check in fields.items()))


def _strings(*keys):
    return {key: lambda value: isinstance(value, str) for key in keys}


def _integer(value):
    return type(value) is int


def _number(value):
    import math
    return type(value) in {int, float} and math.isfinite(value)


def _map_shape(value, fields):
    return (isinstance(value, dict) and all(isinstance(key, str) and _shape(item, fields)
                                          for key, item in value.items()))


def _verified_metadata_row(data):
    """Accept only named schemas from the current client's sanitized census."""
    kind = data.get("type")
    if not isinstance(kind, str):
        return False
    scalar = {"mode": "mode", "ai-title": "aiTitle", "atis-latch": "atis",
              "relocated": "relocatedCwd"}
    if kind in scalar:
        return _shape(data, _strings("type", "sessionId", scalar[kind]))
    if kind == "continued-in":
        return _shape(data, _strings("type", "sessionId", "timestamp", "continuedInSessionId"))
    if kind == "bridge-session":
        return _shape(data, dict(_strings("type", "sessionId", "bridgeSessionId", "ownerAccountUuid",
                                         "ownerOrganizationUuid"), lastSequenceNum=_integer))
    if kind == "frame-link":
        fields = dict(_strings("type", "sessionId", "timestamp"), artifactCount=_integer)
        return (_shape(data, fields) or _shape(data, dict(fields, **_strings("frameUrl", "path", "title"))))
    if kind == "cost-state":
        usage = {key: _integer for key in ("cacheCreationInputTokens", "cacheReadInputTokens", "inputTokens",
                                          "outputTokens", "thinkingTokens", "webSearchRequests")}
        usage["costUSD"] = _number
        fields = dict(_strings("type", "sessionId"), hasUnknownModelCost=lambda value: type(value) is bool,
                      modelUsage=lambda value: _map_shape(value, usage), totalCostUSD=_number)
        fields.update({key: _integer for key in ("startTime", "totalAPIDuration", "totalAPIDurationWithoutRetries",
                      "totalDuration", "totalLinesAdded", "totalLinesRemoved", "totalToolDuration")})
        return _shape(data, fields)
    if kind == "worktree-state":
        core = _strings("originalCwd", "preEnterOriginalCwd", "sessionId", "worktreeName", "worktreePath")
        variants = [dict(core, enteredExisting=lambda value: type(value) is bool),
                    dict(core, enteredExisting=lambda value: type(value) is bool, **_strings("worktreeBranch")),
                    dict(core, **_strings("originalBranch", "originalHeadCommit", "worktreeBranch"))]
        return _shape(data, dict(_strings("type", "sessionId"), worktreeSession=lambda value:
                                value is None or any(_shape(value, fields) for fields in variants)))
    if kind == "file-history-delta":
        backup = dict(_strings("backupTime", "realParentDir"), version=_integer,
                      backupFileName=lambda value: value is None or isinstance(value, str))
        return _shape(data, dict(_strings("type", "messageId", "snapshotMessageId", "timestamp", "trackingPath"),
                                backup=lambda value: _shape(value, backup)))
    if kind in {"artifact-autoreact-ledger", "artifact-comment-monitor"}:
        if kind == "artifact-autoreact-ledger":
            item = dict(_strings("stampHighWater"), savedAt=_integer,
                        everBaselined=lambda value: type(value) is bool,
                        everHadThreads=lambda value: type(value) is bool,
                        threads=lambda value: value == [], turnTimestamps=lambda value: value == [])
            fields = _strings("type", "sessionId", "accountUuid")
        else:
            item = dict(_strings("title"), state=lambda value: value == "armed", writtenAtMs=_integer)
            fields = _strings("type", "sessionId")
        return _shape(data, dict(fields, v=_integer, artifacts=lambda value: _map_shape(value, item)))
    return False


def safe_record_type_label(value):
    """Expose only a short native record-type identifier in diagnostics."""
    if (not isinstance(value, str)
            or re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", value) is None
            or value.startswith(("sk-", "sk_", "ghp_", "github_pat_", "xox", "eyj"))):
        return "unrecognized"
    return value


def _visible(text):
    return isinstance(text, str) and bool(text.strip()) and not text.lstrip().startswith(_PRIVATE_PREFIXES)


def _safe_id(value):
    return value if isinstance(value, str) and 0 < len(value) <= 4096 else None


def _metadata(context, rows, header, state):
    values = dict(state.get("metadata", {}))
    values.update(host="claude")
    if getattr(context, "canonical_project_root", None) is not None:
        values["project_root"] = str(context.canonical_project_root)
    if getattr(context, "worktree", None) is not None:
        values.setdefault("worktree", str(context.worktree))
    expected = context.native_session_id
    identities = set()
    for raw in (header,) + tuple(rows):
        data = raw.data
        kind = data.get("type", data.get("role"))
        # Unknown schemas carry no trusted identity or provenance fields.
        # Read-only format probes pass a bare identity descriptor as the header.
        trusted_header = raw is header and set(data) in ({"sessionId"}, {"session_id"})
        understood = (isinstance(kind, str) and kind in ({"user", "assistant", "system", "developer"} | _IGNORED_ROWS))
        if not trusted_header and not understood and not _verified_metadata_row(data):
            continue
        sid = _safe_id(data.get("sessionId", data.get("session_id")))
        if sid:
            identities.add(sid)
        for field, target in (("cwd", "worktree"), ("version", "runtime_version"),
                              ("parentSessionId", "parent_session_id"), ("parentUuid", "parent_message_id")):
            value = _safe_id(data.get(field))
            if value:
                values[target] = value
                if field == "cwd":
                    values["recorded_worktree"] = value
        if data.get("isSidechain") is True:
            values["sidechain"] = True
    if values.get("native_session_id"):
        identities.add(values["native_session_id"])
    if len(identities) > 1 or (expected and identities and identities != {expected}):
        raise ValueError("Native Claude session identity does not match")
    if not identities:
        raise ValueError("Native Claude session identity is unavailable")
    values["native_session_id"] = next(iter(identities))
    return values


def _text_content(content):
    if isinstance(content, str):
        return content if _visible(content) else ""
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content
                         if isinstance(block, dict) and block.get("type") == "text" and _visible(block.get("text")))
    return ""


def parse_rows(context, rows, header, cursor_state):
    """Keep verified visible rows and private references to unknown schemas."""
    try:
        metadata = _metadata(context, rows, header, cursor_state)
    except ValueError:
        return ParsedRecords(status="unsupported", blocked_offset=rows[0].offset if rows else 0,
                             warnings=("Native Claude session identity is unavailable or conflicting",))
    tools = {key: value for key, value in cursor_state.get("tools", {}).items()
             if _safe_id(key) and _safe_id(value)}
    records = []
    deferred = []
    requires_proof = bool(cursor_state.get("requires_session_proof"))
    warnings = []
    seen = set()
    for raw in rows:
        if raw.unparsed_reason == "oversized":
            deferred.append(DeferredRow(raw.offset, raw.end_offset, "oversized:unrecognized"))
            warnings.append("Oversized Claude transcript record retained")
            requires_proof = True
            continue
        data = raw.data
        kind = data.get("type", data.get("role"))
        if not isinstance(kind, str):
            kind = None
        message_id = data.get("message", {}).get("id") if isinstance(data.get("message"), dict) else None
        uuid = _safe_id(data.get("uuid")) or _safe_id(message_id) or "offset:" + str(raw.offset)
        timestamp = _safe_id(data.get("timestamp"))
        turn_id = _safe_id(data.get("turnId", data.get("turn_id")))
        if kind in {"system", "developer"}:
            if kind == "system" and data.get("subtype") == "compact_boundary":
                records.append(SourceRecord(uuid, "system", "", raw.offset, timestamp, "compaction", turn_id=turn_id))
            continue
        if _verified_metadata_row(data):
            if kind == "relocated":
                metadata["relocated_cwd"] = data["relocatedCwd"]
            elif kind == "continued-in":
                metadata["continued_in_session_id"] = data["continuedInSessionId"]
            continue
        if kind in _IGNORED_ROWS:
            continue
        if kind not in {"user", "assistant"}:
            warnings.append("Unknown substantive Claude transcript record (type="
                            + safe_record_type_label(kind) + ")")
            deferred.append(DeferredRow(raw.offset, raw.end_offset, "unknown_schema:" + safe_record_type_label(kind)))
            requires_proof = True
            continue
        message = data.get("message", data)
        if not isinstance(message, dict) or message.get("phase") == "analysis":
            if isinstance(message, dict):
                continue
            return ParsedRecords(tuple(records), metadata, "partial" if records else "unsupported",
                                 ("Malformed Claude message",), raw.offset, {"metadata": metadata, "tools": tools, "requires_session_proof": requires_proof}, deferred_rows=tuple(deferred))
        content = message.get("content", "")
        if kind == "user" and (data.get("isMeta") is True or
                _text_content(content).lstrip().startswith(("<local-command", "<task-notification>",
                                                          "Base directory for this skill:"))):
            continue
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = content
        else:
            return ParsedRecords(tuple(records), metadata, "partial" if records else "unsupported",
                                 ("Malformed Claude content",), raw.offset, {"metadata": metadata, "tools": tools, "requires_session_proof": requires_proof}, deferred_rows=tuple(deferred))
        row_records = []
        row_tools = dict(tools)
        unsupported = False
        unknown_block = None
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                unsupported = True
                break
            block_type = block.get("type")
            if not isinstance(block_type, str):
                unsupported = True
                break
            if block_type in {"thinking", "redacted_thinking", "reasoning", "encrypted_reasoning",
                              "image", "document", "server_tool_use", "web_search_tool_result"}:
                continue
            source_id = uuid + ":" + str(index) + ":message"
            if block_type == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    unsupported = True
                    break
                if _visible(text):
                    row_records.append(SourceRecord(source_id, kind, text, raw.offset, timestamp, turn_id=turn_id))
            elif block_type == "tool_use":
                name = _safe_id(block.get("name"))
                call_id = _safe_id(block.get("id"))
                if not name or not call_id or not isinstance(block.get("input", {}), dict):
                    unsupported = True
                    break
                row_tools[call_id] = name
                # Preserve native structured input as evidence; never parse it as a file edit.
                text = json.dumps(block.get("input", {}), sort_keys=True, ensure_ascii=False)
                row_records.append(SourceRecord(call_id + ":call", "assistant", text, raw.offset, timestamp,
                                                "tool_call", name, _CATEGORIES.get(name, "unknown"), turn_id))
            elif block_type == "tool_result":
                call_id = _safe_id(block.get("tool_use_id"))
                if not call_id:
                    unsupported = True
                    break
                name = row_tools.get(call_id)
                text = _text_content(block.get("content", ""))
                row_records.append(SourceRecord(call_id + ":result", "tool", text, raw.offset, timestamp,
                                                "tool_result", name, _CATEGORIES.get(name, "unknown"), turn_id))
            else:
                unsupported = True
                unknown_block = safe_record_type_label(block_type)
                break
        if unknown_block is not None:
            warnings.append("Unknown substantive Claude transcript record (type=" + unknown_block + ")")
            deferred.append(DeferredRow(raw.offset, raw.end_offset, "unknown_schema:" + unknown_block))
            requires_proof = True
            continue
        if unsupported:
            return ParsedRecords(tuple(records), metadata, "partial" if records else "unsupported",
                                 ("Unknown substantive Claude content block",), raw.offset,
                                 {"metadata": metadata, "tools": tools, "requires_session_proof": requires_proof}, deferred_rows=tuple(deferred))
        if requires_proof:
            sid = _safe_id(data.get("sessionId", data.get("session_id")))
            if sid != context.native_session_id:
                deferred.append(DeferredRow(raw.offset, raw.end_offset, "ambiguous_ownership"))
                if "Ambiguous Claude message ownership retained" not in warnings:
                    warnings.append("Ambiguous Claude message ownership retained")
                continue
            requires_proof = False
        tools = row_tools
        for record in row_records:
            if record.source_id not in seen:
                records.append(record)
                seen.add(record.source_id)
    # Bound state to stable metadata/IDs/native tool names, never raw content.
    tools = dict(list(tools.items())[-1024:])
    return ParsedRecords(tuple(records), metadata, status="partial" if deferred else "ok", warnings=tuple(warnings), parser_state={"metadata": metadata, "tools": tools, "requires_session_proof": requires_proof}, deferred_rows=tuple(deferred))


def read_records(context, cursor, deadline):
    return read_source(context, cursor, deadline, parse_rows)
