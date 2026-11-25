# #532 — take the harden-repo v1.5.0 hooks; tests stop pinning one release

Issue: abhattacherjee/obsidian-brain#532. Base: `develop` (a5e3b0a).
Source of every vendored byte: harden-repo tag `v1.5.0` (9424640), checked
out detached at a scratch worktree (`hr150`).

## Ruling (user, 2026-11-25)

`commit-preflight.sh` becomes the v1.5.0 template outside its marker
blocks, so the doctor certifies it and updates `require-preflight.py` with
it (the pair rule, harden-repo #232). The two repo parts that ran before the
skip-mode exit cannot stay:

- the plugin.json / marketplace.json version sync moves into the LINT
  marker block (normal mode only) and a new CI pytest checks the same pair on
  every PR;
- `--allow-empty` (#116) is dropped with its tests;
- a harden-repo issue asks for an always-run marker block and
  `--allow-empty`, so both can come back without leaving the template.

## Applied already (Task 1, by tool, not by hand)

- `scripts/commit-preflight.sh`: `gen-preflight.py` = v1.5.0 template, LINT
  body = version sync, TEST body = develop's (heavy, timed pass, 90% gate).
- `doctor.py --repo . --plugin-root hr150 --fix --safe`: the five
  `.claude/hooks/*.py` and the draft gate in `ci.yml`.
- Doctor after: 6 in-sync (5 hooks + `commit-preflight.sh`); left:
  `ci.yml` UNKNOWN (custom layout, no markers), `bump-version.sh`,
  `git-flow-finish.sh`, `pre-commit.sh` UNRECOGNIZED (repo-owned),
  `check-assertion-strength.sh` MISSING (a /harden-repo choice).

## Task 1: vendor the v1.5.0 pair, hooks and draft gate
Delegate: claude — writes `.claude/` and `.github/`, which cx refuses; done by the doctor.

## Task 2: tests follow the new token location and template
Delegate: claude — its tests only pass with the uncommitted Task 1 `.claude/` files, which cx cannot carry or check; one commit with Task 1.

Measured with Task 1 applied: 104 failures (87 in
`TestPreflightTokenSurvivesADeniedCall`, 16 in `test_vendored_hooks.py`, 1 in
`test_ci_workflow.py`). Root cause for all but the CI one: tests compute
`/tmp/.preflight-token-<h>`; v1.5.0 uses
`${XDG_CACHE_HOME:-$HOME/.cache}/claude/preflight/harden-token-<h>` in a 0700
directory.

- `tests/preflight_token.py` (new, written): `token_dir(env)`,
  `token_path(work, env, make_dir=True)`.
- `tests/conftest.py`: `_isolate_hook_log` also unsets `XDG_CACHE_HOME`.
- `tests/test_security.py::TestPreflightTokenSurvivesADeniedCall._token_path`
  and `tests/test_preflight_heavy.py::_token_path` use the helper with the
  test's env (tmp HOME).
- `tests/test_vendored_hooks.py`: `_token_path` uses the helper; the fixture
  puts an explicit `HOME=<tmp>/home` in `repo.env`; the version-sync test
  stops overriding HOME.
- Delete the `--allow-empty` tests in `test_security.py` (#116 block).
- `test_version_sync_runs_before_every_skip_mode` becomes
  `test_version_sync_runs_in_the_lint_block`: normal mode, mismatch → rc 1,
  `MISMATCH`, no token; in sync → rc 0, token `checks_run` has
  `version-sync`.

## Task 3: the hook-bytes test checks the harden-repo ledger, not one release
Delegate: claude — design of a new check against an outside source; one file, done here.

`test_hook_bytes_match_harden_repo_v131` becomes
`test_vendored_files_are_one_released_generation`:
- Roots, in order: `$OB_HARDEN_REPO_ROOT`; the real home's (pwd, not the tmp
  HOME) `~/.claude/plugins/cache/harden-repo/repo-hardener/*`,
  `~/.claude/plugins/marketplaces/harden-repo`,
  `~/dev/claude_workspace/harden-repo`. A root counts when it has
  `skills/harden-repo/data/template-ledger.json`,
  `skills/harden-repo/scripts/doctor_core.py` and `.claude-plugin/plugin.json`.
- Hash with that root's own `doctor_core.artifact_hash` (marker bodies set
  aside), so `commit-preflight.sh` is covered too.
- Versions of a file = ledger versions for its hash ∪ the root's version when
  it equals that root's template. Each of the six files needs at least one;
  the six must share one version (no mixed generations).
- No root found (CI): skip, naming the env var. Recorded in the PR: CI has no
  harden-repo checkout, and a copied ledger is just another pin.

## Task 4: CI checks the version sync and the draft gate
Delegate: claude — `test_ci_workflow.py` reads the doctor-written `ci.yml`, uncommitted with Task 1; one commit.

- New `tests/test_manifest_version_sync.py`: the real plugin.json version
  equals every marketplace.json entry with its name.
- `tests/test_ci_workflow.py`: the aggregator `if:` is the gated form; every
  PR job's `if:` ends with the draft gate; `on.pull_request.types` holds
  `opened, synchronize, reopened, ready_for_review` (without
  `ready_for_review` a PR opened as draft would keep its skipped, "green"
  run when marked ready: a green-without-running path, #505).

## Task 5: docs
Delegate: claude — CLAUDE.md is a guarded path.

CLAUDE.md (vendored v1.5.0 lines), CHANGELOG `[Unreleased]`,
`architecture.json`/`.html` if it names the token path, harden-repo issue.

## Failure modes

- **Fail-open paths:** a token test that looks at the wrong path passes
  vacuously (`assert not token.exists()` in `test_preflight_heavy.py` did,
  after Task 1). Test: each token test first proves a token at the helper's
  path is read (allow), or the preflight run writes one there.
- **Bypass routes (gate change):** (1) draft run skipped → required
  `tests-passed` "green" → PR marked ready with no new run: guarded by the
  `types` test. (2) A ledger test that skips everywhere: when a root is
  found it must assert; mutation: edit one byte of a hook → red. (3) Mixed
  generations (new hook, old script): the shared-version assertion.
- **Exit codes:** version sync exit 1 (mismatch) and 2 (structural) both set
  `CHECKS_PASSED=false`; test both.
- **Date boundaries:** none (token expiry is the hook's, unchanged here).
- **Doc counts:** CHANGELOG/PR counts taken from the diff and the run log.
- **Broken Markdown:** CHANGELOG entry inside `[Unreleased]`, re-read.
- **Unrelated changes:** none; doctor touched only the files listed.
- **Inputs:** hook bytes (source of truth: harden-repo v1.5.0 tag), the
  ledger (source: harden-repo checkout or plugin cache; stale copy → the
  shared-version check fails, missing → skip), plugin.json/marketplace.json
  (the repo's).
