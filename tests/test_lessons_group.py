"""Grouping and recurrence contracts for the retro lessons rollup."""

from copy import deepcopy
from hashlib import sha1

from hooks.lessons_group import assign_ids, classify, lesson_tracking, rank_key, star_lessons


def _item(key, note=None, date="2026-11-01", project="p", trace=None):
    return {"key": key, "note": note or key, "date": date, "project": project, "trace": trace}


def _lesson(*members):
    return {"centre": members[0], "members": list(members)}


def _id(centre):
    return "L" + sha1(centre.encode()).hexdigest()[:8]


def test_star_bridge_cannot_join_two_rules():
    keys = list("abcdef")
    edges = {("a", "b"), ("a", "c"), ("b", "c"), ("c", "d"), ("d", "e"), ("d", "f")}
    # c and d tie at degree three; c wins and consumes d. The remaining
    # e and f have no edge, so the specified greedy loop stops here.
    assert star_lessons(keys, edges, dict(zip(keys, keys))) == [
        {"centre": "c", "members": list("abcd")},
    ]
    # With an edge between the remaining pair, a second star survives.
    assert star_lessons(keys, edges | {("e", "f")}, dict(zip(keys, keys))) == [
        {"centre": "c", "members": list("abcd")},
        {"centre": "e", "members": list("ef")},
    ]


def test_star_drops_single_note_lessons():
    assert star_lessons(list("abcd"), {("a", "b"), ("c", "d")},
                        {"a": "same", "b": "same", "c": "one", "d": "two"}) == [
        {"centre": "c", "members": ["c", "d"]}
    ]


def test_star_is_deterministic_on_ties():
    notes = dict(zip("abcdef", "abcdef"))
    expected = [{"centre": "a", "members": ["a", "b"]},
                {"centre": "c", "members": ["c", "d"]}]
    edges = {("b", "a"), ("c", "d"), ("z", "c"), ("z", "e"), ("e", "e")}
    assert star_lessons(list("fedcba"), edges, notes) == expected
    assert star_lessons(list("abcdef"), edges, notes) == expected
    assert star_lessons([], edges, {}) == []


def test_assign_ids_keeps_id_at_half_overlap_and_not_below():
    old = {"Lold": list("abcd")}
    assert assign_ids([_lesson("a", "b", "x", "y")], old, {})[0][0]["id"] == "Lold"
    assert assign_ids([_lesson("a", "x", "y", "z")], old, {})[0][0]["id"] == _id("a")
    assert assign_ids([_lesson("a", "b")], old, {})[0][0]["id"] == "Lold"
    assert assign_ids([_lesson("x", "y")], {}, {})[0][0]["id"] == _id("x")


def test_assign_ids_records_merge_and_split():
    lessons = [_lesson("a", "b", "e", "f"), _lesson("c", "d")]
    old = {"L2": ["e", "f"], "L1": list("abcd"), "Lgone": ["z"]}
    retired = {"Lprevious": {"gone": True}}
    before = deepcopy((lessons, old, retired))
    assigned, result = assign_ids(lessons, old, retired)
    assert assigned == [dict(lessons[0], id="L1"),
                        dict(lessons[1], id=_id("c"), split_from="L1")]
    assert result == {"Lprevious": {"gone": True}, "L2": {"merged_into": "L1"}, "Lgone": {"gone": True}}
    assert (lessons, old, retired) == before
    assert assign_ids([], old, {})[1] == {i: {"gone": True} for i in old}


def test_tracking_most_frequent_then_issue_path_memory_tiebreak():
    items = {
        "i": _item("i", date="2026-10-03", trace={"kind": "issue", "ref": "o/r#2"}),
        "p": _item("p", date="2026-10-01", trace={"kind": "path", "path": "rules.md"}),
        "m": _item("m", date="2026-09-01", trace={"kind": "memory", "stem": "rule"}),
    }
    assert lesson_tracking(list(items), items) == {"kind": "issue", "ref": "o/r#2", "since": "2026-10-03"}
    assert lesson_tracking(["p", "m"], items)["kind"] == "path"
    items["m2"] = _item("m2", date="2026-10-01", trace={"kind": "memory", "stem": "rule"})
    assert lesson_tracking(list(items), items) == {"kind": "memory", "ref": "rule", "since": "2026-09-01"}
    items["j"] = _item("j", date="2026-10-02", trace={"kind": "issue", "ref": "o/r#3"})
    assert lesson_tracking(["i", "j"], items)["ref"] == "o/r#3"
    items["i"]["date"] = "2026-10-02"
    assert lesson_tracking(["j", "i"], items)["ref"] == "o/r#2"


