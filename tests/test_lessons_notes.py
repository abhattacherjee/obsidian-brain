"""Lesson rendering, snooze persistence, and report contracts."""
from __future__ import annotations

import json

import pytest

import lessons_notes as notes


@pytest.fixture
def sample():
    lesson = {"id": "L123", "name": 'Use "x" #1: now', "centre": "b:2",
              "members": ["b:2", "a:1", "a:0"], "split_from": "Lold"}
    c = {"sessions": 2, "recent_sessions": 1, "projects": ['a: "b"', "demo"],
         "first_seen": "2026-10-01", "last_seen": "2026-10-31",
         "sessions_after_tracking": 1, "bucket": "raise", "dormant": False,
         "tracking_gone": False}
    tracking = {"kind": "issue", "ref": "org/repo#471", "since": "2026-10-05"}
    info = {"exists": True, "state": "OPEN", "milestone": 'Next: "one" #2',
            "priority": "P1", "created": "2026-10-02", "closed": None}
    items = {
        "b:2": {"note": "b", "project": "demo", "date": "2026-10-31",
                "text": "# Start\nnext"},
        "a:1": {"note": "a", "project": "demo", "date": "2026-10-01",
                "text": "Use | [[x]]\r\nnow"},
        "a:0": {"note": "a", "project": "demo", "date": "2026-10-01",
                "text": "--- keep this"},
    }
    return lesson, c, tracking, info, items


def frontmatter(content):
    return {key: json.loads(value) for key, value in (
        line.split(": ", 1) for line in content.split("---\n", 2)[1].splitlines()
    )}


def test_render_frontmatter_parses_with_hostile_text(sample):
    content = notes.render_note(*sample, None)
    lesson, c, tracking, info, _ = sample
    assert frontmatter(content) == {
        "type": "claude-lesson", "lesson_id": lesson["id"], "name": lesson["name"],
        **{k: c[k] for k in ("sessions", "recent_sessions", "projects", "first_seen", "last_seen")},
        "tracking": "issue", "tracking_ref": tracking["ref"],
        "tracking_since": info["created"], "issue_state": "open",
        "issue_milestone": info["milestone"], "issue_priority": "P1",
        "sessions_after_tracking": 1, "bucket": "raise", "dormant": False,
        "snoozed_until": None, "split_from": "Lold",
        "tags": ["claude/lesson", 'claude/project/a: "b"', "claude/project/demo"],
    }
    body = content.split("---\n", 2)[2]
    assert "[[x]]" not in body
    assert body.splitlines() == ["", '# Use "x" #1: now', "",
                                "- \\--- keep this · [[a]] · demo",
                                "- Use | [ [x] ] now · [[a]] · demo",
                                "- \\# Start next · [[b]] · demo"]


def test_render_is_byte_stable(sample):
    first = notes.render_note(*sample, None)
    sample[0]["members"].reverse()
    assert notes.render_note(*sample, None).encode() == first.encode()
    assert first.endswith("\n") and not first.endswith("\n\n")


def test_write_if_changed_skips_identical(tmp_path):
    assert notes.write_if_changed(str(tmp_path), "lessons", "note.md", "héllo\r\n")
    path = tmp_path / "lessons/note.md"
    before = path.stat().st_mtime_ns
    assert path.read_bytes() == "héllo\r\n".encode()
    assert notes.write_if_changed(str(tmp_path), "lessons", "note.md", "héllo\r\n") is False
    assert path.stat().st_mtime_ns == before
    assert notes.write_if_changed(str(tmp_path), "lessons", "note.md", "héllo\n")
    assert path.read_bytes() == "héllo\n".encode()


def test_snooze_survives_rerender(tmp_path, sample):
    filename = notes.note_filename(sample[0])
    notes.write_if_changed(str(tmp_path), "lessons", filename,
                           notes.render_note(*sample, "2026-12-01"))
    snooze = notes.read_snooze(tmp_path / "lessons" / filename)
    assert snooze == "2026-12-01"
    assert frontmatter(notes.render_note(*sample, snooze))["snoozed_until"] == snooze


def make_state(sample):
    lesson, c, tracking, info, _ = sample
    return {"generated": "2026-10-31", "totals": {"lessons": 1}, "lessons": {
        lesson["id"]: {**lesson, "classify": c, "tracking": tracking, "info": info}
    }}


