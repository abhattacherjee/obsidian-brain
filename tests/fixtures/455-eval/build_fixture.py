"""Rebuild the #455 selection regression fixture from the spike (a dev tool; CI never runs it).

Reads, never writes:
- the spike directory (default ``~/.claude/obsidian-brain/spike-455``):
  ``spike455v2-report.json``, ``key.json`` and ``labels/*.json``;
- the live vault index (default ``~/.claude/obsidian-brain-vault.db``),
  opened READ-ONLY (``mode=ro``), for each note's ``tfidf_vector``.

Writes, next to this script, a fixture with no note text (the repo is public):
- ``key.json``: the spike key, each note name replaced by an opaque id;
- ``labels/<judge>.json``: ``[{"id", "label"}]``, the judges' reasons dropped;
- ``notes.json.gz``: per labelled unit, every note as
  ``{name, type, ts, score, novelty, vector}`` plus the spike's ``top_v2``.
  Note ids and vector terms are HMAC-SHA256 values under one random salt that
  is not stored: cosines are exact, but neither the words nor the note names
  can be recovered or checked by guessing.

Usage::

    python3 tests/fixtures/455-eval/build_fixture.py [--spike DIR] [--db PATH]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
JUDGE_FILES = ("opus_round1", "opus_round2", "astra_round2")


def _opaque(salt: bytes, name: str) -> str:
    """An opaque note id: HMAC-SHA256 of the name under the unstored salt (a bare hash of a name can be guessed)."""
    return "n" + hmac.new(salt, b"id\0" + name.encode(), hashlib.sha256).hexdigest()[:12]


def _blind(salt: bytes, bid: str) -> str:
    """An opaque blind id: the spike's own ids are sha1(file name)[:12], so they must not be kept."""
    return "b" + hmac.new(salt, b"blind\0" + bid.encode(), hashlib.sha256).hexdigest()[:12]


def _term(salt: bytes, t: str) -> str:
    return hmac.new(salt, b"term\0" + t.encode(), hashlib.sha256).hexdigest()[:10]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--spike", type=Path, default=Path.home() / ".claude" / "obsidian-brain" / "spike-455")
    p.add_argument("--db", type=Path, default=Path.home() / ".claude" / "obsidian-brain-vault.db")
    args = p.parse_args(argv)
    report = json.loads((args.spike / "spike455v2-report.json").read_text())
    key = json.loads((args.spike / "key.json").read_text())
    units = sorted({k["unit"] for k in key.values()})

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)  # read-only: this never writes the live index
    try:
        vectors = {os.path.basename(path): json.loads(vec) for path, vec in
                   con.execute("SELECT path, tfidf_vector FROM notes WHERE tfidf_vector IS NOT NULL")}
    finally:
        con.close()

    salt = secrets.token_bytes(32)

    notes, missing = {}, 0
    for unit in units:
        rows = []
        for name, n in report[unit]["notes"].items():
            vec = vectors.get(name)
            missing += vec is None
            rows.append({"name": _opaque(salt, name), "type": n["type"], "ts": n["ts"], "score": n["score2"],
                         "novelty": n["novelty"],
                         "vector": {_term(salt, t): w for t, w in vec.items()} if vec else None})
        rows.sort(key=lambda r: r["ts"])
        notes[unit] = {"notes": rows, "top_v2": sorted(_opaque(salt, x) for x in report[unit]["top_v2"])}

    (HERE / "key.json").write_text(json.dumps(
        {_blind(salt, bid): {**k, "item": _opaque(salt, k["item"])} for bid, k in sorted(key.items())}, indent=1) + "\n")
    for judge in JUDGE_FILES:
        labels = json.loads((args.spike / "labels" / f"{judge}.json").read_text())
        (HERE / "labels" / f"{judge}.json").write_text(json.dumps(
            [{"id": _blind(salt, r["id"]), "label": r["label"]} for r in labels], indent=1) + "\n")
    with gzip.open(HERE / "notes.json.gz", "wt", encoding="utf-8", compresslevel=9) as fh:
        json.dump(notes, fh, separators=(",", ":"))
    total = sum(len(u["notes"]) for u in notes.values())
    print(f"{total} notes in {len(units)} units; {missing} without a vector", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
