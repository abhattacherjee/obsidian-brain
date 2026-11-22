# #526 — skill-block harness: OS sandbox for writes, scanner fixes

Issue: #526 (from PR #523's `/review:deep`, which did not sign off). All of it is
test tooling: `tests/skill_shell.py` and its tests, plus the CI test jobs.

## Measured before planning (2026-11-21)

- macOS `sandbox-exec` with `(deny file-write* (require-not (subpath W)))` blocks
  `>`, `>|`, `sort -o` and `TMPDIR=/tmp mktemp` outside `W`, and allows writes inside.
- Inside Codex's sandbox, nested `sandbox-exec` fails: `sandbox_apply: Operation not
  permitted`, exit 71 (`cx ask` 33a2cd5e). So the harness must detect a refused
  sandbox, and harness tasks cannot use `cx` (their checks need the sandbox).
- `bwrap --ro-bind / / --dev /dev --bind W W` on GitHub runners (probe branch, now
  deleted): `ubuntu-24.04` fails `setting up uid map: Permission denied` until
  `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`; `ubuntu-latest`
  (26.04) works. Both then block `/tmp` writes (`Read-only file system`). The block
  keeps the launcher's session (same sid inside and out) and host pids.
- Baseline: all 156 skill blocks run under today's harness with no harness error
  (scratchpad `baseline-before.json`).

## Design

1. `run_with_stubs` runs bash under an OS sandbox that denies writes outside the
   workdir: `sandbox-exec -p <profile>` on macOS, `bwrap` on Linux. A one-time probe
   (`sandbox_prefix()`, cached) runs a real deny test: a write inside must succeed
   and one outside must fail. Anything else means no sandbox.
2. With a sandbox, file writers and `sed sort uniq awk env mktemp` are the real tools
   (symlinks). `python3 git gh grep` stay recording stubs. Removed:
   `escaping_redirects`, `escaping_argument`, `_AWK_WRITES`, the guarded wrapper and
   the writer stubs.
3. Without a sandbox, `run_with_stubs` raises `SandboxUnavailable` unless the caller
   passes `unsandboxed_ok=True`. Then the writers stay recording stubs (today's
   behaviour, minus the regex guards). The all-blocks test runs only the pinned
   `_CALL_SITES` blocks in that mode; the containment tests skip.
4. CI: the 3.12 and 3.9 pytest jobs install `bubblewrap`, set the AppArmor sysctl,
   and set `OB_REQUIRE_SANDBOX=1`. A test fails when that variable is set and no
   sandbox is found, so CI cannot quietly fall back.
5. `_kill_session` never raises. If the process listing fails, it still kills the
   group, and the block's result (or the `TimeoutExpired`) is kept.
6. Scanner: stays a conservative lint with its gaps listed. Fix the non-ASCII crash
   (X-026) and the comment that swallows a closing backtick (X-027). Add a no-crash
   fuzz test. No new parser logic beyond those two fixes.
7. Root-step check (X-029): the step section ends at the next heading of any level,
   and every `'<REPO_ROOT>'` block must come after the root command's fence, not
   only after the heading.

## Failure modes

- **Fail-open paths:** (a) the probe says "sandbox" but writes are not denied → the
  probe itself writes outside and requires failure; test with a fake prefix that
  allows everything → probe returns None. (b) sandbox refused → silent unsandboxed
  run of every block → `SandboxUnavailable` test. (c) CI falls back → the
  `OB_REQUIRE_SANDBOX` test, pinned in `test_ci_workflow.py`.
- **Bypass routes:** cheapest escapes to test, all aimed at a sibling of the workdir
  under `tmp_path` (not `/tmp`, which an outer sandbox may deny on its own): `>|`,
  `sort -o /abs`, `TMPDIR=/abs mktemp`, `/bin/cp` by absolute path, a symlink in the
  workdir pointing out, a hardlink to an outside file followed by `>>`. Mutation: the
  same block with the sandbox prefix removed and real writers must create the files.
- **Exit codes:** `sandbox-exec` refused exits 71; `bwrap` refused exits 1. Both make
  the probe return None. A block's own non-zero exit is still returned, unchanged.
- **Date boundaries:** none (no dates).
- **Doc counts:** CHANGELOG and docstrings state no counts, except counts taken from
  test output in this run.
- **Broken Markdown:** CHANGELOG entry inside `[Unreleased]`; re-read around it.
- **Unrelated changes:** none. `hooks/` and `skills/` stay untouched.
- **Inputs:** `skills/*/SKILL.md` read from the checkout (the only copy). Real tools
  come from `shutil.which` on the test process PATH; a missing tool is skipped, as
  today. Before/after behaviour: re-run `block_baseline.py` after Task 1 and explain
  every block whose `rc` or non-writer calls changed.

## Task 1: OS sandbox in `run_with_stubs`

Delegate: claude — its checks need a real sandbox, which Codex refuses (exit 71).

Files: `tests/skill_shell.py`, `tests/test_skill_shell_blocks.py`.

- Add `SandboxUnavailable`, `sandbox_prefix(workdir)` with the cached probe, the
  macOS profile and the bwrap argv (no `--unshare-pid`, no `--new-session`).
- `run_with_stubs(block, workdir, timeout=60, *, unsandboxed_ok=False)`.
- Remove the regex guards and their tests; replace them with the containment tests
  and the mutation test above, plus `test_a_refused_sandbox_raises_unless_allowed`.
- The all-blocks test: with no sandbox, run only `_CALL_BLOCKS` and skip the new-call-
  site half with a reason.
- Check: `python3 -m pytest tests/test_skill_shell_blocks.py tests/test_skill_shell_parser.py -q`
  then the baseline diff.

## Task 2: cleanup keeps the result (X-025)

Delegate: claude — same file and run path as Task 1, and its test runs blocks.

Files: `tests/skill_shell.py`, `tests/test_skill_shell_blocks.py`.

- `_kill_session`: wrap the listing in `try` (OSError, non-zero exit); on failure
  `killpg` and return.
- Tests: monkeypatch `skill_shell.subprocess.run` to raise `PermissionError`; the run
  returns its result, and a `set -m` background child is still killed when the
  listing works; a timeout still raises `TimeoutExpired`, not `PermissionError`.

## Task 3: CI installs bubblewrap and requires the sandbox

Delegate: claude — `.github/` is a guarded path `cx apply` refuses.

Files: `.github/workflows/ci.yml`, `tests/test_ci_workflow.py`,
`tests/test_skill_shell_blocks.py`.

- In `python-tests` and `python-tests-py39`: a step that installs `bubblewrap` and
  runs the sysctl; `OB_REQUIRE_SANDBOX: '1'` on the pytest step.
- `test_ci_workflow.py`: both jobs have the install step and the variable.
- `test_the_sandbox_is_there_when_ci_requires_it`.

## Task 4: scanner crash and backtick comment (X-026, X-027)

Delegate: codex — self-contained in two test files with a single pytest check.

Files: `tests/skill_shell.py` (`_scan` only), `tests/test_skill_shell_parser.py`.

- `_scan`: a word starts only on an ASCII letter or `_` (`'é'.isalpha()` is True but
  `_WORD` is ASCII, so `.end()` on None crashes). Non-ASCII characters are plain text.
- In a backtick frame, a comment ends at the next unescaped backtick or newline,
  whichever comes first (bash finds the closing backtick before it parses comments).
- Tests: `echo café`, `echo café $(git x)` (reports the substitution),
  ``x=`echo ok # comment`\ngit status`` (no problem reported), a seeded fuzz test
  (5,000 strings from shell metacharacters, letters, `é`, `ü`, emoji) that `_scan`
  never raises and returns `len(flags) == len(block)`.
- `isolation_problems` docstring lists the gaps (unchanged list plus the X-027 class
  fixed).
- Check: `python3 -m pytest tests/test_skill_shell_parser.py tests/test_skill_worktree_isolation.py -q`

## Task 5: root-step check measures from the command (X-029)

Delegate: codex — one test file, one pytest check.

Files: `tests/test_skill_worktree_isolation.py`.

- `root_step_problems`: the section ends at the next Markdown heading of any level
  outside a fence; the root command fence must be inside it; every user block must
  start after that fence's line.
- Test: Codex's repro (root heading kept, then a `cd '<REPO_ROOT>'` block, then
  `## Step 1` with the command and the pwd fallback, no later `###`) is flagged.
- Check: `python3 -m pytest tests/test_skill_worktree_isolation.py -q`

## Task 6: CHANGELOG

Delegate: claude — two lines, written after the rest is known.

Files: `CHANGELOG.md`.