def test_report_orders_buckets_then_rank(sample):
    state = make_state(sample)
    base = state["lessons"].pop("L123")
    cases = [
        ("quiet", "quiet", {}), ("rule", "rule_not_followed", {}),
        ("closed", "didnt_hold", {}), ("file", "file", {"recent_sessions": 99}),
        ("z-id", "raise", {}), ("a-id", "raise", {}),
        ("date", "raise", {"last_seen": "2026-11-01"}),
        ("projects", "raise", {"projects": ["a", "b", "c"]}),
        ("sessions", "raise", {"sessions": 3}),
        ("recent", "raise", {"recent_sessions": 2}),
    ]
    for lesson_id, bucket, overrides in cases:
        state["lessons"][lesson_id] = {**base, "classify": {
            **base["classify"], "bucket": bucket, **overrides}}
    result = notes.report(state)
    assert [e["id"] for e in result["lessons"]] == [
        "recent", "sessions", "projects", "date", "a-id", "z-id", "file", "closed", "rule", "quiet"]
    assert result["generated"] == state["generated"]
    assert result["totals"] == state["totals"]


def test_report_has_no_item_text(sample):
    state = make_state(sample)
    entry = notes.report(state)["lessons"][0]
    assert entry == {
        "id": "L123", "name": sample[0]["name"],
        **{k: sample[1][k] for k in ("bucket", "dormant", "sessions", "recent_sessions",
                                    "projects", "first_seen", "last_seen", "sessions_after_tracking")},
        "tracking": "issue", "tracking_ref": "org/repo#471", "issue_state": "open",
        "issue_milestone": sample[3]["milestone"], "issue_priority": "P1",
        "members": [{"note": "b", "item_key": "b:2"}, {"note": "a", "item_key": "a:1"},
                    {"note": "a", "item_key": "a:0"}],
    }
    assert all(item["text"] not in json.dumps(entry) for item in sample[4].values())


@pytest.mark.parametrize(("name", "slug"), [
    ("A / B!", "a-b"), ("日本語 !!!", "lesson"), ("a" * 49 + " / tail", "a" * 49),
    ("x" * 60, "x" * 50),
])
def test_note_filename(name, slug):
    assert notes.note_filename({"id": "L1", "name": name}) == f"{slug}-L1.md"


@pytest.mark.parametrize(("tracking", "info", "gone", "kind", "since"), [
    (None, None, False, "none", None),
    ({"kind": "path", "ref": "docs/a.md", "since": "2026-01-01"}, None,
     False, "rule", "2026-01-01"),
    ({"kind": "memory", "ref": "check", "since": "2026-01-01"}, None,
     True, "gone", "2026-01-01"),
    ({"kind": "issue", "ref": "a/b#1", "since": "2026-01-01"}, {"exists": False},
     True, "gone", "2026-01-01"),
])
def test_tracking_fields_match_report(sample, tracking, info, gone, kind, since):
    lesson, c, _, _, items = sample
    c["tracking_gone"] = gone
    del lesson["split_from"]
    fm = frontmatter(notes.render_note(lesson, c, tracking, info, items, None))
    state = make_state(sample)
    state["lessons"][lesson["id"]].update(tracking=tracking, info=info)
    entry = notes.report(state)["lessons"][0]
    assert fm["tracking"] == entry["tracking"] == kind
    assert fm["tracking_since"] == since
    assert "split_from" not in fm
    for key in ("tracking_ref", "issue_state", "issue_milestone", "issue_priority"):
        assert fm[key] == entry[key]


@pytest.mark.parametrize("content", [
    "", "snoozed_until: \"outside\"\n", "---\nsnoozed_until: null\n---\n",
    "---\nsnoozed_until: invalid\n---\n", "---\nsnoozed_until: 123\n---\n",
    '---\nsnoozed_until: "unterminated"\n', '---\n---\nsnoozed_until: "body"\n',
])
def test_read_snooze_absent_or_invalid(tmp_path, content):
    path = tmp_path / "note.md"
    path.write_text(content, encoding="utf-8")
    assert notes.read_snooze(path) is None


def test_read_snooze_unavailable(tmp_path):
    assert notes.read_snooze(tmp_path / "missing") is None
    assert notes.read_snooze(tmp_path) is None
    path = tmp_path / "bad.md"
    path.write_bytes(b"\xff")
    assert notes.read_snooze(path) is None


def test_write_error_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(notes.obsidian_utils, "write_vault_note", lambda *args: "write failed")
    with pytest.raises(OSError, match="write failed"):
        notes.write_if_changed(str(tmp_path), "lessons", "note.md", "content")


def test_write_rejects_escape(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("same", encoding="utf-8")
    with pytest.raises(OSError, match="path traversal blocked"):
        notes.write_if_changed(str(vault), "..", "outside.md", "same")
    assert outside.read_text() == "same"
