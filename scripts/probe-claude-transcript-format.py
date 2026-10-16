#!/usr/bin/env python3
"""Read an explicitly selected local corpus; report aggregate format drift only."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import stat
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
from transcripts import RawRecord
from transcripts.claude import parse_rows, safe_record_type_label
from transcripts.codex import parse_rows as parse_codex_rows


def _count(counter, label):
    # Bound diagnostic cardinality even for adversarial type names.
    if label not in counter and len(counter) >= 128:
        label = "unrecognized"
    counter[label] += 1


def inspect_row(data, counts):
    if not isinstance(data, dict):
        counts["unsupported_schema"] += 1
        return
    kind = data.get("type", data.get("role"))
    label = safe_record_type_label(kind)
    sid = data.get("sessionId", data.get("session_id", "probe"))
    if not isinstance(sid, str) or not sid:
        counts["unsupported_schema"] += 1
        _count(counts["unknown_records"], label)
        return
    context = SimpleNamespace(native_session_id=sid, canonical_project_root=None, worktree=None)
    header = RawRecord(0, 1, {"sessionId": sid})
    parsed = parse_rows(context, [RawRecord(1, 2, data)], header, {})
    if parsed.status == "ok":
        _count(counts["known_records"], label)
    elif kind in {"user", "assistant"}:
        _count(counts["known_records"], label)
        counts["unsupported_schema"] += 1
    else:
        _count(counts["unknown_records"], label)
        counts["unsupported_schema"] += 1
    message = data.get("message", data)
    content = message.get("content") if isinstance(message, dict) else None
    if kind not in {"user", "assistant"} or not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            _count(counts["unknown_blocks"], "unrecognized")
            continue
        block_label = safe_record_type_label(block.get("type"))
        trial = dict(data)
        trial["message"] = {"content": [block]}
        check = parse_rows(context, [RawRecord(1, 2, trial)], header, {})
        target = "known_blocks" if check.status == "ok" else "unknown_blocks"
        _count(counts[target], block_label)



def inspect_codex_row(data, counts, source, offset, end_offset):
    """Use the selected file's native header, never a guessed session identity."""
    if not isinstance(data, dict):
        counts["unsupported_schema"] += 1
        return
    payload = data.get("payload")
    parts = [safe_record_type_label(data.get("type"))]
    if isinstance(payload, dict) and isinstance(payload.get("type"), str):
        parts.append(safe_record_type_label(payload["type"]))
        item = payload.get("item")
        if payload["type"] == "item_completed" and isinstance(item, dict):
            kind = item.get("type")
            parts.append(safe_record_type_label(kind.lower() if isinstance(kind, str) else kind))
    label = "/".join(parts)
    raw = RawRecord(offset, end_offset, data)
    if "header" not in source:
        sid = payload.get("id", payload.get("session_id")) if isinstance(payload, dict) else None
        if data.get("type") != "session_meta" or not isinstance(sid, str) or not sid:
            counts["unsupported_schema"] += 1
            _count(counts["unknown_records"], label)
            return
        source["header"] = raw
        source["context"] = SimpleNamespace(native_session_id=sid)
    parsed = parse_codex_rows(source["context"], [raw], source["header"], source.get("state", {}))
    if parsed.blocked_offset is None:
        source["state"] = parsed.parser_state
    if parsed.status == "ok":
        _count(counts["known_records"], label)
    else:
        counts["unsupported_schema"] += 1
        _count(counts["unknown_records"], label)


