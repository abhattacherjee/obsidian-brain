"""Pure grouping, identity, tracking and ranking for recurring retro lessons."""

from __future__ import annotations

from datetime import date, timedelta
from hashlib import sha1


def star_lessons(
    keys: list[str], edges: set[tuple[str, str]], note_of: dict[str, str]
) -> list[dict]:
    """Greedily form disjoint stars, keeping only stars spanning two notes."""
    remaining = set(keys)
    neighbours = {key: set() for key in remaining}
    for a, b in edges:
        if a != b and a in remaining and b in remaining:
            neighbours[a].add(b)
            neighbours[b].add(a)
    lessons = []
    while remaining:
        centre = min(remaining, key=lambda k: (-len(neighbours[k] & remaining), k))
        adjacent = neighbours[centre] & remaining
        if not adjacent:
            break
        members = sorted(adjacent | {centre})
        remaining.difference_update(members)
        if len({note_of[m] for m in members}) >= 2:
            lessons.append({"centre": centre, "members": members})
    return lessons


def assign_ids(
    lessons: list[dict], old: dict[str, list[str]], retired: dict
) -> tuple[list[dict], dict]:
    """Reuse ids at half overlap; record merges and splits deterministically."""
    old_members = {lesson_id: set(members) for lesson_id, members in old.items()}
    taken = set()
    assigned = []
    new_members = []
    for lesson in lessons:
        result = dict(lesson)
        members = set(lesson["members"])
        overlaps = {lesson_id: len(members & ms) for lesson_id, ms in old_members.items()}
        best = min(overlaps, key=lambda i: (-overlaps[i], i)) if overlaps else None
        if best is not None and 2 * overlaps[best] >= min(len(members), len(old_members[best])):
            if best not in taken:
                result["id"] = best
                taken.add(best)
            else:
                result["id"] = "L" + sha1(lesson["centre"].encode()).hexdigest()[:8]
                result["split_from"] = best
        else:
            result["id"] = "L" + sha1(lesson["centre"].encode()).hexdigest()[:8]
        assigned.append(result)
        new_members.append(members)

    new_retired = dict(retired)
    for lesson_id, members in old_members.items():
        if lesson_id in taken:
            continue
        # Creation order breaks equal overlaps between new lessons.
        overlaps = [len(members & ms) for ms in new_members]
        if overlaps and max(overlaps) > 0:
            target = assigned[overlaps.index(max(overlaps))]["id"]
            new_retired[lesson_id] = {"merged_into": target}
        else:
            new_retired[lesson_id] = {"gone": True}
    return assigned, new_retired


def lesson_tracking(members: list[str], items: dict[str, dict]) -> dict | None:
    """Choose the most frequent member trace, then kind, earliest date and ref."""
    fields = {"issue": "ref", "memory": "stem", "path": "path"}
    kind_order = {"issue": 0, "path": 1, "memory": 2}
    traces = {}
    for member in members:
        item = items[member]
        trace = item["trace"]
        if trace is None or trace["kind"] not in fields:
            continue
        kind = trace["kind"]
        key = (kind, trace[fields[kind]])
        traces.setdefault(key, []).append(item["date"])
    if not traces:
        return None
    kind, ref = min(
        traces,
        key=lambda t: (-len(traces[t]), kind_order[t[0]], min(traces[t]), t[1]),
    )
    return {"kind": kind, "ref": ref, "since": min(traces[(kind, ref)])}


def classify(
    lesson: dict,
    items: dict[str, dict],
    tracking: dict | None,
    info: dict | None,
    today: str,
    recent_days: int,
) -> dict:
    """Count note sessions and classify recurrence against the tracked action."""
    note_dates = {}
    projects = set()
    for member in lesson["members"]:
        item = items[member]
        note = item["note"]
        note_dates[note] = max(note_dates.get(note, item["date"]), item["date"])
        projects.add(item["project"])
    dates = list(note_dates.values())
    cutoff = (date.fromisoformat(today) - timedelta(days=recent_days)).isoformat()
    recent_sessions = sum(d >= cutoff for d in dates)
    since = tracking.get("since") if tracking is not None else None
    if tracking is not None and tracking["kind"] == "issue" and info is not None:
        since = info.get("created") or since
    after = sum(d > since for d in dates) if tracking is not None and since is not None else None
    tracking_gone = tracking is not None and info is not None and info.get("exists") is False
    if tracking is None or tracking_gone:
        bucket = "file"
    elif tracking["kind"] == "issue":
        if info is not None and info.get("state") == "CLOSED":
            closed = info.get("closed")
            bucket = "didnt_hold" if closed is not None and any(d > closed for d in dates) else "quiet"
        else:
            bucket = "raise" if after is not None and after > 0 else "quiet"
    else:
        bucket = "rule_not_followed" if after is not None and after > 0 else "quiet"
    return {
        "sessions": len(dates),
        "recent_sessions": recent_sessions,
        "projects": sorted(projects),
        "first_seen": min(dates) if dates else None,
        "last_seen": max(dates) if dates else None,
        "sessions_after_tracking": after,
        "bucket": bucket,
        "dormant": recent_sessions == 0,
        "tracking_gone": tracking_gone,
    }


def rank_key(lesson_id: str, c: dict) -> tuple:
    """Sort by recent sessions, total sessions, projects, recency, then id."""
    last_seen = tuple(-int(part) for part in c["last_seen"].split("-"))
    return (-c["recent_sessions"], -c["sessions"], -len(c["projects"]), last_seen, lesson_id)
