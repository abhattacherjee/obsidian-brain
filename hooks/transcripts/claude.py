"""Normalize visible Claude records without exposing runtime instructions."""
import json

from . import ParsedRecords, SourceRecord, read_source


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
        sid = _safe_id(data.get("sessionId", data.get("session_id")))
        if sid:
            identities.add(sid)
        for field, target in (("cwd", "worktree"), ("version", "runtime_version"),
                              ("parentSessionId", "parent_session_id"), ("parentUuid", "parent_message_id")):
            value = _safe_id(data.get(field))
            if value:
                values[target] = value
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
    """Normalize native or legacy flat rows; unknown substantive layouts block."""
    try:
        metadata = _metadata(context, rows, header, cursor_state)
    except ValueError:
        return ParsedRecords(status="unsupported", blocked_offset=rows[0].offset if rows else 0,
                             warnings=("Native Claude session identity is unavailable or conflicting",))
    tools = {key: value for key, value in cursor_state.get("tools", {}).items()
             if _safe_id(key) and _safe_id(value)}
    records = []
    warnings = []
    seen = set()
    for raw in rows:
        data = raw.data
        kind = data.get("type", data.get("role"))
        message_id = data.get("message", {}).get("id") if isinstance(data.get("message"), dict) else None
        uuid = _safe_id(data.get("uuid")) or _safe_id(message_id) or "offset:" + str(raw.offset)
        timestamp = _safe_id(data.get("timestamp"))
        turn_id = _safe_id(data.get("turnId", data.get("turn_id")))
        if kind in {"system", "developer"}:
            if kind == "system" and data.get("subtype") == "compact_boundary":
                records.append(SourceRecord(uuid, "system", "", raw.offset, timestamp, "compaction", turn_id=turn_id))
            continue
        if kind in _IGNORED_ROWS:
            continue
        if kind not in {"user", "assistant"}:
            warnings.append("Unknown substantive Claude transcript record")
            return ParsedRecords(tuple(records), metadata, "partial" if records else "unsupported",
                                 tuple(warnings), raw.offset, {"metadata": metadata, "tools": tools})
        message = data.get("message", data)
        if not isinstance(message, dict) or message.get("phase") == "analysis":
            if isinstance(message, dict):
                continue
            return ParsedRecords(tuple(records), metadata, "partial" if records else "unsupported",
                                 ("Malformed Claude message",), raw.offset, {"metadata": metadata, "tools": tools})
        content = message.get("content", "")
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = content
        else:
            return ParsedRecords(tuple(records), metadata, "partial" if records else "unsupported",
                                 ("Malformed Claude content",), raw.offset, {"metadata": metadata, "tools": tools})
        row_records = []
        row_tools = dict(tools)
        unsupported = False
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                unsupported = True
                break
            block_type = block.get("type")
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
                break
        if unsupported:
            return ParsedRecords(tuple(records), metadata, "partial" if records else "unsupported",
                                 ("Unknown substantive Claude content block",), raw.offset,
                                 {"metadata": metadata, "tools": tools})
        tools = row_tools
        for record in row_records:
            if record.source_id not in seen:
                records.append(record)
                seen.add(record.source_id)
    # Bound state to stable metadata/IDs/native tool names, never raw content.
    tools = dict(list(tools.items())[-1024:])
    return ParsedRecords(tuple(records), metadata, parser_state={"metadata": metadata, "tools": tools})


def read_records(context, cursor, deadline):
    return read_source(context, cursor, deadline, parse_rows)
