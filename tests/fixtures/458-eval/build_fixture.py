"""Rebuild the #458 /recall retro-rule fixture from the spike (a dev tool; CI never runs it).

Reads, never writes, the spike directory (default ``~/.claude/obsidian-brain/spike-458``):
- ``layered.json``: per project, the ordered layered candidate list
  (``{path, layer, type}``) and the ``current`` top 20;
- ``v2sets.json``: per project, ``current`` and the spike's ``v2`` top 20;
- ``key.json`` + ``labels_opus.json`` + ``labels_gpt.json`` (round 1) and
  ``key2.json`` + ``labels2_opus.json`` + ``labels2_gpt.json`` (round 2):
  blind id -> ``{unit, item}`` and ``[{id, label, reason}]``.

Writes, next to this script, ``units.json`` with no note names or text (the repo is public):
``{"units": {unit: {"layered": [{id, layer, type}], "current": [id], "v2": [id]}},
"labels": {judge: {unit: {id: "keep" | "drop"}}}}``. Labels are merged per
(unit, note): round 1 first, round 2 fills notes round 1 lacks. Judges'
reasons and blind ids are dropped. Note ids are HMAC-SHA256 values under a
random salt that is not stored, so a guessed note name cannot be checked.

Usage::

    python3 tests/fixtures/458-eval/build_fixture.py [--spike DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUNDS = (("key.json", {"opus": "labels_opus.json", "gpt": "labels_gpt.json"}),
          ("key2.json", {"opus": "labels2_opus.json", "gpt": "labels2_gpt.json"}))


def _opaque(salt: bytes, path: str) -> str:
    """An opaque note id: HMAC-SHA256 of the note path under the unstored salt."""
    return "n" + hmac.new(salt, b"id\0" + path.encode(), hashlib.sha256).hexdigest()[:12]


def _guessable(inputs: set[str]) -> set[str]:
    """Every plain hash prefix of an input (path, file name or stem) an attacker could recompute."""
    out = set(inputs)
    for x in inputs:
        for algo in ("sha1", "sha256", "md5"):
            h = hashlib.new(algo, x.encode()).hexdigest()
            out |= {h[:n] for n in (8, 10, 12, 16)} | {h}
    return out


def _assert_no_guessable_ids(outputs: set[str], inputs: set[str]) -> None:
    """Fail the build if an output id, with or without its prefix letter, is a plain hash of an input or an input id."""
    guessable = _guessable(inputs)
    bad = sorted(o for o in outputs if o in guessable or o[1:] in guessable)
    if bad:
        raise SystemExit(f"refusing to write: {len(bad)} output id(s) are guessable from an input, e.g. {bad[0]}")


def _names(path: str) -> set[str]:
    base = os.path.basename(path)
    return {path, base, os.path.splitext(base)[0]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--spike", type=Path, default=Path.home() / ".claude" / "obsidian-brain" / "spike-458")
    args = p.parse_args(argv)
    layered = json.loads((args.spike / "layered.json").read_text())
    v2sets = json.loads((args.spike / "v2sets.json").read_text())
    salt = secrets.token_bytes(32)

    merged: dict[str, dict[str, dict[str, str]]] = {}
    blind_ids: set[str] = set()
    for key_file, judges in ROUNDS:
        key = json.loads((args.spike / key_file).read_text())
        blind_ids |= set(key)
        for judge, label_file in judges.items():
            for row in json.loads((args.spike / label_file).read_text()):
                k = key[row["id"]]
                merged.setdefault(judge, {}).setdefault(k["unit"], {}).setdefault(k["item"], row["label"])

    units, paths = {}, set()
    for unit, u in layered.items():
        if u["current"] != v2sets[unit]["current"]:
            raise SystemExit(f"{unit}: layered.json and v2sets.json disagree on current")
        paths |= {r["path"] for r in u["layered"]} | set(v2sets[unit]["v2"])
        units[unit] = {
            "layered": [{"id": _opaque(salt, r["path"]), "layer": r["layer"], "type": r["type"]} for r in u["layered"]],
            "current": [_opaque(salt, x) for x in u["current"]],
            "v2": [_opaque(salt, x) for x in v2sets[unit]["v2"]],
        }
    labels = {j: {unit: {_opaque(salt, path): lab for path, lab in sorted(notes.items())}
                  for unit, notes in sorted(per_unit.items())} for j, per_unit in sorted(merged.items())}
    paths |= {path for per_unit in merged.values() for notes in per_unit.values() for path in notes}

    outputs = {r["id"] for u in units.values() for r in u["layered"]}
    outputs |= {i for u in labels.values() for notes in u.values() for i in notes}
    inputs = set().union(*(_names(x) for x in paths)) | blind_ids
    _assert_no_guessable_ids(outputs, inputs)

    out = HERE / "units.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"units": units, "labels": labels}, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, out)
    n = sum(len(u["layered"]) for u in units.values())
    print(f"{n} candidates in {len(units)} units; labels: "
          + ", ".join(f"{j} {sum(len(v) for v in u.values())}" for j, u in labels.items()), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
