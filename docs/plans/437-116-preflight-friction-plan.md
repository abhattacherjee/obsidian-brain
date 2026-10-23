# #437 + #116: preflight friction

**Goal:** stop two timing tests failing preflight on one noisy sample (#437), and let `commit-preflight.sh` mint a token for a `git commit --allow-empty` (#116).

## Task 1: retry a failed timing check once (#437)

File: `tests/test_security.py`.

1. Add a module-level helper `_retry_timing_once(check)`. `check()` runs one full measurement and returns `None` when it is within bounds, or a failure message. The helper calls it; on a message it calls it once more and returns `None` if the second run passes, else both messages joined. The bounds do not change.
2. `test_work_grows_no_faster_than_the_input`: move the measure-and-compare body into a `check()` closure and fail with the helper's message. A cell that times out (`measure` returns `None`) is a failure on that run, so a stall that repeats still fails. The "compared nothing" skip stays, decided on the last run.
3. `test_a_large_command_does_not_stall_the_gate`: the verdict assertion (`deny`) stays outside the retry and runs every time. Only the elapsed-time bound goes through the helper.
4. Tests for the helper:
   - one slow sample then a fast one: passes, `check` ran twice;
   - a fast first sample: passes, `check` ran once;
   - slow every time: fails, and the message names both samples.
5. A planted slow path: copy the hooks, add `time.sleep(3.5)` at the top of `prevent-direct-push.py`'s `main()`, and run the stall test's real check through the helper. It must still fail.

## Task 2: `commit-preflight.sh --allow-empty` (#116)

Files: `scripts/commit-preflight.sh`, `tests/test_security.py`, `scripts/dev-test/DEV-TEST-ISSUE-105.md`.

1. New flag `--allow-empty`. With nothing staged, it skips the "No staged files" refusal and takes the skip-tests path, with the reason `empty commit (--allow-empty)`. The version-sync check still runs first. There is nothing to scan or test.
2. With files staged, `--allow-empty` refuses (exit 1), so it cannot be used to skip tests on real changes.
3. Fix `staged_files` in the token: `echo "" | wc -l` counts an empty list as 1. Count the non-empty lines instead.
4. `require-preflight.py` is not changed. The token is bound to HEAD (#408), and an empty commit moves HEAD, so the token is spent by the commit it was made for.
5. Revert `DEV-TEST-ISSUE-105.md` Phase 2 to `./scripts/commit-preflight.sh --allow-empty` + `git commit --allow-empty`.
6. Tests (in `TestPreflightTokenSurvivesADeniedCall`, through the real script and hook):
   - `--allow-empty` with nothing staged exits 0 and writes a token at HEAD with `staged_files: 0`;
   - the hook allows `git commit --allow-empty -m ...` with it;
   - after that empty commit actually runs, the hook denies the next commit (single use);
   - `--allow-empty` with a file staged exits 1 and writes no token;
   - no flag and nothing staged still exits 1 (the old refusal stays).
   - `test_preflight_writes_head_into_every_token` still counts 2 token heredocs (no third one added).

## Task 3: carry `--allow-empty` upstream

File a harden-repo issue: the template `templates/scripts/commit-preflight.sh` has no `--allow-empty`, so #418's template swap would drop it. The new tests here then fail, which is the signal.

## Failure modes

- **Fail-open paths:** the retry could turn a real regression into a pass. Test: slow every time still fails (helper test, plus the planted 3.5 s sleep). A timed-out cell counts as a failed run, never a skip. For `--allow-empty`, the open path is staged files plus the flag; test: exits 1, no token.
- **Exit codes:** `--allow-empty` with staged files exits 1; no flag with nothing staged exits 1; tests assert both.
- **Date boundaries:** none. Token expiry is unchanged, and nothing reads a calendar date.
- **Doc counts:** the CHANGELOG entry and PR body state no counts that are not taken from the diff.
- **Broken Markdown:** the 105 doc's fenced block is replaced whole; re-read the lines around it.
- **Unrelated changes:** none planned; check with `git diff --diff-filter=D --name-only origin/develop...HEAD`.
- **Inputs:** the script reads the git index (`git diff --cached --name-only`) and HEAD. The source of truth is the live index at the time it runs. A stale index can't happen within one run. The hook reads the token file at the `/tmp/.preflight-token-<hash>` path; tests use the fixture's own path and clean it up.
- **Bypass routes:** the cheapest way past `--allow-empty` is to mint the token with nothing staged and then stage code before committing. The hook does not bind the token to the staged content, and `--docs-only`/`--skip-tests` have the same gap today. This needs a hook change, and the hook is harden-repo's (#418), so it is a stated limit and goes in the Task 3 issue. Test planned: none here. The limit is named at the gate.
