"""#501 spike fixture: re-score the committed verdicts against the bar in the issue.

tests/fixtures/501-eval/ holds opaque ids, verdicts and blind labels only (the
repo is public): no lesson text and no note stems. The bar was written into
issue #501 before any spike call ran.
"""
import hashlib
import json
import re
from pathlib import Path

import pytest

import lessons_verify as lv

FIX = Path(__file__).resolve().parent / "fixtures" / "501-eval"


def _load(name):
    return json.loads((FIX / name).read_text())


def _run(name):
    run = _load("pairs.json")[name]
    return run, [dict(zip(run["columns"], row)) for row in run["rows"]]


def _stats(rows, mode):
    tp = sum(1 for r in rows if r[mode] and r["cache"])
    fp = sum(1 for r in rows if r[mode] and not r["cache"])
    fn = sum(1 for r in rows if not r[mode] and r["cache"])
    n = len(rows)
    tn = n - tp - fp - fn
    po = (tp + tn) / n
    pe = ((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / n / n
    return {"kappa": (po - pe) / (1 - pe), "precision": tp / (tp + fp),
            "recall": tp / (tp + fn), "agree": po}


def _passes(b, s1):
    # Bar items 1-3 from the issue's "## Spike" section.
    return (b["kappa"] >= s1["kappa"] - 0.05
            and b["precision"] >= s1["precision"] - 0.03
            and b["recall"] >= s1["recall"] - 0.05)


def test_random_sample_coverage_and_no_parse_failures():
    run, rows = _run("random")
    assert run["anchors"] == 150
    assert run["coverage"]["proposed"] == 1500 and run["coverage"]["scored"] == len(rows) == 1478
    for mode in ("S1", "B5", "B10", "B10b"):
        assert all(r[mode] in (0, 1) for r in rows), mode  # bar item 4: no failed anchors


@pytest.mark.parametrize("mode, kappa, precision, recall", [
    ("S1", 0.569, 0.766, 0.524),
    ("B5", 0.539, 0.692, 0.529),
    ("B10", 0.571, 0.755, 0.533),
    ("B10b", 0.580, 0.783, 0.529),
])
def test_random_sample_numbers_match_the_issue(mode, kappa, precision, recall):
    s = _stats(_run("random")[1], mode)
    assert round(s["kappa"], 3) == kappa
    assert round(s["precision"], 3) == precision
    assert round(s["recall"], 3) == recall


def test_b10_passes_the_bar_twice_and_b5_fails_on_precision():
    rows = _run("random")[1]
    s1 = _stats(rows, "S1")
    assert _passes(_stats(rows, "B10"), s1)
    assert _passes(_stats(rows, "B10b"), s1)
    b5 = _stats(rows, "B5")
    assert not _passes(b5, s1) and b5["precision"] < s1["precision"] - 0.03


def test_batch_positions_cover_the_whole_batch():
    rows = _run("random")[1]
    assert {r["pos_B10"] for r in rows} == set(range(1, 11))
    assert {r["pos_B5"] for r in rows} == set(range(1, 6))


def test_same_note_batches_fail_on_precision():
    run, rows = _run("same_note")
    assert run["anchors"] == 86 and len(rows) == run["coverage"]["scored"] == 843
    s1, b = _stats(rows, "S1"), _stats(rows, "B10n")
    assert round(s1["precision"], 3) == 0.841 and round(b["precision"], 3) == 0.764
    assert b["precision"] < s1["precision"] - 0.03
    assert not _passes(b, s1)


def test_same_note_rows_have_one_batch_position_per_anchor():
    # Note stems are not stored, so this checks shape only: each anchor sat at one position.
    rows = _run("same_note")[1]
    pos = {}
    for r in rows:
        pos.setdefault(r["anchor"], set()).add(r["pos_B10n"])
    assert all(len(p) == 1 for p in pos.values())
    assert max(next(iter(p)) for p in pos.values()) <= 10


def test_blind_labels_side_with_b10_at_least_40_percent():
    key = _load("blind_key.json")
    assert len(key) == 94
    assert all(v["S1"] != v["B10"] for v in key.values())
    matches = {}
    for judge in ("opus", "codex"):
        labels = _load(f"labels_{judge}.json")
        assert set(labels) == set(key)
        matches[judge] = sum((labels[i] == "same") == bool(key[i]["B10"]) for i in key)
        assert matches[judge] / len(key) >= 0.40  # bar item 5
    assert matches == {"opus": 54, "codex": 51}


def test_judges_agree_beyond_chance():
    key = _load("blind_key.json")
    o, x = _load("labels_opus.json"), _load("labels_codex.json")
    n = len(key)
    po = sum(o[i] == x[i] for i in key) / n
    so = sum(v == "same" for v in o.values()) / n
    sx = sum(v == "same" for v in x.values()) / n
    pe = so * sx + (1 - so) * (1 - sx)
    assert round((po - pe) / (1 - pe), 3) == 0.518


def test_shipped_batch_prompt_is_the_one_the_spike_measured():
    want = (FIX / "prompt_sha1.txt").read_text().strip()
    prompt = lv.build_batch_prompt([("anchor one", ["cand a", "cand b"]), ("anchor two", ["cand c"])])
    assert hashlib.sha1(prompt.encode()).hexdigest() == want
    assert lv.BATCH == 10 and lv.RUBRIC_VERSION == "r2-2026-10-31"


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


@pytest.mark.parametrize("name", ["pairs.json", "blind_key.json", "labels_opus.json", "labels_codex.json"])
def test_fixture_holds_no_text(name):
    allowed = {"random", "same_note", "coverage", "proposed", "scored", "pool_pos", "pool_neg",
               "anchors", "columns", "rows", "anchor", "cand", "cache", "S1", "B5", "B10", "B10b",
               "B10n", "pos_B5", "pos_B10", "pos_B10b", "pos_B10n", "same", "different"}
    for s in _strings(_load(name)):
        assert s in allowed or re.fullmatch(r"[0-9a-f]{10,12}", s), s
