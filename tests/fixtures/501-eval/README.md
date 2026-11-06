# #501 spike fixture

Verdicts and blind labels from the spike behind batched anchor judging (#501). The bar and both results are in the issue body, under `## Spike`; the bar was written there before any call ran. `tests/test_501_fixture.py` re-scores the bar from these files.

obsidian-brain is a public repo, so nothing here holds lesson text or note stems. An anchor or candidate is an opaque id: the first 12 hex characters of `sha1("501:" + item key)`.

## Files

| Path | What it holds |
|------|---------------|
| `pairs.json` | Two samples, `random` (150 anchors, 1,478 scored pairs) and `same_note` (86 anchors from 15 notes, 843 scored pairs). Each row is one anchor-candidate pair: `cache` is the cached `r2-2026-10-31` verdict (1 = `same`), each run column is that run's verdict, and `pos_<run>` is the anchor's position in its batch. Runs: `S1` one anchor per call, `B5` and `B10` random batches of 5 and 10, `B10b` a second B10 run, `B10n` batches of one note's anchors. Pairs missing from the cache copy are left out. |
| `blind_key.json` | The 94 pairs where `B10` and `S1` disagree, by blind id (sha1 prefix), with each run's verdict. Judges never saw this file. |
| `labels_opus.json`, `labels_codex.json` | The two blind judges' `same` or `different` per blind id. |
| `prompt_sha1.txt` | sha1 of the spike's batched prompt for the fixed input `[("anchor one", ["cand a", "cand b"]), ("anchor two", ["cand c"])]`, computed from the spike script before the build. |