def scan(source_root, *, max_files=1000, max_entries=20000, max_bytes=67108864,
         max_line_bytes=1048576, seconds=10.0, host="claude"):
    if host not in {"claude", "codex"}:
        raise ValueError("explicit supported host required")
    counts = {name: Counter() for name in ("known_records", "unknown_records", "known_blocks", "unknown_blocks")}
    counts.update(scanned=0, skipped=0, truncated=0, malformed=0, unsupported_schema=0,
                  read_failures=0, entries=0, bytes_read=0, scan_complete=True)
    deadline = time.monotonic() + seconds
    flags = os.O_RDONLY | os.O_NOFOLLOW
    directory_flags = flags | os.O_DIRECTORY
    def stop():
        counts["truncated"] += 1
        counts["scan_complete"] = False
    def walk(fd, depth=0):
        if depth >= 64:
            stop()
            return False
        with os.scandir(fd) as entries:
            for entry in entries:
                if counts["entries"] >= max_entries or time.monotonic() >= deadline:
                    stop()
                    return False
                counts["entries"] += 1
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        counts["skipped"] += 1
                        counts["scan_complete"] = False
                    elif stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, directory_flags, dir_fd=fd)
                        try:
                            if not walk(child, depth + 1):
                                return False
                        finally:
                            os.close(child)
                    elif entry.name.endswith(".jsonl"):
                        if not stat.S_ISREG(info.st_mode):
                            counts["skipped"] += 1
                            counts["scan_complete"] = False
                            continue
                        if counts["scanned"] >= max_files:
                            stop()
                            return False
                        child = os.open(entry.name, flags | os.O_NONBLOCK, dir_fd=fd)
                        with os.fdopen(child, "rb") as stream:
                            opened = os.fstat(stream.fileno())
                            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                                counts["read_failures"] += 1
                                counts["scan_complete"] = False
                                continue
                            counts["scanned"] += 1
                            source = {}
                            file_offset = 0
                            while True:
                                remaining = max_bytes - counts["bytes_read"]
                                if remaining <= 0 or time.monotonic() >= deadline:
                                    stop()
                                    return False
                                line = stream.readline(min(max_line_bytes + 1, remaining + 1))
                                counts["bytes_read"] += len(line)
                                if not line:
                                    break
                                if len(line) > max_line_bytes or counts["bytes_read"] > max_bytes:
                                    stop()
                                    return False
                                if not line.endswith(b"\n"):
                                    counts["malformed"] += 1
                                    counts["scan_complete"] = False
                                    break
                                try:
                                    data = json.loads(line)
                                    if host == "codex":
                                        inspect_codex_row(data, counts, source, file_offset, file_offset + len(line))
                                    else:
                                        inspect_row(data, counts)
                                    file_offset += len(line)
                                except (ValueError, UnicodeError, TypeError, RecursionError):
                                    counts["malformed"] += 1
                except OSError:
                    counts["read_failures"] += 1
                    counts["scan_complete"] = False
        return True
    try:
        # Reject symlinked ancestors as well as the selected root itself.
        path = Path(source_root)
        if not path.is_absolute():
            raise ValueError("absolute root required")
        fd = os.open(path.anchor, directory_flags)
        try:
            for part in path.parts[1:]:
                if part in {".", ".."}:
                    raise ValueError("canonical root required")
                child = os.open(part, directory_flags, dir_fd=fd)
                os.close(fd)
                fd = child
            walk(fd)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        counts["read_failures"] += 1
        counts["scan_complete"] = False
    if counts["malformed"] or counts["unsupported_schema"]:
        counts["scan_complete"] = False
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--host", choices=("claude", "codex"), default="claude")
    for name, default in (("max-files", 1000), ("max-entries", 20000), ("max-bytes", 67108864), ("max-line-bytes", 1048576)):
        parser.add_argument("--" + name, type=int, default=default)
    parser.add_argument("--seconds", type=float, default=10.0)
    args = parser.parse_args(argv)
    if any(value <= 0 for value in (args.max_files, args.max_entries, args.max_bytes, args.max_line_bytes, args.seconds)) or not args.seconds < float("inf"):
        parser.error("bounds must be positive and finite")
    result = scan(args.source_root, max_files=args.max_files, max_entries=args.max_entries,
                  max_bytes=args.max_bytes, max_line_bytes=args.max_line_bytes, seconds=args.seconds, host=args.host)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["scan_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
