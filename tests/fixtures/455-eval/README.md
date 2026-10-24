# #455 selection regression fixture

`tests/test_capture_select_eval.py` replays `capture_select.select` on these
notes and scores the picks against blind keep/drop labels.

## Provenance

The #455 spike (2026-10-23) was read-only and blind. For each of 9 sessions,
the judges saw a shuffled set of 21 to 25 auto notes: the v2 top 10, the
capture-order top 10 and 5 random others. They also saw the session note, its
snapshots and retros, and later sessions' summaries.

| Judge | Sessions | v2 top 10 kept | Capture order kept |
|---|---|---|---|
| Opus (rounds 1 and 2) | 7 | 59/70 (84%) | 30/70 (43%) |
| GPT-6 Astra (round 2) | 4 | 27/40 (68%) | 16/40 (40%) |

The replay reproduces the spike's top 10 exactly in all 9 sessions and gives
the same keep rates.

## Files

- `key.json`: blind id to `{unit, item, sets, random}`, in the
  `blind_eval.py` format (claude-code-config #385). `item` is an opaque note id.
- `labels/opus_round1.json`, `labels/opus_round2.json`,
  `labels/astra_round2.json`: `[{"id", "label"}]`.
- `notes.json.gz`: per session (`unit`), every note captured in it (1,547 in
  all) as `{name, type, ts, score, novelty, vector}`, plus the spike's
  `top_v2`. `score` is the Haiku rubric v2 score and `novelty` the spike's.
- `build_fixture.py`: rebuilds the three above from the spike directory.

The repo is public, so the fixture holds no note text: note names are hashed,
the judges' reasons are dropped, and each vector's terms are hashed with a
random salt that is not stored. Cosines are unchanged. The full spike,
`blind.json` with the note text included, stays in
`~/.claude/obsidian-brain/spike-455/` on the machine that ran it.

## Rebuild

```bash
python3 tests/fixtures/455-eval/build_fixture.py
```

It reads the spike directory and opens the live index
(`~/.claude/obsidian-brain-vault.db`) read-only for the vectors. It writes
only the files above. A rebuild uses a new salt, so `notes.json.gz` changes
even when nothing else did.
