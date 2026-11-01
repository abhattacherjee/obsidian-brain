# #471 spike fixture

Labels and verdicts from the spikes behind the retro lessons rollup (#471). Results and bars were posted on the issue before each run. `tests/test_lessons_fixture.py` checks the bars against these files.

obsidian-brain is a public repo, so nothing here holds retro item text or judge reasons. An item is named by its ref, `<retro note stem>#<item number>`, as it stood on 2026-10-31.

## Files

| Path | What it holds |
|------|---------------|
| `round1/key.json` | Blind id → anchor unit, item ref, candidate sets (`theme`, `keyword`, `knn`) and whether it was a random control. |
| `round1/labels_{opus,astra,sol}.json` | `keep`/`drop` per blind id from three judges. |
| `round2/key.json` | Same shape; sets are `verified` (Haiku said "same lesson") and `knn`. |
| `round2/labels_{opus,astra,sol}.json` | Labels for round 2. |
| `round3/e1_key.json` | 30 lesson units (15 `star`, 15 `linked`): anchor ref and up to 8 member refs. |
| `round3/e1_labels_{opus,astra}.json` | Labels per member (`<unit>-<n>`). |
| `round3/graph_verdicts.jsonl` | The full graph: one row per open item (3,881), with its TF-IDF top-10 candidates (`c`) and Haiku's verdict for each (`v`). |
| `round3/lessons_star.json` | Shipped `lessons_group.star_lessons` over that graph. |

## Judges and models

- `opus`: a Claude Opus subagent.
- `astra`: Codex, model `gpt-6-astra`.
- `sol`: Codex, its default model.
- Verifier: Claude Haiku, with the round 2 rubric (`lessons_verify.RUBRIC`, version `r2-2026-10-31`).

## Results

| Round | Question | Bar | Result |
|-------|----------|-----|--------|
| 1 | Do TF-IDF average-linkage theme members share the anchor's lesson? | ≥ 60% kept | 13, 11 and 11 of 48 (about 25%). Fail. |
| 2 | Do Haiku-verified top-10 candidates share the anchor's lesson? | ≥ 70% kept, each judge | 17 of 17, all three judges. Pass. |
| 3, E1 | Do star lesson members share the centre's lesson? | ≥ 80% kept, judges averaged | 80 of 108 (74%). Below the bar; accepted as Haiku's per-pair precision, because nothing is traced without the user's batch confirm. |

Random controls: at most 2 of 100 kept in either round.

## Replay note

The spike script broke ties between equal-degree centres on the highest ref; the shipped `star_lessons` takes the lowest. On this graph the spike formed 538 lessons and the shipped code forms 544 (1,832 items, largest 23). The E1 labels judged the spike's lessons. `lessons_star.json` is the shipped output, so the replay test pins today's code.
