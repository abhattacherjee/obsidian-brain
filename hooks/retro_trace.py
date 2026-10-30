"""Parse and atomically record outcomes for retro Process Improvements."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

from frontmatter import split_frontmatter, split_lines_lf_crlf
from note_writer import (
    _acquire_lock,
    _atomic_rewrite,
    _detect_line_ending,
    _release_lock,
    _resolve_note_path,
    _split_flow_items,
    _unquote_tag,
)

_REF = r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([1-9][0-9]*)"
_SUFFIX = r'(?: \((?P<note>[^)\r\n]*)\))?(?:, "[^"\r\n]*")?\.?\s*'
_ISSUE = re.compile(
    r"\[" + _REF + r"\]\((https://github\.com/\1/\2/issues/\3)\)" + _SUFFIX
)
_MEMORY = re.compile(r"memory `(?P<stem>[A-Za-z0-9_.-]+)`" + _SUFFIX)
_PATH = re.compile(r"`(?P<path>[^`\r\n]+)`" + _SUFFIX)
_ITEM = re.compile(r"^- \[([ xX])\] ")
_TRACKED = re.compile(r"^tracked_issues\s*:")
GH_TIMEOUT_SECONDS = 10


class TraceError(ValueError):
    """Invalid trace input or note; no note content has been written."""


def parse_trace(line: str) -> dict | None:
    """Recognize an end-anchored trace after the last spaced arrow."""
    if " → " not in line:
        return None
    tail = line.rsplit(" → ", 1)[1]
    match = _ISSUE.fullmatch(tail)
    if match:
        owner, repo, number, url = match.group(1, 2, 3, 4)
        return {"kind": "issue", "ref": f"{owner}/{repo}#{number}", "url": url}
    match = _MEMORY.fullmatch(tail)
    if match:
        return {"kind": "memory", "stem": match["stem"], "note": match["note"]}
    match = _PATH.fullmatch(tail)
    if match:
        path = match["path"]
        if "/" in path or "~" in path or re.search(r"\.[A-Za-z0-9]+\Z", path):
            return {"kind": "path", "path": path}
    if tail.startswith("skipped: "):
        reason = tail[len("skipped: "):].strip()
        if reason:
            return {"kind": "skipped", "reason": reason}
    return None


def parse_items(text: str) -> list[dict]:
    """Number top-level checkbox items in the Process Improvements section."""
    lines = [line.rstrip("\r\n") for line in split_lines_lf_crlf(text)]
    items = []
    inside = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if not inside:
            if line.startswith("## Process Improvements"):
                inside = True
            i += 1
            continue
        if line.startswith("## "):
            break
        match = _ITEM.match(line)
        if not match:
            i += 1
            continue
        end = i
        while end + 1 < len(lines):
            following = lines[end + 1]
            if not following.strip() or not following.startswith((" ", "\t")):
                break
            end += 1
        items.append({
            "item": len(items) + 1, "start": i, "end": end,
            "checked": match[1] != " ", "text": "\n".join(lines[i:end + 1]),
            "trace": parse_trace(lines[end]),
        })
        i = end + 1
    return items


def read_tracked_issues(text: str) -> list[str]:
    """Read a flow or indented block list from valid frontmatter."""
    _, fm, _, _, error = split_frontmatter(split_lines_lf_crlf(text))
    if error:
        return []
    for i, line in enumerate(fm):
        match = _TRACKED.match(line)
        if not match:
            continue
        value = line[match.end():].strip()
        if value.startswith("[") and value.endswith("]"):
            return [_unquote_tag(raw) for raw in _split_flow_items(value[1:-1])]
        refs = []
        if not value:
            for continuation in fm[i + 1:]:
                if not continuation.startswith((" ", "\t")):
                    break
                item = re.match(r"^[ \t]+-[ \t]+(.*?)\s*$", continuation)
                if item:
                    refs.append(_unquote_tag(item[1]))
        return refs
    return []


def _read_note(path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _run_gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args], stdin=subprocess.DEVNULL, capture_output=True,
        text=True, timeout=GH_TIMEOUT_SECONDS,
    )


def closed_candidates(vault_path: str, insights_folder: str,
                      project: str | None = None, gh=None) -> dict:
    """Suggest ticks for open items whose tracked issue is closed on GitHub."""
    gh = _run_gh if gh is None else gh
    candidates = []
    errors = []
    states = {}
    vault = Path(vault_path).resolve()
    folder = Path(vault_path, insights_folder).resolve()
    if not folder.is_relative_to(vault):
        return {"candidates": [], "errors": []}
    for path in sorted(folder.glob("*-retro-*.md")):
        try:
            if not path.resolve().is_relative_to(vault):
                continue
            text = _read_note(path)
        except (OSError, UnicodeError):
            continue
        _, fm, _, _, error = split_frontmatter(split_lines_lf_crlf(text))
        if error:
            continue
        if project is not None:
            note_project = None
            for line in fm:
                match = re.match(r"^project\s*:\s*(.*?)\s*$", line)
                if match:
                    note_project = _unquote_tag(match[1]).strip()
                    break
            if note_project != project:
                continue
        tracked = read_tracked_issues(text)
        if not tracked:
            continue
        for item in parse_items(text):
            trace = item["trace"]
            if item["checked"] or trace is None or trace["kind"] != "issue":
                continue
            ref = trace["ref"]
            if ref not in tracked:
                continue
            if ref not in states:
                # Cache failures too, so each ref produces at most one error.
                states[ref] = None
                repo, number = ref.rsplit("#", 1)
                try:
                    result = gh(["issue", "view", number, "--repo", repo,
                                 "--json", "state,stateReason"])
                    if result.returncode:
                        errors.append({"ref": ref, "error": f"gh exited with code {result.returncode}"})
                        continue
                    data = json.loads(result.stdout)
                    if not isinstance(data, dict) or not isinstance(data.get("state"), str):
                        errors.append({"ref": ref, "error": "gh JSON has no string state"})
                        continue
                    states[ref] = data
                except subprocess.TimeoutExpired:
                    errors.append({"ref": ref, "error": "gh timed out"})
                except OSError as exc:
                    errors.append({"ref": ref, "error": str(exc)[:200]})
                except json.JSONDecodeError:
                    errors.append({"ref": ref, "error": "gh returned invalid JSON"})
            data = states[ref]
            if data is None or data["state"].upper() != "CLOSED":
                continue
            reason = data.get("stateReason") or ""
            preselect = isinstance(reason, str) and reason.upper() == "COMPLETED"
            candidates.append({
                "file": str(path.resolve()), "line": item["start"] + 1,
                "text": item["text"].split("\n", 1)[0], "ref": ref,
                "state_reason": reason, "preselect": preselect,
                "label": "[closed on GH]" if preselect else "[closed on GH: not planned]",
            })
    return {"candidates": sorted(candidates, key=lambda c: (c["file"], c["line"])),
            "errors": sorted(errors, key=lambda e: e["ref"])}


def untraced_count(path) -> int | None:
    """Count unchecked outcomes (including checked boxes); None if unreadable."""
    try:
        text = _read_note(path)
    except (OSError, UnicodeError):
        return None
    if split_frontmatter(split_lines_lf_crlf(text))[4]:
        return None
    return sum(item["trace"] is None for item in parse_items(text))


def _value(entry: dict, key: str, *, backtick: bool = False, allow_empty: bool = False) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise TraceError(f"{key} must be a non-empty string")
    if len(value) > 300 or any(c in value for c in ("\n", "\r", "→")):
        raise TraceError(f"invalid {key}: over 300 characters or contains a newline/arrow")
    if backtick and "`" in value:
        raise TraceError(f"invalid backtick in {key}")
    return value


def _render(entry: dict) -> str:
    kind = entry.get("kind")
    if kind == "issue":
        ref = _value(entry, "ref")
        match = re.fullmatch(_REF, ref)
        if not match:
            raise TraceError("invalid issue ref")
        owner, repo, number = match.groups()
        rendered = f"[{ref}](https://github.com/{owner}/{repo}/issues/{number})"
        expected = {"kind": kind, "ref": ref, "url": f"https://github.com/{owner}/{repo}/issues/{number}"}
    elif kind == "memory":
        stem = _value(entry, "stem", backtick=True)
        note = _value(entry, "note", allow_empty=True) if "note" in entry else None
        rendered = f"memory `{stem}`" + (f" ({note})" if note is not None else "")
        expected = {"kind": kind, "stem": stem, "note": note}
    elif kind == "path":
        path = _value(entry, "path", backtick=True)
        rendered = f"`{path}`"
        expected = {"kind": kind, "path": path}
    elif kind == "skipped":
        reason = _value(entry, "reason")
        rendered = f"skipped: {reason}"
        expected = {"kind": kind, "reason": reason.strip()}
    else:
        raise TraceError(f"unknown trace kind: {kind!r}")
    # This must be a runtime check, including under python -O.
    if parse_trace(" → " + rendered) != expected:
        raise TraceError("rendered trace does not round-trip through the trace grammar")
    return rendered


def _merge_field(fm: list[str], refs: list[str], eol: str) -> list[str]:
    """Replace the key and its continuations in place, collapsing duplicates."""
    new_line = f"tracked_issues: {json.dumps(refs)}{eol}"
    out = []
    placed = False
    i = 0
    while i < len(fm):
        if not _TRACKED.match(fm[i]):
            out.append(fm[i])
            i += 1
            continue
        if not placed:
            out.append(new_line)
            placed = True
        i += 1
        while i < len(fm) and fm[i].startswith((" ", "\t")):
            i += 1
    if not placed:
        out.append(new_line)
    return out


def apply_traces(vault_path: str, note_path: str, traces: list[dict]) -> dict:
    """Validate every outcome before rewriting a note under its writer lock."""
    if not isinstance(traces, list):
        raise TraceError("traces must be a list")
    path = Path(note_path)
    resolved, error = _resolve_note_path(
        vault_path, str(path if path.is_absolute() else Path(vault_path) / path)
    )
    if error:
        raise TraceError(error)
    lock, error = _acquire_lock(resolved)
    if error:
        raise OSError(error)
    try:
        with open(resolved, "r", encoding="utf-8", newline="") as fh:
            st = os.fstat(fh.fileno())
            original = fh.read()
        lines = split_lines_lf_crlf(original)
        opening, fm, closing, _, error = split_frontmatter(lines)
        if error:
            raise TraceError(error)
        items = parse_items(original)
        by_number = {item["item"]: item for item in items}
        validated = {}
        for entry in traces:
            if not isinstance(entry, dict) or type(entry.get("item")) is not int:
                raise TraceError("each trace needs an integer item number")
            number = entry["item"]
            if number not in by_number:
                raise TraceError(f"unknown item: {number}")
            if by_number[number]["trace"] is not None:
                raise TraceError(f"item {number} is already traced")
            if number in validated:
                raise TraceError(f"duplicate item: {number}")
            validated[number] = (entry, _render(entry))
        if set(validated) != {item["item"] for item in items if item["trace"] is None}:
            raise TraceError("missing trace for an untraced item")
        refs = list(dict.fromkeys(read_tracked_issues(original)))
        for number, (entry, rendered) in validated.items():
            item = by_number[number]
            end = item["end"]
            last = lines[end]
            bare = last.rstrip("\r\n")
            lines[end] = bare + " → " + rendered + last[len(bare):]
            if entry["kind"] != "issue":
                start = item["start"]
                if lines[start].startswith("- [ ] "):
                    lines[start] = "- [x] " + lines[start][6:]
            elif entry["ref"] not in refs:
                refs.append(entry["ref"])
        # An empty apply is a true no-op; do not insert an empty field.
        if validated:
            new_fm = _merge_field(fm, refs, _detect_line_ending(original))
            content = opening + "".join(new_fm) + closing + "".join(lines[len(fm) + 2:])
        else:
            content = original
        result = {"written": content != original, "untraced": 0, "tracked_issues": refs}
        if content == original:
            return result
        error = _atomic_rewrite(resolved, content, (st.st_mtime_ns, st.st_size))
        if error:
            raise OSError(error)
        return result
    finally:
        _release_lock(lock)


def main(argv=None) -> int:
    """CLI for retro traces; JSON input is read only for apply."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or not (
        (args[0] == "apply" and len(args) == 3)
        or (args[0] in ("untraced", "items") and len(args) == 2)
        or (args[0] == "closed-candidates" and (
            len(args) == 3 or (len(args) == 5 and args[3] == "--project")))
    ):
        print("usage: retro_trace.py apply <vault> <note> | untraced <note> | items <note>"
              " | closed-candidates <vault> <insights_folder> [--project P]", file=sys.stderr)
        return 2
    try:
        if args[0] == "apply":
            payload = json.loads(sys.stdin.read(1_000_000))
            if not isinstance(payload, dict) or "traces" not in payload:
                raise TraceError('expected JSON object with "traces" list')
            print(json.dumps(apply_traces(args[1], args[2], payload["traces"])))
        elif args[0] == "closed-candidates":
            print(json.dumps(closed_candidates(
                args[1], args[2], project=args[4] if len(args) == 5 else None)))
        elif args[0] == "untraced":
            count = untraced_count(args[1])
            print("unreadable" if count is None else count)
            return 1 if count is None else 0
        else:
            items = parse_items(_read_note(args[1]))
            print(json.dumps([
                {"item": item["item"], "line": item["start"] + 1,
                 "checked": item["checked"], "text": item["text"], "trace": item["trace"]}
                for item in items
            ]))
    except (TraceError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, UnicodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