def test_tracking_ignores_skipped_traces():
    items = {"a": _item("a"), "b": _item("b", trace={"kind": "skipped", "reason": "one-off"})}
    assert lesson_tracking(list(items), items) is None
    assert lesson_tracking([], {}) is None


def test_recent_window_is_inclusive_at_exactly_90_days():
    items = {"a": _item("a", date="2026-08-03"), "b": _item("b", date="2026-08-02", project="q"),
             "c": _item("c", note="a", date="2026-08-01")}
    result = classify(_lesson(*items), items, None, None, "2026-11-01", 90)
    assert result == {"sessions": 2, "recent_sessions": 1, "projects": ["p", "q"],
                      "first_seen": "2026-08-02", "last_seen": "2026-08-03",
                      "sessions_after_tracking": None, "bucket": "file", "dormant": False,
                      "tracking_gone": False}
    assert classify(_lesson("b"), items, None, None, "2026-11-01", 90)["dormant"] is True


def test_sessions_after_tracking_is_strictly_after():
    items = {"a": _item("a", date="2026-10-01"), "b": _item("b", date="2026-10-02"),
             "c": _item("c", note="b", date="2026-10-02")}
    tracking = {"kind": "issue", "ref": "o/r#1", "since": "2026-10-01"}
    def run(info=None, trace=tracking):
        return classify(_lesson(*items), items, trace, info, "2026-11-01", 90)
    assert run()["sessions_after_tracking"] == 1
    assert run({"created": "2026-10-02"})["sessions_after_tracking"] == 0
    assert run({"created": None})["sessions_after_tracking"] == 1
    assert run(trace=dict(tracking, since=None))["sessions_after_tracking"] is None


def test_buckets_all_five_and_tracking_gone():
    items = {"a": _item("a", date="2026-10-02")}
    tracking = {"kind": "issue", "ref": "o/r#1", "since": "2026-10-01"}
    def run(trace=tracking, info=None):
        return classify(_lesson("a"), items, trace, info, "2026-11-01", 90)
    assert run(None)["bucket"] == "file"
    assert run(info={"state": "OPEN"})["bucket"] == "raise"
    assert run()["bucket"] == "raise"
    assert run(dict(tracking, since=None))["bucket"] == "quiet"
    assert run(info={"state": "CLOSED", "closed": "2026-10-01"})["bucket"] == "didnt_hold"
    assert run(info={"state": "CLOSED", "closed": "2026-10-02"})["bucket"] == "quiet"
    assert run(info={"state": "CLOSED", "closed": None})["bucket"] == "quiet"
    for kind in ("memory", "path"):
        assert run(dict(tracking, kind=kind))["bucket"] == "rule_not_followed"
        assert run(dict(tracking, kind=kind, since="2026-10-02"))["bucket"] == "quiet"
    for kind in ("issue", "memory", "path"):
        result = run(dict(tracking, kind=kind), {"exists": False})
        assert result["bucket"] == "file"
        assert result["tracking_gone"] is True


def test_rank_key_recent_before_total():
    c = {"recent_sessions": 1, "sessions": 100, "projects": ["p"], "last_seen": "2026-10-31"}
    assert rank_key("L2", dict(c, recent_sessions=2, sessions=2)) < rank_key("L1", c)
    assert rank_key("L2", dict(c, sessions=101)) < rank_key("L1", c)
    assert rank_key("L2", dict(c, projects=["p", "q"])) < rank_key("L1", c)
    assert rank_key("L2", dict(c, last_seen="2026-11-01")) < rank_key("L1", c)
    assert rank_key("L1", c) < rank_key("L2", c)
