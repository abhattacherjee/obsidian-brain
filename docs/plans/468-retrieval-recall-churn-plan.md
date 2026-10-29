# #468 Retrieval recall and selection churn — plan

Issue: #468. Branch: `feature/468-retrieval-recall-churn` (worktree `obsidian-brain-468`; the main
checkout is the live plugin and stays on `develop`).

## Rulings

- **R1 — opt-in OR fill.** `search_vault` gets `min_results: int = 0`. Default 0 keeps today's path
  for every dedup caller (`capture_save.find_match`, `compress`, `open_item_dedup`, `wiki.lookup`),
  whose guards read the rank gap and `or_fallback`. Only `vault-ask` and `vault-search` pass 5.
- **R2 — AND first.** When AND returns fewer than `min_results` candidates, run OR with its own
  candidate limit, drop paths already in the AND set, mark the rest `or_fallback=True`, rerank each
  leg on its own, and concatenate AND then OR, truncated to `limit`. BM25 from two MATCH queries is
  not comparable, so one combined rerank could reorder across legs.
- **R3 — no hub down-weighting.** The hub note that blocks recall is in the AND leg; AND-first keeps
  it at rank 1 and the OR leg fills ranks 2–5.
- **R4 — Grep exclusion.** `vault_scan.py grep` gets a repeatable `--exclude-dir NAME` (basename
  match, pruned during the walk). The vault-ask skill passes `--exclude-dir _provisional
  --exclude-dir _rejected` and tells the Grep-tool path to drop any path containing
  `/_provisional/` or `/_rejected/` before Step 5.
- **R5 — hysteresis, not a time window.** `select()` takes `incumbent_bonus: float = 0.0`; an item
  with `incumbent: True` scores `merit + bonus - lam*sim`. `plan_session` marks a candidate an
  incumbent when its frontmatter status is already the promoted tier. δ (`INCUMBENT_BONUS`) is
  picked from the replay sweep (T2) and recorded here before T3 lands.
- **R6 — measurement scripts never touch live stores.** Both scripts refuse the default DB path and
  the live ledger path, open the DB copy read-only, and never call `ensure_index`, `plan_session`
  or `ledger_update`.

## Tasks

1. **T1 `scripts/retrieval_eval.py`** — runs the 10 known-answer queries from the issue against a
   DB copy (`--db`, required, refused when it resolves to the default DB). Per query and per mode
   (`include_provisional` True/False): target rank, result count, OR-candidate count, time. Add
   `--min-results N` so before/after runs use one script. Tests: refuses the default DB; reports a
   planted target's rank on a tmp index; `--min-results` reaches `search_vault`.
2. **T2 `scripts/selection_replay.py`** — reads a copied ledger (`--ledger`), a DB copy (`--db`)
   and copied hook logs (`--log`, repeatable). For each session with `saved` lines in the window,
   at each `saved` timestamp it runs `capture_select.select()` over that session's new, active,
   not-rejected, not-duplicate rows with `ts` at or before it; merit from `capture_select.merit`,
   vectors from the DB copy. Arms: `--bonus 0` (today) and each `--bonus` value. Prints per arm:
   notes moved, moved more than once, moved more than twice, max moves, and the mean merit of each
   session's final picks. Tests: refuses live paths; a planted three-slice session where today's
   arm flips a note and a bonus arm does not.
3. **T3 hysteresis** — `select(..., incumbent_bonus)` and `plan_session` incumbents, with
   `INCUMBENT_BONUS` from T2. Tests: a challenger within δ does not displace an incumbent; one
   beyond δ does; bonus 0 reproduces today's picks; the duplicate (`dup_cos`) rule still wins over
   the bonus.
4. **T4 OR fill + Grep exclusion** — R1, R2, R4. Tests: AND below `min_results` adds OR-only rows
   after AND rows, flagged `or_fallback=True`, no duplicates; `min_results=0` is byte-identical to
   today on the same index; `--exclude-dir` prunes nested dirs and is repeatable; skill-text tests
   pin `min_results=5` in vault-ask and vault-search and the exclusion in vault-ask Step 4.
5. **T5 docs** — CHANGELOG `[Unreleased]`, `docs/architecture/architecture.json` (two new scripts,
   the `min_results` flow), re-render and smoke-test.

## Failure modes

- **Fail-open paths:** an OR query that raises must not drop the AND hits — test an OR failure
  (`sqlite3.Error` on the second execute) returns the AND results. The replay must not count a
  session with no vectors as "no churn": it reports sessions skipped and why. A missing
  `INCUMBENT_BONUS` cannot happen (module constant); a non-numeric bonus argument is rejected.
- **Exit codes:** both scripts exit 2 on a refused path or bad argument, 1 on an unreadable DB or
  ledger, 0 on success. A test per code.
- **Date boundaries:** the replay window is UTC; `saved` lines carry `+00:00` and ledger `ts` is
  epoch seconds. Test a session whose slices cross UTC midnight.
- **Doc counts:** CHANGELOG and PR numbers come from script output pasted verbatim, never from
  memory.
- **Broken Markdown:** CHANGELOG entry inside `[Unreleased]`; re-read the five surrounding lines.
- **Unrelated changes:** none planned; `git diff --diff-filter=D --name-only origin/develop...HEAD`
  must be empty.
- **Inputs:** `retrieval_eval.py` reads only the DB copy (source of truth: a `.backup` of the live
  DB taken before the run). `selection_replay.py` reads the ledger copy, the DB copy and hook-log
  copies; a stale or missing log gives "0 sessions in window" and exit 1, tested. `select()` reads
  `incumbent` from the item dict; a missing key means not an incumbent, tested.
- **Bypass routes (R6 is a guard):** passing the default DB through a symlink or a relative path —
  the check resolves both sides (`Path.resolve()`); tests cover a symlink and `./` forms.
