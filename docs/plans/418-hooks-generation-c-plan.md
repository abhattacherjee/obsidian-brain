# Plan: vendor harden-repo v1.3.0 hooks (#418, #343, #366, #353)

**Source:** harden-repo release `v1.3.0` (tag on `main` at `892d0f5`, published 2026-10-27). The user first chose `release/v1.3.0` at `633d813` rather than wait for the tag. The tag was then published during planning, and `git diff 633d813 v1.3.0 -- templates/` is empty, so this plan pins the release itself.

**Rulings (user, 2026-10-27):** accept every harden-repo#156 design row as upstream has it: G-11, G-18, G-20, G-21 and G-22. The hooks are vendored unchanged. Tests that encode our old rules are rewritten or retired.

**Baseline (measured 2026-10-27, `develop` `8c76197`):** with the five templates swapped in, `tests/test_security.py` gives 293 failed and 1,074 passed. `tests/test_host_hook_single_source.py` and `tests/test_py39_compat.py` pass (95). The probe numbers are questions answered, not expected values.

**Hot path (measured):** the median of 7 runs on an ordinary command, summed over the five hooks, is 137 ms today and 253 ms with upstream. The slowest single hook goes from 31 ms to 67 ms.

## Tasks

1. **Vendor the hooks.** Copy the five `templates/hooks/*.py` from `v1.3.0` unchanged into `.claude/hooks/`. Anchor the five `.claude/settings.json` commands on `"$CLAUDE_PROJECT_DIR/.claude/hooks/<hook>.py"` (upstream's own settings shape). Check: `cmp` against `git show v1.3.0:templates/hooks/<h>.py` for all five.
2. **Take upstream's `commit-preflight.sh`.** Start from `templates/scripts/commit-preflight.sh` at `v1.3.0`. Fill `__HARDEN_LINT__` (no linter, as today) and `__HARDEN_TEST__` (today's pytest + `--cov-fail-under=90` block). Port the two repo-specific parts: the plugin manifest version sync, which must run before every skip-mode early exit, and `--allow-empty` (#437). Drop `--allow-empty` only if a probe shows upstream's tree-bound token already allows an empty commit with nothing staged. Record that probe in the commit message. Check: `bash -n`, a real preflight run in this tree, and the existing preflight tests.
3. **Retire obsolete tests:** G-30 (reads hook source or calls private helpers), G-29 (strict xfail markers for gaps upstream fixed: remove the marker, keep the test), G-31 (the fake `gh` speaks our text protocol: switch it to upstream's JSON) and the 14 `TestGlobalOptionSpellingsMatchThePlainVerdict` rows that read hook internals. Replace `test_all_five_copies_of_the_helper_are_identical` with a check that the five vendored files are byte-identical to the pinned upstream SHA's templates, recorded in a constant. Keep every subprocess behaviour test.
4. **Rewrite the token tests for upstream's token** (G-17 fixture path and format; G-18 5 s claim hold; G-19 keep-on-approval; G-20 stage-in-call refusal, other-checkout refusal, unknown-scope refusal). The token path is `/tmp/.preflight-token-<hash of git-common-dir>`.
5. **Rebuild the behaviour fixtures:** G-11 (missing dir → use an existing outside dir for the allow control, plus a new row asserting a missing dir denies), G-21 (only destination refs judged), G-22 (the test repo gets a base branch so the changelog-on-branch check can run), G-1 (raise the oversize fixture past upstream's 16 MiB `_MAX_INPUT_BYTES`), G-13 (benign allows), and G-15/G-16 timing rows (best-of-N, serial-safe). The stdin-cap AST guard accepts upstream's cap, and `docs/architecture/architecture.json` line 2536's "1 MB stdin caps" is corrected to match.
6. **New tests for the acceptance criteria:**
   - #418 C.1–C.3: a linked worktree's own preflight authorises its own commit; a token from worktree A does not authorise a commit at a different HEAD in worktree B; `git push origin main` from a linked worktree is denied.
   - #343: a heredoc body, a quoted `grep` pattern and a quoted `echo` argument that mention each hook's verb all allow, on all five hooks.
   - #366: a spelling axis on `TestBashTruthDifferential` (`git`, `"git"`, `'git'`, `\git`, `g\it`, `g""it`, and the same for `gh`).
   - #353: a `cd`-density and a `-C`-density axis at 2/4/8/17 KB, with an absolute budget and a growth-ratio assertion (best of N). A differential run shows the new rows red against the pre-vendor hooks from `develop` `8c76197`.
7. **Docs and CHANGELOG.** Record the workflow change from G-20 (staging and committing in one call is refused) in CLAUDE.md's Git Flow section. Add a CHANGELOG `[Unreleased]` entry. Note the hot-path cost in the PR body.

## Failure modes (self-check classes)

- **Fail-open paths:** the vendored hooks are upstream's, so the risk is a test retired as "obsolete" that was really the only guard on a fail-open. Every retired test is listed in the commit message with its G-bucket. Before deleting any deny-asserting test, run its command through the vendored hook and confirm it still denies. Test: the T6 rows plus the kept subprocess tests.
- **Exit codes:** `commit-preflight.sh` must exit non-zero on a test failure, a version-sync mismatch and a bad flag. Test: forced-failure runs in Phase 6 (a failing test, a mismatched `marketplace.json`) check `$?`.
- **Date boundaries:** not applicable. No date logic changes; the token expiry is a relative number of seconds.
- **Doc counts:** the PR body's test counts, file counts and ms figures come from commands run at head.
- **Broken Markdown:** the CHANGELOG entry goes inside `[Unreleased]`; the CLAUDE.md edit goes inside its list. Re-read the surrounding lines.
- **Unrelated changes:** the deleted and retired tests are each named in a self-check case with their G-bucket.
- **Inputs:** the hooks read the payload `cwd`, `CLAUDE_PROJECT_DIR`, git state and the token file. The source of truth for the hook bytes is harden-repo `v1.3.0` (`892d0f5`); the byte-identity test (T3) fails on a stale or hand-edited copy.

**Bypass routes (this is a gate change):**
- A token written in another worktree, or before a new commit: covered by C.2 and upstream's HEAD+tree binding.
- A token with no binding fields (`{"expires": 9999999999}`), the legacy path #156 notes: add a test that it does not authorise a commit when our own preflight writes the bound format.
- Staging in the same call (`git add -A && git commit`): it must deny (G-20 test).
- A command that names the verb only through spelling (`g\it`): #366 axis.
- Skipping the check by hand-editing a vendored hook: the T3 byte-identity test.
