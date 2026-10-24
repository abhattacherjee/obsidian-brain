"""#455 regression: replay capture selection on the blind-judged spike fixture.

The fixture (``tests/fixtures/455-eval/``, see its README) holds every note of
the 9 judged sessions with the worker score, novelty and TF-IDF vector the
spike used, and the judges' keep/drop labels. ``capture_select.select`` must
keep the spike's pass bar: v2 top-10 keep rate >= 80% under Opus and >= 65%
under Astra, and at least 25 points above capture order under each judge.
A pick with no label counts as a drop.
"""

import gzip
import importlib.util
import json
import re
from pathlib import Path

import pytest

import capture_select

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "455-eval"
BLIND_EVAL = Path.home() / ".claude" / "skills" / "ship" / "scripts" / "blind_eval.py"
BARS = {"opus": 0.80, "astra": 0.65}
MARGIN = 0.25


def _labels(name):
    return {r["id"]: r["label"] for r in json.loads((FIXTURE / "labels" / f"{name}.json").read_text())}


@pytest.fixture(scope="module")
def replay():
    notes = json.loads(gzip.open(FIXTURE / "notes.json.gz", "rt", encoding="utf-8").read())
    key = json.loads((FIXTURE / "key.json").read_text())
    judges = {"opus": {**_labels("opus_round1"), **_labels("opus_round2")}, "astra": _labels("astra_round2")}
    sets = {}
    for unit, u in notes.items():
        items = [{"id": n["name"], "merit": capture_select.merit(n["score"], n["novelty"], n["type"]),
                  "vector": n["vector"], "ts": n["ts"]} for n in u["notes"]]
        order = [n["name"] for n in sorted(u["notes"], key=lambda n: n["ts"])[:capture_select.CAP]]
        sets[unit] = {"v2": capture_select.select(items), "order": order, "spike_v2": u["top_v2"]}
    return notes, key, judges, sets


def _keep_rate(key, labels, sets, which):
    blind = {(k["unit"], k["item"]): b for b, k in key.items()}
    units = sorted({key[b]["unit"] for b in labels})
    kept = total = 0
    for unit in units:
        for name in sets[unit][which]:
            total += 1
            kept += labels.get(blind.get((unit, name))) == "keep"  # no label: a drop
    return kept / total


def test_fixture_has_the_judged_units(replay):
    notes, key, judges, _ = replay
    assert len(notes) == 9 and sum(len(u["notes"]) for u in notes.values()) == 1547
    assert {key[b]["unit"] for j in judges.values() for b in j} == set(notes)


@pytest.mark.parametrize("judge", sorted(BARS))
def test_v2_keep_rate_meets_the_bar(replay, judge):
    _, key, judges, sets = replay
    v2 = _keep_rate(key, judges[judge], sets, "v2")
    order = _keep_rate(key, judges[judge], sets, "order")
    assert v2 >= BARS[judge], f"{judge}: v2 keep rate {v2:.1%} < {BARS[judge]:.0%}"
    assert v2 - order >= MARGIN, f"{judge}: v2 {v2:.1%} is not {MARGIN:.0%} above capture order {order:.1%}"


def test_capture_order_alone_fails_the_bar(replay):
    """Positive control: the bar can fail. Capture order, the old behaviour, is below it under both judges."""
    _, key, judges, sets = replay
    for judge, bar in BARS.items():
        assert _keep_rate(key, judges[judge], sets, "order") < bar


def test_replay_reproduces_most_of_the_spike(replay):
    """Not the pass bar, a drift alarm: the spike's own top-10 came out of the same inputs."""
    _, _, _, sets = replay
    exact = sum(sorted(s["v2"]) == s["spike_v2"] for s in sets.values())
    assert exact >= 7, f"only {exact} of {len(sets)} units reproduce the spike's top 10"


@pytest.mark.skipif(not BLIND_EVAL.is_file(), reason="blind_eval.py is a local /ship script, absent in CI")
@pytest.mark.parametrize("judge", sorted(BARS))
def test_blind_eval_score_agrees(replay, judge):
    """``blind_eval.py score`` over a key rebuilt from the replay agrees with the in-test rate within 1 point."""
    spec = importlib.util.spec_from_file_location("blind_eval_455", BLIND_EVAL)
    blind_eval = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(blind_eval)
    _, key, judges, sets = replay
    labels = judges[judge]
    replay_key = {b: {"unit": key[b]["unit"], "item": key[b]["item"], "random": False,
                      "sets": [s for s in ("v2", "order") if key[b]["item"] in sets[key[b]["unit"]][s]]}
                  for b in labels}
    totals = blind_eval.score(replay_key, {judge: labels})["judges"][judge]["totals"]
    kept, n = totals["v2"]
    assert abs(kept / n - _keep_rate(key, labels, sets, "v2")) <= 0.01


def test_blind_ids_are_salted_not_filename_hashes():
    """The spike's blind ids were sha1(file name)[:12]; a guessed name could be confirmed against them."""
    key = json.loads((FIXTURE / "key.json").read_text())
    ids = set(key) | {i for j in ("opus_round1", "opus_round2", "astra_round2") for i in _labels(j)}
    assert ids and all(re.fullmatch(r"b[0-9a-f]{12}", i) for i in ids)
    assert not any(re.fullmatch(r"[0-9a-f]{12}", i) for i in ids)
    assert set(_labels("opus_round1")) <= set(key)


def test_build_fixture_refuses_a_guessable_id():
    """The builder's own check (CI never sees the raw names): an id equal to sha1(name)[:12] fails the build."""
    import hashlib
    spec = importlib.util.spec_from_file_location("build_fixture", FIXTURE / "build_fixture.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    name = "2026-10-20-some-note.md"
    guess = hashlib.sha1(name.encode()).hexdigest()[:12]
    for bad in (guess, "n" + guess, "b" + guess):
        with pytest.raises(SystemExit):
            mod._assert_no_guessable_ids({bad}, {name})
    mod._assert_no_guessable_ids({mod._opaque(b"salt", name), mod._blind(b"salt", guess)}, {name, guess})
