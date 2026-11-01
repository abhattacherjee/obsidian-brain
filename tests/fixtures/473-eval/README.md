# #473 matching spike: committed evidence

Ids and labels only. No lesson, issue, memory or rule text, and no refs (this repo is public).

- `round3/`: top 5 per source, every Haiku-confirmed match. `key.json` maps a blind item id to its
  lesson (`unit`), an opaque doc id (`item`), the sets it belonged to (`design`, `tfidf1`) and whether it
  was a random control. `labels_opus.json` and `labels_astra.json` are the two blind judges' `keep` or
  `drop` per item (the free-text reason was removed). This round missed its bar: `design` did not beat
  `tfidf1`.
- `round4/`: the single highest-cosine candidate per lesson (`top1`), accepted or rejected by Haiku
  (`haiku.json`, lesson id to `accept` or `reject`). 22 of 30 accepted; those 22 were all `keep` under
  both judges, against 26 of 30 for `top1` alone. This is the ruling `hooks/lessons_match.py` ships.

`tests/test_matching_fixture.py` re-scores both rounds from these files.
