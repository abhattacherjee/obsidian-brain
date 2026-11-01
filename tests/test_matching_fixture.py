"""The #473 matching spike, re-scored from the committed ids and labels (no text, no refs).

Round 3 (top 5 per source, every Haiku-confirmed match) missed its bar. Round 4 (the single
highest-cosine candidate, accepted or rejected by Haiku) passed it. These tests recompute both
from the committed fixture, so the ruling the shipped design rests on stays checkable.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "473-eval"
JUDGES = ("opus", "astra")


def load(name: str):
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


def keep_rate(key: dict, labels: dict, wanted) -> tuple[int, int]:
    ids = [i for i, entry in key.items() if wanted(entry)]
    return sum(labels[i] == "keep" for i in ids), len(ids)


def labels_of(round_name: str, judge: str) -> dict:
    return {row["id"]: row["label"] for row in load(f"{round_name}/labels_{judge}.json")}


@pytest.mark.parametrize("judge", JUDGES)
def test_round4_bar_holds_on_committed_labels(judge):
    key, haiku = load("round4/key.json"), load("round4/haiku.json")
    labels = labels_of("round4", judge)
    accepted = {lesson for lesson, verdict in haiku.items() if verdict == "accept"}
    assert len(accepted) >= 15
    kept, n = keep_rate(key, labels, lambda e: "top1" in e["sets"] and e["unit"] in accepted)
    base_kept, base_n = keep_rate(key, labels, lambda e: "top1" in e["sets"])
    assert n == len(accepted) and base_n == len(haiku) == 30
    design, baseline = kept / n, base_kept / base_n
    assert design >= 0.80 and design > baseline, (judge, design, baseline)


@pytest.mark.parametrize("judge", JUDGES)
def test_round3_bar_missed_as_recorded(judge):
    key = load("round3/key.json")
    labels = labels_of("round3", judge)
    kept, n = keep_rate(key, labels, lambda e: "design" in e["sets"])
    base_kept, base_n = keep_rate(key, labels, lambda e: "tfidf1" in e["sets"])
    assert (n, base_n) == (34, 20)
    assert kept / n <= base_kept / base_n, (judge, kept, n, base_kept, base_n)


def test_labels_cover_every_key_id_once():
    for round_name in ("round3", "round4"):
        key = load(f"{round_name}/key.json")
        for judge in JUDGES:
            rows = load(f"{round_name}/labels_{judge}.json")
            assert sorted(r["id"] for r in rows) == sorted(key), (round_name, judge)
            assert {r["label"] for r in rows} <= {"keep", "drop"}
            assert all(set(r) == {"id", "label"} for r in rows)  # no free-text reason column
    assert set(load("round4/haiku.json").values()) <= {"accept", "reject"}


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


def test_fixture_holds_no_text():
    files = sorted(FIXTURE.glob("round*/*.json"))
    assert len(files) == 7
    for path in files:
        for text in _strings(json.loads(path.read_text(encoding="utf-8"))):
            assert re.fullmatch(r"[A-Za-z0-9_-]{1,40}", text), (path.name, text[:20])
