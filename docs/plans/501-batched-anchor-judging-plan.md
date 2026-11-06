# #501: judge several anchors per Haiku call — plan

Epic #496, milestone "Headless cache cost". The spike, its pass bar and both results are in the issue body (`## Spike`).

## Findings (measured 2026-11-05)

- **Random groups pass.** 150 anchors, 1,478 pairs scored against a copy of `lessons-verdicts.db`. One-anchor retest (`S1`): kappa 0.569, `same` precision 0.766, recall 0.524. B10: 0.571 / 0.755 / 0.533, and a second B10 run 0.580 / 0.783 / 0.529. No parse failures. Blind labels on the 94 B10-vs-S1 disagreements: B10 matched Opus on 57.4% and Codex on 54.3% (bar 40%). B5 failed precision (0.692), so N is 10.
- **Same-note groups fail.** 15 notes, 86 anchors, grouped by note: precision 0.764 against a bar of 0.811 (S1 0.841). A false `same` is permanent (`record` keeps `MAX(same)`), so this blocks same-note batches.
- **`RUBRIC_VERSION` stays `r2-2026-10-31`.** B10 agrees with the cache as well as a retest does.

## Design

- `BATCH = 10` in `lessons_verify`.
- **Grouping.** `verify_anchors` gains `note_of: dict[str, str] | None = None` and `batch: int = BATCH`. It orders pending anchors by `sha1(key)` and puts each in the first open group with fewer than `batch` members and no member from the same note. With `note_of=None`, every anchor counts as its own note. `retro_rollup.regroup` passes its `note_of`.
- **A group of one** uses today's `build_prompt` and `parse_verdicts`, byte for byte. That keeps the measured one-anchor path for the steady-state `/retro` (one note).
- **A group of two or more** uses `build_batch_prompt(groups)`: the exact wrapper the spike measured, with `RUBRIC` unchanged inside it. The reply is one JSON object keyed by group number, each value the same `{"1": "same", ...}` object as today. `parse_batch(stdout, sizes) -> list[list[bool] | None] | None`: None when there is no JSON object; otherwise one entry per group, None for a group that is missing or incomplete.
- **Failure handling per anchor, as in #498.** Runner returns None or an empty reply: every anchor in the group fails and none is parked (retry next run). Non-empty reply with no JSON object: every anchor is parked. Partial reply: answered anchors are recorded, the rest parked.
- **Counts.** `calls` in the result becomes the number of runner calls (groups). A new `anchors` key holds the number of anchors sent. `progress(done, total, failed)` still counts anchors and fires when `done` crosses a multiple of 100, and once at the end. The `LessonsRollup` log line already counts real calls through `spawn_count()`; `retro_rollup` adds `anchors` to its printed summary.

## Failure modes

- **Fail-open paths:** a reply missing a group must not cache that group as `different` — `parse_batch` returns None for it, and it is parked. Test: partial reply records one anchor and parks the other.
- **Exit codes:** no CLI exit codes change. N/a.
- **Date boundaries:** parking keeps `PARK_DAYS`; unchanged. N/a.
- **Doc counts:** CHANGELOG and `architecture.json` purpose text say "10 anchors per call, never two from one note". Counted from the code.
- **Broken Markdown:** CHANGELOG entry inside the Unreleased block; re-read the five lines around it.
- **Unrelated changes:** none.
- **Inputs:** `verify_anchors` reads `texts`, `cands`, `note_of` (from `retro_rollup._load`, the live retro notes). A key missing from `note_of` counts as its own note (test). The fixture reads only `tests/fixtures/501-eval/`.
- **Bypass routes:** the grouping is the guard against same-note batches. Cheapest bypass: `note_of=None` from a caller that has notes. Test: `regroup` passes `note_of` (two anchors from one note make two calls).

## Task 1: batched judging in `verify_anchors`

Delegate: codex — one module and its test file, one check command

- `hooks/lessons_verify.py`: add `BATCH = 10`, `BATCH_HEAD`, `BATCH_TAIL`, `build_batch_prompt(groups: list[tuple[str, list[str]]]) -> str`, `parse_batch(stdout: str, sizes: list[int]) -> list[list[bool] | None] | None`, `_group(anchors, note_of, batch) -> list[list[str]]`, and the new `verify_anchors` signature and result keys above. Factor the per-object check out of `parse_verdicts` so both parsers share it.
- `tests/test_lessons_verify.py`: update the multi-anchor tests for the new `calls` meaning; add tests: `BATCH == 10`; M anchors from distinct notes make `ceil(M/10)` calls (M = 1, 10, 11, 25); two anchors from one note make two calls; a key missing from `note_of` is its own note; grouping is deterministic; a group of one gets exactly `build_prompt(...)`; partial reply records answered anchors and parks the rest; no-JSON reply parks all; None and empty reply park none; progress counts anchors.
- Check: `python3 -m pytest tests/test_lessons_verify.py -q`

## Task 2: `retro_rollup` passes `note_of` and prints anchors

Delegate: claude — two-line wiring plus one test; folded into the Task 1 review

- `hooks/retro_rollup.py` `regroup`: pass `note_of=note_of` to `verify_anchors`; default `verify` dict gains `"anchors": 0`; the printed summary shows anchors sent.
- Test in `tests/test_retro_rollup.py`: two pending items from one note make two runner calls.

## Task 3: spike fixture and its test

Delegate: claude — reads the spike output in this session's scratchpad, outside any worktree

- `tests/fixtures/501-eval/`: `README.md`; `pairs.json` (opaque pair id → cached verdict and each run's verdict, plus run, group and position); `blind_key.json` and `labels_opus.json` / `labels_codex.json`; `prompt_sha1.txt` (sha1 of the spike's batched prompt for a fixed two-group input). Ids are sha1 prefixes; no lesson text, no note stems (public repo).
- `tests/test_501_fixture.py`: re-scores each run's kappa, precision and recall from `pairs.json` and asserts the issue's bar and numbers; asserts blind-label match rates; asserts `build_batch_prompt` on the fixed input hashes to `prompt_sha1.txt`; asserts no fixture string looks like text.

## Task 4: docs

Delegate: claude — CHANGELOG and architecture render scripts live outside the repo

- `CHANGELOG.md` Unreleased entry. `docs/architecture/architecture.json` `lessons_verify.py` purpose text, `lastUpdated`; re-render and smoke-test.
