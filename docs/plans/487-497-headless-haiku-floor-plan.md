# #487 + #497: headless Haiku floor and call volume — plan

Batch: #487 (lessons verifier floor and call volume) and #497 (find the caller of the
headless Haiku chains, then fix it). Epic #496, milestone "Headless cache cost".

## Findings (measured 2026-11-05)

- **Caller (#497 C.1).** Transcripts in `~/.claude/projects/-Users-abhishek-dev-claude-workspace-obsidian-brain/`
  from 2026-10-25 on, grouped by first prompt: 4,072 start with the verifier rubric
  ("You decide whether lessons state the SAME rule"), 1,626 with the matcher gate ("You are
  matching a lesson…"), 14 with lesson naming. All `sdk-cli`, cwd = the obsidian-brain repo,
  all 1h cache writes. 5,712 in total against the 5,703 in the audit. All three go through
  `lessons_verify.run_haiku`. `capture_worker` is not the caller: it already runs from
  `worker-cwd`.
- **Floor.** Live `claude -p --model haiku` with `run_haiku`'s flags and the prompt "Reply with only
  the word ok." gave these first-turn totals: repo cwd 20,789 (20,138 written), empty cwd 3,689. A trimmed
  env changed nothing. The second call from the empty cwd read the whole 3,687-token prefix from cache. The
  backfill's 70K first turn (56K written) came from the repo cwd. The extra ~50K above today's 20.8K is a
  hypothesis (larger memory or CLAUDE.md then, or a different git status). It is not reproduced. The cwd fix
  removes it either way.
- **Volume.** The cache holds 4,147 anchors under `r2-2026-10-31`, close to the 4,072 verifier calls.
  The calls were the one-time backfill of 3,965 retro items: one call per anchor, and the cache
  worked. No sign of a loop or a missed key. A gap remains: an anchor whose reply does not parse is
  never recorded, so every later rollup calls Haiku for it again.
- **Caching choice (#497 C.2).** `DISABLE_PROMPT_CACHING_HAIKU=1` turns the write off (tested: 3,689
  plain input tokens). It is not used. With a fixed cwd the 3.7K prefix is the same for every call and
  is read from cache (0.1x input). Only the prompt tail is written. That is option (a): the shared prefix
  is reused within the TTL. Turning caching off would make every call pay full input on the prefix.

## Out of scope

- Putting several anchors in one call (#487's Fix list). It changes the judging prompt, so it needs a
  quality spike. A `RUBRIC_VERSION` bump would drop all 4,147 cached anchors and re-judge them,
  which is ~4,000 calls. With a 3.7K reused prefix the saving is small.
- The other `claude -p` callers (`obsidian_utils` summarizer, `check_items_cli`). They made 9 calls
  in the window.

## Task 1: run_haiku runs from the shared worker cwd

Delegate: codex — two files plus tests, one check command

- `hooks/obsidian_utils.py`: add `worker_cwd() -> str`: `os.path.join(_SECURE_DIR, "capture", "worker-cwd")`,
  created with `os.makedirs(..., mode=0o700, exist_ok=True)`, returned as a str.
- `hooks/capture_worker.py`: `_worker_cwd()` returns `Path(obsidian_utils.worker_cwd())`, so the
  capture worker and the lessons verifier share one folder and one cached prefix.
- `hooks/lessons_verify.py`: `run_haiku` passes `cwd=obsidian_utils.worker_cwd()` to `subprocess.run`,
  computed inside the `try`, so an `OSError` from `makedirs` returns None.
- Tests (`tests/test_lessons_verify.py`): `run_haiku` passes `cwd` equal to `obsidian_utils.worker_cwd()`.
  The folder is not the process cwd and holds no `CLAUDE.md` or `.git`. A `makedirs` OSError returns None
  without calling `subprocess.run`.

## Task 2: park unparseable anchors; count calls and cache hits

Delegate: codex — one module plus its tests

- `VerdictCache`: new table `failures(anchor TEXT, cands TEXT, rubric TEXT, model TEXT,
  PRIMARY KEY(anchor,cands,rubric,model))`. `cands` is the sha1 hex of `"\n".join(cand_keys)`.
  Add methods `park(anchor_key, cand_keys)` and `parked(anchor_key, cand_keys) -> bool`.
  `prune(live_keys)` also deletes `failures` rows whose anchor is not live.
- `verify_anchors`: an anchor with candidates is
  - a cache hit when `cache.judged(a)`;
  - skipped as parked when `cache.parked(a, cands[a])`;
  - otherwise one call.
  The runner returning a string that `parse_verdicts` rejects parks the anchor (`unparseable += 1`).
  The runner returning None (timeout, non-zero exit, OSError) does not park it (`failed += 1`), so it
  is retried next rollup.
  The return dict is `{"judged", "failed", "unparseable", "parked", "cache_hits", "calls", "skipped"}`.
  `calls` = the anchors sent to the runner. `skipped` keeps its meaning: `len(cands) - calls`.
  A changed candidate set, model or rubric version gives a new key, so the anchor is retried.
- Tests:
  - A second `verify_anchors` over the same cands with a counting runner makes 0 calls and
    reports `cache_hits` = number of anchors.
  - An unparseable reply parks the anchor; the next call makes 0 runner calls and reports `parked=1`.
  - A changed candidate list for a parked anchor calls again.
  - A runner None is not parked.
  - `prune` drops dead failure rows.

## Task 3: one log line per rollup

Delegate: codex — two files plus tests

- `hooks/lessons_verify.py`: count every `subprocess.run` spawn in `run_haiku` (timeouts included)
  with a thread-safe counter. Expose it as `spawn_count() -> int`.
- `hooks/obsidian_utils.py`: add `append_event_log(event: str, fields: dict) -> None`. It writes the same file,
  rotation and sanitising as `_append_sessionend_log`, as one line
  `<iso ts> <event> k=v k=v`. It is best effort and never raises.
- `hooks/retro_rollup.py`:
  - `regroup` adds `calls` and `cache_hits` (from verify), `parked` and `unparseable` to its summary.
  - `failed` = verify `failed` + `unparseable`, so the run that meets a bad reply still exits 1.
  - `main()`, for `update`, `rebuild`, `match` and `refresh-tracking`, appends
    `LessonsRollup cmd=<cmd> claude_calls=<spawn delta> cache_hits=<n> parked=<n> failed=<n>`.
  - The `update`/`rebuild` stdout line gains `, claude calls: N, cache hits: M, parked: P`.
- Tests:
  - A second `update` over the same retro notes, with `subprocess.run` faked in `lessons_verify`,
    spawns 0 processes, and its log line says `claude_calls=0`.
  - The log line lands in `$HOME/.claude/obsidian-brain-hook.log` (tmp HOME).

## Failure modes

1. **Fail-open paths:** `makedirs` failing must return None, not run in the repo cwd (test in T1).
   A parked anchor must not count as judged (test: `unjudged` still counts it).
   A log write error must not fail the rollup (test: log path is a directory, rc unchanged).
2. **Exit codes:** an unparseable reply still exits 1 on the run where it happens (test in T3).
   Later runs exit 0, and the stdout line shows `parked`.
3. **Date boundaries:** none. No dates are computed. The log timestamp uses the existing UTC isoformat.
4. **Doc counts:** the CHANGELOG and PR body numbers (5,712 / 4,072 / 1,626 / 14, 20,789 / 3,689) are copied from the
   measurement output above.
5. **Broken Markdown:** CHANGELOG entry inside the Unreleased block; re-read after the edit.
6. **Unrelated changes:** none planned. Check with `git diff --diff-filter=D`.
7. **Inputs:** `lessons-verdicts.db` (live cache is the source of truth). The new table is created with
   `IF NOT EXISTS`, so an old DB gains it on open (test: open a DB made without it). The cwd folder is
   made on demand (test: missing folder is created).
