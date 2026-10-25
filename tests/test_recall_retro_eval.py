"""#458: the shipped /recall retro rule reproduces the spike and meets its bar on the committed labels.

The fixture (``tests/fixtures/458-eval/units.json``) holds, per project, the
layered candidate list as ``query_related_notes`` built it on a copy of the
live DB, today's top 20, the spike's at-most-1-retro top 20 (``v2``) and both
judges' blind labels. See the README there.
"""

import json
from pathlib import Path

import pytest

from related_select import select_related

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "458-eval" / "units.json").read_text())
UNITS = FIXTURE["units"]
LABELS = FIXTURE["labels"]
LIMIT = 20
# The spike's round-2 result (#458 comment): kept out of 100 slots, today -> at most 1 retro.
SPIKE = {"opus": (68, 74), "gpt": (59, 67)}


def test_fixture_has_the_five_projects_and_both_judges():
    assert len(UNITS) == 5
    assert sorted(LABELS) == ["gpt", "opus"]


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_today_is_the_first_twenty_layered_rows(unit):
    u = UNITS[unit]
    assert [r["id"] for r in u["layered"][:LIMIT]] == u["current"]


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_select_related_reproduces_the_spike_v2_set(unit):
    u = UNITS[unit]
    picked = select_related(u["layered"], LIMIT)
    assert [r["id"] for r in picked] == u["v2"]
    assert sum(r["type"] == "claude-retro" for r in picked) <= 1


def _kept(judge, sets):
    keep = total = 0
    for unit, ids in sets.items():
        for i in ids:
            label = LABELS[judge][unit][i]  # KeyError = an unlabelled slot, which would bias the rate
            total += 1
            keep += label == "keep"
    return keep, total


@pytest.mark.parametrize("judge", sorted(SPIKE))
def test_selection_beats_today_by_five_points_under_each_judge(judge):
    shipped = {u: [r["id"] for r in select_related(UNITS[u]["layered"], LIMIT)] for u in UNITS}
    today = {u: UNITS[u]["current"] for u in UNITS}
    (k_today, n_today), (k_new, n_new) = _kept(judge, today), _kept(judge, shipped)
    assert n_today == n_new == 100
    assert 100 * k_new / n_new >= 100 * k_today / n_today + 5
    assert (k_today, k_new) == SPIKE[judge]
