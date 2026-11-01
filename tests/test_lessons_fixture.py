"""#471 spike fixture: ids, labels and verdicts only, and the bars it records.

The fixture in tests/fixtures/471-eval/ holds the blind-eval keys, the judge
labels and the full Haiku verdict graph from the #471 spikes. obsidian-brain is
a public repo, so no retro item text is stored: items are named by their
``<note stem>#<item number>`` ref.
"""
import json
from pathlib import Path

import lessons_group

FIX = Path(__file__).resolve().parent / "fixtures" / "471-eval"
JUDGES = ("opus", "astra", "sol")


def _load(rel):
    return json.loads((FIX / rel).read_text())


def _graph():
    return [json.loads(l) for l in (FIX / "round3" / "graph_verdicts.jsonl").read_text().splitlines()]


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from _keys(v)


def _all_data():
    files = sorted(FIX.rglob("*.json")) + sorted(FIX.rglob("*.jsonl"))
    assert len(files) == 13  # positive control: an empty walk proves nothing
    for f in files:
        text = f.read_text()
        if f.suffix == ".jsonl":
            yield f, [json.loads(l) for l in text.splitlines()]
        else:
            yield f, json.loads(text)


def test_fixture_has_no_item_text():
    for f, data in _all_data():
        assert not {"text", "reason", "anchor_text"} & set(_keys(data)), f
        long = [s for s in _strings(data) if len(s) > 64]
        assert not long, (f, long[:1])


def _keep_rate(rnd, judge, select):
    key = _load(f"{rnd}/key.json")
    labels = {l["id"]: l["label"] == "keep" for l in _load(f"{rnd}/labels_{judge}.json")}
    ids = [i for i, v in key.items() if select(v)]
    return sum(labels[i] for i in ids) / len(ids), len(ids)


def test_round1_themes_missed_the_bar():
    # Round 1 bar: >= 60% of theme members kept. TF-IDF average-linkage themes failed.
    for judge in JUDGES:
        rate, n = _keep_rate("round1", judge, lambda v: "theme" in v["sets"])
        assert n == 48 and rate < 0.30, (judge, rate)


def test_round2_verified_keep_rate_meets_bar():
    # Round 2 bar: >= 70% of Haiku-verified candidates kept by each judge.
    for judge in JUDGES:
        rate, n = _keep_rate("round2", judge, lambda v: "verified" in v["sets"])
        assert n == 17 and rate >= 0.70, (judge, rate)


def test_random_controls_stay_near_zero():
    for rnd in ("round1", "round2"):
        for judge in JUDGES:
            rate, n = _keep_rate(rnd, judge, lambda v: v["random"])
            assert n == 100 and rate <= 0.05, (rnd, judge, rate)


def test_e1_star_score_not_below_spike():
    key = _load("round3/e1_key.json")
    kept = total = 0
    for judge in ("opus", "astra"):
        labels = {l["id"]: l["label"] == "keep" for l in _load(f"round3/e1_labels_{judge}.json")}
        for unit, k in key.items():
            if k["design"] == "star":
                total += len(k["members"])
                kept += sum(labels[f"{unit}-{n}"] for n in range(len(k["members"])))
    assert total == 108 and kept / total >= 0.74


def test_star_replay_reproduces_frozen_lessons():
    rows = _graph()
    keys = sorted({r["id"] for r in rows})
    edges = {(r["id"], c) for r in rows for c, v in zip(r["c"], r["v"]) if v}
    refs = set(keys) | {c for r in rows for c in r["c"]}
    note_of = {k: k.split("#")[0] for k in refs}
    assert len(rows) == 3881 and edges
    assert lessons_group.star_lessons(keys, edges, note_of) == _load("round3/lessons_star.json")


def test_frozen_lessons_hold_the_size_cap():
    lessons = _load("round3/lessons_star.json")
    assert len(lessons) == 544
    assert max(len(l["members"]) for l in lessons) <= 25
    for l in lessons:
        assert len({m.split("#")[0] for m in l["members"]}) >= 2
