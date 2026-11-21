# Worktree-safe skill blocks and a stub-run shell harness (#521, #522)

Batch: two issues, one branch (`feature/521-522-worktree-safe-skills`), one PR, milestone v3.13.

## Probe (2026-11-21, worktree-isolated agent, Claude Code 2.1.330)

| Form | Result |
|---|---|
| `cd "$(git rev-parse --show-toplevel 2>/dev/null \|\| pwd)"` | REFUSED |
| `x=$(python3 -c "...['git', '--version']...")` (git only as a word in Python) | REFUSED |
| `out=$(FOO=1 python3 << 'PYEOF' ... "git word" ... PYEOF)` | REFUSED |
| `test -f /etc/hosts \|\| { echo missing; exit 1; }` | RAN (unlike #520) |
| `if ! test -f ...; then ...; fi` | RAN |
| `test -f x && echo OK \|\| echo MISSING` | RAN |
| `_T='/etc'` then `if [ -n "$_T" ] && [ -f ... ]; then ... fi` | RAN |
| `git rev-parse --show-toplevel` alone | RAN |
| `cd '/tmp'` then `H=$(python3 -c "...")` with no git word | RAN |

So the refusal reads command text: any `git` word inside `$(...)` or backticks is refused, even inside Python source. The isolation check stays a static, fail-closed text check (the #520 `_isolation_problems` rule, without the retro-only heredoc rule). Brace groups ran today but AC1 still bans them; `if/then` is proven to run.

## Scope found by running the #520 checker over every skill

61 blocks with git in a substitution and 23 brace groups across 17 skills. Beyond the issue's 15 skills: `check-items` (5 blocks wrap Python that names `git` in `x=$(...)`) and `dev-test` (`_T="$(git rev-parse ...)"`, the layer-1 resolver, 3 copies). Heredocs are out of scope: they ran in #520's probe and AC1 does not list them; only /retro keeps its no-heredoc rule (#517).

## Task 1: shared fence parser and isolation checker

Delegate: codex — self-contained new test helper plus its tests, 3 files, one pytest check.

- New `tests/skill_shell.py`: `fenced_blocks(text, langs=SHELL_LANGS) -> list[Block]` (`Block(line, info, body)`), CommonMark-style: backtick or tilde fences, length >= 3, any indent; the closer uses the same character, is at least as long, and has nothing after it but blanks; the opener's indent is stripped from body lines. `substitution_bodies(block)` and `isolation_problems(block, *, heredoc=False)` move here from `tests/test_retro_trace_skill_contract.py`.
- `tests/test_retro_trace_skill_contract.py` imports them; retro keeps `heredoc=True`.
- New `tests/test_skill_shell_parser.py`: fence cases (``` and ~~~, 3/4/5 long, indented, a shorter inner fence inside a longer one, unclosed fence runs to EOF, info string with extra words), and checker controls (every `_REFUSED_FORMS` entry, git as a word in `python3 -c` inside `$()`, git in a heredoc inside `$()`, and clean blocks).
- Before trusting the parser, diff its block set against the old collectors on every skill and explain each difference in the self-check.

## Task 2: note_writer sites from recorded argv (#522)

Delegate: codex — two test files, one pytest check; interfaces named below.

- `tests/skill_shell.py`: `run_with_stubs(block, tmp_path) -> list[list[str]]`. Runs the block under `bash` with `HOME`, cwd and `PATH` pointing at temp dirs. `PATH` holds only stubs: `python3`, `git`, `gh` record argv as JSON lines; a `python3 -c`/`python3 -` call prints a fake hooks dir that holds an empty file for each name in `hooks/`. Placeholders `'<REPO_ROOT>'` become the temp cwd. Needed coreutils (`test` is a builtin; `rm`, `mkdir`, `cat`, `sed`, `ls`, `echo`, `printf`, `dirname`, `mktemp`) are symlinked into the stub dir; nothing else runs.
- `tests/test_skill_shell_blocks.py`: `_collect_blocks` and `_LOOSE_INVOCATION_RE` go. Blocks come from `fenced_blocks`; a block is a call site when its recorded `python3` argv has a script whose basename is `note_writer.py` (interpreter flags `-B -I -u -X v -W v` skipped) followed by `write` or `append-update`. Pinned: 8 `write` sites, all with `--body-file`, and 1 `append-update`. Every shell block whose text contains `note_writer` but records no call is listed by id in a literal set, so a silent early exit fails loudly.
- Positive controls run real blocks through `run_with_stubs`: `python3 -B`, `python3 -X utf8`, a `\` continuation before `write`, a `~~~~bash` fence, and a comment naming `note_writer.py write` (must record nothing).

## Task 3: the repo-root step in 15 skills (#521)

Delegate: claude — scripted rewrite over 15 SKILL.md files, more than cx's 5-file limit.

- Each skill with a `cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"` line gets the /retro preamble once, before its first block: run `git rev-parse --show-toplevel` on its own, `pwd` if it fails, keep the path as `<REPO_ROOT>`.
- Every such line becomes `cd '<REPO_ROOT>'` (indent kept).
- Every `test -f "$HOOKS/<x>.py" || { ...; exit 1; }` becomes `if ! test -f "$HOOKS/<x>.py"; then ...; exit 1; fi`.
- Fix the pins the suite reports (known: `tests/test_vault_scan_skill_text.py:184,186`).
- Check the step is reached on every entry path, including indented blocks in subagent prompts (`decide:171`).

## Task 4: check-items blocks without a substitution

Delegate: claude — five long blocks with several tests that pin them.

- `raw_path=$(python3 -c "...")`, `closed_path=`, `part_path=`, `_step5_out=` and `report_path=` lose the `x=$(` / `)` wrapper. The Python prints `x=<path>` itself (Step 5 prints both `evidence_path=` and `gaps_path=`); the trailing `echo "x=$x"` lines go.
- Fix `tests/test_skill_project_name.py` pins and the Step 2b run in `tests/test_retro_trace_skill_contract.py`.

## Task 5: dev-test layer 1

Delegate: claude — load-bearing resolver run by `tests/test_hooks_resolver_drift.py`.

- `_T="$(git rev-parse --show-toplevel 2>/dev/null || true)"` becomes `_T='<REPO_ROOT>'`, with the repo-root step above Step 2. The sentinel check stays.
- `_run_dev_test_block` substitutes `<REPO_ROOT>` the way the model does: the `git rev-parse --show-toplevel` output in that cwd, else the cwd.

## Task 6: the every-skill test and the CHANGELOG

Delegate: codex — one new test file plus CHANGELOG, one pytest check.

- `tests/test_skill_worktree_isolation.py`: `isolation_problems(b)` is empty for every shell block of every `skills/*/SKILL.md`; positive control: the checker flags each refused form, and a scratch copy of one real skill with the old `cd` line put back.
- CHANGELOG `[Unreleased]` entry.

## Acceptance check (Phase 6)

AC3: dogfood `/decide` Step 1 (config) and `/vault-search` (a vault_scan block) in a worktree-isolated agent pinned to the branch head, running the branch's SKILL.md text: no refusal.

## Failure modes

- Fail-open paths: the stub run exits early (guard fails, `cd` to a placeholder) and records nothing, so a site is missed. Test: the literal set of note_writer-mentioning blocks with no call, plus pinned counts 8 and 1.
- Exit codes: a block's `exit 1` under the stubs is read as "no call". Covered by the same set; the harness also asserts every call site block exits 0.
- Date boundaries: none; no dates in the change.
- Doc counts: the CHANGELOG and PR body counts (skills, lines) are counted from the diff with grep.
- Broken Markdown: the scripted rewrite can break a fence. Test: Task 1's parser finds the same block count per skill before and after.
- Unrelated changes: `git diff --stat origin/develop...HEAD`; no deletions expected except the regex helpers named above.
- Inputs: the tests read `skills/*/SKILL.md` and `hooks/` in the checkout (source of truth); the stub hooks dir mirrors `hooks/` at test time, so a new hook script cannot go stale.
- Bypass routes (the isolation test is a gate): the cheapest pass is a fence spelling the parser misses (`~~~~sh`, indented) — covered by Task 1's cases; a git word the scan misses (`/usr/bin/git`, `command git`, backticks) — covered by `_REFUSED_FORMS`; an `skills/*/SKILL.md` glob that finds nothing — the test asserts at least 17 skills.
