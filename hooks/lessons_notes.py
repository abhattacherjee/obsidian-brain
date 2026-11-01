"""Deterministic lesson notes and text-free retro rollup reports."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re

import obsidian_utils


def note_filename(lesson: dict) -> str:
    """Return a bounded ASCII slug followed by the stable lesson id."""
    slug = re.sub(r"[^a-z0-9]+", "-", lesson["name"].lower()).strip("-")
    slug = slug[:50].strip("-") or "lesson"
    return f"{slug}-{lesson['id']}.md"


def _tracking_fields(c: dict, tracking: dict | None, info: dict | None) -> dict:
    issue = tracking is not None and tracking["kind"] == "issue"
    issue_info = (info or {}) if issue else {}
    kind = tracking["kind"] if tracking else "none"
    return {
        "tracking": "gone" if c.get("tracking_gone") else (
            "rule" if kind == "path" else kind
        ),
        "tracking_ref": tracking["ref"] if tracking else None,
        "tracking_since": issue_info.get("created") or (
            tracking["since"] if tracking else None
        ),
        "issue_state": (
            issue_info["state"].lower() if issue_info.get("state") is not None else None
        ),
        "issue_milestone": issue_info.get("milestone"),
        "issue_priority": issue_info.get("priority"),
    }


def _one_line(text: str) -> str:
    return " ".join(text.splitlines())


def render_note(
    lesson: dict, c: dict, tracking: dict | None, info: dict | None,
    items: dict, snoozed_until: str | None,
) -> str:
    """Render JSON-valued frontmatter and escaped, chronologically sorted items."""
    fields = {
        "type": "claude-lesson",
        "lesson_id": lesson["id"],
        "name": lesson["name"],
        **{key: c[key] for key in (
            "sessions", "recent_sessions", "projects", "first_seen", "last_seen"
        )},
        **_tracking_fields(c, tracking, info),
        **{key: c[key] for key in ("sessions_after_tracking", "bucket", "dormant")},
        "snoozed_until": snoozed_until,
    }
    if "split_from" in lesson:
        fields["split_from"] = lesson["split_from"]
    fields["tags"] = ["claude/lesson"] + ["claude/project/" + p for p in c["projects"]]
    lines = ["---", *(f"{key}: {json.dumps(value)}" for key, value in fields.items()),
             "---", "", "# " + _one_line(lesson["name"]), ""]
    for key in sorted(lesson["members"], key=lambda k: (items[k]["date"], k)):
        item = items[key]
        text = _one_line(item["text"]).replace("[[", "[ [").replace("]]", "] ]")
        if text.startswith(("---", "#")):
            text = "\\" + text
        lines.append(f"- {text} · [[{item['note']}]] · {item['project']}")
    return "\n".join(lines).rstrip("\n") + "\n"


def read_snooze(path) -> str | None:
    """Read a JSON string snooze from frontmatter; unavailable values are absent."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    if not lines or lines[0] != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if separator and key == "snoozed_until":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]  # rendered notes quote it; Obsidian's date editor does not
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return None
            try:
                date.fromisoformat(value)
            except ValueError:
                return None
            return value
    return None


def write_if_changed(vault_path: str, folder: str, filename: str, content: str) -> bool:
    """Preserve unchanged notes and delegate changed notes to the atomic writer."""
    dest = Path(vault_path) / folder / filename
    if not dest.resolve().is_relative_to(Path(vault_path).resolve()):
        raise OSError(f"path traversal blocked: {dest}")
    try:
        if dest.read_bytes() == content.encode("utf-8"):
            return False
    except OSError:
        pass
    error = obsidian_utils.write_vault_note(vault_path, folder, filename, content)
    if error is not None:
        raise OSError(error)
    return True


def report(state: dict, snoozes: dict[str, str | None] | None = None) -> dict:
    """Project state into a ranked report without exposing item text.

    ``snoozes`` maps lesson id to the ``snoozed_until`` read live from its note.
    """
    snoozes = snoozes or {}
    entries = []
    for lesson_id, lesson in state["lessons"].items():
        c = lesson["classify"]
        tracking_fields = _tracking_fields(c, lesson["tracking"], lesson["info"])
        del tracking_fields["tracking_since"]
        entries.append({
            "id": lesson_id,
            "name": lesson["name"],
            **{key: c[key] for key in (
                "bucket", "dormant", "sessions", "recent_sessions", "projects",
                "first_seen", "last_seen",
            )},
            **tracking_fields,
            "sessions_after_tracking": c["sessions_after_tracking"],
            "snoozed_until": snoozes.get(lesson_id),
            "members": [{"note": key.split(":")[0], "item_key": key}
                        for key in lesson["members"]],
        })
    bucket_order = {bucket: i for i, bucket in enumerate(
        ("raise", "file", "didnt_hold", "rule_not_followed", "quiet")
    )}
    entries.sort(key=lambda e: (
        bucket_order[e["bucket"]], -e["recent_sessions"], -e["sessions"],
        -len(e["projects"]), -date.fromisoformat(e["last_seen"]).toordinal(), e["id"],
    ))
    return {"generated": state["generated"], "totals": state["totals"], "lessons": entries}
