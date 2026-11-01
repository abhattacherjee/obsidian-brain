# #458 /recall retro-rule fixture

`tests/test_recall_retro_eval.py` runs the shipped `select_related` on each
project's layered candidate list and scores the top 20 against blind
keep/drop labels.

## Provenance

The #458 spike (2026-10-24) replayed `query_related_notes` on a copy of the
live DB for 5 projects, with the inputs `/recall` uses. Two judges, Opus and
GPT (through `cx ask`), labelled the notes blind. Round 1 labelled 189 notes;
round 2 labelled 7 more, mixed with anchors already labelled in round 1.

| Judge | Today's top 20 kept | At most 1 retro kept |
|---|---|---|
| Opus | 68/100 | 74/100 |
| GPT | 59/100 | 67/100 |

The bar was +5 points under both judges. The test checks the bar and pins
these numbers.

## Files

- `units.json`:
  - `units`: per project, `layered` (the ordered candidate list as
    `{id, layer, type}`), `current` (today's top 20) and `v2` (the spike's top
    20 with at most 1 retro).
  - `labels`: per judge, per project, note id to `keep` or `drop`. Rounds 1
    and 2 are merged per note: round 1 first, round 2 only for notes round 1
    lacks.
- `build_fixture.py`: rebuilds `units.json` from the spike directory.

The repo is public, so the fixture holds no note names or text. Note ids are
HMAC-SHA256 values under a random salt that is not stored, and the build
refuses to write if any id is a plain hash of a note path, file name or blind
id. The judges' reasons and the blind ids are dropped. The raw spike files stay
in `~/.claude/obsidian-brain/spike-458/` on the machine that ran it.

## Rebuild

```bash
python3 tests/fixtures/458-eval/build_fixture.py
```

It only reads the spike directory and writes only `units.json`. A rebuild
uses a new salt, so every id changes even when nothing else did.
